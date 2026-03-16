/// Tier 2 Checkpoint: gate suite that runs before `git commit`.
///
/// Gates run sequentially, fail-fast. Each gate produces a `GateResult`.
/// On all-pass, a `Dev-Loop-Gate: <sha256>` trailer is returned for
/// injection into the commit message.
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::path::Path;
use std::process::Command;
use std::time::Instant;

use crate::config::CheckpointConfig;

/// Input to the /checkpoint endpoint.
#[derive(Debug, Deserialize)]
pub struct CheckpointRequest {
    pub cwd: String,
    pub session_id: Option<String>,
}

/// Overall checkpoint result.
#[derive(Debug, Serialize)]
pub struct CheckpointResult {
    pub passed: bool,
    pub gates_run: usize,
    pub gates_passed: usize,
    pub gates_failed: usize,
    pub first_failure: Option<String>,
    pub trailer: Option<String>,
    pub gate_results: Vec<GateResult>,
    pub duration_ms: u64,
}

/// Result of a single gate.
#[derive(Debug, Clone, Serialize)]
pub struct GateResult {
    pub gate: String,
    pub passed: bool,
    pub reason: Option<String>,
    pub findings: Vec<String>,
    pub duration_ms: u64,
}

/// Run the full checkpoint gate suite for a repo at `cwd`.
pub fn run_checkpoint(cwd: &str, config: &CheckpointConfig) -> CheckpointResult {
    let start = Instant::now();
    let mut gate_results = Vec::new();
    let active_gates = &config.gates;

    // Get staged files for targeted scanning
    let staged_files = get_staged_files(cwd);

    // No staged files → nothing to check
    if staged_files.is_empty() {
        return CheckpointResult {
            passed: true,
            gates_run: 0,
            gates_passed: 0,
            gates_failed: 0,
            first_failure: None,
            trailer: Some(build_trailer(&[])),
            gate_results: Vec::new(),
            duration_ms: start.elapsed().as_millis() as u64,
        };
    }

    // Run gates sequentially, fail-fast
    for gate_name in active_gates {
        let result = match gate_name.as_str() {
            "sanity" => run_sanity_gate(cwd, config),
            "semgrep" => run_semgrep_gate(cwd, &staged_files),
            "secrets" => run_secrets_gate(cwd),
            "atdd" => run_atdd_gate(cwd, config, &staged_files),
            "review" => {
                // Review gate is optional / deferred
                GateResult {
                    gate: "review".into(),
                    passed: true,
                    reason: Some("skipped (not configured)".into()),
                    findings: vec![],
                    duration_ms: 0,
                }
            }
            other => GateResult {
                gate: other.into(),
                passed: true,
                reason: Some(format!("unknown gate '{other}', skipped")),
                findings: vec![],
                duration_ms: 0,
            },
        };

        let failed = !result.passed;
        gate_results.push(result);

        if failed {
            // Fail-fast: stop running remaining gates
            break;
        }
    }

    let gates_run = gate_results.len();
    let gates_passed = gate_results.iter().filter(|g| g.passed).count();
    let gates_failed = gate_results.iter().filter(|g| !g.passed).count();
    let first_failure = gate_results
        .iter()
        .find(|g| !g.passed)
        .map(|g| g.gate.clone());
    let passed = gates_failed == 0;

    let trailer = if passed {
        Some(build_trailer(&gate_results))
    } else {
        None
    };

    CheckpointResult {
        passed,
        gates_run,
        gates_passed,
        gates_failed,
        first_failure,
        trailer,
        gate_results,
        duration_ms: start.elapsed().as_millis() as u64,
    }
}

/// Get list of staged files (git diff --cached --name-only).
fn get_staged_files(cwd: &str) -> Vec<String> {
    let output = Command::new("git")
        .args(["diff", "--cached", "--name-only"])
        .current_dir(cwd)
        .output();

    match output {
        Ok(o) if o.status.success() => String::from_utf8_lossy(&o.stdout)
            .lines()
            .filter(|l| !l.is_empty())
            .map(String::from)
            .collect(),
        _ => Vec::new(),
    }
}

// ── Gate: Sanity (test runner) ──────────────────────────────────

fn run_sanity_gate(cwd: &str, config: &CheckpointConfig) -> GateResult {
    let start = Instant::now();

    // Use configured test command, or auto-detect
    let test_cmd = config
        .test_command
        .as_deref()
        .or_else(|| detect_test_command(cwd));

    let test_cmd = match test_cmd {
        Some(cmd) => cmd,
        None => {
            return GateResult {
                gate: "sanity".into(),
                passed: true,
                reason: Some("no test command configured or detected, skipped".into()),
                findings: vec![],
                duration_ms: start.elapsed().as_millis() as u64,
            };
        }
    };

    let output = Command::new("sh")
        .args(["-c", test_cmd])
        .current_dir(cwd)
        .output();

    match output {
        Ok(o) => {
            let passed = o.status.success();
            let stderr = String::from_utf8_lossy(&o.stderr);
            let stdout = String::from_utf8_lossy(&o.stdout);
            GateResult {
                gate: "sanity".into(),
                passed,
                reason: if passed {
                    None
                } else {
                    Some(format!("test command failed: {test_cmd}"))
                },
                findings: if passed {
                    vec![]
                } else {
                    // Capture last 20 lines of output for diagnostics
                    let combined = format!("{stdout}\n{stderr}");
                    combined
                        .lines()
                        .rev()
                        .take(20)
                        .collect::<Vec<_>>()
                        .into_iter()
                        .rev()
                        .map(String::from)
                        .collect()
                },
                duration_ms: start.elapsed().as_millis() as u64,
            }
        }
        Err(e) => GateResult {
            gate: "sanity".into(),
            passed: true, // fail-open if test runner can't start
            reason: Some(format!("could not run test command: {e}")),
            findings: vec![],
            duration_ms: start.elapsed().as_millis() as u64,
        },
    }
}

/// Auto-detect test command based on project files.
fn detect_test_command(cwd: &str) -> Option<&'static str> {
    let p = Path::new(cwd);
    if p.join("Cargo.toml").exists() {
        Some("cargo test")
    } else if p.join("package.json").exists() {
        Some("npm test")
    } else if p.join("pyproject.toml").exists() {
        Some("uv run pytest")
    } else if p.join("setup.py").exists() {
        Some("python -m pytest")
    } else {
        None
    }
}

// ── Gate: Semgrep (SAST) ────────────────────────────────────────

fn run_semgrep_gate(cwd: &str, staged_files: &[String]) -> GateResult {
    let start = Instant::now();

    // Check if semgrep is available
    if !is_tool_available("semgrep") {
        return GateResult {
            gate: "semgrep".into(),
            passed: true,
            reason: Some("semgrep not installed, skipped".into()),
            findings: vec![],
            duration_ms: start.elapsed().as_millis() as u64,
        };
    }

    // Run semgrep on staged files with auto config
    let mut cmd = Command::new("semgrep");
    cmd.args(["--config", "auto", "--json", "--quiet", "--no-git-ignore"])
        .current_dir(cwd);

    // Add each staged file as a target
    for file in staged_files {
        // Only scan files that exist (not deleted)
        let full_path = Path::new(cwd).join(file);
        if full_path.exists() {
            cmd.arg(file);
        }
    }

    let output = cmd.output();

    match output {
        Ok(o) => {
            let stdout = String::from_utf8_lossy(&o.stdout);

            // Parse semgrep JSON output
            let findings = parse_semgrep_findings(&stdout);

            if findings.is_empty() {
                GateResult {
                    gate: "semgrep".into(),
                    passed: true,
                    reason: None,
                    findings: vec![],
                    duration_ms: start.elapsed().as_millis() as u64,
                }
            } else {
                GateResult {
                    gate: "semgrep".into(),
                    passed: false,
                    reason: Some(format!("{} finding(s) from semgrep", findings.len())),
                    findings,
                    duration_ms: start.elapsed().as_millis() as u64,
                }
            }
        }
        Err(e) => GateResult {
            gate: "semgrep".into(),
            passed: true, // fail-open
            reason: Some(format!("semgrep failed to run: {e}")),
            findings: vec![],
            duration_ms: start.elapsed().as_millis() as u64,
        },
    }
}

/// Parse semgrep JSON output into human-readable finding strings.
fn parse_semgrep_findings(json_str: &str) -> Vec<String> {
    let parsed: serde_json::Value = match serde_json::from_str(json_str) {
        Ok(v) => v,
        Err(_) => return Vec::new(),
    };

    let results = match parsed.get("results").and_then(|v| v.as_array()) {
        Some(arr) => arr,
        None => return Vec::new(),
    };

    results
        .iter()
        .filter_map(|r| {
            let path = r.get("path")?.as_str()?;
            let line = r
                .get("start")
                .and_then(|s| s.get("line"))
                .and_then(|l| l.as_u64())
                .unwrap_or(0);
            let check_id = r.get("check_id")?.as_str()?;
            let message = r
                .get("extra")
                .and_then(|e| e.get("message"))
                .and_then(|m| m.as_str())
                .unwrap_or("");
            let severity = r
                .get("extra")
                .and_then(|e| e.get("severity"))
                .and_then(|s| s.as_str())
                .unwrap_or("WARNING");

            Some(format!("{path}:{line} [{severity}] {check_id}: {message}"))
        })
        .collect()
}

// ── Gate: Secrets (gitleaks) ────────────────────────────────────

fn run_secrets_gate(cwd: &str) -> GateResult {
    let start = Instant::now();

    if !is_tool_available("gitleaks") {
        return GateResult {
            gate: "secrets".into(),
            passed: true,
            reason: Some("gitleaks not installed, skipped".into()),
            findings: vec![],
            duration_ms: start.elapsed().as_millis() as u64,
        };
    }

    // Run gitleaks on staged changes using --pipe mode:
    // git diff --cached | gitleaks detect --pipe
    let git_diff = Command::new("git")
        .args(["diff", "--cached"])
        .current_dir(cwd)
        .output();

    let diff_content = match git_diff {
        Ok(o) if o.status.success() => o.stdout,
        _ => {
            return GateResult {
                gate: "secrets".into(),
                passed: true,
                reason: Some("could not get staged diff".into()),
                findings: vec![],
                duration_ms: start.elapsed().as_millis() as u64,
            };
        }
    };

    // Empty diff → no secrets possible
    if diff_content.is_empty() {
        return GateResult {
            gate: "secrets".into(),
            passed: true,
            reason: None,
            findings: vec![],
            duration_ms: start.elapsed().as_millis() as u64,
        };
    }

    let mut child = match Command::new("gitleaks")
        .args(["detect", "--pipe", "--report-format", "json", "--no-banner"])
        .current_dir(cwd)
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
    {
        Ok(c) => c,
        Err(e) => {
            return GateResult {
                gate: "secrets".into(),
                passed: true,
                reason: Some(format!("gitleaks failed to start: {e}")),
                findings: vec![],
                duration_ms: start.elapsed().as_millis() as u64,
            };
        }
    };

    // Write diff to gitleaks stdin
    if let Some(mut stdin) = child.stdin.take() {
        use std::io::Write;
        let _ = stdin.write_all(&diff_content);
    }

    let output = child.wait_with_output();

    match output {
        Ok(o) => {
            // gitleaks exit code 1 = leaks found, 0 = clean
            let passed = o.status.success();
            let stdout = String::from_utf8_lossy(&o.stdout);
            let stderr = String::from_utf8_lossy(&o.stderr);

            if passed {
                GateResult {
                    gate: "secrets".into(),
                    passed: true,
                    reason: None,
                    findings: vec![],
                    duration_ms: start.elapsed().as_millis() as u64,
                }
            } else {
                let findings = parse_gitleaks_findings(&stdout, &stderr);
                GateResult {
                    gate: "secrets".into(),
                    passed: false,
                    reason: Some(format!("{} secret(s) detected by gitleaks", findings.len())),
                    findings,
                    duration_ms: start.elapsed().as_millis() as u64,
                }
            }
        }
        Err(e) => GateResult {
            gate: "secrets".into(),
            passed: true, // fail-open
            reason: Some(format!("gitleaks failed to run: {e}")),
            findings: vec![],
            duration_ms: start.elapsed().as_millis() as u64,
        },
    }
}

/// Parse gitleaks JSON output.
fn parse_gitleaks_findings(stdout: &str, stderr: &str) -> Vec<String> {
    // gitleaks outputs JSON array of findings
    let parsed: Vec<serde_json::Value> = match serde_json::from_str(stdout) {
        Ok(v) => v,
        Err(_) => {
            // If stdout isn't valid JSON, try stderr, or return raw message
            if !stderr.is_empty() {
                return vec![stderr.trim().to_string()];
            }
            return vec!["gitleaks detected secrets (details unavailable)".into()];
        }
    };

    parsed
        .iter()
        .filter_map(|f| {
            let file = f.get("File")?.as_str()?;
            let line = f
                .get("StartLine")
                .and_then(|l| l.as_u64())
                .unwrap_or(0);
            let rule = f.get("RuleID")?.as_str()?;
            let description = f
                .get("Description")
                .and_then(|d| d.as_str())
                .unwrap_or("");
            Some(format!("{file}:{line} [{rule}] {description}"))
        })
        .collect()
}

// ── Gate: ATDD (spec-before-code enforcement) ───────────────────

fn run_atdd_gate(_cwd: &str, config: &CheckpointConfig, staged_files: &[String]) -> GateResult {
    let start = Instant::now();

    if !config.atdd_required {
        return GateResult {
            gate: "atdd".into(),
            passed: true,
            reason: Some("atdd_required not set, skipped".into()),
            findings: vec![],
            duration_ms: start.elapsed().as_millis() as u64,
        };
    }

    // Check if there are code changes without spec files
    let has_code_changes = staged_files.iter().any(|f| is_code_file(f));
    let has_spec_files = staged_files
        .iter()
        .any(|f| is_spec_file(f) || f.starts_with("specs/"));

    if has_code_changes && !has_spec_files {
        GateResult {
            gate: "atdd".into(),
            passed: false,
            reason: Some(
                "ATDD: Write a Given/When/Then spec before implementing code".into(),
            ),
            findings: vec![
                "Code changes detected without corresponding spec files.".into(),
                "Add a spec file (*.spec.md or specs/*.md) to the staged changes.".into(),
            ],
            duration_ms: start.elapsed().as_millis() as u64,
        }
    } else {
        GateResult {
            gate: "atdd".into(),
            passed: true,
            reason: None,
            findings: vec![],
            duration_ms: start.elapsed().as_millis() as u64,
        }
    }
}

/// Check if a file is a code file (not config, docs, or tests metadata).
fn is_code_file(path: &str) -> bool {
    let code_extensions = [
        ".rs", ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".java", ".c", ".cpp", ".h", ".rb",
        ".php", ".swift", ".kt", ".cs", ".scala", ".ex", ".exs",
    ];
    code_extensions.iter().any(|ext| path.ends_with(ext))
        && !path.contains("test")
        && !path.contains("spec")
        && !path.contains("fixture")
}

/// Check if a file is a spec file.
fn is_spec_file(path: &str) -> bool {
    path.ends_with(".spec.md")
        || path.ends_with(".spec.yaml")
        || path.ends_with(".spec.yml")
        || path.ends_with(".feature")
}

// ── Trailer ─────────────────────────────────────────────────────

/// Build a Dev-Loop-Gate trailer from gate results.
/// Hash is sha256 of the JSON-serialized gate results.
fn build_trailer(gate_results: &[GateResult]) -> String {
    let json = serde_json::to_string(gate_results).unwrap_or_default();
    let mut hasher = Sha256::new();
    hasher.update(json.as_bytes());
    let hash = hasher.finalize();
    let short_hash = hex::encode(&hash[..8]); // 16 hex chars
    format!("Dev-Loop-Gate: {short_hash}")
}

// ── Helpers ─────────────────────────────────────────────────────

/// Check if a CLI tool is available on PATH.
fn is_tool_available(name: &str) -> bool {
    Command::new("which")
        .arg(name)
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

// ── Tests ───────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn build_trailer_deterministic() {
        let results = vec![GateResult {
            gate: "test".into(),
            passed: true,
            reason: None,
            findings: vec![],
            duration_ms: 42,
        }];
        let t1 = build_trailer(&results);
        let t2 = build_trailer(&results);
        assert_eq!(t1, t2);
        assert!(t1.starts_with("Dev-Loop-Gate: "));
        assert_eq!(t1.len(), "Dev-Loop-Gate: ".len() + 16); // 16 hex chars
    }

    #[test]
    fn build_trailer_empty() {
        let t = build_trailer(&[]);
        assert!(t.starts_with("Dev-Loop-Gate: "));
    }

    #[test]
    fn is_code_file_detects_source() {
        assert!(is_code_file("src/main.rs"));
        assert!(is_code_file("app/index.ts"));
        assert!(is_code_file("lib/utils.py"));
        assert!(!is_code_file("README.md"));
        assert!(!is_code_file("Cargo.toml"));
        assert!(!is_code_file("tests/test_main.rs")); // contains "test"
    }

    #[test]
    fn is_spec_file_detects_specs() {
        assert!(is_spec_file("specs/auth.spec.md"));
        assert!(is_spec_file("feature.spec.yaml"));
        assert!(is_spec_file("login.feature"));
        assert!(!is_spec_file("src/main.rs"));
        assert!(!is_spec_file("README.md"));
    }

    #[test]
    fn parse_semgrep_empty() {
        assert!(parse_semgrep_findings("{}").is_empty());
        assert!(parse_semgrep_findings("{\"results\":[]}").is_empty());
        assert!(parse_semgrep_findings("not json").is_empty());
    }

    #[test]
    fn parse_semgrep_findings_extracts() {
        let json = r#"{
            "results": [{
                "path": "src/main.py",
                "start": {"line": 42},
                "check_id": "python.lang.security.audit.exec-detected",
                "extra": {
                    "message": "Use of exec() detected",
                    "severity": "ERROR"
                }
            }]
        }"#;
        let findings = parse_semgrep_findings(json);
        assert_eq!(findings.len(), 1);
        assert!(findings[0].contains("src/main.py:42"));
        assert!(findings[0].contains("[ERROR]"));
        assert!(findings[0].contains("exec-detected"));
    }

    #[test]
    fn parse_gitleaks_empty() {
        let findings = parse_gitleaks_findings("[]", "");
        assert!(findings.is_empty());
    }

    #[test]
    fn parse_gitleaks_findings_extracts() {
        let json = r#"[{
            "File": ".env",
            "StartLine": 3,
            "RuleID": "generic-api-key",
            "Description": "Generic API Key"
        }]"#;
        let findings = parse_gitleaks_findings(json, "");
        assert_eq!(findings.len(), 1);
        assert!(findings[0].contains(".env:3"));
        assert!(findings[0].contains("generic-api-key"));
    }

    #[test]
    fn detect_test_command_cargo() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("Cargo.toml"), "[package]\nname = \"test\"").unwrap();
        assert_eq!(
            detect_test_command(dir.path().to_str().unwrap()),
            Some("cargo test")
        );
    }

    #[test]
    fn detect_test_command_npm() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("package.json"), "{}").unwrap();
        assert_eq!(
            detect_test_command(dir.path().to_str().unwrap()),
            Some("npm test")
        );
    }

    #[test]
    fn detect_test_command_none() {
        let dir = tempfile::tempdir().unwrap();
        assert_eq!(detect_test_command(dir.path().to_str().unwrap()), None);
    }

    #[test]
    fn atdd_skipped_when_not_required() {
        let config = CheckpointConfig {
            atdd_required: false,
            ..Default::default()
        };
        let result = run_atdd_gate("/tmp", &config, &["src/main.rs".into()]);
        assert!(result.passed);
        assert!(result.reason.unwrap().contains("skipped"));
    }

    #[test]
    fn atdd_fails_code_without_spec() {
        let config = CheckpointConfig {
            atdd_required: true,
            ..Default::default()
        };
        let result = run_atdd_gate("/tmp", &config, &["src/main.rs".into()]);
        assert!(!result.passed);
        assert!(result.reason.unwrap().contains("ATDD"));
    }

    #[test]
    fn atdd_passes_code_with_spec() {
        let config = CheckpointConfig {
            atdd_required: true,
            ..Default::default()
        };
        let result = run_atdd_gate(
            "/tmp",
            &config,
            &["src/main.rs".into(), "specs/auth.spec.md".into()],
        );
        assert!(result.passed);
    }

    #[test]
    fn atdd_passes_no_code_files() {
        let config = CheckpointConfig {
            atdd_required: true,
            ..Default::default()
        };
        let result = run_atdd_gate(
            "/tmp",
            &config,
            &["README.md".into(), "Cargo.toml".into()],
        );
        assert!(result.passed);
    }

    #[test]
    fn checkpoint_no_staged_files() {
        // Use a temp dir with no git repo — get_staged_files returns empty
        let dir = tempfile::tempdir().unwrap();
        let config = CheckpointConfig::default();
        let result = run_checkpoint(dir.path().to_str().unwrap(), &config);
        assert!(result.passed);
        assert_eq!(result.gates_run, 0);
        assert!(result.trailer.is_some());
    }
}
