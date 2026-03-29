use crate::check::{Action, CheckEngine, CheckPhase, CheckRequest};
use crate::config;
use crate::continuity;
use crate::override_mgr;
use crate::transcript;
use std::io::Read;

const DAEMON_SOCKET: &str = "/tmp/dev-loop/dl.sock";

/// Read all stdin as a string.
fn read_stdin() -> String {
    let mut input = String::new();
    let _ = std::io::stdin().read_to_string(&mut input);
    input
}

/// Check if cwd is inside a dev-loop worktree (skip all checks).
fn is_worktree(cwd: &str) -> bool {
    cwd.starts_with("/tmp/dev-loop/worktrees/")
}

/// Send an HTTP request to the daemon's Unix socket.
/// Returns the response body on success, or None on any error.
/// Used by hooks to communicate with the daemon without blocking.
fn request_daemon(method: &str, path: &str, body: &str) -> Option<String> {
    use std::io::Write;
    use std::os::unix::net::UnixStream;

    let mut stream = UnixStream::connect(DAEMON_SOCKET).ok()?;
    stream
        .set_write_timeout(Some(std::time::Duration::from_millis(500)))
        .ok()?;
    stream
        .set_read_timeout(Some(std::time::Duration::from_millis(500)))
        .ok()?;

    let request = if body.is_empty() {
        format!(
            "{method} {path} HTTP/1.1\r\n\
             Host: localhost\r\n\
             Connection: close\r\n\
             \r\n"
        )
    } else {
        format!(
            "{method} {path} HTTP/1.1\r\n\
             Host: localhost\r\n\
             Content-Type: application/json\r\n\
             Content-Length: {}\r\n\
             Connection: close\r\n\
             \r\n\
             {body}",
            body.len()
        )
    };
    stream.write_all(request.as_bytes()).ok()?;

    let mut response = String::new();
    stream.read_to_string(&mut response).ok()?;

    // Extract body from HTTP response
    response
        .find("\r\n\r\n")
        .map(|pos| response[pos + 4..].to_string())
}

/// Fire-and-forget POST to the daemon's Unix socket.
fn post_to_daemon(path: &str, body: &str) -> Option<String> {
    request_daemon("POST", path, body)
}

/// GET request to the daemon's Unix socket.
fn get_from_daemon(path: &str) -> Option<String> {
    request_daemon("GET", path, "")
}

/// Fire-and-forget event posting to daemon. Does not wait for response.
fn fire_event_to_daemon(
    session_id: &str,
    tool_name: &str,
    action: &str,
    check_type: &str,
    duration_us: u64,
    category: Option<&str>,
    pattern: Option<&str>,
    tool_key: &str,
) {
    let body = serde_json::json!({
        "type": "check",
        "session_id": session_id,
        "tool": tool_name,
        "action": action,
        "check": check_type,
        "us": duration_us,
        "category": category,
        "pattern": pattern,
        "tool_key": tool_key,
    })
    .to_string();

    // Fire and forget — use a short-lived connection
    let _ = post_to_daemon("/event", &body);
}

/// Log a shadow verdict to the daemon's event log.
/// In shadow mode, checks run but never block — verdicts are recorded for analysis.
fn fire_shadow_verdict(
    session_id: &str,
    tool_name: &str,
    verdict: &str,
    check_type: &str,
    pattern_matched: Option<&str>,
    reason: Option<&str>,
    duration_us: u64,
) {
    let body = serde_json::json!({
        "type": "shadow_verdict",
        "session_id": session_id,
        "tool": tool_name,
        "verdict": verdict,
        "check": check_type,
        "pattern": pattern_matched,
        "reason": reason,
        "us": duration_us,
    })
    .to_string();

    let _ = post_to_daemon("/event", &body);
}

/// Outcome of a checkpoint call to the daemon.
enum CheckpointOutcome {
    /// All gates passed; trailer to inject.
    Passed(String),
    /// At least one gate failed; reason string.
    Failed(String),
    /// Daemon not running or checkpoint skipped.
    Skipped,
}

/// Contact the daemon's /checkpoint endpoint (blocking, with longer timeout).
/// Returns the checkpoint outcome. Fail-open if daemon unavailable.
fn run_checkpoint_via_daemon(cwd: &str, session_id: Option<&str>) -> CheckpointOutcome {
    use std::io::Write;
    use std::os::unix::net::UnixStream;

    let body = serde_json::json!({
        "cwd": cwd,
        "session_id": session_id,
    })
    .to_string();

    // Longer timeout for checkpoint (gates run external tools)
    let timeout = std::time::Duration::from_secs(120);

    let mut stream = match UnixStream::connect(DAEMON_SOCKET) {
        Ok(s) => s,
        Err(_) => return CheckpointOutcome::Skipped,
    };
    let _ = stream.set_write_timeout(Some(std::time::Duration::from_secs(5)));
    let _ = stream.set_read_timeout(Some(timeout));

    let request = format!(
        "POST /checkpoint HTTP/1.1\r\n\
         Host: localhost\r\n\
         Content-Type: application/json\r\n\
         Content-Length: {}\r\n\
         Connection: close\r\n\
         \r\n\
         {body}",
        body.len()
    );

    if stream.write_all(request.as_bytes()).is_err() {
        return CheckpointOutcome::Skipped;
    }

    let mut response = String::new();
    if std::io::Read::read_to_string(&mut stream, &mut response).is_err() {
        return CheckpointOutcome::Skipped;
    }

    // Extract JSON body from HTTP response
    let json_body = match response.find("\r\n\r\n") {
        Some(pos) => &response[pos + 4..],
        None => return CheckpointOutcome::Skipped,
    };

    let parsed: serde_json::Value = match serde_json::from_str(json_body) {
        Ok(v) => v,
        Err(_) => return CheckpointOutcome::Skipped,
    };

    let passed = parsed.get("passed").and_then(|v| v.as_bool()).unwrap_or(true);

    if passed {
        let trailer = parsed
            .get("trailer")
            .and_then(|v| v.as_str())
            .unwrap_or("Dev-Loop-Gate: unknown")
            .to_string();
        CheckpointOutcome::Passed(trailer)
    } else {
        // Build failure message from gate results
        let first_failure = parsed
            .get("first_failure")
            .and_then(|v| v.as_str())
            .unwrap_or("unknown");

        let mut details = format!("Gate '{first_failure}' failed.");

        // Append findings from the failed gate
        if let Some(gate_results) = parsed.get("gate_results").and_then(|v| v.as_array()) {
            for gr in gate_results {
                if gr.get("passed").and_then(|v| v.as_bool()) == Some(false) {
                    if let Some(reason) = gr.get("reason").and_then(|v| v.as_str()) {
                        details.push_str(&format!("\n  {reason}"));
                    }
                    if let Some(findings) = gr.get("findings").and_then(|v| v.as_array()) {
                        for f in findings.iter().take(10) {
                            if let Some(s) = f.as_str() {
                                details.push_str(&format!("\n  - {s}"));
                            }
                        }
                    }
                }
            }
        }

        CheckpointOutcome::Failed(details)
    }
}

/// PreToolUse hook: deny list (Write/Edit), dangerous ops (Bash).
///
/// Protocol:
/// - Exit 2 + stderr reason = block the tool call
/// - Exit 0 + JSON with permissionDecision: "ask" = prompt user
/// - Exit 0 (no output) = allow
pub fn pre_tool_use() {
    let input = read_stdin();
    let data: serde_json::Value = match serde_json::from_str(&input) {
        Ok(v) => v,
        Err(_) => std::process::exit(0), // fail-open on parse error
    };

    // Check enable state (fast path — just reads global config toggle)
    if !config::is_enabled_tier1() {
        std::process::exit(0);
    }

    // Worktree detection — TB pipeline has its own gates
    let cwd = data.get("cwd").and_then(|v| v.as_str()).unwrap_or("");
    if is_worktree(cwd) {
        std::process::exit(0);
    }

    // Load merged config (global + per-repo) and build check engine
    let merged = config::load_merged(Some(cwd));
    if !merged.enabled || merged.ambient_mode == "disabled" {
        std::process::exit(0);
    }

    let shadow = merged.ambient_mode == "shadow";
    let engine = CheckEngine::from_config(&merged);

    // Build check request from hook input
    let tool_name = data
        .get("tool_name")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let tool_input = data
        .get("tool_input")
        .cloned()
        .unwrap_or(serde_json::Value::Object(Default::default()));
    let session_id = data
        .get("session_id")
        .and_then(|v| v.as_str())
        .map(String::from);

    // Extract file_path before moving tool_input into the request
    let file_path_for_override = tool_input
        .get("file_path")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();

    // Compute tool_key before moving tool_input
    let tool_key = match tool_name {
        "Bash" => tool_input
            .get("command")
            .and_then(|v| v.as_str())
            .and_then(|c| c.split_whitespace().next())
            .unwrap_or("bash")
            .to_string(),
        "Write" | "Edit" => file_path_for_override.clone(),
        other => other.to_string(),
    };

    let request = CheckRequest {
        tool_name: tool_name.to_string(),
        tool_input,
        phase: CheckPhase::Pre,
        session_id,
    };

    let result = engine.check(&request);

    // Fire check event to daemon for session tracking (non-blocking)
    if let Some(ref sid) = request.session_id {
        let action_str = format!("{:?}", result.action).to_lowercase();
        let check_type = result.check_type.as_deref().unwrap_or("unknown");
        fire_event_to_daemon(
            sid, tool_name, &action_str, check_type, result.duration_us,
            result.category.as_deref(), result.pattern.as_deref(), &tool_key,
        );
    }

    // Shadow mode: log verdict but always allow
    if shadow {
        if result.action != Action::Allow {
            if let Some(ref sid) = request.session_id {
                let verdict = format!("{:?}", result.action).to_lowercase();
                let check_type = result.check_type.as_deref().unwrap_or("unknown");
                let reason = result.reason.as_deref();
                fire_shadow_verdict(sid, tool_name, &verdict, check_type, None, reason, result.duration_us);
            }
        }
        // Shadow mode: always allow, no output
        return;
    }

    // Tier 2: If this is a git commit, run checkpoint gates
    if result.is_commit && merged.tier2 {
        match run_checkpoint_via_daemon(cwd, request.session_id.as_deref()) {
            CheckpointOutcome::Passed(trailer) => {
                // Checkpoint passed — allow the commit.
                // Output the trailer so Claude can inject it into the commit message.
                let output = serde_json::json!({
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "additionalContext": format!(
                            "dev-loop checkpoint PASSED. Append this trailer to the commit message:\n{trailer}"
                        )
                    }
                });
                println!("{output}");
                return; // allow the commit
            }
            CheckpointOutcome::Failed(reason) => {
                eprintln!("Checkpoint FAILED: {reason}");
                std::process::exit(2);
            }
            CheckpointOutcome::Skipped => {
                // Daemon not running or tier2 disabled — fall through to normal handling
            }
        }
    }

    match result.action {
        Action::Block => {
            // Check for allow-once override before blocking
            let file_path = &file_path_for_override;
            if !file_path.is_empty() && override_mgr::check_and_consume(file_path) {
                // Override consumed — allow this one
                let output = serde_json::json!({
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "additionalContext": format!(
                            "dev-loop: allow-once override consumed for '{file_path}'. Future writes will be blocked again."
                        )
                    }
                });
                println!("{output}");
                return;
            }

            let reason = result
                .reason
                .as_deref()
                .unwrap_or("Blocked by dev-loop ambient layer");
            eprintln!("{reason}");
            std::process::exit(2);
        }
        Action::Warn => {
            let reason = result
                .reason
                .as_deref()
                .unwrap_or("Flagged by dev-loop ambient layer");
            let output = serde_json::json!({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "ask",
                    "permissionDecisionReason": reason
                }
            });
            println!("{output}");
        }
        Action::Allow => {
            // Silent allow — no output needed
        }
    }
}

/// PostToolUse hook: secret detection (Write/Edit).
///
/// Always exits 0. Injects additionalContext warning if secrets found.
pub fn post_tool_use() {
    let input = read_stdin();
    let data: serde_json::Value = match serde_json::from_str(&input) {
        Ok(v) => v,
        Err(_) => std::process::exit(0),
    };

    if !config::is_enabled_tier1() {
        std::process::exit(0);
    }

    let cwd = data.get("cwd").and_then(|v| v.as_str()).unwrap_or("");
    if is_worktree(cwd) {
        std::process::exit(0);
    }

    // Load merged config and build check engine
    let merged = config::load_merged(Some(cwd));
    if !merged.enabled || merged.ambient_mode == "disabled" {
        std::process::exit(0);
    }

    let shadow = merged.ambient_mode == "shadow";
    let engine = CheckEngine::from_config(&merged);

    // Check file allowlist before secret scanning
    let file_path = data
        .get("tool_input")
        .and_then(|v| v.get("file_path"))
        .and_then(|v| v.as_str())
        .unwrap_or("");
    if engine.secrets.is_file_allowed(file_path) {
        std::process::exit(0);
    }

    let tool_name = data
        .get("tool_name")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let tool_input = data
        .get("tool_input")
        .cloned()
        .unwrap_or(serde_json::Value::Object(Default::default()));
    let session_id = data
        .get("session_id")
        .and_then(|v| v.as_str())
        .map(String::from);

    let request = CheckRequest {
        tool_name: tool_name.to_string(),
        tool_input,
        phase: CheckPhase::Post,
        session_id,
    };

    let result = engine.check(&request);

    // Fire check event to daemon for session tracking (non-blocking)
    if let Some(ref sid) = request.session_id {
        let action_str = format!("{:?}", result.action).to_lowercase();
        let check_type = result.check_type.as_deref().unwrap_or("unknown");
        let post_tool_key = match tool_name {
            "Write" | "Edit" => file_path.to_string(),
            other => other.to_string(),
        };
        fire_event_to_daemon(
            sid, tool_name, &action_str, check_type, result.duration_us,
            result.category.as_deref(), result.pattern.as_deref(), &post_tool_key,
        );
    }

    // Shadow mode: log verdict but don't warn user
    if shadow {
        if result.action == Action::Warn {
            if let Some(ref sid) = request.session_id {
                let check_type = result.check_type.as_deref().unwrap_or("unknown");
                let reason = result.reason.as_deref();
                fire_shadow_verdict(sid, tool_name, "warn", check_type, None, reason, result.duration_us);
            }
        }
        return;
    }

    if result.action == Action::Warn {
        if let Some(reason) = &result.reason {
            let output = serde_json::json!({
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": format!(
                        "dev-loop ambient warning: {reason}\n\
                         Do NOT commit this file. Use environment variables or .env.example with placeholders instead."
                    )
                }
            });
            println!("{output}");
        }
    }
}

/// Session start hook: register session with daemon, inject handoff state.
///
/// Differentiated SessionStart (from CC-v3 research):
/// - Fresh start (no recent handoff): one-liner notification
/// - Resume/compact (recent handoff found): full state injection via additionalContext
///
/// Reads stdin JSON from Claude Code, extracts session_id + cwd,
/// POSTs to daemon's /session/start endpoint. Fail-open if daemon not running.
pub fn session_start() {
    let input = read_stdin();
    let data: serde_json::Value = match serde_json::from_str(&input) {
        Ok(v) => v,
        Err(_) => return, // fail-open on parse error
    };

    let session_id = data
        .get("session_id")
        .and_then(|v| v.as_str())
        .unwrap_or("unknown");
    let cwd = data.get("cwd").and_then(|v| v.as_str()).unwrap_or("");

    // Skip worktrees
    if is_worktree(cwd) {
        return;
    }

    // Resolve repo root for session metadata
    let repo_root = config::find_repo_root(cwd);

    let body = serde_json::json!({
        "session_id": session_id,
        "cwd": cwd,
        "repo_root": repo_root.as_ref().map(|p| p.to_string_lossy().to_string()),
    })
    .to_string();

    // Register with daemon (fail-open if daemon not running)
    let _ = post_to_daemon("/session/start", &body);

    // Generate ambient-rules.md (best-effort)
    let merged = config::load_merged(Some(cwd));
    crate::rules_md::generate(&merged);

    // Differentiated SessionStart: check for recent handoff
    let shadow_note = if merged.ambient_mode == "shadow" {
        " (SHADOW MODE — logging only, not blocking)"
    } else {
        ""
    };
    if let Some(handoff) = continuity::find_recent_handoff(cwd) {
        // Resume/compact scenario: inject full handoff state
        let context = continuity::format_for_injection(&handoff);
        let output = serde_json::json!({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": format!(
                    "dev-loop ambient active{shadow_note} (resumed). Handoff from previous session:\n{context}"
                )
            }
        });
        println!("{output}");
    } else {
        // Fresh start: get last session stats from daemon
        let mode_tag = if merged.ambient_mode == "shadow" {
            " (SHADOW MODE — logging only, not blocking)"
        } else {
            ""
        };
        let notification = match get_last_session_summary() {
            Some(summary) => format!(
                "dev-loop ambient active{mode_tag}. Last session: {} checks, {} blocks.",
                summary.checks, summary.blocks
            ),
            None => format!("dev-loop ambient active{mode_tag}."),
        };
        let output = serde_json::json!({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": notification
            }
        });
        println!("{output}");
    }
}

/// Quick summary of last session stats (from daemon status).
struct LastSessionSummary {
    checks: u32,
    blocks: u32,
}

fn get_last_session_summary() -> Option<LastSessionSummary> {
    let response = get_from_daemon("/status")?;
    let data: serde_json::Value = serde_json::from_str(&response).ok()?;
    // Look for any session data in the response
    let sessions = data.get("sessions").and_then(|v| v.as_array())?;
    if sessions.is_empty() {
        return None;
    }
    // Use the first (most recent) session
    let last = &sessions[0];
    Some(LastSessionSummary {
        checks: last.get("checks").and_then(|v| v.as_u64()).unwrap_or(0) as u32,
        blocks: last.get("blocks").and_then(|v| v.as_u64()).unwrap_or(0) as u32,
    })
}

/// Session end hook: write final handoff YAML, deregister, trigger OTel span flush.
///
/// Writes a session-end handoff for continuity, then POSTs to daemon's
/// /session/end endpoint. Daemon handles span construction and OTLP export.
/// Fail-open if daemon not running.
pub fn session_end() {
    let input = read_stdin();
    let data: serde_json::Value = match serde_json::from_str(&input) {
        Ok(v) => v,
        Err(_) => return,
    };

    let session_id = data
        .get("session_id")
        .and_then(|v| v.as_str())
        .unwrap_or("unknown");
    let cwd = data.get("cwd").and_then(|v| v.as_str()).unwrap_or("");

    // Write final handoff YAML (best-effort)
    write_session_handoff(session_id, cwd, "session_end");

    // Deregister with daemon (fail-open if daemon not running)
    let body = serde_json::json!({
        "session_id": session_id,
    })
    .to_string();
    let _ = post_to_daemon("/session/end", &body);

    // Clean up old handoff files (>24h)
    continuity::cleanup_old_handoffs();
}

/// Stop hook: context guard + handoff writer.
///
/// Fires after every assistant turn. Checks context usage via
/// transcript file size heuristic. If over threshold (default 85%),
/// writes handoff YAML and outputs a warning via additionalContext.
///
/// Protocol:
/// - Exit 0 (no output) = continue normally
/// - Exit 0 + JSON with additionalContext = warn about context usage
pub fn stop() {
    let input = read_stdin();
    let data: serde_json::Value = match serde_json::from_str(&input) {
        Ok(v) => v,
        Err(_) => return, // fail-open
    };

    let session_id = data
        .get("session_id")
        .and_then(|v| v.as_str())
        .unwrap_or("unknown");
    let cwd = data.get("cwd").and_then(|v| v.as_str()).unwrap_or("");

    // Skip worktrees
    if is_worktree(cwd) {
        return;
    }

    // Fast path: if we already wrote a recent handoff, skip
    if continuity::has_recent_handoff(session_id) {
        return;
    }

    // Load config for context limit and warn threshold
    let merged = config::load_merged(Some(cwd));

    // Check if transcript_path is provided in hook JSON
    let transcript_path = data
        .get("transcript_path")
        .and_then(|v| v.as_str())
        .map(std::path::PathBuf::from)
        .or_else(|| transcript::find_transcript(session_id));

    let Some(transcript_path) = transcript_path else {
        return; // No transcript found — can't estimate context
    };

    // Fast check: estimate context from file size
    let (est_pct, exceeds) = transcript::check_context_threshold(
        &transcript_path,
        merged.continuity.context_warn_pct,
        merged.continuity.context_limit,
    );

    if !exceeds {
        return; // Under threshold — silent allow
    }

    // Over threshold: write handoff YAML
    let handoff_path = write_session_handoff(session_id, cwd, "stop_guard");

    // Output context warning
    let pct_display = (est_pct * 100.0).round() as u32;
    let handoff_note = match handoff_path {
        Some(p) => format!("Auto-handoff written to {}", p.display()),
        None => "Handoff write failed.".to_string(),
    };

    let output = serde_json::json!({
        "stopReason": format!(
            "Context at ~{pct_display}%. {handoff_note}. Consider running /compact or starting a new session."
        )
    });
    println!("{output}");
}

/// PreCompact hook: write handoff YAML before compaction.
///
/// Can be triggered manually (`dl hook pre-compact`) or registered as
/// a hook if Claude Code supports the PreCompact event. Always writes
/// the handoff regardless of context level.
pub fn pre_compact() {
    let input = read_stdin();
    let data: serde_json::Value = match serde_json::from_str(&input) {
        Ok(v) => v,
        Err(_) => return,
    };

    let session_id = data
        .get("session_id")
        .and_then(|v| v.as_str())
        .unwrap_or("unknown");
    let cwd = data.get("cwd").and_then(|v| v.as_str()).unwrap_or("");

    // Skip worktrees
    if is_worktree(cwd) {
        return;
    }

    // Write handoff YAML (always, regardless of context level)
    let handoff_path = write_session_handoff(session_id, cwd, "pre_compact");

    let note = match handoff_path {
        Some(p) => format!("Pre-compact handoff written to {}", p.display()),
        None => "Handoff write failed.".to_string(),
    };

    let output = serde_json::json!({
        "hookSpecificOutput": {
            "hookEventName": "PreCompact",
            "additionalContext": format!("dev-loop: {note}")
        }
    });
    println!("{output}");
}

/// Write a session handoff YAML. Queries daemon for session stats
/// and optionally parses transcript for file information.
///
/// Returns the handoff file path on success.
fn write_session_handoff(
    session_id: &str,
    cwd: &str,
    source: &str,
) -> Option<std::path::PathBuf> {
    let repo_root = config::find_repo_root(cwd).map(|p| p.to_string_lossy().to_string());
    let merged = config::load_merged(Some(cwd));
    let context_limit = merged.continuity.context_limit;

    // Get session stats from daemon
    let (checks, blocked, warned, trace_id, root_span_id) = get_session_stats(session_id);

    // Try to get transcript info
    let transcript_path = transcript::find_transcript(session_id);
    let (files_modified, files_created, token_estimate, goal, now, test_plan) = match transcript_path {
        Some(ref path) => {
            // Wait briefly for async writes to flush before parsing
            transcript::wait_for_flush(path, std::time::Duration::from_millis(500));
            let summary = transcript::parse_transcript(path);
            let token_est = if summary.total_tokens() > 0 {
                Some(continuity::TokenEstimate {
                    input_tokens: summary.input_tokens,
                    output_tokens: summary.output_tokens,
                    total: summary.total_tokens(),
                    context_pct: summary.context_pct_with_limit(context_limit),
                })
            } else {
                // Fall back to file size estimate
                transcript::estimate_tokens_from_size(path).map(|tokens| {
                    continuity::TokenEstimate {
                        input_tokens: 0,
                        output_tokens: 0,
                        total: tokens,
                        context_pct: tokens as f32 / context_limit as f32,
                    }
                })
            };
            (summary.files_modified, summary.files_created, token_est, summary.goal, summary.now, summary.test_plan)
        }
        None => (vec![], vec![], None, None, None, None),
    };

    let handoff = continuity::Handoff {
        session_id: session_id.to_string(),
        date: chrono::Utc::now().format("%Y-%m-%d").to_string(),
        source: source.to_string(),
        cwd: cwd.to_string(),
        repo_root,
        outcome: None, // Outcome set later via dl outcome
        notes: None,
        goal,
        now,
        test_plan,
        trace_id,
        root_span_id,
        files_modified,
        files_created,
        ambient_stats: continuity::AmbientStats {
            checks,
            blocked,
            warned,
        },
        token_estimate,
    };

    continuity::write_handoff(&handoff).ok()
}

/// Get session check/block/warn stats and trace IDs from daemon.
fn get_session_stats(
    session_id: &str,
) -> (u32, u32, u32, Option<String>, Option<String>) {
    let response = match get_from_daemon("/status") {
        Some(r) => r,
        None => return (0, 0, 0, None, None),
    };

    let data: serde_json::Value = match serde_json::from_str(&response) {
        Ok(v) => v,
        Err(_) => return (0, 0, 0, None, None),
    };

    let sessions = match data.get("sessions").and_then(|v| v.as_array()) {
        Some(s) => s,
        None => return (0, 0, 0, None, None),
    };

    for session in sessions {
        if session.get("session_id").and_then(|v| v.as_str()) == Some(session_id) {
            let checks = session
                .get("checks")
                .and_then(|v| v.as_u64())
                .unwrap_or(0) as u32;
            let blocks = session
                .get("blocks")
                .and_then(|v| v.as_u64())
                .unwrap_or(0) as u32;
            let warns = session
                .get("warns")
                .and_then(|v| v.as_u64())
                .unwrap_or(0) as u32;
            let trace_id = session
                .get("trace_id")
                .and_then(|v| v.as_str())
                .map(String::from);
            let root_span_id = session
                .get("root_span_id")
                .and_then(|v| v.as_str())
                .map(String::from);
            return (checks, blocks, warns, trace_id, root_span_id);
        }
    }

    (0, 0, 0, None, None)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn worktree_detection() {
        assert!(is_worktree("/tmp/dev-loop/worktrees/abc123/repo"));
        assert!(is_worktree("/tmp/dev-loop/worktrees/"));
        assert!(!is_worktree("/home/user/repo"));
        assert!(!is_worktree("/tmp/dev-loop/events.jsonl"));
        assert!(!is_worktree("/tmp/other/worktrees/"));
    }
}
