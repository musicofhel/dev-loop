pub mod dangerous_ops;
pub mod deny_list;
pub mod secrets;

use serde::{Deserialize, Serialize};
use std::time::Instant;

use crate::config::MergedConfig;
use dangerous_ops::{DangerousOps, Severity};
use deny_list::DenyList;
use secrets::SecretScanner;

/// Pre-compiled check engines, initialized once at daemon startup.
pub struct CheckEngine {
    pub deny_list: DenyList,
    pub dangerous_ops: DangerousOps,
    pub secrets: SecretScanner,
}

impl CheckEngine {
    /// Build with hardcoded defaults (no config overrides).
    pub fn new() -> Self {
        Self {
            deny_list: DenyList::default_patterns(),
            dangerous_ops: DangerousOps::default_patterns(),
            secrets: SecretScanner::default_patterns(),
        }
    }

    /// Build from merged config: applies extra/remove/allow overrides.
    pub fn from_config(config: &MergedConfig) -> Self {
        Self {
            deny_list: DenyList::from_config(&config.deny_list_extra, &config.deny_list_remove),
            dangerous_ops: DangerousOps::from_config(
                &config.dangerous_ops_extra,
                &config.dangerous_ops_allow,
            ),
            secrets: SecretScanner::from_config(
                &config.secrets_extra,
                &config.secrets_file_allowlist,
            ),
        }
    }

    /// Run the appropriate checks based on the tool call.
    pub fn check(&self, request: &CheckRequest) -> CheckResult {
        let start = Instant::now();

        match request.tool_name.as_str() {
            "Write" | "Edit" => {
                // PreToolUse: check deny list on file path
                // PostToolUse: check secrets in content
                if request.phase == CheckPhase::Pre {
                    self.check_deny_list(request, start)
                } else {
                    self.check_secrets(request, start)
                }
            }
            "Bash" => {
                // PreToolUse: check dangerous ops + commit detection
                self.check_bash(request, start)
            }
            _ => CheckResult {
                action: Action::Allow,
                reason: None,
                check_type: None,
                duration_us: start.elapsed().as_micros() as u64,
                is_commit: false,
            },
        }
    }

    fn check_deny_list(&self, request: &CheckRequest, start: Instant) -> CheckResult {
        let file_path = request
            .tool_input
            .get("file_path")
            .and_then(|v| v.as_str())
            .unwrap_or("");

        if let Some(deny_match) = self.deny_list.check(file_path) {
            CheckResult {
                action: Action::Block,
                reason: Some(format!(
                    "Blocked: write to '{}' matches deny pattern '{}'",
                    file_path, deny_match.pattern
                )),
                check_type: Some("deny_list".to_string()),
                duration_us: start.elapsed().as_micros() as u64,
                is_commit: false,
            }
        } else {
            CheckResult {
                action: Action::Allow,
                reason: None,
                check_type: Some("deny_list".to_string()),
                duration_us: start.elapsed().as_micros() as u64,
                is_commit: false,
            }
        }
    }

    fn check_secrets(&self, request: &CheckRequest, start: Instant) -> CheckResult {
        let content = request
            .tool_input
            .get("content")
            .and_then(|v| v.as_str())
            // Also check new_string for Edit tool
            .or_else(|| request.tool_input.get("new_string").and_then(|v| v.as_str()))
            .unwrap_or("");

        let matches = self.secrets.check(content);
        if matches.is_empty() {
            CheckResult {
                action: Action::Allow,
                reason: None,
                check_type: Some("secrets".to_string()),
                duration_us: start.elapsed().as_micros() as u64,
                is_commit: false,
            }
        } else {
            let descriptions: Vec<String> = matches
                .iter()
                .map(|m| format!("line {}: {} ({})", m.line, m.description, m.preview))
                .collect();
            CheckResult {
                action: Action::Warn,
                reason: Some(format!(
                    "Possible secrets detected: {}",
                    descriptions.join("; ")
                )),
                check_type: Some("secrets".to_string()),
                duration_us: start.elapsed().as_micros() as u64,
                is_commit: false,
            }
        }
    }

    fn check_bash(&self, request: &CheckRequest, start: Instant) -> CheckResult {
        let command = request
            .tool_input
            .get("command")
            .and_then(|v| v.as_str())
            .unwrap_or("");

        // Check for git commit (Tier 2 interception)
        let is_commit = DangerousOps::is_git_commit(command);

        // Check for dangerous operations
        let matches = self.dangerous_ops.check(command);
        if matches.is_empty() {
            CheckResult {
                action: Action::Allow,
                reason: None,
                check_type: Some("dangerous_ops".to_string()),
                duration_us: start.elapsed().as_micros() as u64,
                is_commit,
            }
        } else {
            // Use the highest severity match
            let has_block = matches.iter().any(|m| m.severity == Severity::Block);
            let descriptions: Vec<String> = matches
                .iter()
                .map(|m| format!("{} (matched: '{}')", m.description, m.matched_text))
                .collect();

            CheckResult {
                action: if has_block {
                    Action::Block
                } else {
                    Action::Warn
                },
                reason: Some(descriptions.join("; ")),
                check_type: Some("dangerous_ops".to_string()),
                duration_us: start.elapsed().as_micros() as u64,
                is_commit,
            }
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct CheckRequest {
    pub tool_name: String,
    #[serde(default)]
    pub tool_input: serde_json::Value,
    #[serde(default)]
    pub phase: CheckPhase,
    #[serde(default)]
    pub session_id: Option<String>,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum CheckPhase {
    #[default]
    Pre,
    Post,
}

#[derive(Debug, Clone, Serialize)]
pub struct CheckResult {
    pub action: Action,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub check_type: Option<String>,
    pub duration_us: u64,
    /// True if this is a git commit command (triggers Tier 2)
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub is_commit: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Action {
    Allow,
    Block,
    Warn,
}
