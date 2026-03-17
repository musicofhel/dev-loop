/// Regex-based dangerous command scanner for Bash tool calls.
///
/// Scans the command string for patterns that indicate destructive or
/// risky operations (rm -rf, force push, DROP TABLE, etc.).
/// Returns Warn severity — these aren't blocked, but flagged.
use regex::Regex;

/// Number of built-in dangerous operation patterns.
pub const BUILTIN_DANGEROUS_PATTERNS: usize = 25;

pub struct DangerousOps {
    patterns: Vec<DangerousPattern>,
    /// Commands matching these substrings bypass dangerous ops checks.
    allow_patterns: Vec<String>,
}

pub struct DangerousPattern {
    regex: Regex,
    description: String,
    severity: Severity,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Severity {
    /// Block the operation entirely
    Block,
    /// Warn via additionalContext but allow
    Warn,
}

#[derive(Debug, Clone)]
pub struct DangerousMatch {
    pub description: String,
    pub severity: Severity,
    pub matched_text: String,
}

impl DangerousOps {
    pub fn default_patterns() -> Self {
        let patterns = vec![
            // Filesystem destruction
            danger(r"rm\s+(-[a-zA-Z]*)?r[a-zA-Z]*f", "rm -rf (recursive force delete)", Severity::Block),
            danger(r"rm\s+(-[a-zA-Z]*)?f[a-zA-Z]*r", "rm -fr (recursive force delete)", Severity::Block),
            danger(r">\s*/dev/sd[a-z]", "write to block device", Severity::Block),
            danger(r"mkfs\.", "format filesystem", Severity::Block),
            danger(r"dd\s+.*of=/dev/", "dd to device", Severity::Block),

            // Git dangerous ops
            danger(r"git\s+push\s+.*--force", "git force push", Severity::Warn),
            danger(r"git\s+push\s+.*-f\b", "git force push (-f)", Severity::Warn),
            danger(r"git\s+reset\s+--hard", "git reset --hard", Severity::Warn),
            danger(r"git\s+clean\s+.*-f", "git clean -f", Severity::Warn),
            danger(r"git\s+checkout\s+--\s+\.", "git checkout -- . (discard all changes)", Severity::Warn),
            danger(r"git\s+branch\s+.*-D\b", "git branch -D (force delete)", Severity::Warn),

            // Database destructive ops
            danger(r"(?i)\bDROP\s+(TABLE|DATABASE|INDEX|VIEW|SCHEMA)\b", "DROP statement", Severity::Block),
            danger(r"(?i)\bDELETE\s+FROM\b", "DELETE FROM statement", Severity::Warn),
            danger(r"(?i)\bTRUNCATE\s+(TABLE\s+)?\w+", "TRUNCATE statement", Severity::Block),
            danger(r"(?i)\bALTER\s+TABLE\s+\w+\s+DROP\b", "ALTER TABLE DROP", Severity::Warn),

            // Process/system ops
            danger(r"kill\s+-9", "kill -9 (SIGKILL)", Severity::Warn),
            danger(r"pkill\s+-9", "pkill -9 (SIGKILL)", Severity::Warn),
            danger(r"sudo\s+rm\b", "sudo rm", Severity::Block),
            danger(r"chmod\s+777\b", "chmod 777", Severity::Warn),
            danger(r"chmod\s+-R\s", "chmod -R (recursive permission change)", Severity::Warn),

            // Package/dependency ops
            danger(r"npm\s+publish\b", "npm publish", Severity::Warn),
            danger(r"cargo\s+publish\b", "cargo publish", Severity::Warn),
            danger(r"pip\s+install\s+--break-system-packages", "pip break-system-packages", Severity::Warn),

            // CI/CD and config
            danger(r"(?i)curl\s+.*\|\s*(?:sudo\s+)?(?:ba)?sh", "curl | sh (pipe to shell)", Severity::Warn),
            danger(r"wget\s+.*\|\s*(?:sudo\s+)?(?:ba)?sh", "wget | sh (pipe to shell)", Severity::Warn),
        ];

        Self {
            patterns,
            allow_patterns: Vec::new(),
        }
    }

    /// Build with config overrides: extra patterns added as Warn, allow patterns bypass checks.
    pub fn from_config(extra: &[String], allow: &[String]) -> Self {
        let mut ops = Self::default_patterns();
        for pattern_str in extra {
            if let Ok(regex) = Regex::new(pattern_str) {
                ops.patterns.push(DangerousPattern {
                    regex,
                    description: format!("Custom: {}", pattern_str),
                    severity: Severity::Warn,
                });
            }
        }
        ops.allow_patterns = allow.to_vec();
        ops
    }

    /// Scan a command string for dangerous patterns.
    /// Returns all matches found (there can be multiple).
    /// Commands matching an allow_pattern bypass all checks.
    pub fn check(&self, command: &str) -> Vec<DangerousMatch> {
        // Check allow patterns first — exact substring match
        for allow in &self.allow_patterns {
            if command.contains(allow.as_str()) {
                return Vec::new();
            }
        }

        let mut matches = Vec::new();
        for pat in &self.patterns {
            if let Some(m) = pat.regex.find(command) {
                matches.push(DangerousMatch {
                    description: pat.description.clone(),
                    severity: pat.severity,
                    matched_text: m.as_str().to_string(),
                });
            }
        }
        matches
    }

    /// Check if the command is a git commit (for Tier 2 interception).
    pub fn is_git_commit(command: &str) -> bool {
        // Match: git commit, git commit -m, git commit --amend, etc.
        // But NOT: git commit --help, echo "git commit"
        lazy_static_regex(r"^\s*git\s+commit\b").is_match(command)
    }
}

fn danger(pattern: &str, description: &str, severity: Severity) -> DangerousPattern {
    DangerousPattern {
        regex: Regex::new(pattern).expect("invalid dangerous ops regex"),
        description: description.to_string(),
        severity,
    }
}

fn lazy_static_regex(pattern: &str) -> Regex {
    Regex::new(pattern).expect("invalid regex")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ops() -> DangerousOps {
        DangerousOps::default_patterns()
    }

    #[test]
    fn catches_rm_rf() {
        let matches = ops().check("rm -rf /");
        assert_eq!(matches.len(), 1);
        assert_eq!(matches[0].severity, Severity::Block);

        let matches = ops().check("rm -fr /tmp/stuff");
        assert_eq!(matches.len(), 1);
    }

    #[test]
    fn catches_force_push() {
        let matches = ops().check("git push origin main --force");
        assert_eq!(matches.len(), 1);
        assert_eq!(matches[0].severity, Severity::Warn);

        let matches = ops().check("git push -f origin main");
        assert_eq!(matches.len(), 1);
    }

    #[test]
    fn catches_git_reset_hard() {
        let matches = ops().check("git reset --hard HEAD~1");
        assert_eq!(matches.len(), 1);
        assert_eq!(matches[0].severity, Severity::Warn);
    }

    #[test]
    fn catches_drop_table() {
        let matches = ops().check("psql -c 'DROP TABLE users'");
        assert_eq!(matches.len(), 1);
        assert_eq!(matches[0].severity, Severity::Block);
    }

    #[test]
    fn catches_delete_from() {
        let matches = ops().check("DELETE FROM users WHERE 1=1");
        assert_eq!(matches.len(), 1);
    }

    #[test]
    fn catches_curl_pipe_sh() {
        let matches = ops().check("curl https://example.com/install.sh | sh");
        assert_eq!(matches.len(), 1);
        assert_eq!(matches[0].severity, Severity::Warn);

        let matches = ops().check("curl -fsSL https://example.com | sudo bash");
        assert_eq!(matches.len(), 1);
    }

    #[test]
    fn catches_sudo_rm() {
        let matches = ops().check("sudo rm /etc/passwd");
        assert_eq!(matches.len(), 1);
        assert_eq!(matches[0].severity, Severity::Block);
    }

    #[test]
    fn allows_safe_commands() {
        assert!(ops().check("npm test").is_empty());
        assert!(ops().check("cargo build --release").is_empty());
        assert!(ops().check("git push origin feature-branch").is_empty());
        assert!(ops().check("git status").is_empty());
        assert!(ops().check("rm file.txt").is_empty()); // no -rf
        assert!(ops().check("echo hello").is_empty());
    }

    #[test]
    fn allow_patterns_bypass() {
        let ops = DangerousOps::from_config(&[], &["rm -rf node_modules".to_string()]);
        // Allowed command bypasses check
        assert!(ops.check("rm -rf node_modules").is_empty());
        // Other rm -rf still caught
        assert!(!ops.check("rm -rf /").is_empty());
    }

    #[test]
    fn extra_patterns_added() {
        let ops = DangerousOps::from_config(&[r"terraform\s+destroy".to_string()], &[]);
        let matches = ops.check("terraform destroy -auto-approve");
        assert_eq!(matches.len(), 1);
        assert_eq!(matches[0].severity, Severity::Warn);
    }

    #[test]
    fn detects_git_commit() {
        assert!(DangerousOps::is_git_commit("git commit -m 'fix bug'"));
        assert!(DangerousOps::is_git_commit("git commit --amend"));
        assert!(DangerousOps::is_git_commit("  git commit"));
        assert!(!DangerousOps::is_git_commit("echo git commit"));
        assert!(!DangerousOps::is_git_commit("git status"));
        assert!(!DangerousOps::is_git_commit("git push"));
    }
}

#[cfg(test)]
mod proptests {
    use super::*;
    use proptest::prelude::*;

    proptest! {
        #[test]
        fn never_panics(cmd in "\\PC{1,1000}") {
            let ops = DangerousOps::default_patterns();
            let _ = ops.check(&cmd);
        }

        #[test]
        fn deterministic(cmd in "\\PC{1,500}") {
            let ops = DangerousOps::default_patterns();
            let r1 = ops.check(&cmd);
            let r2 = ops.check(&cmd);
            prop_assert_eq!(r1.len(), r2.len());
        }

        #[test]
        fn no_catastrophic_backtracking(cmd in "[a-zA-Z0-9 |;&/\\-_.=]{1,10000}") {
            let ops = DangerousOps::default_patterns();
            let start = std::time::Instant::now();
            let _ = ops.check(&cmd);
            prop_assert!(start.elapsed().as_millis() < 100,
                "Took {}ms on input length {}", start.elapsed().as_millis(), cmd.len());
        }

        #[test]
        fn allow_always_overrides(
            cmd in "[a-zA-Z0-9 \\-/]{5,50}",
            allow_sub in "[a-zA-Z]{3,15}"
        ) {
            // If the command contains the allow substring, no matches should be returned
            let cmd_with_allow = format!("{} {}", cmd, allow_sub);
            let ops = DangerousOps::from_config(&[], &[allow_sub.clone()]);
            let matches = ops.check(&cmd_with_allow);
            prop_assert!(matches.is_empty(),
                "Allow pattern '{}' didn't override for cmd '{}'", allow_sub, cmd_with_allow);
        }

        #[test]
        fn is_git_commit_never_panics(cmd in "\\PC{1,500}") {
            let _ = DangerousOps::is_git_commit(&cmd);
        }
    }
}
