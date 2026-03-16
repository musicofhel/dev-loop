/// Glob-based file deny list — blocks writes to sensitive files.
///
/// Ported from `src/devloop/runtime/deny_list.py`. Matches against the full
/// path, basename, and every suffix of the path parts so that directory-scoped
/// patterns like `.aws/*` catch paths like `home/user/.aws/credentials`.
use glob::Pattern;

/// Pre-compiled deny patterns.
pub struct DenyList {
    patterns: Vec<(String, Pattern)>,
}

pub const BUILTIN_DENY_PATTERNS: &[&str] = &[
    // Environment / dotenv
    ".env",
    ".env.*",
    // Cryptographic keys and certificates
    "*.key",
    "*.pem",
    "*.p12",
    "*.pfx",
    // Credentials files (any extension)
    "credentials.*",
    // Anything with "secret" in the name
    "*secret*",
    // Cloud provider credential directories
    ".aws/*",
    ".ssh/*",
    // Token / auth files
    "*.keystore",
    "*.jks",
    ".netrc",
    ".npmrc",
    ".pypirc",
];

impl DenyList {
    /// Build the default deny list from the hardcoded patterns.
    pub fn default_patterns() -> Self {
        Self::compile(BUILTIN_DENY_PATTERNS.iter().map(|s| s.to_string()).collect())
    }

    /// Build a deny list with config overrides applied.
    ///
    /// - `extra`: additional glob patterns to deny
    /// - `remove`: built-in patterns to remove (exact match on pattern string)
    pub fn from_config(extra: &[String], remove: &[String]) -> Self {
        let mut patterns: Vec<String> = BUILTIN_DENY_PATTERNS
            .iter()
            .map(|s| s.to_string())
            .filter(|p| !remove.contains(p))
            .collect();

        for p in extra {
            if !patterns.contains(p) {
                patterns.push(p.clone());
            }
        }

        Self::compile(patterns)
    }

    fn compile(raw: Vec<String>) -> Self {
        let patterns = raw
            .into_iter()
            .filter_map(|p| Pattern::new(&p).ok().map(|compiled| (p, compiled)))
            .collect();

        Self { patterns }
    }

    /// Check if a file path matches any denied pattern.
    ///
    /// Returns `Some((pattern, reason))` if blocked, `None` if allowed.
    pub fn check(&self, path: &str) -> Option<DenyMatch> {
        // Normalize: strip leading slashes for relative matching
        let path = path.strip_prefix('/').unwrap_or(path);

        for (raw, pattern) in &self.patterns {
            // Match against the full path
            if pattern.matches(path) {
                return Some(DenyMatch {
                    pattern: raw.clone(),
                    matched_on: path.to_string(),
                });
            }

            // Match against just the filename (basename)
            if let Some(basename) = path.rsplit('/').next() {
                if pattern.matches(basename) {
                    return Some(DenyMatch {
                        pattern: raw.clone(),
                        matched_on: basename.to_string(),
                    });
                }
            }

            // Match against each suffix of the path parts so that
            // directory-scoped patterns like ".aws/*" work on paths
            // like "home/user/.aws/credentials"
            let parts: Vec<&str> = path.split('/').collect();
            for i in 1..parts.len() {
                let sub = parts[i..].join("/");
                if pattern.matches(&sub) {
                    return Some(DenyMatch {
                        pattern: raw.clone(),
                        matched_on: sub,
                    });
                }
            }
        }

        None
    }
}

#[derive(Debug, Clone)]
pub struct DenyMatch {
    pub pattern: String,
    pub matched_on: String,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dl() -> DenyList {
        DenyList::default_patterns()
    }

    #[test]
    fn blocks_dotenv() {
        assert!(dl().check(".env").is_some());
        assert!(dl().check(".env.local").is_some());
        assert!(dl().check(".env.production").is_some());
        assert!(dl().check("config/.env").is_some());
    }

    #[test]
    fn blocks_crypto_keys() {
        assert!(dl().check("server.key").is_some());
        assert!(dl().check("certs/ca.pem").is_some());
        assert!(dl().check("auth.p12").is_some());
        assert!(dl().check("store.pfx").is_some());
    }

    #[test]
    fn blocks_credentials() {
        assert!(dl().check("credentials.json").is_some());
        assert!(dl().check("config/credentials.yaml").is_some());
    }

    #[test]
    fn blocks_secrets() {
        assert!(dl().check("secret.txt").is_some());
        assert!(dl().check("my_secret_key").is_some());
        assert!(dl().check("app-secrets.yaml").is_some());
    }

    #[test]
    fn blocks_cloud_dirs() {
        assert!(dl().check(".aws/credentials").is_some());
        assert!(dl().check(".ssh/id_rsa").is_some());
        assert!(dl().check("home/user/.aws/config").is_some());
        assert!(dl().check("/home/user/.ssh/authorized_keys").is_some());
    }

    #[test]
    fn blocks_auth_files() {
        assert!(dl().check("release.keystore").is_some());
        assert!(dl().check("truststore.jks").is_some());
        assert!(dl().check(".netrc").is_some());
        assert!(dl().check(".npmrc").is_some());
        assert!(dl().check(".pypirc").is_some());
    }

    #[test]
    fn from_config_extra_patterns() {
        let dl = DenyList::from_config(&["*.vault".to_string()], &[]);
        assert!(dl.check("secrets.vault").is_some());
        // Built-in still works
        assert!(dl.check(".env").is_some());
    }

    #[test]
    fn from_config_remove_patterns() {
        let dl = DenyList::from_config(&[], &[".npmrc".to_string()]);
        // Removed pattern no longer blocks
        assert!(dl.check(".npmrc").is_none());
        // Other built-ins still work
        assert!(dl.check(".env").is_some());
        assert!(dl.check(".pypirc").is_some());
    }

    #[test]
    fn from_config_extra_and_remove() {
        let dl = DenyList::from_config(
            &["*.vault".to_string()],
            &[".npmrc".to_string(), ".pypirc".to_string()],
        );
        assert!(dl.check("secrets.vault").is_some());
        assert!(dl.check(".npmrc").is_none());
        assert!(dl.check(".pypirc").is_none());
        assert!(dl.check(".env").is_some());
    }

    #[test]
    fn allows_normal_files() {
        assert!(dl().check("src/main.rs").is_none());
        assert!(dl().check("package.json").is_none());
        assert!(dl().check("README.md").is_none());
        assert!(dl().check("src/config.ts").is_none());
        assert!(dl().check("Cargo.toml").is_none());
        assert!(dl().check(".gitignore").is_none());
    }
}
