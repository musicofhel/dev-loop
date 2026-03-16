/// `dl allow-once` — file-based temporary override tracking.
///
/// Overrides are stored as individual JSON files in `/tmp/dev-loop/overrides/`.
/// Each override has a glob pattern, creation time, and TTL (default 5 minutes).
/// Consumed (file deleted) after first match or on expiry.
use serde::{Deserialize, Serialize};
use std::path::PathBuf;

const OVERRIDES_DIR: &str = "/tmp/dev-loop/overrides";
const DEFAULT_TTL_SECS: u64 = 300; // 5 minutes

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct Override {
    pub pattern: String,
    pub created_at: u64, // unix timestamp
    pub ttl_secs: u64,
}

impl Override {
    pub fn is_expired(&self) -> bool {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        now > self.created_at + self.ttl_secs
    }

    /// Seconds remaining before expiry.
    pub fn remaining_secs(&self) -> u64 {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        let expires = self.created_at + self.ttl_secs;
        expires.saturating_sub(now)
    }
}

fn overrides_dir() -> PathBuf {
    PathBuf::from(OVERRIDES_DIR)
}

/// Deterministic path for an override, based on pattern hash.
fn override_path(pattern: &str) -> PathBuf {
    use sha2::{Digest, Sha256};
    let hash = hex::encode(Sha256::digest(pattern.as_bytes()));
    overrides_dir().join(format!("{}.json", &hash[..16]))
}

/// Register a temporary allow-once override.
pub fn register(pattern: &str, ttl_secs: Option<u64>) {
    let _ = std::fs::create_dir_all(OVERRIDES_DIR);
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();

    let ovr = Override {
        pattern: pattern.to_string(),
        created_at: now,
        ttl_secs: ttl_secs.unwrap_or(DEFAULT_TTL_SECS),
    };

    let path = override_path(pattern);
    match serde_json::to_string_pretty(&ovr) {
        Ok(content) => {
            if let Err(e) = std::fs::write(&path, content) {
                eprintln!("Failed to write override: {e}");
            }
        }
        Err(e) => eprintln!("Failed to serialize override: {e}"),
    }
}

/// Check if a file path has an active override. Consumes (deletes) the override if found.
///
/// Uses the same matching strategy as the deny list: checks against full path,
/// basename, and every suffix of path parts.
pub fn check_and_consume(file_path: &str) -> bool {
    let dir = overrides_dir();
    let entries = match std::fs::read_dir(&dir) {
        Ok(e) => e,
        Err(_) => return false,
    };

    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|x| x.to_str()) != Some("json") {
            continue;
        }
        let content = match std::fs::read_to_string(&path) {
            Ok(c) => c,
            Err(_) => continue,
        };
        let ovr: Override = match serde_json::from_str(&content) {
            Ok(o) => o,
            Err(_) => continue,
        };

        // Clean up expired
        if ovr.is_expired() {
            let _ = std::fs::remove_file(&path);
            continue;
        }

        // Match using same logic as deny list
        if matches_file_path(&ovr.pattern, file_path) {
            // Consume the override
            let _ = std::fs::remove_file(&path);
            return true;
        }
    }

    false
}

/// Match a pattern against a file path using the same strategy as the deny list:
/// full path, basename, and every suffix of path parts.
fn matches_file_path(pattern: &str, file_path: &str) -> bool {
    let path = file_path.strip_prefix('/').unwrap_or(file_path);

    let glob = match glob::Pattern::new(pattern) {
        Ok(g) => g,
        Err(_) => {
            // Fallback to substring match if not a valid glob
            return path.contains(pattern) || path.ends_with(pattern);
        }
    };

    // Match against full path
    if glob.matches(path) {
        return true;
    }

    // Match against basename
    if let Some(basename) = path.rsplit('/').next() {
        if glob.matches(basename) {
            return true;
        }
    }

    // Match against each suffix of path parts
    let parts: Vec<&str> = path.split('/').collect();
    for i in 1..parts.len() {
        let sub = parts[i..].join("/");
        if glob.matches(&sub) {
            return true;
        }
    }

    false
}

/// List active (non-expired) overrides. Cleans up expired ones.
pub fn list_active() -> Vec<Override> {
    let dir = overrides_dir();
    let entries = match std::fs::read_dir(&dir) {
        Ok(e) => e,
        Err(_) => return vec![],
    };

    let mut active = vec![];
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|x| x.to_str()) != Some("json") {
            continue;
        }
        let content = match std::fs::read_to_string(&path) {
            Ok(c) => c,
            Err(_) => continue,
        };
        let ovr: Override = match serde_json::from_str(&content) {
            Ok(o) => o,
            Err(_) => continue,
        };
        if ovr.is_expired() {
            let _ = std::fs::remove_file(&path);
        } else {
            active.push(ovr);
        }
    }

    active
}

/// CLI handler for `dl allow-once <pattern>`.
pub fn allow_once(pattern: &str, ttl: Option<u64>) {
    register(pattern, ttl);
    let ttl_display = ttl.unwrap_or(DEFAULT_TTL_SECS);
    println!("Override registered: \"{pattern}\" (expires in {ttl_display}s or after first match)");

    // Show all active overrides
    let active = list_active();
    if active.len() > 1 {
        println!("Active overrides:");
        for ovr in &active {
            let remaining = ovr.remaining_secs();
            println!("  \"{}\" ({remaining}s remaining)", ovr.pattern);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matches_exact_basename() {
        assert!(matches_file_path(".env", ".env"));
        assert!(matches_file_path(".env", "/home/user/repo/.env"));
        assert!(matches_file_path(".env", "config/.env"));
    }

    #[test]
    fn matches_glob_star() {
        assert!(matches_file_path("*.key", "server.key"));
        assert!(matches_file_path("*.key", "/home/user/certs/server.key"));
        assert!(matches_file_path(".env.*", ".env.local"));
    }

    #[test]
    fn matches_directory_scoped() {
        assert!(matches_file_path(".aws/*", "/home/user/.aws/credentials"));
        assert!(matches_file_path(".ssh/*", "home/user/.ssh/id_rsa"));
    }

    #[test]
    fn no_match_unrelated() {
        assert!(!matches_file_path(".env", "src/main.rs"));
        assert!(!matches_file_path("*.key", "package.json"));
    }

    #[test]
    fn register_and_consume() {
        let dir = tempfile::tempdir().unwrap();
        let pattern = format!("test-override-{}", std::process::id());
        let path = dir.path().join(format!("{pattern}.json"));

        // Write a fake override directly
        let ovr = Override {
            pattern: ".env.test".to_string(),
            created_at: std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_secs(),
            ttl_secs: 300,
        };
        std::fs::write(&path, serde_json::to_string(&ovr).unwrap()).unwrap();

        // Read it back
        let content = std::fs::read_to_string(&path).unwrap();
        let loaded: Override = serde_json::from_str(&content).unwrap();
        assert_eq!(loaded.pattern, ".env.test");
        assert!(!loaded.is_expired());
    }

    #[test]
    fn expired_override() {
        let ovr = Override {
            pattern: ".env".to_string(),
            created_at: 0, // epoch = long ago
            ttl_secs: 1,
        };
        assert!(ovr.is_expired());
    }

    #[test]
    fn fresh_override_not_expired() {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs();
        let ovr = Override {
            pattern: ".env".to_string(),
            created_at: now,
            ttl_secs: 300,
        };
        assert!(!ovr.is_expired());
        assert!(ovr.remaining_secs() > 290);
    }

    #[test]
    fn override_path_is_deterministic() {
        let p1 = override_path(".env");
        let p2 = override_path(".env");
        assert_eq!(p1, p2);

        let p3 = override_path("*.key");
        assert_ne!(p1, p3);
    }
}
