/// `dl traces --last N` — terminal event log viewer.
///
/// Reads the JSONL event log at `/tmp/dev-loop/events.jsonl` and displays
/// the last N events in a formatted table.
///
/// Event format (flat, via `#[serde(flatten)]` in Event struct):
/// ```json
/// {"ts":"14:32:01","type":"check","session":"abc-123","tool":"Write","action":"block","check":"deny_list","us":42}
/// ```
use std::io::{BufRead, BufReader};
use std::path::PathBuf;

const EVENT_LOG_PATH: &str = "/tmp/dev-loop/events.jsonl";

/// Display the last N events from the event log.
pub fn show_traces(last: usize) {
    let path = PathBuf::from(EVENT_LOG_PATH);

    if !path.exists() {
        eprintln!("No event log found at {}", path.display());
        eprintln!("Start the daemon with `dl start` to begin logging events.");
        std::process::exit(1);
    }

    let file = match std::fs::File::open(&path) {
        Ok(f) => f,
        Err(e) => {
            eprintln!("Failed to open {}: {e}", path.display());
            std::process::exit(1);
        }
    };

    // Read all lines and keep only the last N
    let reader = BufReader::new(file);
    let lines: Vec<String> = reader.lines().map_while(Result::ok).collect();

    let start = lines.len().saturating_sub(last);
    let entries: Vec<serde_json::Value> = lines[start..]
        .iter()
        .filter_map(|line| serde_json::from_str(line).ok())
        .collect();

    if entries.is_empty() {
        println!("No events found (log has {} lines).", lines.len());
        return;
    }

    // Print header
    println!(
        "{:<10} {:<16} {:<14} {}",
        "TIME", "TYPE", "SESSION", "DETAILS"
    );
    println!("{}", "-".repeat(76));

    for entry in &entries {
        // Timestamp is "ts" field (HH:MM:SS format)
        let ts = entry
            .get("ts")
            .and_then(|v| v.as_str())
            .unwrap_or("?");

        let event_type = entry
            .get("type")
            .and_then(|v| v.as_str())
            .unwrap_or("unknown");

        // Session ID: try "session" then "session_id"
        let session = entry
            .get("session")
            .or_else(|| entry.get("session_id"))
            .and_then(|v| v.as_str())
            .unwrap_or("");
        // Show first 12 chars of session id
        let session_short = if session.len() > 12 {
            &session[..12]
        } else {
            session
        };

        let details = format_details(event_type, entry);

        println!(
            "{:<10} {:<16} {:<14} {}",
            ts, event_type, session_short, details
        );
    }

    println!("{}", "-".repeat(76));
    println!(
        "Showing {} of {} events from {}",
        entries.len(),
        lines.len(),
        path.display()
    );
}

/// Format event-specific details for display.
///
/// All fields are at the top level of the entry (flat structure via serde flatten).
fn format_details(event_type: &str, e: &serde_json::Value) -> String {
    match event_type {
        "check" => {
            let tool = e.get("tool").and_then(|v| v.as_str()).unwrap_or("?");
            let action = e.get("action").and_then(|v| v.as_str()).unwrap_or("?");
            let check = e.get("check").and_then(|v| v.as_str()).unwrap_or("");
            let us = e.get("us").and_then(|v| v.as_u64()).unwrap_or(0);
            format!("{tool} -> {action} ({check}, {us}us)")
        }
        "checkpoint" => {
            let passed = e.get("passed").and_then(|v| v.as_bool()).unwrap_or(false);
            let gates_run = e.get("gates_run").and_then(|v| v.as_u64()).unwrap_or(0);
            let gates_passed = e
                .get("gates_passed")
                .and_then(|v| v.as_u64())
                .unwrap_or(0);
            let ms = e
                .get("duration_ms")
                .and_then(|v| v.as_u64())
                .unwrap_or(0);
            let status = if passed { "PASS" } else { "FAIL" };
            format!("{status} {gates_passed}/{gates_run} gates ({ms}ms)")
        }
        "session_start" => {
            let cwd = e.get("cwd").and_then(|v| v.as_str()).unwrap_or("?");
            format!("cwd={cwd}")
        }
        "session_end" => {
            let dur = e.get("duration_s").and_then(|v| v.as_u64()).unwrap_or(0);
            let checks = e.get("checks").and_then(|v| v.as_u64()).unwrap_or(0);
            format!("duration={dur}s checks={checks}")
        }
        "daemon_started" | "daemon_stopped" => String::new(),
        _ => {
            // Show interesting top-level keys (skip ts, type, session)
            if let Some(obj) = e.as_object() {
                let skip = ["ts", "type", "session", "session_id"];
                let pairs: Vec<String> = obj
                    .iter()
                    .filter(|(k, _)| !skip.contains(&k.as_str()))
                    .map(|(k, v)| {
                        if let Some(s) = v.as_str() {
                            format!("{k}={s}")
                        } else {
                            format!("{k}={v}")
                        }
                    })
                    .collect();
                pairs.join(", ")
            } else {
                String::new()
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn format_check_details_flat() {
        // Flat format (as actually stored in JSONL)
        let entry = serde_json::json!({
            "ts": "14:32:01",
            "type": "check",
            "session": "abc-123",
            "tool": "Write",
            "action": "block",
            "check": "deny_list",
            "us": 42
        });
        let details = format_details("check", &entry);
        assert!(details.contains("Write"));
        assert!(details.contains("block"));
        assert!(details.contains("deny_list"));
        assert!(details.contains("42us"));
    }

    #[test]
    fn format_checkpoint_details_flat() {
        let entry = serde_json::json!({
            "ts": "14:32:10",
            "type": "checkpoint",
            "passed": true,
            "gates_run": 3,
            "gates_passed": 3,
            "duration_ms": 4500
        });
        let details = format_details("checkpoint", &entry);
        assert!(details.contains("PASS"));
        assert!(details.contains("3/3"));
    }

    #[test]
    fn format_session_start_flat() {
        let entry = serde_json::json!({
            "ts": "14:33:00",
            "type": "session_start",
            "session": "abc-123",
            "cwd": "/home/user/repo"
        });
        let details = format_details("session_start", &entry);
        assert!(details.contains("/home/user/repo"));
    }

    #[test]
    fn format_session_end_flat() {
        let entry = serde_json::json!({
            "ts": "14:33:30",
            "type": "session_end",
            "duration_s": 180,
            "checks": 12
        });
        let details = format_details("session_end", &entry);
        assert!(details.contains("180s"));
        assert!(details.contains("12"));
    }
}
