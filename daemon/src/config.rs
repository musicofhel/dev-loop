use serde::{Deserialize, Serialize};
use std::path::PathBuf;

// ── Defaults ───────────────────────────────────────────────────

fn default_true() -> bool {
    true
}
fn default_socket() -> String {
    "/tmp/dev-loop/dl.sock".to_string()
}
fn default_pid_file() -> String {
    "/tmp/dev-loop/dl.pid".to_string()
}
fn default_auto_stop() -> u32 {
    30
}
fn default_log_level() -> String {
    "info".to_string()
}
fn default_oo_url() -> String {
    "http://localhost:5080".to_string()
}
fn default_oo_org() -> String {
    "default".to_string()
}
fn default_service_name() -> String {
    "dev-loop-ambient".to_string()
}
fn default_gates() -> Vec<String> {
    vec![
        "sanity".into(),
        "semgrep".into(),
        "secrets".into(),
        "atdd".into(),
        "review".into(),
    ]
}
fn default_context_limit() -> u64 {
    200_000
}
fn default_context_warn_pct() -> f32 {
    0.85
}
fn default_fail_mode() -> String {
    "open".to_string()
}
fn default_gate_timeout() -> u64 {
    60
}
fn default_channel_capacity() -> usize {
    10_000
}
fn default_max_file_size_mb() -> u64 {
    50
}
fn default_max_rotated_files() -> u32 {
    3
}
fn default_ambient_mode() -> String {
    "enforce".to_string()
}

// ── Global Config (~/.config/dev-loop/ambient.yaml) ────────────

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct AmbientConfig {
    #[serde(default = "default_true")]
    pub enabled: bool,
    #[serde(default = "default_true")]
    pub tier1: bool,
    #[serde(default = "default_true")]
    pub tier2: bool,
    /// "enforce" (default), "shadow" (log only, never block), or "disabled"
    #[serde(default = "default_ambient_mode")]
    pub ambient_mode: String,
    #[serde(default)]
    pub daemon: DaemonConfig,
    #[serde(default)]
    pub deny_list: PatternOverrides,
    #[serde(default)]
    pub dangerous_ops: DangerousOpsOverrides,
    #[serde(default)]
    pub secrets: SecretsOverrides,
    #[serde(default)]
    pub observability: ObservabilityConfig,
    #[serde(default)]
    pub checkpoint: CheckpointConfig,
    #[serde(default)]
    pub continuity: ContinuityConfig,
    #[serde(default)]
    pub event_log: EventLogConfig,
    #[serde(default)]
    pub loop_detection: LoopDetectionConfig,
}

impl Default for AmbientConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            tier1: true,
            tier2: true,
            ambient_mode: default_ambient_mode(),
            daemon: DaemonConfig::default(),
            deny_list: PatternOverrides::default(),
            dangerous_ops: DangerousOpsOverrides::default(),
            secrets: SecretsOverrides::default(),
            observability: ObservabilityConfig::default(),
            checkpoint: CheckpointConfig::default(),
            continuity: ContinuityConfig::default(),
            event_log: EventLogConfig::default(),
            loop_detection: LoopDetectionConfig::default(),
        }
    }
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct DaemonConfig {
    #[serde(default = "default_socket")]
    pub socket: String,
    #[serde(default = "default_pid_file")]
    pub pid_file: String,
    #[serde(default = "default_auto_stop")]
    pub auto_stop_minutes: u32,
    #[serde(default = "default_log_level")]
    pub log_level: String,
}

impl Default for DaemonConfig {
    fn default() -> Self {
        Self {
            socket: default_socket(),
            pid_file: default_pid_file(),
            auto_stop_minutes: default_auto_stop(),
            log_level: default_log_level(),
        }
    }
}

#[derive(Debug, Serialize, Deserialize, Clone, Default)]
pub struct PatternOverrides {
    #[serde(default)]
    pub extra_patterns: Vec<String>,
    #[serde(default)]
    pub remove_patterns: Vec<String>,
    #[serde(default)]
    pub allow_patterns: Vec<String>,
}

#[derive(Debug, Serialize, Deserialize, Clone, Default)]
pub struct DangerousOpsOverrides {
    #[serde(default)]
    pub extra_patterns: Vec<String>,
    #[serde(default)]
    pub allow_patterns: Vec<String>,
}

#[derive(Debug, Serialize, Deserialize, Clone, Default)]
pub struct SecretsOverrides {
    #[serde(default)]
    pub extra_patterns: Vec<String>,
    #[serde(default)]
    pub file_allowlist: Vec<String>,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct ObservabilityConfig {
    #[serde(default = "default_oo_url")]
    pub openobserve_url: String,
    #[serde(default = "default_oo_org")]
    pub openobserve_org: String,
    #[serde(default)]
    pub openobserve_user: String,
    #[serde(default)]
    pub openobserve_password: String,
    #[serde(default = "default_service_name")]
    pub service_name: String,
}

impl Default for ObservabilityConfig {
    fn default() -> Self {
        Self {
            openobserve_url: default_oo_url(),
            openobserve_org: default_oo_org(),
            openobserve_user: String::new(),
            openobserve_password: String::new(),
            service_name: default_service_name(),
        }
    }
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct CheckpointConfig {
    #[serde(default = "default_gates")]
    pub gates: Vec<String>,
    #[serde(default)]
    pub skip_review: bool,
    #[serde(default)]
    pub atdd_required: bool,
    #[serde(default)]
    pub test_command: Option<String>,
    #[serde(default = "default_semgrep_extra_configs")]
    pub semgrep_extra_configs: Vec<String>,
    /// "open" (default) = skip gate when tool missing; "closed" = fail gate
    #[serde(default = "default_fail_mode")]
    pub fail_mode: String,
    /// Overall checkpoint timeout in seconds (default: 60)
    #[serde(default = "default_gate_timeout")]
    pub gate_timeout_s: u64,
}

fn default_semgrep_extra_configs() -> Vec<String> {
    let default_path = dirs::home_dir()
        .map(|h| h.join(".local/share/semgrep-ai-rules/rules"))
        .filter(|p| p.exists())
        .map(|p| p.to_string_lossy().to_string());
    match default_path {
        Some(p) => vec![p],
        None => vec![],
    }
}

impl Default for CheckpointConfig {
    fn default() -> Self {
        Self {
            gates: default_gates(),
            skip_review: false,
            atdd_required: false,
            test_command: None,
            semgrep_extra_configs: default_semgrep_extra_configs(),
            fail_mode: default_fail_mode(),
            gate_timeout_s: default_gate_timeout(),
        }
    }
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct ContinuityConfig {
    #[serde(default = "default_context_limit")]
    pub context_limit: u64,
    #[serde(default = "default_context_warn_pct")]
    pub context_warn_pct: f32,
}

impl Default for ContinuityConfig {
    fn default() -> Self {
        Self {
            context_limit: default_context_limit(),
            context_warn_pct: default_context_warn_pct(),
        }
    }
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct EventLogConfig {
    #[serde(default = "default_channel_capacity")]
    pub channel_capacity: usize,
    #[serde(default = "default_max_file_size_mb")]
    pub max_file_size_mb: u64,
    #[serde(default = "default_max_rotated_files")]
    pub max_rotated_files: u32,
}

impl Default for EventLogConfig {
    fn default() -> Self {
        Self {
            channel_capacity: default_channel_capacity(),
            max_file_size_mb: default_max_file_size_mb(),
            max_rotated_files: default_max_rotated_files(),
        }
    }
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct LoopDetectionConfig {
    #[serde(default = "default_true")]
    pub enabled: bool,
    #[serde(default = "default_loop_window")]
    pub window_secs: u64,
    #[serde(default = "default_loop_threshold")]
    pub threshold: u32,
}

fn default_loop_window() -> u64 {
    120
}
fn default_loop_threshold() -> u32 {
    5
}

impl Default for LoopDetectionConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            window_secs: 120,
            threshold: 5,
        }
    }
}

// ── Per-Repo Config (.devloop.yaml) ────────────────────────────

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct RepoConfig {
    #[serde(default = "default_true")]
    pub ambient: bool,
    #[serde(default)]
    pub deny_list: PatternOverrides,
    #[serde(default)]
    pub dangerous_ops: DangerousOpsOverrides,
    #[serde(default)]
    pub secrets: SecretsOverrides,
    #[serde(default)]
    pub checkpoint: RepoCheckpointOverrides,
    #[serde(default)]
    pub workflow: Option<String>,
    #[serde(default)]
    pub spec_required: Option<bool>,
}

#[derive(Debug, Serialize, Deserialize, Clone, Default)]
pub struct RepoCheckpointOverrides {
    #[serde(default)]
    pub skip_gates: Vec<String>,
    #[serde(default)]
    pub test_command: Option<String>,
    #[serde(default)]
    pub atdd_required: Option<bool>,
}

// ── Merged Config (three-layer merge result) ───────────────────

#[derive(Debug, Serialize, Clone)]
pub struct MergedConfig {
    pub enabled: bool,
    pub tier1: bool,
    pub tier2: bool,
    pub ambient_mode: String,

    // Deny list: built-in + extra - remove, with allow bypass
    pub deny_list_extra: Vec<String>,
    pub deny_list_remove: Vec<String>,
    pub deny_list_allow: Vec<String>,

    // Dangerous ops: built-in + extra, with allow bypass
    pub dangerous_ops_extra: Vec<String>,
    pub dangerous_ops_allow: Vec<String>,

    // Secrets: built-in + extra, with file allowlist bypass
    pub secrets_extra: Vec<String>,
    pub secrets_file_allowlist: Vec<String>,

    // Pass-through sections
    pub daemon: DaemonConfig,
    pub observability: ObservabilityConfig,
    pub checkpoint: CheckpointConfig,
    pub continuity: ContinuityConfig,
    pub event_log: EventLogConfig,
    pub loop_detection: LoopDetectionConfig,

    // Where the repo config came from (if any)
    pub repo_root: Option<PathBuf>,
}

impl MergedConfig {
    /// SHA-256 hash of the active check engine configuration.
    /// Returns the first 16 hex chars (8 bytes) for compact identification.
    pub fn config_hash(&self) -> String {
        use sha2::{Digest, Sha256};
        let mut hasher = Sha256::new();
        for p in &self.deny_list_extra {
            hasher.update(p.as_bytes());
            hasher.update(b"\x00");
        }
        hasher.update(b"\x01");
        for p in &self.deny_list_remove {
            hasher.update(p.as_bytes());
            hasher.update(b"\x00");
        }
        hasher.update(b"\x01");
        for p in &self.dangerous_ops_extra {
            hasher.update(p.as_bytes());
            hasher.update(b"\x00");
        }
        hasher.update(b"\x01");
        for p in &self.dangerous_ops_allow {
            hasher.update(p.as_bytes());
            hasher.update(b"\x00");
        }
        hasher.update(b"\x01");
        for p in &self.secrets_extra {
            hasher.update(p.as_bytes());
            hasher.update(b"\x00");
        }
        hasher.update(b"\x01");
        for p in &self.secrets_file_allowlist {
            hasher.update(p.as_bytes());
            hasher.update(b"\x00");
        }
        let result = hasher.finalize();
        hex::encode(&result[..8]) // 16 hex chars
    }
}

// ── Loading ────────────────────────────────────────────────────

pub fn config_path() -> PathBuf {
    dirs::config_dir()
        .unwrap_or_else(|| PathBuf::from("/tmp"))
        .join("dev-loop")
        .join("ambient.yaml")
}

pub fn load() -> AmbientConfig {
    let path = config_path();
    match std::fs::read_to_string(&path) {
        Ok(content) => serde_yaml::from_str(&content).unwrap_or_default(),
        Err(_) => AmbientConfig::default(),
    }
}

pub fn save(config: &AmbientConfig) {
    let path = config_path();
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let content = serde_yaml::to_string(config).unwrap_or_default();
    let _ = std::fs::write(&path, content);
}

/// Walk up from `cwd` to find the repo root (directory containing `.git` or `.devloop.yaml`).
pub fn find_repo_root(cwd: &str) -> Option<PathBuf> {
    let mut dir = PathBuf::from(cwd);
    loop {
        if dir.join(".devloop.yaml").exists() || dir.join(".git").exists() {
            return Some(dir);
        }
        if !dir.pop() {
            return None;
        }
    }
}

/// Load per-repo config from `.devloop.yaml` in the repo root.
pub fn load_repo_config(cwd: &str) -> Option<(PathBuf, RepoConfig)> {
    let repo_root = find_repo_root(cwd)?;
    let config_file = repo_root.join(".devloop.yaml");
    let content = std::fs::read_to_string(&config_file).ok()?;
    let config: RepoConfig = serde_yaml::from_str(&content).ok()?;
    Some((repo_root, config))
}

// ── Merge ──────────────────────────────────────────────────────

/// Merge: built-in defaults → global config → per-repo config.
///
/// Merge rules:
/// - `extra_patterns`: appended to existing list
/// - `remove_patterns`: subtracted from existing list
/// - `allow_patterns` / `file_allowlist`: appended
/// - `skip_gates`: subtracted from active gates
/// - Scalars (booleans, strings): later layer wins
pub fn load_merged(cwd: Option<&str>) -> MergedConfig {
    let global = load();
    let repo = cwd.and_then(load_repo_config);
    merge(&global, repo.as_ref().map(|(root, cfg)| (root.as_path(), cfg)))
}

fn merge(
    global: &AmbientConfig,
    repo: Option<(&std::path::Path, &RepoConfig)>,
) -> MergedConfig {
    let (repo_root, repo_cfg) = match repo {
        Some((root, cfg)) => (Some(root.to_path_buf()), Some(cfg)),
        None => (None, None),
    };

    // Enabled: global.enabled AND repo.ambient (if repo config exists)
    let enabled = global.enabled && repo_cfg.map_or(false, |r| r.ambient);

    // Merge deny_list: global extra/remove/allow + repo extra/remove/allow
    let mut deny_list_extra = global.deny_list.extra_patterns.clone();
    let mut deny_list_remove = global.deny_list.remove_patterns.clone();
    let mut deny_list_allow = global.deny_list.allow_patterns.clone();
    if let Some(repo) = repo_cfg {
        deny_list_extra.extend(repo.deny_list.extra_patterns.iter().cloned());
        deny_list_remove.extend(repo.deny_list.remove_patterns.iter().cloned());
        deny_list_allow.extend(repo.deny_list.allow_patterns.iter().cloned());
    }

    // Merge dangerous_ops: global extra/allow + repo extra/allow
    let mut dangerous_ops_extra = global.dangerous_ops.extra_patterns.clone();
    let mut dangerous_ops_allow = global.dangerous_ops.allow_patterns.clone();
    if let Some(repo) = repo_cfg {
        dangerous_ops_extra.extend(repo.dangerous_ops.extra_patterns.iter().cloned());
        dangerous_ops_allow.extend(repo.dangerous_ops.allow_patterns.iter().cloned());
    }

    // Merge secrets: global extra/allowlist + repo extra/allowlist
    let mut secrets_extra = global.secrets.extra_patterns.clone();
    let mut secrets_file_allowlist = global.secrets.file_allowlist.clone();
    if let Some(repo) = repo_cfg {
        secrets_extra.extend(repo.secrets.extra_patterns.iter().cloned());
        secrets_file_allowlist.extend(repo.secrets.file_allowlist.iter().cloned());
    }

    // Merge checkpoint: skip_gates subtracts from gates, scalars last-wins
    let mut checkpoint = global.checkpoint.clone();
    if let Some(repo) = repo_cfg {
        checkpoint
            .gates
            .retain(|g| !repo.checkpoint.skip_gates.contains(g));
        if let Some(cmd) = &repo.checkpoint.test_command {
            checkpoint.test_command = Some(cmd.clone());
        }
        if let Some(atdd) = repo.checkpoint.atdd_required {
            checkpoint.atdd_required = atdd;
        }
    }

    MergedConfig {
        enabled,
        tier1: global.tier1,
        tier2: global.tier2,
        ambient_mode: global.ambient_mode.clone(),
        deny_list_extra,
        deny_list_remove,
        deny_list_allow,
        dangerous_ops_extra,
        dangerous_ops_allow,
        secrets_extra,
        secrets_file_allowlist,
        daemon: global.daemon.clone(),
        observability: global.observability.clone(),
        checkpoint,
        continuity: global.continuity.clone(),
        event_log: global.event_log.clone(),
        loop_detection: global.loop_detection.clone(),
        repo_root,
    }
}

// ── Public API ─────────────────────────────────────────────────

pub fn is_enabled_tier1() -> bool {
    let config = load();
    config.enabled && config.tier1
}

pub fn is_enabled_tier2() -> bool {
    let config = load();
    config.enabled && config.tier2
}

pub fn enable(tier: Option<u8>) {
    let mut config = load();
    config.enabled = true;
    match tier {
        Some(1) => config.tier1 = true,
        Some(2) => config.tier2 = true,
        _ => {
            config.tier1 = true;
            config.tier2 = true;
        }
    }
    save(&config);
    match tier {
        Some(t) => println!("Ambient layer Tier {t} enabled"),
        None => println!("Ambient layer enabled (Tier 1 + Tier 2)"),
    }
}

pub fn disable() {
    let mut config = load();
    config.enabled = false;
    save(&config);
    println!("Ambient layer disabled (all hooks no-op)");
}

/// Dump merged config as YAML for debugging.
pub fn dump_config(dir: Option<&str>) {
    let cwd = dir
        .map(String::from)
        .unwrap_or_else(|| std::env::current_dir().unwrap_or_default().to_string_lossy().to_string());
    let merged = load_merged(Some(&cwd));

    // Print a readable summary
    println!("# Merged ambient config");
    if let Some(ref root) = merged.repo_root {
        println!("# Repo root: {}", root.display());
        println!("# Per-repo config: {}", root.join(".devloop.yaml").display());
    } else {
        println!("# No per-repo config found");
    }
    println!("# Global config: {}", config_path().display());
    println!();
    println!("{}", serde_yaml::to_string(&merged).unwrap_or_default());
}

// ── Lint ───────────────────────────────────────────────────────

#[derive(Debug)]
pub enum LintLevel {
    Error,
    Warning,
    Info,
}

impl std::fmt::Display for LintLevel {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            LintLevel::Error => write!(f, "ERROR"),
            LintLevel::Warning => write!(f, "WARN"),
            LintLevel::Info => write!(f, "INFO"),
        }
    }
}

#[derive(Debug)]
pub struct LintWarning {
    pub level: LintLevel,
    pub field: String,
    pub message: String,
}

pub const KNOWN_GATES: &[&str] = &["sanity", "semgrep", "secrets", "atdd", "review"];

pub fn lint(merged: &MergedConfig) -> Vec<LintWarning> {
    let mut warnings = Vec::new();

    // Check skip_gates references valid gate names
    let active_and_skipped: Vec<&str> = merged.checkpoint.gates.iter().map(|s| s.as_str()).collect();
    // We can't see skip_gates directly from merged (they're already subtracted),
    // but we can check that all active gates are known
    for gate in &merged.checkpoint.gates {
        if !KNOWN_GATES.contains(&gate.as_str()) {
            warnings.push(LintWarning {
                level: LintLevel::Warning,
                field: "checkpoint.gates".into(),
                message: format!("Unknown gate '{gate}'. Known: {}", KNOWN_GATES.join(", ")),
            });
        }
    }
    drop(active_and_skipped);

    // Context limit sanity
    if merged.continuity.context_limit < 10_000 {
        warnings.push(LintWarning {
            level: LintLevel::Warning,
            field: "continuity.context_limit".into(),
            message: format!(
                "Context limit {} is very low (< 10,000 tokens)",
                merged.continuity.context_limit
            ),
        });
    }
    if merged.continuity.context_limit > 1_000_000 {
        warnings.push(LintWarning {
            level: LintLevel::Warning,
            field: "continuity.context_limit".into(),
            message: format!(
                "Context limit {} is very high (> 1,000,000 tokens)",
                merged.continuity.context_limit
            ),
        });
    }

    // Warn threshold sanity
    if merged.continuity.context_warn_pct <= 0.0 || merged.continuity.context_warn_pct > 1.0 {
        warnings.push(LintWarning {
            level: LintLevel::Error,
            field: "continuity.context_warn_pct".into(),
            message: format!(
                "Warn threshold {} is out of range (must be 0.0 < x <= 1.0)",
                merged.continuity.context_warn_pct
            ),
        });
    }

    // Empty OTel credentials
    if merged.observability.openobserve_user.is_empty() {
        warnings.push(LintWarning {
            level: LintLevel::Info,
            field: "observability.openobserve_user".into(),
            message: "OTel user is empty — span export will silently fail".into(),
        });
    }

    // Tool availability: warn/error depending on fail_mode
    let fail_closed = merged.checkpoint.fail_mode == "closed";
    if merged.checkpoint.gates.contains(&"semgrep".to_string()) && !is_tool_on_path("semgrep") {
        warnings.push(LintWarning {
            level: if fail_closed { LintLevel::Error } else { LintLevel::Warning },
            field: "checkpoint.gates".into(),
            message: "Gate 'semgrep' is enabled but semgrep is not installed".into(),
        });
    }
    if merged.checkpoint.gates.contains(&"secrets".to_string())
        && !is_tool_on_path("betterleaks")
        && !is_tool_on_path("gitleaks")
    {
        warnings.push(LintWarning {
            level: if fail_closed { LintLevel::Error } else { LintLevel::Warning },
            field: "checkpoint.gates".into(),
            message: "Gate 'secrets' is enabled but neither betterleaks nor gitleaks is installed"
                .into(),
        });
    }

    // Invalid ambient_mode
    if !["enforce", "shadow", "disabled"].contains(&merged.ambient_mode.as_str()) {
        warnings.push(LintWarning {
            level: LintLevel::Error,
            field: "ambient_mode".into(),
            message: format!(
                "Invalid ambient_mode '{}' — must be 'enforce', 'shadow', or 'disabled'",
                merged.ambient_mode
            ),
        });
    }

    // Invalid fail_mode
    if merged.checkpoint.fail_mode != "open" && merged.checkpoint.fail_mode != "closed" {
        warnings.push(LintWarning {
            level: LintLevel::Error,
            field: "checkpoint.fail_mode".into(),
            message: format!(
                "Invalid fail_mode '{}' — must be 'open' or 'closed'",
                merged.checkpoint.fail_mode
            ),
        });
    }

    warnings
}

fn is_tool_on_path(name: &str) -> bool {
    std::process::Command::new("which")
        .arg(name)
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

pub fn lint_and_print(dir: Option<&str>) {
    let cwd = dir
        .map(String::from)
        .unwrap_or_else(|| std::env::current_dir().unwrap_or_default().to_string_lossy().to_string());
    let merged = load_merged(Some(&cwd));

    let warnings = lint(&merged);

    if warnings.is_empty() {
        println!("Config OK — no issues found.");
        return;
    }

    for w in &warnings {
        println!("[{}] {}: {}", w.level, w.field, w.message);
    }

    let errors = warnings.iter().filter(|w| matches!(w.level, LintLevel::Error)).count();
    let warns = warnings.iter().filter(|w| matches!(w.level, LintLevel::Warning)).count();
    let infos = warnings.iter().filter(|w| matches!(w.level, LintLevel::Info)).count();
    println!(
        "\n{} issue(s): {} error, {} warning, {} info",
        warnings.len(),
        errors,
        warns,
        infos
    );

    if errors > 0 {
        std::process::exit(1);
    }
}

// ── Tests ──────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_config_all_enabled() {
        let config = AmbientConfig::default();
        assert!(config.enabled);
        assert!(config.tier1);
        assert!(config.tier2);
    }

    #[test]
    fn parse_yaml_config() {
        let yaml = "enabled: true\ntier1: true\ntier2: false\n";
        let config: AmbientConfig = serde_yaml::from_str(yaml).unwrap();
        assert!(config.enabled);
        assert!(config.tier1);
        assert!(!config.tier2);
    }

    #[test]
    fn parse_partial_yaml_defaults() {
        let yaml = "enabled: false\n";
        let config: AmbientConfig = serde_yaml::from_str(yaml).unwrap();
        assert!(!config.enabled);
        assert!(config.tier1);
        assert!(config.tier2);
    }

    #[test]
    fn parse_empty_yaml_defaults() {
        let yaml = "{}";
        let config: AmbientConfig = serde_yaml::from_str(yaml).unwrap();
        assert!(config.enabled);
        assert!(config.tier1);
        assert!(config.tier2);
    }

    #[test]
    fn save_and_load_roundtrip() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("ambient.yaml");

        let config = AmbientConfig {
            enabled: true,
            tier1: false,
            ..Default::default()
        };
        let content = serde_yaml::to_string(&config).unwrap();
        std::fs::write(&path, &content).unwrap();

        let loaded: AmbientConfig =
            serde_yaml::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        assert!(loaded.enabled);
        assert!(!loaded.tier1);
        assert!(loaded.tier2);
    }

    #[test]
    fn parse_full_global_config() {
        let yaml = r#"
enabled: true
tier1: true
tier2: true
daemon:
  socket: /tmp/custom.sock
  auto_stop_minutes: 60
deny_list:
  extra_patterns: ["*.vault"]
dangerous_ops:
  allow_patterns:
    - "rm -rf node_modules"
    - "rm -rf dist"
secrets:
  file_allowlist: ["tests/fixtures/fake-key.pem"]
observability:
  openobserve_url: "http://localhost:5080"
checkpoint:
  gates: [sanity, semgrep, secrets]
  atdd_required: true
"#;
        let config: AmbientConfig = serde_yaml::from_str(yaml).unwrap();
        assert_eq!(config.daemon.socket, "/tmp/custom.sock");
        assert_eq!(config.daemon.auto_stop_minutes, 60);
        assert_eq!(config.deny_list.extra_patterns, vec!["*.vault"]);
        assert_eq!(config.dangerous_ops.allow_patterns.len(), 2);
        assert_eq!(config.secrets.file_allowlist, vec!["tests/fixtures/fake-key.pem"]);
        assert_eq!(config.checkpoint.gates.len(), 3);
        assert!(config.checkpoint.atdd_required);
    }

    #[test]
    fn parse_repo_config() {
        let yaml = r#"
ambient: true
deny_list:
  extra_patterns: ["config/production.yaml"]
  remove_patterns: [".npmrc"]
dangerous_ops:
  allow_patterns:
    - "docker compose down"
checkpoint:
  skip_gates: [review]
  test_command: "npm test"
  atdd_required: true
"#;
        let config: RepoConfig = serde_yaml::from_str(yaml).unwrap();
        assert!(config.ambient);
        assert_eq!(config.deny_list.extra_patterns, vec!["config/production.yaml"]);
        assert_eq!(config.deny_list.remove_patterns, vec![".npmrc"]);
        assert_eq!(config.dangerous_ops.allow_patterns, vec!["docker compose down"]);
        assert_eq!(config.checkpoint.skip_gates, vec!["review"]);
        assert_eq!(config.checkpoint.test_command.as_deref(), Some("npm test"));
        assert_eq!(config.checkpoint.atdd_required, Some(true));
    }

    #[test]
    fn merge_global_only() {
        let global = AmbientConfig {
            deny_list: PatternOverrides {
                extra_patterns: vec!["*.vault".into()],
                ..Default::default()
            },
            dangerous_ops: DangerousOpsOverrides {
                allow_patterns: vec!["rm -rf node_modules".into()],
                ..Default::default()
            },
            ..Default::default()
        };

        let merged = merge(&global, None);
        // No repo config → disabled (opt-in: repos must have .devloop.yaml)
        assert!(!merged.enabled);
        assert_eq!(merged.deny_list_extra, vec!["*.vault"]);
        assert!(merged.deny_list_remove.is_empty());
        assert_eq!(merged.dangerous_ops_allow, vec!["rm -rf node_modules"]);
        assert!(merged.repo_root.is_none());
    }

    #[test]
    fn merge_global_plus_repo() {
        let global = AmbientConfig {
            deny_list: PatternOverrides {
                extra_patterns: vec!["*.vault".into()],
                ..Default::default()
            },
            dangerous_ops: DangerousOpsOverrides {
                allow_patterns: vec!["rm -rf node_modules".into()],
                ..Default::default()
            },
            ..Default::default()
        };

        let repo = RepoConfig {
            ambient: true,
            deny_list: PatternOverrides {
                extra_patterns: vec!["config/production.yaml".into()],
                remove_patterns: vec![".npmrc".into()],
                ..Default::default()
            },
            dangerous_ops: DangerousOpsOverrides {
                allow_patterns: vec!["docker compose down".into()],
                ..Default::default()
            },
            secrets: SecretsOverrides {
                file_allowlist: vec!["tests/fixtures/fake-key.pem".into()],
                ..Default::default()
            },
            checkpoint: RepoCheckpointOverrides {
                skip_gates: vec!["review".into()],
                test_command: Some("npm test".into()),
                atdd_required: Some(true),
            },
            workflow: None,
            spec_required: None,
        };

        let repo_root = PathBuf::from("/home/user/my-repo");
        let merged = merge(&global, Some((repo_root.as_path(), &repo)));

        // Extra patterns are appended
        assert_eq!(merged.deny_list_extra, vec!["*.vault", "config/production.yaml"]);
        // Remove patterns from repo
        assert_eq!(merged.deny_list_remove, vec![".npmrc"]);
        // Allow patterns are appended
        assert_eq!(
            merged.dangerous_ops_allow,
            vec!["rm -rf node_modules", "docker compose down"]
        );
        // File allowlist from repo
        assert_eq!(merged.secrets_file_allowlist, vec!["tests/fixtures/fake-key.pem"]);
        // Skip gates subtracts from gates
        assert!(!merged.checkpoint.gates.contains(&"review".to_string()));
        assert!(merged.checkpoint.gates.contains(&"sanity".to_string()));
        // Scalar overrides
        assert_eq!(merged.checkpoint.test_command.as_deref(), Some("npm test"));
        assert!(merged.checkpoint.atdd_required);
        // Repo root tracked
        assert_eq!(merged.repo_root.as_deref(), Some(repo_root.as_path()));
    }

    #[test]
    fn merge_repo_disables_ambient() {
        let global = AmbientConfig::default();
        let repo = RepoConfig {
            ambient: false,
            deny_list: Default::default(),
            dangerous_ops: Default::default(),
            secrets: Default::default(),
            checkpoint: Default::default(),
            workflow: None,
            spec_required: None,
        };

        let merged = merge(&global, Some((std::path::Path::new("/repo"), &repo)));
        assert!(!merged.enabled);
    }

    #[test]
    fn find_repo_root_with_devloop_yaml() {
        let dir = tempfile::tempdir().unwrap();
        let sub = dir.path().join("src").join("deep");
        std::fs::create_dir_all(&sub).unwrap();
        std::fs::write(dir.path().join(".devloop.yaml"), "ambient: true\n").unwrap();

        let root = find_repo_root(sub.to_str().unwrap());
        assert_eq!(root, Some(dir.path().to_path_buf()));
    }

    #[test]
    fn find_repo_root_with_git() {
        let dir = tempfile::tempdir().unwrap();
        let sub = dir.path().join("src");
        std::fs::create_dir_all(&sub).unwrap();
        std::fs::create_dir(dir.path().join(".git")).unwrap();

        let root = find_repo_root(sub.to_str().unwrap());
        assert_eq!(root, Some(dir.path().to_path_buf()));
    }

    #[test]
    fn find_repo_root_none() {
        let dir = tempfile::tempdir().unwrap();
        let sub = dir.path().join("nowhere");
        std::fs::create_dir_all(&sub).unwrap();

        let root = find_repo_root(sub.to_str().unwrap());
        assert!(root.is_none());
    }

    #[test]
    fn load_repo_config_from_file() {
        let dir = tempfile::tempdir().unwrap();
        let sub = dir.path().join("src");
        std::fs::create_dir_all(&sub).unwrap();
        std::fs::create_dir(dir.path().join(".git")).unwrap();
        std::fs::write(
            dir.path().join(".devloop.yaml"),
            "ambient: true\ndeny_list:\n  extra_patterns: [\"*.custom\"]\n",
        )
        .unwrap();

        let result = load_repo_config(sub.to_str().unwrap());
        assert!(result.is_some());
        let (root, cfg) = result.unwrap();
        assert_eq!(root, dir.path().to_path_buf());
        assert!(cfg.ambient);
        assert_eq!(cfg.deny_list.extra_patterns, vec!["*.custom"]);
    }

    #[test]
    fn parse_continuity_config() {
        let yaml = "continuity:\n  context_limit: 300000\n  context_warn_pct: 0.9\n";
        let config: AmbientConfig = serde_yaml::from_str(yaml).unwrap();
        assert_eq!(config.continuity.context_limit, 300_000);
        assert!((config.continuity.context_warn_pct - 0.9).abs() < 0.001);
    }

    #[test]
    fn continuity_defaults() {
        let config = AmbientConfig::default();
        assert_eq!(config.continuity.context_limit, 200_000);
        assert!((config.continuity.context_warn_pct - 0.85).abs() < 0.001);
    }

    #[test]
    fn lint_clean_config() {
        let global = AmbientConfig {
            observability: ObservabilityConfig {
                openobserve_user: "admin".into(),
                ..Default::default()
            },
            // Use only gates that don't require external tools,
            // so the test passes in CI environments.
            checkpoint: CheckpointConfig {
                gates: vec!["sanity".into()],
                ..Default::default()
            },
            ..Default::default()
        };
        let merged = merge(&global, None);
        let warnings = lint(&merged);
        assert!(warnings.is_empty(), "Expected no warnings, got: {:?}", warnings.iter().map(|w| &w.message).collect::<Vec<_>>());
    }

    #[test]
    fn lint_low_context_limit() {
        let global = AmbientConfig {
            continuity: ContinuityConfig {
                context_limit: 5_000,
                ..Default::default()
            },
            ..Default::default()
        };
        let merged = merge(&global, None);
        let warnings = lint(&merged);
        assert!(warnings.iter().any(|w| w.field == "continuity.context_limit"));
    }

    #[test]
    fn lint_empty_otel_creds() {
        let global = AmbientConfig::default(); // empty OTel user by default
        let merged = merge(&global, None);
        let warnings = lint(&merged);
        assert!(warnings.iter().any(|w| w.field == "observability.openobserve_user"));
    }

    #[test]
    fn lint_invalid_fail_mode() {
        let global = AmbientConfig {
            checkpoint: CheckpointConfig {
                fail_mode: "invalid".into(),
                ..Default::default()
            },
            observability: ObservabilityConfig {
                openobserve_user: "admin".into(),
                ..Default::default()
            },
            ..Default::default()
        };
        let merged = merge(&global, None);
        let warnings = lint(&merged);
        assert!(warnings.iter().any(|w| w.field == "checkpoint.fail_mode"));
    }

    #[test]
    fn parse_checkpoint_fail_mode() {
        let yaml = "checkpoint:\n  fail_mode: closed\n  gate_timeout_s: 120\n";
        let config: AmbientConfig = serde_yaml::from_str(yaml).unwrap();
        assert_eq!(config.checkpoint.fail_mode, "closed");
        assert_eq!(config.checkpoint.gate_timeout_s, 120);
    }

    #[test]
    fn checkpoint_fail_mode_defaults_to_open() {
        let config = CheckpointConfig::default();
        assert_eq!(config.fail_mode, "open");
        assert_eq!(config.gate_timeout_s, 60);
    }

    #[test]
    fn parse_event_log_config() {
        let yaml = "event_log:\n  channel_capacity: 5000\n  max_file_size_mb: 100\n  max_rotated_files: 5\n";
        let config: AmbientConfig = serde_yaml::from_str(yaml).unwrap();
        assert_eq!(config.event_log.channel_capacity, 5000);
        assert_eq!(config.event_log.max_file_size_mb, 100);
        assert_eq!(config.event_log.max_rotated_files, 5);
    }

    #[test]
    fn event_log_config_defaults() {
        let config = EventLogConfig::default();
        assert_eq!(config.channel_capacity, 10_000);
        assert_eq!(config.max_file_size_mb, 50);
        assert_eq!(config.max_rotated_files, 3);
    }

    #[test]
    fn parse_ambient_mode_shadow() {
        let yaml = "ambient_mode: shadow\n";
        let config: AmbientConfig = serde_yaml::from_str(yaml).unwrap();
        assert_eq!(config.ambient_mode, "shadow");
    }

    #[test]
    fn ambient_mode_defaults_to_enforce() {
        let config = AmbientConfig::default();
        assert_eq!(config.ambient_mode, "enforce");
    }

    #[test]
    fn lint_invalid_ambient_mode() {
        let global = AmbientConfig {
            ambient_mode: "invalid".into(),
            observability: ObservabilityConfig {
                openobserve_user: "admin".into(),
                ..Default::default()
            },
            ..Default::default()
        };
        let merged = merge(&global, None);
        let warnings = lint(&merged);
        assert!(warnings.iter().any(|w| w.field == "ambient_mode"));
    }

    #[test]
    fn merged_config_includes_ambient_mode() {
        let global = AmbientConfig {
            ambient_mode: "shadow".into(),
            ..Default::default()
        };
        let merged = merge(&global, None);
        assert_eq!(merged.ambient_mode, "shadow");
    }

    #[test]
    fn backward_compat_simple_yaml() {
        // Phase 3 config files (just enabled/tier1/tier2) should still parse
        let yaml = "enabled: true\ntier1: true\ntier2: false\n";
        let config: AmbientConfig = serde_yaml::from_str(yaml).unwrap();
        assert!(config.enabled);
        assert!(config.tier1);
        assert!(!config.tier2);
        // New sections default to empty/defaults
        assert!(config.deny_list.extra_patterns.is_empty());
        assert!(config.dangerous_ops.allow_patterns.is_empty());
        assert_eq!(config.checkpoint.gates.len(), 5);
    }

    #[test]
    fn config_hash_same_config_same_hash() {
        let global = AmbientConfig::default();
        let merged1 = merge(&global, None);
        let merged2 = merge(&global, None);
        assert_eq!(merged1.config_hash(), merged2.config_hash());
    }

    #[test]
    fn config_hash_different_patterns_different_hash() {
        let global1 = AmbientConfig::default();
        let merged1 = merge(&global1, None);

        let global2 = AmbientConfig {
            deny_list: PatternOverrides {
                extra_patterns: vec!["*.vault".into()],
                ..Default::default()
            },
            ..Default::default()
        };
        let merged2 = merge(&global2, None);

        assert_ne!(merged1.config_hash(), merged2.config_hash());
    }

    #[test]
    fn config_hash_is_16_hex_chars() {
        let global = AmbientConfig::default();
        let merged = merge(&global, None);
        let hash = merged.config_hash();
        assert_eq!(hash.len(), 16);
        assert!(hash.chars().all(|c| c.is_ascii_hexdigit()));
    }

    #[test]
    fn loop_detection_config_defaults() {
        let config = LoopDetectionConfig::default();
        assert!(config.enabled);
        assert_eq!(config.window_secs, 120);
        assert_eq!(config.threshold, 5);
    }

    #[test]
    fn parse_loop_detection_config() {
        let yaml = "loop_detection:\n  enabled: false\n  window_secs: 60\n  threshold: 3\n";
        let config: AmbientConfig = serde_yaml::from_str(yaml).unwrap();
        assert!(!config.loop_detection.enabled);
        assert_eq!(config.loop_detection.window_secs, 60);
        assert_eq!(config.loop_detection.threshold, 3);
    }
}
