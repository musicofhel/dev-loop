/// Regex-based secret pattern scanner for file content.
///
/// Used in PostToolUse[Write|Edit] to warn if Claude just wrote something
/// that looks like a secret. These are fast in-process checks (<1ms).
/// Full gitleaks scanning happens in Tier 2 checkpoints.
use regex::Regex;

/// Number of built-in secret detection patterns.
pub const BUILTIN_SECRET_PATTERNS: usize = 15;

pub struct SecretScanner {
    patterns: Vec<SecretPattern>,
    /// File paths that should skip secret scanning.
    pub file_allowlist: Vec<String>,
}

struct SecretPattern {
    regex: Regex,
    description: String,
}

#[derive(Debug, Clone)]
pub struct SecretMatch {
    pub description: String,
    /// The line number where the match was found (1-based)
    pub line: usize,
    /// A redacted preview of the matched line
    pub preview: String,
}

impl SecretScanner {
    pub fn default_patterns() -> Self {
        let patterns = vec![
            // API keys with common prefixes
            secret(r#"(?i)(api[_-]?key|apikey)\s*[:=]\s*['"]?[a-zA-Z0-9_\-]{20,}"#, "API key assignment"),
            secret(r#"(?i)(access[_-]?key|aws_access_key_id)\s*[:=]\s*['"]?[A-Z0-9]{16,}"#, "AWS access key"),
            secret(r#"(?i)(secret[_-]?key|aws_secret_access_key)\s*[:=]\s*['"]?[a-zA-Z0-9/+=]{20,}"#, "AWS secret key"),

            // Tokens
            secret(r#"(?i)(auth[_-]?token|bearer)\s*[:=]\s*['"]?[a-zA-Z0-9_\-.]{20,}"#, "Auth token"),
            secret(r#"ghp_[a-zA-Z0-9]{36}"#, "GitHub personal access token"),
            secret(r#"gho_[a-zA-Z0-9]{36}"#, "GitHub OAuth token"),
            secret(r#"github_pat_[a-zA-Z0-9_]{22,}"#, "GitHub fine-grained PAT"),
            secret(r#"sk-[a-zA-Z0-9]{32,}"#, "OpenAI/Anthropic API key"),
            secret(r#"sk-ant-[a-zA-Z0-9\-]{80,}"#, "Anthropic API key"),
            secret(r#"xoxb-[0-9]+-[0-9]+-[a-zA-Z0-9]+"#, "Slack bot token"),
            secret(r#"xoxp-[0-9]+-[0-9]+-[0-9]+-[a-f0-9]+"#, "Slack user token"),

            // Passwords in config
            secret(r#"(?i)(password|passwd|pwd)\s*[:=]\s*['"][^'"]{8,}['"]"#, "Password in config"),

            // Private keys
            secret(r#"-----BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"#, "Private key header"),

            // Connection strings with credentials
            secret(r#"(?i)(postgres|mysql|mongodb|redis)://[^:]+:[^@]+@"#, "Database connection string with password"),

            // Generic high-entropy strings in assignment context
            secret(r#"(?i)(token|secret|private[_-]?key)\s*[:=]\s*['"][a-zA-Z0-9+/=_\-]{32,}['"]"#, "High-entropy secret assignment"),
        ];

        Self {
            patterns,
            file_allowlist: Vec::new(),
        }
    }

    /// Build with config overrides: extra regex patterns, file allowlist.
    pub fn from_config(extra: &[String], file_allowlist: &[String]) -> Self {
        let mut scanner = Self::default_patterns();
        for pattern_str in extra {
            if let Ok(regex) = Regex::new(pattern_str) {
                scanner.patterns.push(SecretPattern {
                    regex,
                    description: format!("Custom: {}", pattern_str),
                });
            }
        }
        scanner.file_allowlist = file_allowlist.to_vec();
        scanner
    }

    /// Check if a file path is in the allowlist (should skip scanning).
    pub fn is_file_allowed(&self, file_path: &str) -> bool {
        let normalized = file_path.strip_prefix('/').unwrap_or(file_path);
        self.file_allowlist
            .iter()
            .any(|entry| normalized.ends_with(entry.as_str()))
    }

    /// Scan file content for secret patterns.
    /// Returns all matches found with line numbers.
    pub fn check(&self, content: &str) -> Vec<SecretMatch> {
        let mut matches = Vec::new();

        for (line_num, line) in content.lines().enumerate() {
            // Skip comments (common false positives)
            let trimmed = line.trim();
            if trimmed.starts_with('#')
                || trimmed.starts_with("//")
                || (trimmed.starts_with("--") && !trimmed.starts_with("-----"))
            {
                continue;
            }
            // Skip lines that look like example/placeholder values
            // Use word-boundary-ish checks to avoid false positives on hostnames
            if is_placeholder(trimmed) {
                continue;
            }

            for pat in &self.patterns {
                if pat.regex.is_match(line) {
                    matches.push(SecretMatch {
                        description: pat.description.clone(),
                        line: line_num + 1,
                        preview: redact_line(line),
                    });
                    break; // One match per line is enough
                }
            }
        }

        matches
    }
}

/// Check if a line looks like a placeholder/example value.
fn is_placeholder(line: &str) -> bool {
    // Check for placeholder markers — but not as part of hostnames
    let lower = line.to_lowercase();
    // "your-api-key" or "YOUR_API_KEY" style placeholders
    if lower.contains("your-") || lower.contains("your_") {
        return true;
    }
    // "xxx" placeholder but not in a URL/hostname context
    if lower.contains("xxx") {
        return true;
    }
    if lower.contains("<redacted>") || lower.contains("redacted") {
        return true;
    }
    // "example" as a standalone word/value, not in hostnames like example.com
    // Skip if it's clearly a hostname pattern
    if (lower.contains("example") && !lower.contains("example."))
        || lower.contains("_example")
        || lower.contains("example_")
    {
        return true;
    }
    false
}

/// Redact a line to avoid leaking the actual secret in logs/events.
/// Keeps the first 20 chars and replaces the rest with "***".
fn redact_line(line: &str) -> String {
    let trimmed = line.trim();
    if trimmed.len() <= 30 {
        "***REDACTED***".to_string()
    } else {
        format!("{}***", &trimmed[..20])
    }
}

fn secret(pattern: &str, description: &str) -> SecretPattern {
    SecretPattern {
        regex: Regex::new(pattern).expect("invalid secret pattern regex"),
        description: description.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scanner() -> SecretScanner {
        SecretScanner::default_patterns()
    }

    #[test]
    fn catches_api_key() {
        let content = r#"const API_KEY = "sk-1234567890abcdefghijklmnopqrstuvwxyz";"#;
        let matches = scanner().check(content);
        assert!(!matches.is_empty(), "should catch API key");
    }

    #[test]
    fn catches_github_pat() {
        // nosemgrep: generic.secrets.security.detected-github-token
        let content = "token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij";
        let matches = scanner().check(content);
        assert!(!matches.is_empty(), "should catch GitHub PAT");
    }

    #[test]
    fn catches_anthropic_key() {
        let content = r#"ANTHROPIC_API_KEY=sk-ant-api03-very-long-key-string-that-is-at-least-eighty-characters-long-for-the-pattern-to-match"#;
        let matches = scanner().check(content);
        assert!(!matches.is_empty(), "should catch Anthropic key");
    }

    #[test]
    fn catches_private_key() {
        let content = "-----BEGIN RSA PRIVATE KEY-----\nblahblah\n-----END RSA PRIVATE KEY-----";
        let matches = scanner().check(content);
        assert!(!matches.is_empty(), "should catch private key header");
    }

    #[test]
    fn catches_db_connection_string() {
        let content = r#"DATABASE_URL=postgres://admin:supersecret@db.example.com:5432/mydb"#;
        let matches = scanner().check(content);
        assert!(!matches.is_empty(), "should catch DB connection string");
    }

    #[test]
    fn catches_password_in_config() {
        let content = r#"password: "MyS3cur3P@ssw0rd!""#;
        let matches = scanner().check(content);
        assert!(!matches.is_empty(), "should catch password in config");
    }

    #[test]
    fn skips_comments() {
        let content = "# API_KEY=sk-1234567890abcdefghijklmnopqrstuvwxyz";
        let matches = scanner().check(content);
        assert!(matches.is_empty(), "should skip commented lines");

        let content = "// password: \"hunter2isagoodpassword\"";
        let matches = scanner().check(content);
        assert!(matches.is_empty(), "should skip JS comments");
    }

    #[test]
    fn skips_examples() {
        let content = r#"API_KEY=your-api-key-here"#;
        let matches = scanner().check(content);
        assert!(matches.is_empty(), "should skip example/placeholder values");

        let content = r#"token: "EXAMPLE_TOKEN_XXXXXXXXXXXXXXXXX""#;
        let matches = scanner().check(content);
        assert!(matches.is_empty(), "should skip EXAMPLE values");
    }

    #[test]
    fn allows_normal_code() {
        let content = r#"
fn main() {
    let config = Config::new();
    println!("Hello, world!");
    let x = 42;
}
"#;
        let matches = scanner().check(content);
        assert!(matches.is_empty(), "should allow normal code");
    }

    #[test]
    fn file_allowlist() {
        let scanner =
            SecretScanner::from_config(&[], &["tests/fixtures/fake-key.pem".to_string()]);
        assert!(scanner.is_file_allowed("tests/fixtures/fake-key.pem"));
        assert!(scanner.is_file_allowed("/home/user/repo/tests/fixtures/fake-key.pem"));
        assert!(!scanner.is_file_allowed("real-key.pem"));
    }

    #[test]
    fn extra_secret_patterns() {
        let scanner = SecretScanner::from_config(&[r"CUSTOM_[A-Z]{20,}".to_string()], &[]);
        let content = "key = CUSTOM_ABCDEFGHIJKLMNOPQRSTU";
        let matches = scanner.check(content);
        assert!(!matches.is_empty());
    }

    #[test]
    fn redacts_output() {
        let line = r#"API_KEY = "sk-1234567890abcdefghijklmnopqrstuvwxyz""#;
        let redacted = redact_line(line);
        assert!(redacted.contains("***"), "should be redacted");
        assert!(!redacted.contains("1234567890"), "should not contain the secret");
    }
}

#[cfg(test)]
mod proptests {
    use super::*;
    use proptest::prelude::*;

    proptest! {
        #[test]
        fn never_panics(content in "\\PC{1,5000}") {
            let scanner = SecretScanner::default_patterns();
            let _ = scanner.check(&content);
        }

        #[test]
        fn deterministic(content in "\\PC{1,1000}") {
            let scanner = SecretScanner::default_patterns();
            let r1 = scanner.check(&content);
            let r2 = scanner.check(&content);
            prop_assert_eq!(r1.len(), r2.len());
        }

        #[test]
        fn hash_comments_never_match(rest in "[a-zA-Z0-9_=:'\"/\\-]{1,200}") {
            let line = format!("# {rest}");
            let scanner = SecretScanner::default_patterns();
            let matches = scanner.check(&line);
            prop_assert!(matches.is_empty(),
                "Hash comment matched: {}", line);
        }

        #[test]
        fn slash_comments_never_match(rest in "[a-zA-Z0-9_=:'\"/\\-]{1,200}") {
            let line = format!("// {rest}");
            let scanner = SecretScanner::default_patterns();
            let matches = scanner.check(&line);
            prop_assert!(matches.is_empty(),
                "Slash comment matched: {}", line);
        }

        #[test]
        fn your_placeholder_never_matches(
            prefix in "[a-zA-Z_]{3,15}",
            suffix in "[a-zA-Z0-9_\\-]{5,30}"
        ) {
            // Lines containing "your-" or "your_" should be skipped
            let line = format!("{prefix} = your-{suffix}");
            let scanner = SecretScanner::default_patterns();
            let matches = scanner.check(&line);
            prop_assert!(matches.is_empty(),
                "Placeholder with 'your-' matched: {}", line);
        }

        #[test]
        fn xxx_placeholder_never_matches(
            prefix in "[a-zA-Z_]{3,15}",
            suffix in "[a-zA-Z0-9]{5,30}"
        ) {
            let line = format!("{prefix} = xxx{suffix}");
            let scanner = SecretScanner::default_patterns();
            let matches = scanner.check(&line);
            prop_assert!(matches.is_empty(),
                "Placeholder with 'xxx' matched: {}", line);
        }

        #[test]
        fn no_catastrophic_backtracking(content in "[a-zA-Z0-9_=:'\"/\\- ]{1,10000}") {
            let scanner = SecretScanner::default_patterns();
            let start = std::time::Instant::now();
            let _ = scanner.check(&content);
            prop_assert!(start.elapsed().as_millis() < 100,
                "Took {}ms on input length {}", start.elapsed().as_millis(), content.len());
        }

        #[test]
        fn file_allowlist_suffix_match(
            base in "[a-z]{3,10}",
            ext in prop::sample::select(vec![".pem", ".key", ".json", ".env"])
        ) {
            let file_name = format!("tests/fixtures/{base}{ext}");
            let scanner = SecretScanner::from_config(&[], &[file_name.clone()]);
            prop_assert!(scanner.is_file_allowed(&file_name));
            let full_path = format!("/home/user/repo/{}", file_name);
            prop_assert!(scanner.is_file_allowed(&full_path));
        }
    }
}
