/// Session transcript JSONL parser.
///
/// Reads Claude Code's conversation JSONL files to extract:
/// - Token counts (for context estimation)
/// - Modified/created files (from tool calls)
/// - Turn count
///
/// Also implements the flush sentinel pattern (from Entire CLI research):
/// poll the last 4KB of the transcript for a completion marker before parsing.
use std::io::{BufRead, Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};

/// Summary extracted from a transcript.
#[derive(Debug, Default)]
pub struct TranscriptSummary {
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub turns: u32,
    pub files_modified: Vec<String>,
    pub files_created: Vec<String>,
    pub goal: Option<String>,
    pub now: Option<String>,
    pub test_plan: Option<String>,
}

impl TranscriptSummary {
    pub fn total_tokens(&self) -> u64 {
        self.input_tokens + self.output_tokens
    }

    pub fn context_pct_with_limit(&self, limit: u64) -> f32 {
        if limit == 0 {
            return 0.0;
        }
        self.total_tokens() as f32 / limit as f32
    }
}

/// Discover the transcript path for a session.
///
/// Claude Code stores conversations as JSONL in:
///   `~/.claude/projects/<project-hash>/<conversation-id>.jsonl`
///
/// The session_id from hooks may match the conversation-id.
pub fn find_transcript(session_id: &str) -> Option<PathBuf> {
    let home = dirs::home_dir()?;
    let claude_dir = home.join(".claude").join("projects");

    if !claude_dir.exists() {
        return None;
    }

    for entry in std::fs::read_dir(&claude_dir).ok()?.flatten() {
        if !entry.file_type().ok().map(|t| t.is_dir()).unwrap_or(false) {
            continue;
        }

        // Check both flat layout and conversations/ subdirectory
        let candidates = [
            entry.path().join(format!("{session_id}.jsonl")),
            entry
                .path()
                .join("conversations")
                .join(format!("{session_id}.jsonl")),
        ];

        for path in &candidates {
            if path.exists() {
                return Some(path.clone());
            }
        }
    }

    None
}

/// Wait for transcript to flush (poll for completion marker).
///
/// Before parsing, polls the last 4KB of the transcript file for a
/// `"type":"result"` or `"stop_reason"` marker. Ensures async writes
/// complete before parsing. Returns true if marker found, false on timeout.
pub fn wait_for_flush(path: &Path, timeout: std::time::Duration) -> bool {
    let start = std::time::Instant::now();
    while start.elapsed() < timeout {
        if let Ok(tail) = read_last_4kb(path) {
            if tail.contains("\"type\":\"result\"") || tail.contains("\"stop_reason\"") {
                return true;
            }
        }
        std::thread::sleep(std::time::Duration::from_millis(100));
    }
    false // parse anyway, best-effort
}

/// Read the last 4KB of a file.
fn read_last_4kb(path: &Path) -> Result<String, std::io::Error> {
    let mut file = std::fs::File::open(path)?;
    let len = file.metadata()?.len();
    let offset = if len > 4096 { len - 4096 } else { 0 };
    file.seek(SeekFrom::Start(offset))?;
    let mut buf = String::new();
    std::io::BufReader::new(file).read_to_string(&mut buf)?;
    Ok(buf)
}

/// Estimate token count from transcript file size (fast, no parsing).
///
/// Heuristic: JSONL has ~40% overhead (JSON structure, metadata).
/// At ~4 chars/token for mixed content: size * 0.6 / 4 ≈ size / 7.
pub fn estimate_tokens_from_size(path: &Path) -> Option<u64> {
    let metadata = std::fs::metadata(path).ok()?;
    Some(metadata.len() / 7)
}

/// Check if estimated context usage exceeds a threshold.
///
/// Returns (estimated_pct, exceeds_threshold).
pub fn check_context_threshold(path: &Path, threshold_pct: f32, context_limit: u64) -> (f32, bool) {
    if context_limit == 0 {
        return (0.0, false);
    }
    match estimate_tokens_from_size(path) {
        Some(tokens) => {
            let pct = tokens as f32 / context_limit as f32;
            (pct, pct >= threshold_pct)
        }
        None => (0.0, false),
    }
}

/// Parse the transcript JSONL for detailed information.
///
/// Reads the full file and extracts token counts, file operations,
/// and turn count. Use `estimate_tokens_from_size` for the fast path.
pub fn parse_transcript(path: &Path) -> TranscriptSummary {
    let mut summary = TranscriptSummary::default();

    let file = match std::fs::File::open(path) {
        Ok(f) => f,
        Err(_) => return summary,
    };

    let reader = std::io::BufReader::new(file);
    let mut seen_files: std::collections::HashSet<String> = std::collections::HashSet::new();

    let mut first_human_message: Option<String> = None;

    for line in reader.lines().flatten() {
        let Ok(data) = serde_json::from_str::<serde_json::Value>(&line) else {
            continue;
        };

        // Extract token usage from any entry that has it
        if let Some(usage) = data.get("usage") {
            if let Some(input) = usage.get("input_tokens").and_then(|v| v.as_u64()) {
                summary.input_tokens += input;
            }
            if let Some(output) = usage.get("output_tokens").and_then(|v| v.as_u64()) {
                summary.output_tokens += output;
            }
        }

        // Count assistant turns
        let msg_type = data.get("type").and_then(|v| v.as_str()).unwrap_or("");
        if msg_type == "assistant" {
            summary.turns += 1;
        }

        // Capture first human message as fallback goal
        if msg_type == "human" && first_human_message.is_none() {
            if let Some(text) = extract_human_text(&data) {
                first_human_message = Some(text);
            }
        }

        // Extract file operations from tool_use content blocks
        extract_file_ops(&data, &mut summary, &mut seen_files);
    }

    // Best-effort goal extraction: first human message, truncated
    if summary.goal.is_none() {
        if let Some(ref msg) = first_human_message {
            let truncated = if msg.len() > 200 {
                format!("{}...", &msg[..197])
            } else {
                msg.clone()
            };
            summary.goal = Some(truncated);
        }
    }

    summary
}

/// Extract text content from a human message entry.
fn extract_human_text(data: &serde_json::Value) -> Option<String> {
    // Try direct "message" string
    if let Some(msg) = data.get("message").and_then(|v| v.as_str()) {
        if !msg.is_empty() {
            return Some(msg.to_string());
        }
    }
    // Try content array with text blocks
    let content = data
        .get("message")
        .and_then(|v| v.get("content"))
        .and_then(|v| v.as_array())
        .or_else(|| data.get("content").and_then(|v| v.as_array()));
    if let Some(blocks) = content {
        for block in blocks {
            if block.get("type").and_then(|v| v.as_str()) == Some("text") {
                if let Some(text) = block.get("text").and_then(|v| v.as_str()) {
                    if !text.is_empty() {
                        return Some(text.to_string());
                    }
                }
            }
        }
    }
    None
}

/// Extract file operations from a JSONL entry's tool_use blocks.
fn extract_file_ops(
    data: &serde_json::Value,
    summary: &mut TranscriptSummary,
    seen: &mut std::collections::HashSet<String>,
) {
    // Look for content array in message
    let content = data
        .get("message")
        .and_then(|v| v.get("content"))
        .and_then(|v| v.as_array())
        .or_else(|| data.get("content").and_then(|v| v.as_array()));

    let Some(blocks) = content else {
        return;
    };

    for block in blocks {
        if block.get("type").and_then(|v| v.as_str()) != Some("tool_use") {
            continue;
        }

        let tool_name = block.get("name").and_then(|v| v.as_str()).unwrap_or("");
        let input = block.get("input");

        if let Some(file_path) = input
            .and_then(|i| i.get("file_path"))
            .and_then(|v| v.as_str())
        {
            if seen.insert(file_path.to_string()) {
                match tool_name {
                    "Write" => summary.files_created.push(file_path.to_string()),
                    "Edit" => summary.files_modified.push(file_path.to_string()),
                    _ => {}
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn estimate_tokens_from_file_size() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test.jsonl");
        // Write ~7000 bytes → should estimate ~1000 tokens
        let data = "x".repeat(7000);
        std::fs::write(&path, data).unwrap();

        let tokens = estimate_tokens_from_size(&path).unwrap();
        assert_eq!(tokens, 1000);
    }

    #[test]
    fn check_context_threshold_under() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("small.jsonl");
        // ~1000 bytes → ~143 tokens → 0.07% of 200K
        std::fs::write(&path, "x".repeat(1000)).unwrap();

        let (pct, exceeds) = check_context_threshold(&path, 0.85, 200_000);
        assert!(!exceeds);
        assert!(pct < 0.01);
    }

    #[test]
    fn check_context_threshold_over() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("large.jsonl");
        // Need file size such that size/7 > 200000 * 0.85 = 170000
        // size > 1_190_000
        std::fs::write(&path, "x".repeat(1_400_000)).unwrap();

        let (pct, exceeds) = check_context_threshold(&path, 0.85, 200_000);
        assert!(exceeds);
        assert!(pct > 0.85);
    }

    #[test]
    fn parse_transcript_with_usage() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("transcript.jsonl");
        let mut file = std::fs::File::create(&path).unwrap();

        // Write some JSONL entries
        writeln!(
            file,
            r#"{{"type":"assistant","usage":{{"input_tokens":1000,"output_tokens":500}}}}"#
        )
        .unwrap();
        writeln!(
            file,
            r#"{{"type":"assistant","usage":{{"input_tokens":2000,"output_tokens":800}}}}"#
        )
        .unwrap();
        writeln!(file, r#"{{"type":"human","message":"hello"}}"#).unwrap();

        let summary = parse_transcript(&path);
        assert_eq!(summary.input_tokens, 3000);
        assert_eq!(summary.output_tokens, 1300);
        assert_eq!(summary.turns, 2);
        assert_eq!(summary.total_tokens(), 4300);
    }

    #[test]
    fn parse_transcript_with_tool_use() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("transcript.jsonl");
        let mut file = std::fs::File::create(&path).unwrap();

        writeln!(
            file,
            r#"{{"type":"assistant","message":{{"content":[{{"type":"tool_use","name":"Edit","input":{{"file_path":"src/main.rs"}}}},{{"type":"tool_use","name":"Write","input":{{"file_path":"src/new.rs"}}}}]}}}}"#
        )
        .unwrap();
        writeln!(
            file,
            r#"{{"type":"assistant","message":{{"content":[{{"type":"tool_use","name":"Edit","input":{{"file_path":"src/main.rs"}}}}]}}}}"#
        )
        .unwrap();

        let summary = parse_transcript(&path);
        assert_eq!(summary.files_modified, vec!["src/main.rs"]);
        assert_eq!(summary.files_created, vec!["src/new.rs"]);
        // main.rs should appear only once (deduped)
    }

    #[test]
    fn parse_empty_transcript() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("empty.jsonl");
        std::fs::write(&path, "").unwrap();

        let summary = parse_transcript(&path);
        assert_eq!(summary.input_tokens, 0);
        assert_eq!(summary.turns, 0);
        assert!(summary.files_modified.is_empty());
    }

    #[test]
    fn parse_transcript_with_malformed_lines() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("bad.jsonl");
        let mut file = std::fs::File::create(&path).unwrap();

        writeln!(file, "not json at all").unwrap();
        writeln!(file, "{{broken json").unwrap();
        writeln!(
            file,
            r#"{{"type":"assistant","usage":{{"input_tokens":500,"output_tokens":100}}}}"#
        )
        .unwrap();

        let summary = parse_transcript(&path);
        assert_eq!(summary.input_tokens, 500);
        assert_eq!(summary.output_tokens, 100);
        assert_eq!(summary.turns, 1);
    }

    #[test]
    fn wait_for_flush_finds_marker() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("flushed.jsonl");
        let mut file = std::fs::File::create(&path).unwrap();
        writeln!(
            file,
            r#"{{"type":"result","stop_reason":"end_turn"}}"#
        )
        .unwrap();

        let found = wait_for_flush(&path, std::time::Duration::from_millis(100));
        assert!(found);
    }

    #[test]
    fn wait_for_flush_times_out() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("noflushed.jsonl");
        std::fs::write(&path, r#"{"type":"human","message":"hi"}"#).unwrap();

        let found = wait_for_flush(&path, std::time::Duration::from_millis(200));
        assert!(!found);
    }

    #[test]
    fn context_pct_calculation() {
        let summary = TranscriptSummary {
            input_tokens: 100_000,
            output_tokens: 50_000,
            turns: 10,
            ..Default::default()
        };
        assert_eq!(summary.total_tokens(), 150_000);
        assert!((summary.context_pct_with_limit(200_000) - 0.75).abs() < 0.01);
        // Custom limit
        assert!((summary.context_pct_with_limit(300_000) - 0.5).abs() < 0.01);
    }

    #[test]
    fn parse_transcript_extracts_goal() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("transcript.jsonl");
        let mut file = std::fs::File::create(&path).unwrap();

        writeln!(
            file,
            r#"{{"type":"human","message":"Add authentication to the API"}}"#
        )
        .unwrap();
        writeln!(
            file,
            r#"{{"type":"assistant","usage":{{"input_tokens":100,"output_tokens":50}}}}"#
        )
        .unwrap();

        let summary = parse_transcript(&path);
        assert_eq!(
            summary.goal.as_deref(),
            Some("Add authentication to the API")
        );
    }

    #[test]
    fn parse_transcript_truncates_long_goal() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("transcript.jsonl");
        let mut file = std::fs::File::create(&path).unwrap();

        let long_msg = "x".repeat(300);
        writeln!(file, r#"{{"type":"human","message":"{long_msg}"}}"#).unwrap();

        let summary = parse_transcript(&path);
        let goal = summary.goal.unwrap();
        assert!(goal.len() <= 200);
        assert!(goal.ends_with("..."));
    }

    #[test]
    fn check_context_threshold_custom_limit() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("custom.jsonl");
        // 700 bytes → 100 tokens. With limit 100 → 100% → exceeds 0.85
        std::fs::write(&path, "x".repeat(700)).unwrap();

        let (pct, exceeds) = check_context_threshold(&path, 0.85, 100);
        assert!(exceeds);
        assert!(pct >= 0.85);

        // Same file, limit 200 → 50% → does not exceed 0.85
        let (pct2, exceeds2) = check_context_threshold(&path, 0.85, 200);
        assert!(!exceeds2);
        assert!(pct2 < 0.85);
    }

    #[test]
    fn find_transcript_missing() {
        // Should not find anything for a random session id
        let result = find_transcript("nonexistent-session-id-99999");
        assert!(result.is_none());
    }
}
