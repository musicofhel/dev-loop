use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(name = "dl", about = "dev-loop ambient layer daemon")]
pub struct Cli {
    #[command(subcommand)]
    pub command: Command,
}

#[derive(Subcommand)]
pub enum Command {
    /// Start the daemon in background
    Start,
    /// Stop the daemon gracefully
    Stop,
    /// Show daemon health + active sessions
    Status,
    /// Tail the SSE event stream
    Stream,
    /// Test a check against the engine (offline, no daemon needed)
    Check {
        /// JSON string: {"tool_name":"Write","tool_input":{"file_path":".env"}}
        json: String,
    },
    /// Claude Code hook commands (called by hook system, not directly)
    Hook {
        #[command(subcommand)]
        hook: HookCommand,
    },
    /// Install dl hooks into ~/.claude/settings.json
    Install,
    /// Remove dl hooks from ~/.claude/settings.json
    Uninstall,
    /// Enable the ambient layer
    Enable {
        /// Enable specific tier only (1 or 2)
        #[arg(long)]
        tier: Option<u8>,
    },
    /// Disable the ambient layer (hooks installed but no-op)
    Disable,
    /// Dump merged configuration (built-in + global + per-repo)
    Config {
        /// Directory to resolve per-repo config (defaults to cwd)
        #[arg(long)]
        dir: Option<String>,
    },
    /// Temporarily allow a blocked file pattern (expires after match or TTL)
    #[command(name = "allow-once")]
    AllowOnce {
        /// Glob pattern to allow (e.g. ".env", "*.key")
        pattern: String,
        /// TTL in seconds (default: 300 = 5 minutes)
        #[arg(long, default_value = "300")]
        ttl: u64,
    },
    /// Show recent events from the JSONL event log
    Traces {
        /// Number of events to show
        #[arg(long, default_value = "20")]
        last: usize,
    },
    /// Validate dashboard panel SQL queries against OpenObserve
    #[command(name = "dashboard-validate")]
    DashboardValidate,
    /// Show active ambient layer rules
    Rules,
    /// Record session outcome (success, partial, or fail)
    Outcome {
        /// Session ID (from handoff or dl status)
        session_id: String,
        /// Outcome: success, partial, or fail
        outcome: String,
        /// Optional notes about the outcome
        #[arg(long)]
        notes: Option<String>,
    },
    /// Lint configuration for errors and warnings
    #[command(name = "config-lint")]
    ConfigLint {
        /// Directory to resolve per-repo config (defaults to cwd)
        #[arg(long)]
        dir: Option<String>,
    },
    /// Reload daemon configuration (sends SIGHUP)
    Reload,
    /// Run checkpoint gates offline (no daemon needed)
    #[command(name = "checkpoint")]
    Checkpoint {
        /// Directory to run checkpoint on (defaults to cwd)
        #[arg(long)]
        dir: Option<String>,
        /// Output JSON instead of human-readable
        #[arg(long)]
        json: bool,
    },
    /// Annotate check events with feedback labels (correct/false-positive/missed)
    Feedback {
        /// Event ID (line number, e.g., "42" or "L42")
        event_id: Option<String>,
        /// Label: correct, false-positive, or missed
        label: Option<String>,
        /// Optional notes about the annotation
        #[arg(long)]
        notes: Option<String>,
        /// List recent unlabeled block/warn events for review
        #[arg(long)]
        list: bool,
        /// Show labeled data statistics (precision/recall/F1 per check type)
        #[arg(long)]
        stats: bool,
        /// Number of events to show with --list (default: 20)
        #[arg(long, default_value = "20")]
        last: usize,
    },
    /// Show shadow mode verdict analysis
    #[command(name = "shadow-report")]
    ShadowReport {
        /// Only show verdicts from the last N hours
        #[arg(long)]
        last: Option<u64>,
        /// Output as CSV instead of human-readable table
        #[arg(long)]
        csv: bool,
    },
}

#[derive(Subcommand)]
pub enum HookCommand {
    /// PreToolUse hook: check before tool execution
    #[command(name = "pre-tool-use")]
    PreToolUse,
    /// PostToolUse hook: check after tool execution
    #[command(name = "post-tool-use")]
    PostToolUse,
    /// Session start hook (differentiated: fresh vs resume)
    #[command(name = "session-start")]
    SessionStart,
    /// Session end hook (writes final handoff + outcome)
    #[command(name = "session-end")]
    SessionEnd,
    /// Stop hook: 85% context guard + handoff writer
    #[command(name = "stop")]
    Stop,
    /// PreCompact hook: write handoff YAML before compaction
    #[command(name = "pre-compact")]
    PreCompact,
}
