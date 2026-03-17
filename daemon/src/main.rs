mod check;
mod checkpoint;
mod cli;
mod config;
mod continuity;
mod daemon;
mod dashboard;
mod event_log;
mod feedback;
mod hook;
mod install;
mod otel;
mod override_mgr;
mod rules_md;
mod server;
mod session;
mod shadow_report;
mod sse;
mod traces;
mod transcript;

use check::{CheckEngine, CheckRequest};
use clap::Parser;
use cli::{Cli, Command, HookCommand};

fn main() {
    let cli = Cli::parse();

    // Only init tracing for non-hook commands.
    // Hooks use stdout for JSON protocol — must stay silent on stderr.
    if !matches!(cli.command, Command::Hook { .. }) {
        tracing_subscriber::fmt()
            .with_env_filter(
                tracing_subscriber::EnvFilter::from_default_env()
                    .add_directive("dl=info".parse().unwrap()),
            )
            .with_target(false)
            .init();
    }

    match cli.command {
        Command::Start => daemon::start(),
        Command::Stop => daemon::stop(),
        Command::Status => daemon::status(),
        Command::Stream => daemon::stream(),
        Command::Check { json } => run_check(&json),
        Command::Hook { hook } => match hook {
            HookCommand::PreToolUse => hook::pre_tool_use(),
            HookCommand::PostToolUse => hook::post_tool_use(),
            HookCommand::SessionStart => hook::session_start(),
            HookCommand::SessionEnd => hook::session_end(),
            HookCommand::Stop => hook::stop(),
            HookCommand::PreCompact => hook::pre_compact(),
        },
        Command::Install => install::install(),
        Command::Uninstall => install::uninstall(),
        Command::Enable { tier } => config::enable(tier),
        Command::Disable => config::disable(),
        Command::Config { dir } => config::dump_config(dir.as_deref()),
        Command::AllowOnce { pattern, ttl } => override_mgr::allow_once(&pattern, Some(ttl)),
        Command::Traces { last } => traces::show_traces(last),
        Command::DashboardValidate => dashboard::validate(None),
        Command::Rules => rules_md::print_rules(),
        Command::Outcome {
            session_id,
            outcome,
            notes,
        } => continuity::record_outcome(&session_id, &outcome, notes.as_deref()),
        Command::ConfigLint { dir } => config::lint_and_print(dir.as_deref()),
        Command::Reload => daemon::reload(),
        Command::Checkpoint { dir, json } => run_checkpoint_cli(dir.as_deref(), json),
        Command::Feedback {
            event_id,
            label,
            notes,
            list,
            stats,
            last,
        } => {
            if stats {
                feedback::show_stats();
            } else if list {
                feedback::list_unlabeled(last);
            } else {
                match (event_id, label) {
                    (Some(eid), Some(lbl)) => {
                        feedback::annotate(&eid, &lbl, notes.as_deref())
                    }
                    _ => {
                        eprintln!("Usage: dl feedback <event-id> <label> [--notes \"...\"]");
                        eprintln!("       dl feedback --list [--last N]");
                        eprintln!("       dl feedback --stats");
                        eprintln!("\nLabels: correct, false-positive, missed");
                        std::process::exit(1);
                    }
                }
            }
        }
        Command::ShadowReport { last, csv } => shadow_report::report(last, csv),
    }
}

/// Offline checkpoint — runs Tier 2 gate suite without a daemon.
fn run_checkpoint_cli(dir: Option<&str>, json_output: bool) {
    let cwd = dir
        .map(String::from)
        .unwrap_or_else(|| std::env::current_dir().unwrap().to_string_lossy().to_string());

    let merged = config::load_merged(Some(&cwd));
    let result = checkpoint::run_checkpoint(&cwd, &merged.checkpoint);

    if json_output {
        println!("{}", serde_json::to_string_pretty(&result).unwrap());
    } else {
        let status = if result.passed { "PASSED" } else { "FAILED" };
        println!("Checkpoint: {status} ({}/{} gates passed, {}ms)",
            result.gates_passed, result.gates_run, result.duration_ms);
        for gr in &result.gate_results {
            let icon = if gr.passed { "✓" } else { "✗" };
            println!("  {icon} {} ({}ms){}", gr.gate, gr.duration_ms,
                gr.reason.as_ref().map(|r| format!(" — {r}")).unwrap_or_default());
            for f in &gr.findings {
                println!("    {f}");
            }
        }
        if let Some(trailer) = &result.trailer {
            println!("\n{trailer}");
        }
    }

    if !result.passed {
        std::process::exit(1);
    }
}

/// Offline check — runs the check engine locally without a daemon.
fn run_check(json: &str) {
    let request: CheckRequest = match serde_json::from_str(json) {
        Ok(r) => r,
        Err(e) => {
            eprintln!("Invalid JSON: {e}");
            eprintln!("Expected: {{\"tool_name\":\"Write\",\"tool_input\":{{\"file_path\":\".env\"}}}}");
            std::process::exit(1);
        }
    };

    let engine = CheckEngine::new();
    let result = engine.check(&request);

    println!("{}", serde_json::to_string_pretty(&result).unwrap());

    // Exit with non-zero if blocked
    if result.action == check::Action::Block {
        std::process::exit(1);
    }
}
