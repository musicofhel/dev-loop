/// Generate `ambient-rules.md` — a summary of active ambient layer rules.
///
/// Written to `~/.claude/dev-loop-ambient-rules.md` on session start.
/// This file gives Claude visibility into what the ambient layer enforces.
use crate::check::deny_list::BUILTIN_DENY_PATTERNS;
use crate::check::dangerous_ops::BUILTIN_DANGEROUS_PATTERNS;
use crate::check::secrets::BUILTIN_SECRET_PATTERNS;
use crate::config::MergedConfig;

/// Path where the rules file is written.
fn rules_path() -> std::path::PathBuf {
    dirs::home_dir()
        .unwrap_or_else(|| std::path::PathBuf::from("/tmp"))
        .join(".claude")
        .join("dev-loop-ambient-rules.md")
}

/// Generate and write the ambient rules markdown file.
pub fn generate(config: &MergedConfig) {
    let content = build_rules_md(config);
    let path = rules_path();

    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }

    if let Err(e) = std::fs::write(&path, &content) {
        eprintln!("Failed to write {}: {e}", path.display());
    }
}

/// Build the rules markdown content.
fn build_rules_md(config: &MergedConfig) -> String {
    let mut md = String::with_capacity(2048);

    md.push_str("# dev-loop Ambient Layer — Active Rules\n\n");
    md.push_str("> Auto-generated on session start. Do not edit manually.\n\n");

    // Status
    md.push_str("## Status\n\n");
    md.push_str(&format!(
        "- Enabled: {}\n",
        if config.enabled { "yes" } else { "no" }
    ));
    md.push_str(&format!(
        "- Tier 1 (always-on checks): {}\n",
        if config.tier1 { "active" } else { "disabled" }
    ));
    md.push_str(&format!(
        "- Tier 2 (checkpoint gates): {}\n\n",
        if config.tier2 { "active" } else { "disabled" }
    ));

    // Deny list
    md.push_str("## Blocked File Patterns (Tier 1)\n\n");
    md.push_str("Writes to files matching these patterns are **blocked**:\n\n");

    let mut deny_patterns: Vec<&str> = BUILTIN_DENY_PATTERNS
        .iter()
        .filter(|p| !config.deny_list_remove.iter().any(|r| r == **p))
        .copied()
        .collect();
    for extra in &config.deny_list_extra {
        deny_patterns.push(extra);
    }

    for p in &deny_patterns {
        md.push_str(&format!("- `{p}`\n"));
    }
    if !config.deny_list_remove.is_empty() {
        md.push_str(&format!(
            "\nRemoved from defaults: {}\n",
            config
                .deny_list_remove
                .iter()
                .map(|p| format!("`{p}`"))
                .collect::<Vec<_>>()
                .join(", ")
        ));
    }
    md.push('\n');

    // Dangerous ops
    md.push_str("## Dangerous Command Patterns (Tier 1)\n\n");
    md.push_str("Bash commands matching these patterns trigger a **warning** or **block**:\n\n");

    let dangerous_count =
        BUILTIN_DANGEROUS_PATTERNS + config.dangerous_ops_extra.len();
    md.push_str(&format!("- {dangerous_count} patterns active\n"));

    if !config.dangerous_ops_allow.is_empty() {
        md.push_str(&format!(
            "- Allowed exceptions: {}\n",
            config
                .dangerous_ops_allow
                .iter()
                .map(|p| format!("`{p}`"))
                .collect::<Vec<_>>()
                .join(", ")
        ));
    }
    md.push('\n');

    // Secret scanning
    md.push_str("## Secret Detection (Tier 1, PostToolUse)\n\n");
    let secret_count =
        BUILTIN_SECRET_PATTERNS + config.secrets_extra.len();
    md.push_str(&format!("- {secret_count} patterns scanned after each Write/Edit\n"));
    if !config.secrets_file_allowlist.is_empty() {
        md.push_str(&format!(
            "- Allowlisted files: {}\n",
            config
                .secrets_file_allowlist
                .iter()
                .map(|p| format!("`{p}`"))
                .collect::<Vec<_>>()
                .join(", ")
        ));
    }
    md.push('\n');

    // Checkpoint gates
    if config.tier2 {
        md.push_str("## Checkpoint Gates (Tier 2, on commit)\n\n");
        md.push_str("Before each `git commit`, these gates run sequentially (fail-fast):\n\n");
        for (i, gate) in config.checkpoint.gates.iter().enumerate() {
            md.push_str(&format!("{}. {gate}\n", i + 1));
        }
        md.push('\n');
    }

    // Override
    md.push_str("## Override\n\n");
    md.push_str("To temporarily allow a blocked file:\n");
    md.push_str("```bash\n");
    md.push_str("dl allow-once \".env\"    # Allows one write (expires in 5 min)\n");
    md.push_str("```\n\n");
    md.push_str("To disable all checks:\n");
    md.push_str("```bash\n");
    md.push_str("dl disable               # All hooks become no-op\n");
    md.push_str("dl enable                # Re-enable\n");
    md.push_str("```\n");

    md
}

/// CLI handler for `dl rules` — print the rules to stdout.
pub fn print_rules() {
    let config = crate::config::load_merged(None);
    let content = build_rules_md(&config);
    print!("{content}");
    println!("---");
    println!("File: {}", rules_path().display());
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rules_md_contains_essential_sections() {
        let config = crate::config::load_merged(None);
        let md = build_rules_md(&config);

        assert!(md.contains("# dev-loop Ambient Layer"));
        assert!(md.contains("Blocked File Patterns"));
        assert!(md.contains("Dangerous Command Patterns"));
        assert!(md.contains("Secret Detection"));
        assert!(md.contains("Override"));
        assert!(md.contains("dl allow-once"));
    }

    #[test]
    fn rules_md_includes_deny_patterns() {
        let config = crate::config::load_merged(None);
        let md = build_rules_md(&config);

        assert!(md.contains("`.env`"));
        assert!(md.contains("`*.key`"));
        assert!(md.contains("`*.pem`"));
    }

    #[test]
    fn rules_md_reflects_tier2_status() {
        let mut config = crate::config::load_merged(None);
        config.tier2 = true;
        let md = build_rules_md(&config);
        assert!(md.contains("Checkpoint Gates"));

        config.tier2 = false;
        let md = build_rules_md(&config);
        assert!(!md.contains("Checkpoint Gates"));
    }

    #[test]
    fn rules_md_shows_removed_patterns() {
        let mut config = crate::config::load_merged(None);
        config.deny_list_remove = vec!["*secret*".to_string()];
        let md = build_rules_md(&config);
        assert!(md.contains("Removed from defaults"));
        assert!(md.contains("`*secret*`"));
    }
}
