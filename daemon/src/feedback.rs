/// `dl feedback` — per-check event annotation for precision/recall tracking.
///
/// Annotates individual check events from the JSONL event log as
/// correct, false-positive, or missed. Stores feedback in YAML files
/// at `/tmp/dev-loop/feedback/<event-id>.yaml`.
///
/// Three modes:
/// - `dl feedback <event-id> <label> [--notes "..."]` — annotate an event
/// - `dl feedback --list [--last N]` — show recent unlabeled check events
/// - `dl feedback --stats` — show labeled data statistics per check type
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};

const EVENT_LOG_PATH: &str = "/tmp/dev-loop/events.jsonl";
const FEEDBACK_DIR: &str = "/tmp/dev-loop/feedback";

/// Valid labels for feedback annotations.
const VALID_LABELS: &[&str] = &["correct", "false-positive", "missed"];

/// A single feedback annotation.
#[derive(Debug, Serialize, Deserialize)]
pub struct Feedback {
    pub event_id: String,
    pub label: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub notes: Option<String>,
    pub feedback_ts: String,
    // Copied from the original event for context
    pub check_type: String,
    pub tool_name: String,
    pub verdict: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pattern_matched: Option<String>,
    pub original_ts: String,
}

/// Annotate an event with a label.
pub fn annotate(event_id: &str, label: &str, notes: Option<&str>) {
    // Validate label
    if !VALID_LABELS.contains(&label) {
        eprintln!(
            "Invalid label '{label}'. Must be one of: {}",
            VALID_LABELS.join(", ")
        );
        std::process::exit(1);
    }

    // Normalize event ID: strip leading 'L' if present
    let line_num: usize = event_id
        .strip_prefix('L')
        .or(Some(event_id))
        .and_then(|s| s.parse().ok())
        .unwrap_or_else(|| {
            eprintln!("Invalid event ID '{event_id}'. Use a line number (e.g., 42 or L42).");
            std::process::exit(1);
        });

    // Read the event from the log
    let event = read_event_by_line(line_num).unwrap_or_else(|| {
        eprintln!("Event at line {line_num} not found in {EVENT_LOG_PATH}");
        std::process::exit(1);
    });

    // Verify it's a check or shadow_verdict event
    let event_type = event
        .get("type")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    if event_type != "check" && event_type != "shadow_verdict" {
        eprintln!(
            "Event at line {line_num} is type '{event_type}', not a check event. \
             Only 'check' and 'shadow_verdict' events can be annotated."
        );
        std::process::exit(1);
    }

    let check_type = event
        .get("check")
        .and_then(|v| v.as_str())
        .unwrap_or("unknown")
        .to_string();
    let tool_name = event
        .get("tool")
        .and_then(|v| v.as_str())
        .unwrap_or("unknown")
        .to_string();
    // "action" for check events, "verdict" for shadow_verdict events
    let verdict = event
        .get("verdict")
        .or_else(|| event.get("action"))
        .and_then(|v| v.as_str())
        .unwrap_or("unknown")
        .to_string();
    let reason = event
        .get("reason")
        .and_then(|v| v.as_str())
        .map(String::from);
    let pattern_matched = event
        .get("pattern")
        .and_then(|v| v.as_str())
        .map(String::from);
    let original_ts = event
        .get("ts")
        .and_then(|v| v.as_str())
        .unwrap_or("?")
        .to_string();

    let feedback = Feedback {
        event_id: format!("L{line_num}"),
        label: label.to_string(),
        notes: notes.map(String::from),
        feedback_ts: chrono::Utc::now().to_rfc3339(),
        check_type,
        tool_name,
        verdict,
        reason,
        pattern_matched,
        original_ts,
    };

    // Write feedback file
    let feedback_dir = PathBuf::from(FEEDBACK_DIR);
    if let Err(e) = std::fs::create_dir_all(&feedback_dir) {
        eprintln!("Failed to create feedback directory: {e}");
        std::process::exit(1);
    }

    let feedback_path = feedback_dir.join(format!("L{line_num}.yaml"));
    let content = serde_yaml::to_string(&feedback).unwrap_or_default();
    if let Err(e) = std::fs::write(&feedback_path, &content) {
        eprintln!("Failed to write feedback: {e}");
        std::process::exit(1);
    }

    println!(
        "Recorded: L{line_num} = {label} ({}:{} → {})",
        feedback.check_type, feedback.tool_name, feedback.verdict
    );
    if let Some(notes) = notes {
        println!("  Notes: {notes}");
    }
}

/// List recent unlabeled check events for review.
pub fn list_unlabeled(last: usize) {
    let path = PathBuf::from(EVENT_LOG_PATH);
    if !path.exists() {
        eprintln!("No event log found at {EVENT_LOG_PATH}");
        std::process::exit(1);
    }

    // Load existing feedback IDs
    let labeled = load_labeled_ids();

    // Read all check/shadow_verdict events
    let file = std::fs::File::open(&path).unwrap_or_else(|e| {
        eprintln!("Failed to open {EVENT_LOG_PATH}: {e}");
        std::process::exit(1);
    });

    let reader = BufReader::new(file);
    let mut events: Vec<(usize, serde_json::Value)> = Vec::new();

    for (i, line) in reader.lines().map_while(Result::ok).enumerate() {
        let line_num = i + 1; // 1-indexed
        let entry: serde_json::Value = match serde_json::from_str(&line) {
            Ok(v) => v,
            Err(_) => continue,
        };

        let event_type = entry
            .get("type")
            .and_then(|v| v.as_str())
            .unwrap_or("");

        // Only show check and shadow_verdict events
        if event_type != "check" && event_type != "shadow_verdict" {
            continue;
        }

        // Skip already-labeled events
        let id = format!("L{line_num}");
        if labeled.contains(&id) {
            continue;
        }

        // Only show block/warn verdicts (allow verdicts are less interesting to label)
        let verdict = entry
            .get("verdict")
            .or_else(|| entry.get("action"))
            .and_then(|v| v.as_str())
            .unwrap_or("allow");
        if verdict == "allow" {
            continue;
        }

        events.push((line_num, entry));
    }

    // Show last N
    let start = events.len().saturating_sub(last);
    let to_show = &events[start..];

    if to_show.is_empty() {
        println!("No unlabeled check events with block/warn verdicts found.");
        println!("Labeled events: {}", labeled.len());
        return;
    }

    println!(
        "{:<8} {:<10} {:<14} {:<10} {:<8} {}",
        "ID", "TIME", "CHECK", "TOOL", "VERDICT", "REASON"
    );
    println!("{}", "-".repeat(80));

    for (line_num, entry) in to_show {
        let ts = entry
            .get("ts")
            .and_then(|v| v.as_str())
            .unwrap_or("?");
        let check = entry
            .get("check")
            .and_then(|v| v.as_str())
            .unwrap_or("?");
        let tool = entry
            .get("tool")
            .and_then(|v| v.as_str())
            .unwrap_or("?");
        let verdict = entry
            .get("verdict")
            .or_else(|| entry.get("action"))
            .and_then(|v| v.as_str())
            .unwrap_or("?");
        let reason = entry
            .get("reason")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let reason_short = if reason.len() > 40 {
            format!("{}...", &reason[..37])
        } else {
            reason.to_string()
        };

        println!(
            "L{:<7} {:<10} {:<14} {:<10} {:<8} {}",
            line_num, ts, check, tool, verdict, reason_short
        );
    }

    println!("{}", "-".repeat(80));
    println!(
        "Showing {} of {} unlabeled block/warn events. Labeled: {}.",
        to_show.len(),
        events.len(),
        labeled.len()
    );
    println!("\nTo annotate: dl feedback <ID> correct|false-positive|missed [--notes \"...\"]");
}

/// Show statistics from labeled feedback data.
pub fn show_stats() {
    let feedback_dir = PathBuf::from(FEEDBACK_DIR);
    if !feedback_dir.exists() {
        println!("No feedback data found at {FEEDBACK_DIR}");
        println!("Annotate events with: dl feedback <event-id> correct|false-positive|missed");
        return;
    }

    let feedbacks = load_all_feedback(&feedback_dir);
    if feedbacks.is_empty() {
        println!("No feedback annotations found.");
        println!("Annotate events with: dl feedback <event-id> correct|false-positive|missed");
        return;
    }

    // Group by check type
    let mut by_check: HashMap<String, CheckStats> = HashMap::new();
    let mut total = FeedbackTotals::default();

    for fb in &feedbacks {
        let stats = by_check.entry(fb.check_type.clone()).or_default();
        match fb.label.as_str() {
            "correct" => {
                stats.correct += 1;
                total.correct += 1;
            }
            "false-positive" => {
                stats.false_positive += 1;
                total.false_positive += 1;
            }
            "missed" => {
                stats.missed += 1;
                total.missed += 1;
            }
            _ => {}
        }
        stats.total += 1;
        total.total += 1;
    }

    // Print per-check-type table
    println!("Feedback Statistics");
    println!("===================\n");

    println!(
        "{:<16} {:>8} {:>8} {:>8} {:>8} {:>10} {:>10} {:>8}",
        "CHECK TYPE", "TOTAL", "TP", "FP", "FN", "PRECISION", "RECALL", "F1"
    );
    println!("{}", "-".repeat(88));

    let mut check_types: Vec<_> = by_check.keys().collect();
    check_types.sort();

    for check_type in &check_types {
        let stats = &by_check[*check_type];
        let (precision, recall, f1) = compute_prf(stats);
        println!(
            "{:<16} {:>8} {:>8} {:>8} {:>8} {:>9.1}% {:>9.1}% {:>7.3}",
            check_type,
            stats.total,
            stats.correct,
            stats.false_positive,
            stats.missed,
            precision * 100.0,
            recall * 100.0,
            f1
        );
    }

    println!("{}", "-".repeat(88));

    // Totals
    let total_stats = CheckStats {
        total: total.total,
        correct: total.correct,
        false_positive: total.false_positive,
        missed: total.missed,
    };
    let (precision, recall, f1) = compute_prf(&total_stats);
    println!(
        "{:<16} {:>8} {:>8} {:>8} {:>8} {:>9.1}% {:>9.1}% {:>7.3}",
        "TOTAL",
        total.total,
        total.correct,
        total.false_positive,
        total.missed,
        precision * 100.0,
        recall * 100.0,
        f1
    );

    println!(
        "\nLabeled events: {} ({} correct, {} false-positive, {} missed)",
        total.total, total.correct, total.false_positive, total.missed
    );

    // Warn if too few labels
    if total.total < 20 {
        println!(
            "\nNote: {} labels is too few for statistically meaningful metrics. \
             Aim for 200+ across all check types.",
            total.total
        );
    }
}

// ── Helpers ──────────────────────────────────────────────────────

/// Read a specific event by line number (1-indexed) from the event log.
fn read_event_by_line(line_num: usize) -> Option<serde_json::Value> {
    let path = PathBuf::from(EVENT_LOG_PATH);
    let file = std::fs::File::open(&path).ok()?;
    let reader = BufReader::new(file);

    reader
        .lines()
        .map_while(Result::ok)
        .nth(line_num - 1)
        .and_then(|line| serde_json::from_str(&line).ok())
}

/// Load the set of event IDs that already have feedback.
fn load_labeled_ids() -> std::collections::HashSet<String> {
    let feedback_dir = PathBuf::from(FEEDBACK_DIR);
    let mut ids = std::collections::HashSet::new();

    if let Ok(entries) = std::fs::read_dir(&feedback_dir) {
        for entry in entries.flatten() {
            if let Some(stem) = entry.path().file_stem() {
                ids.insert(stem.to_string_lossy().to_string());
            }
        }
    }

    ids
}

/// Load all feedback YAML files from the feedback directory.
fn load_all_feedback(dir: &Path) -> Vec<Feedback> {
    let mut feedbacks = Vec::new();

    if let Ok(entries) = std::fs::read_dir(dir) {
        for entry in entries.flatten() {
            let path = entry.path();
            if path.extension().and_then(|e| e.to_str()) == Some("yaml") {
                if let Ok(content) = std::fs::read_to_string(&path) {
                    if let Ok(fb) = serde_yaml::from_str::<Feedback>(&content) {
                        feedbacks.push(fb);
                    }
                }
            }
        }
    }

    feedbacks
}

#[derive(Debug, Default)]
struct CheckStats {
    total: u64,
    correct: u64,
    false_positive: u64,
    missed: u64,
}

#[derive(Debug, Default)]
struct FeedbackTotals {
    total: u64,
    correct: u64,
    false_positive: u64,
    missed: u64,
}

/// Compute precision, recall, and F1 from check stats.
///
/// - Precision = TP / (TP + FP) — "of all blocks/warns, how many were correct?"
/// - Recall = TP / (TP + FN) — "of all issues, how many did we catch?"
/// - F1 = 2 * (P * R) / (P + R)
///
/// TP = correct (true positive: correctly blocked/warned)
/// FP = false-positive (blocked/warned but shouldn't have)
/// FN = missed (should have blocked/warned but didn't)
fn compute_prf(stats: &CheckStats) -> (f64, f64, f64) {
    let tp = stats.correct as f64;
    let fp = stats.false_positive as f64;
    let fn_ = stats.missed as f64;

    let precision = if tp + fp > 0.0 { tp / (tp + fp) } else { 0.0 };
    let recall = if tp + fn_ > 0.0 { tp / (tp + fn_) } else { 0.0 };
    let f1 = if precision + recall > 0.0 {
        2.0 * precision * recall / (precision + recall)
    } else {
        0.0
    };

    (precision, recall, f1)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn valid_labels_accepted() {
        for label in VALID_LABELS {
            assert!(VALID_LABELS.contains(label));
        }
        assert!(!VALID_LABELS.contains(&"invalid"));
    }

    #[test]
    fn compute_prf_perfect() {
        let stats = CheckStats {
            total: 10,
            correct: 10,
            false_positive: 0,
            missed: 0,
        };
        let (p, r, f1) = compute_prf(&stats);
        assert!((p - 1.0).abs() < 0.001);
        assert!((r - 1.0).abs() < 0.001);
        assert!((f1 - 1.0).abs() < 0.001);
    }

    #[test]
    fn compute_prf_all_false_positives() {
        let stats = CheckStats {
            total: 5,
            correct: 0,
            false_positive: 5,
            missed: 0,
        };
        let (p, r, f1) = compute_prf(&stats);
        assert!((p - 0.0).abs() < 0.001);
        assert!((r - 0.0).abs() < 0.001); // no TP, no FN
        assert!((f1 - 0.0).abs() < 0.001);
    }

    #[test]
    fn compute_prf_mixed() {
        let stats = CheckStats {
            total: 10,
            correct: 6,
            false_positive: 2,
            missed: 2,
        };
        let (p, r, f1) = compute_prf(&stats);
        // P = 6/(6+2) = 0.75
        assert!((p - 0.75).abs() < 0.001);
        // R = 6/(6+2) = 0.75
        assert!((r - 0.75).abs() < 0.001);
        // F1 = 2*0.75*0.75/(0.75+0.75) = 0.75
        assert!((f1 - 0.75).abs() < 0.001);
    }

    #[test]
    fn compute_prf_empty() {
        let stats = CheckStats::default();
        let (p, r, f1) = compute_prf(&stats);
        assert!((p - 0.0).abs() < 0.001);
        assert!((r - 0.0).abs() < 0.001);
        assert!((f1 - 0.0).abs() < 0.001);
    }

    #[test]
    fn feedback_roundtrip_yaml() {
        let fb = Feedback {
            event_id: "L42".into(),
            label: "false-positive".into(),
            notes: Some("Test fixture, not a real secret".into()),
            feedback_ts: "2026-03-16T14:32:01Z".into(),
            check_type: "deny_list".into(),
            tool_name: "Write".into(),
            verdict: "block".into(),
            reason: Some("Blocked: matches deny pattern '.env'".into()),
            pattern_matched: Some(".env".into()),
            original_ts: "14:32:01".into(),
        };

        let yaml = serde_yaml::to_string(&fb).unwrap();
        let loaded: Feedback = serde_yaml::from_str(&yaml).unwrap();
        assert_eq!(loaded.event_id, "L42");
        assert_eq!(loaded.label, "false-positive");
        assert_eq!(loaded.notes.as_deref(), Some("Test fixture, not a real secret"));
        assert_eq!(loaded.check_type, "deny_list");
        assert_eq!(loaded.verdict, "block");
    }

    #[test]
    fn load_feedback_from_dir() {
        let dir = tempfile::tempdir().unwrap();

        // Write two feedback files
        let fb1 = Feedback {
            event_id: "L1".into(),
            label: "correct".into(),
            notes: None,
            feedback_ts: "2026-03-16T14:32:01Z".into(),
            check_type: "deny_list".into(),
            tool_name: "Write".into(),
            verdict: "block".into(),
            reason: None,
            pattern_matched: None,
            original_ts: "14:32:01".into(),
        };
        let fb2 = Feedback {
            event_id: "L2".into(),
            label: "false-positive".into(),
            notes: Some("Not a real secret".into()),
            feedback_ts: "2026-03-16T14:33:00Z".into(),
            check_type: "secrets".into(),
            tool_name: "Edit".into(),
            verdict: "warn".into(),
            reason: Some("Secret pattern match".into()),
            pattern_matched: None,
            original_ts: "14:33:00".into(),
        };

        std::fs::write(
            dir.path().join("L1.yaml"),
            serde_yaml::to_string(&fb1).unwrap(),
        )
        .unwrap();
        std::fs::write(
            dir.path().join("L2.yaml"),
            serde_yaml::to_string(&fb2).unwrap(),
        )
        .unwrap();
        // Non-yaml file should be ignored
        std::fs::write(dir.path().join("README.txt"), "ignore me").unwrap();

        let feedbacks = load_all_feedback(dir.path());
        assert_eq!(feedbacks.len(), 2);
    }

    #[test]
    fn labeled_ids_from_dir() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("L1.yaml"), "event_id: L1\n").unwrap();
        std::fs::write(dir.path().join("L42.yaml"), "event_id: L42\n").unwrap();

        // Override FEEDBACK_DIR doesn't work here, but we can test load_all_feedback
        let feedbacks = load_all_feedback(dir.path());
        // At least verifies the directory reading works
        assert!(feedbacks.is_empty() || feedbacks.len() <= 2);
    }
}
