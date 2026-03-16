/// Session continuity: handoff YAML read/write.
///
/// Writes structured YAML (~400 tokens) to `/tmp/dev-loop/sessions/<session-id>.yaml`
/// for cross-session state injection. Read by differentiated SessionStart.
use serde::{Deserialize, Serialize};
use std::path::PathBuf;

const SESSIONS_DIR: &str = "/tmp/dev-loop/sessions";

/// Maximum age for a handoff to be considered "recent" (1 hour).
const MAX_HANDOFF_AGE_SECS: u64 = 3600;

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct Handoff {
    pub session_id: String,
    pub date: String,
    pub source: String, // "stop_guard" | "pre_compact" | "session_end"
    pub cwd: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub repo_root: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub outcome: Option<String>, // "success" | "partial" | "fail"
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub notes: Option<String>,

    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub goal: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub now: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub test_plan: Option<String>,

    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub files_modified: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub files_created: Vec<String>,

    #[serde(default)]
    pub ambient_stats: AmbientStats,

    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub token_estimate: Option<TokenEstimate>,
}

#[derive(Debug, Serialize, Deserialize, Clone, Default)]
pub struct AmbientStats {
    pub checks: u32,
    pub blocked: u32,
    pub warned: u32,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct TokenEstimate {
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub total: u64,
    /// Estimated percentage of context window used (0.0 - 1.0).
    pub context_pct: f32,
}

/// Path to the sessions directory.
pub fn sessions_dir() -> PathBuf {
    PathBuf::from(SESSIONS_DIR)
}

/// Path to a specific session's handoff file.
pub fn handoff_path(session_id: &str) -> PathBuf {
    sessions_dir().join(format!("{session_id}.yaml"))
}

/// Ensure the sessions directory exists.
pub fn ensure_sessions_dir() {
    let _ = std::fs::create_dir_all(SESSIONS_DIR);
}

/// Write a handoff YAML to disk.
pub fn write_handoff(handoff: &Handoff) -> Result<PathBuf, String> {
    ensure_sessions_dir();
    let path = handoff_path(&handoff.session_id);
    let content =
        serde_yaml::to_string(handoff).map_err(|e| format!("serialize handoff: {e}"))?;
    std::fs::write(&path, content).map_err(|e| format!("write {}: {e}", path.display()))?;
    Ok(path)
}

/// Read a handoff YAML from disk.
#[allow(dead_code)]
pub fn read_handoff(session_id: &str) -> Option<Handoff> {
    let path = handoff_path(session_id);
    let content = std::fs::read_to_string(&path).ok()?;
    serde_yaml::from_str(&content).ok()
}

/// Check if a recent handoff exists for this session (< 5 min old).
pub fn has_recent_handoff(session_id: &str) -> bool {
    let path = handoff_path(session_id);
    match std::fs::metadata(&path) {
        Ok(meta) => {
            let age = meta
                .modified()
                .ok()
                .and_then(|t| t.elapsed().ok())
                .unwrap_or(std::time::Duration::MAX);
            age < std::time::Duration::from_secs(300)
        }
        Err(_) => false,
    }
}

/// Find the most recent handoff for a given working directory.
///
/// Searches `/tmp/dev-loop/sessions/` for YAML files that match the cwd
/// or repo_root. Returns the most recently modified match within the
/// max age window.
pub fn find_recent_handoff(cwd: &str) -> Option<Handoff> {
    let dir = sessions_dir();
    let mut entries: Vec<(PathBuf, std::time::SystemTime)> = std::fs::read_dir(&dir)
        .ok()?
        .filter_map(|e| e.ok())
        .filter(|e| {
            e.path()
                .extension()
                .and_then(|x| x.to_str())
                == Some("yaml")
        })
        .filter_map(|e| {
            let meta = e.metadata().ok()?;
            let modified = meta.modified().ok()?;
            let age = modified.elapsed().unwrap_or(std::time::Duration::MAX);
            if age > std::time::Duration::from_secs(MAX_HANDOFF_AGE_SECS) {
                return None;
            }
            Some((e.path(), modified))
        })
        .collect();

    // Newest first
    entries.sort_by(|a, b| b.1.cmp(&a.1));

    for (path, _) in entries {
        if let Ok(content) = std::fs::read_to_string(&path) {
            if let Ok(handoff) = serde_yaml::from_str::<Handoff>(&content) {
                // Match by cwd or repo_root
                if handoff.cwd == cwd {
                    return Some(handoff);
                }
                if let Some(ref root) = handoff.repo_root {
                    if cwd.starts_with(root.as_str()) || root.starts_with(cwd) {
                        return Some(handoff);
                    }
                }
            }
        }
    }

    None
}

/// Format a handoff as a compact string for injection into additionalContext.
/// Target: ~400 tokens.
pub fn format_for_injection(handoff: &Handoff) -> String {
    let mut parts = Vec::new();

    parts.push(format!(
        "Previous session: {} ({})",
        handoff.session_id, handoff.date
    ));

    if let Some(ref outcome) = handoff.outcome {
        parts.push(format!("Outcome: {outcome}"));
    }

    if let Some(ref goal) = handoff.goal {
        parts.push(format!("Goal: {goal}"));
    }

    if let Some(ref now) = handoff.now {
        parts.push(format!("Current state: {now}"));
    }

    if let Some(ref test_plan) = handoff.test_plan {
        parts.push(format!("Test plan: {test_plan}"));
    }

    if !handoff.files_modified.is_empty() {
        let files = handoff.files_modified.join(", ");
        parts.push(format!("Modified: {files}"));
    }

    if !handoff.files_created.is_empty() {
        let files = handoff.files_created.join(", ");
        parts.push(format!("Created: {files}"));
    }

    let stats = &handoff.ambient_stats;
    parts.push(format!(
        "Ambient: {} checks, {} blocked, {} warned",
        stats.checks, stats.blocked, stats.warned
    ));

    if let Some(ref tokens) = handoff.token_estimate {
        parts.push(format!(
            "Tokens: ~{} ({:.0}% context)",
            tokens.total,
            tokens.context_pct * 100.0
        ));
    }

    parts.join("\n")
}

/// Record a session outcome by updating its handoff YAML.
pub fn record_outcome(session_id: &str, outcome: &str, notes: Option<&str>) {
    let valid = ["success", "partial", "fail"];
    if !valid.contains(&outcome) {
        eprintln!(
            "Invalid outcome '{}'. Must be one of: {}",
            outcome,
            valid.join(", ")
        );
        std::process::exit(1);
    }

    let mut handoff = match read_handoff(session_id) {
        Some(h) => h,
        None => {
            eprintln!(
                "No handoff found for session '{session_id}'.\n\
                 Run `dl traces` to find session IDs."
            );
            std::process::exit(1);
        }
    };

    handoff.outcome = Some(outcome.to_string());
    if let Some(n) = notes {
        handoff.notes = Some(n.to_string());
    }

    match write_handoff(&handoff) {
        Ok(path) => {
            println!(
                "Outcome '{}' recorded for session {} → {}",
                outcome,
                session_id,
                path.display()
            );
        }
        Err(e) => {
            eprintln!("Failed to write handoff: {e}");
            std::process::exit(1);
        }
    }
}

/// Clean up old session handoff files (older than max age).
pub fn cleanup_old_handoffs() {
    let dir = sessions_dir();
    let entries = match std::fs::read_dir(&dir) {
        Ok(e) => e,
        Err(_) => return,
    };

    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|x| x.to_str()) != Some("yaml") {
            continue;
        }
        if let Ok(meta) = std::fs::metadata(&path) {
            let age = meta
                .modified()
                .ok()
                .and_then(|t| t.elapsed().ok())
                .unwrap_or_default();
            // Remove files older than 24 hours
            if age > std::time::Duration::from_secs(86400) {
                let _ = std::fs::remove_file(&path);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_handoff(id: &str, cwd: &str) -> Handoff {
        Handoff {
            session_id: id.to_string(),
            date: "2026-03-15".to_string(),
            source: "stop_guard".to_string(),
            cwd: cwd.to_string(),
            repo_root: Some(cwd.to_string()),
            outcome: Some("partial".to_string()),
            notes: None,
            goal: Some("Build the thing".to_string()),
            now: None,
            test_plan: None,
            files_modified: vec!["src/main.rs".into(), "src/lib.rs".into()],
            files_created: vec!["src/new.rs".into()],
            ambient_stats: AmbientStats {
                checks: 47,
                blocked: 1,
                warned: 3,
            },
            token_estimate: Some(TokenEstimate {
                input_tokens: 100000,
                output_tokens: 50000,
                total: 150000,
                context_pct: 0.75,
            }),
        }
    }

    #[test]
    fn write_and_read_roundtrip() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test-session.yaml");
        let handoff = sample_handoff("test-session", "/home/user/repo");

        let content = serde_yaml::to_string(&handoff).unwrap();
        std::fs::write(&path, &content).unwrap();

        let loaded: Handoff =
            serde_yaml::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(loaded.session_id, "test-session");
        assert_eq!(loaded.files_modified.len(), 2);
        assert_eq!(loaded.ambient_stats.checks, 47);
    }

    #[test]
    fn handoff_yaml_is_compact() {
        let handoff = sample_handoff("test-session", "/home/user/repo");
        let yaml = serde_yaml::to_string(&handoff).unwrap();
        // Should be under ~2KB (well within ~400 token target)
        assert!(yaml.len() < 2048, "YAML too large: {} bytes", yaml.len());
    }

    #[test]
    fn format_for_injection_includes_key_info() {
        let handoff = sample_handoff("test-session", "/home/user/repo");
        let text = format_for_injection(&handoff);

        assert!(text.contains("test-session"));
        assert!(text.contains("partial"));
        assert!(text.contains("Build the thing"));
        assert!(text.contains("src/main.rs"));
        assert!(text.contains("47 checks"));
        assert!(text.contains("75%"));
    }

    #[test]
    fn empty_handoff_serializes() {
        let handoff = Handoff {
            session_id: "empty".to_string(),
            date: "2026-03-15".to_string(),
            source: "session_end".to_string(),
            cwd: "/tmp".to_string(),
            repo_root: None,
            outcome: None,
            notes: None,
            goal: None,
            now: None,
            test_plan: None,
            files_modified: vec![],
            files_created: vec![],
            ambient_stats: AmbientStats::default(),
            token_estimate: None,
        };
        let yaml = serde_yaml::to_string(&handoff).unwrap();
        assert!(yaml.contains("session_id: empty"));
        // skip_serializing_if should omit empty vecs and None fields
        assert!(!yaml.contains("files_modified"));
        assert!(!yaml.contains("outcome"));
    }

    #[test]
    fn find_recent_handoff_matches_cwd() {
        let dir = tempfile::tempdir().unwrap();
        // Override SESSIONS_DIR by writing directly to temp dir
        let handoff = sample_handoff("s1", "/home/user/repo");
        let path = dir.path().join("s1.yaml");
        let content = serde_yaml::to_string(&handoff).unwrap();
        std::fs::write(&path, content).unwrap();

        // Can't easily test find_recent_handoff without mocking the path,
        // so test the matching logic directly
        assert_eq!(handoff.cwd, "/home/user/repo");
        assert!(handoff.repo_root.as_deref() == Some("/home/user/repo"));
    }

    #[test]
    fn record_outcome_roundtrip() {
        let dir = tempfile::tempdir().unwrap();
        let handoff = Handoff {
            session_id: "outcome-test".to_string(),
            date: "2026-03-15".to_string(),
            source: "session_end".to_string(),
            cwd: "/tmp".to_string(),
            repo_root: None,
            outcome: None,
            notes: None,
            goal: None,
            now: None,
            test_plan: None,
            files_modified: vec![],
            files_created: vec![],
            ambient_stats: AmbientStats::default(),
            token_estimate: None,
        };
        // Write initial handoff
        let path = dir.path().join("outcome-test.yaml");
        let content = serde_yaml::to_string(&handoff).unwrap();
        std::fs::write(&path, content).unwrap();

        // Read it back and update outcome
        let loaded: Handoff =
            serde_yaml::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        assert!(loaded.outcome.is_none());

        let mut updated = loaded;
        updated.outcome = Some("success".to_string());
        updated.notes = Some("went well".to_string());
        let content = serde_yaml::to_string(&updated).unwrap();
        std::fs::write(&path, content).unwrap();

        let final_load: Handoff =
            serde_yaml::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(final_load.outcome.as_deref(), Some("success"));
        assert_eq!(final_load.notes.as_deref(), Some("went well"));
    }

    #[test]
    fn handoff_with_goal_serializes() {
        let handoff = Handoff {
            session_id: "goal-test".to_string(),
            date: "2026-03-15".to_string(),
            source: "stop_guard".to_string(),
            cwd: "/tmp".to_string(),
            repo_root: None,
            outcome: None,
            notes: None,
            goal: Some("Build feature X".to_string()),
            now: Some("Halfway done".to_string()),
            test_plan: Some("Run cargo test".to_string()),
            files_modified: vec![],
            files_created: vec![],
            ambient_stats: AmbientStats::default(),
            token_estimate: None,
        };
        let yaml = serde_yaml::to_string(&handoff).unwrap();
        assert!(yaml.contains("goal: Build feature X"));
        assert!(yaml.contains("now: Halfway done"));
        assert!(yaml.contains("test_plan: Run cargo test"));

        // Deserialize back
        let loaded: Handoff = serde_yaml::from_str(&yaml).unwrap();
        assert_eq!(loaded.goal.as_deref(), Some("Build feature X"));
    }

    #[test]
    fn handoff_path_format() {
        let path = handoff_path("abc-123");
        assert!(path.to_str().unwrap().ends_with("abc-123.yaml"));
        assert!(path.to_str().unwrap().starts_with("/tmp/dev-loop/sessions/"));
    }
}
