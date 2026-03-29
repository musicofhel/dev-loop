# dev-loop justfile
# Run `just --list` to see all commands

# Default: show help
default:
    @just --list

# ─── Stack Management ───

# Start all services (OpenObserve, etc.)
stack-up:
    @echo "Starting OpenObserve..."
    docker run -d \
      --name dev-loop-openobserve \
      -p 5080:5080 \
      -v dev-loop-openobserve-data:/data \
      -e ZO_ROOT_USER_EMAIL=admin@dev-loop.local \
      -e ZO_ROOT_USER_PASSWORD=devloop123 \
      public.ecr.aws/zinclabs/openobserve:latest 2>/dev/null || \
      docker start dev-loop-openobserve
    @echo "OpenObserve running at http://localhost:5080"

# Stop all services
stack-down:
    docker stop dev-loop-openobserve 2>/dev/null || true
    @echo "Stack stopped"

# Check service health
stack-health:
    @echo "=== OpenObserve ===" && curl -s http://localhost:5080/healthz && echo
    @echo "=== Langfuse ===" && curl -s http://localhost:3001/api/public/health 2>/dev/null && echo || echo "  NOT RUNNING"
    @echo "=== Beads ===" && br stats --quiet 2>/dev/null && echo "  OK" || echo "  NOT INITIALIZED"
    @echo "=== Anthropic API ===" && \
        if [ -z "${ANTHROPIC_API_KEY:-}" ]; then \
            echo "  NOT SET (ANTHROPIC_API_KEY empty)"; \
        else \
            echo "  Key present (${#ANTHROPIC_API_KEY} chars)"; \
        fi

# Import dashboards and alerts into OpenObserve
stack-import:
    uv run python scripts/import-dashboards.py --delete-existing
    uv run python scripts/import-alerts.py --delete-existing

# ─── Beads (Issue Tracking) ───

# Show what's ready to work on
ready:
    br ready

# Show full dependency graph
graph:
    br graph --all

# Show project stats
beads-stats:
    br stats

# Show blocked issues
blocked:
    br blocked

# ─── Tracer Bullets ───

# TB-1: Ticket-to-PR (golden path)
# Usage: just tb1 <issue_id> <repo_path>
# Example: just tb1 dl-abc /home/musicofhel/prompt-bench
tb1 ISSUE_ID REPO_PATH:
    @echo "Running TB-1: Ticket-to-PR"
    @echo "Issue: {{ISSUE_ID}} | Repo: {{REPO_PATH}}"
    uv run python -c "from devloop.feedback.pipeline import run_tb1; import json; print(json.dumps(run_tb1('{{ISSUE_ID}}', '{{REPO_PATH}}'), indent=2))"

# TB-2: Failure-to-retry (feedback path)
# Usage: just tb2 <issue_id> <repo_path>
# Force first gate failure: just tb2-force <issue_id> <repo_path>
tb2 ISSUE_ID REPO_PATH:
    @echo "Running TB-2: Failure-to-Retry"
    @echo "Issue: {{ISSUE_ID}} | Repo: {{REPO_PATH}}"
    uv run python -c "from devloop.feedback.pipeline import run_tb2; import json; print(json.dumps(run_tb2('{{ISSUE_ID}}', '{{REPO_PATH}}'), indent=2))"

# TB-2 with forced first-attempt gate failure (deterministic retry path)
tb2-force ISSUE_ID REPO_PATH:
    @echo "Running TB-2: Failure-to-Retry (FORCED FIRST FAILURE)"
    @echo "Issue: {{ISSUE_ID}} | Repo: {{REPO_PATH}}"
    uv run python -c "from devloop.feedback.pipeline import run_tb2; import json; print(json.dumps(run_tb2('{{ISSUE_ID}}', '{{REPO_PATH}}', force_gate_fail=True), indent=2))"

# TB-3: Security gate (safety path)
# Usage: just tb3 <issue_id> <repo_path>
# Organic (no vuln seed): just tb3-organic <issue_id> <repo_path>
tb3 ISSUE_ID REPO_PATH:
    @echo "Running TB-3: Security-Gate-to-Fix (seeded vulnerability)"
    @echo "Issue: {{ISSUE_ID}} | Repo: {{REPO_PATH}}"
    uv run python -c "from devloop.feedback.pipeline import run_tb3; import json; print(json.dumps(run_tb3('{{ISSUE_ID}}', '{{REPO_PATH}}'), indent=2))"

# TB-3 without pre-seeded vulnerability (organic — relies on agent writing vuln code)
tb3-organic ISSUE_ID REPO_PATH:
    @echo "Running TB-3: Security-Gate-to-Fix (organic — no seed)"
    @echo "Issue: {{ISSUE_ID}} | Repo: {{REPO_PATH}}"
    uv run python -c "from devloop.feedback.pipeline import run_tb3; import json; print(json.dumps(run_tb3('{{ISSUE_ID}}', '{{REPO_PATH}}', force_vuln_seed=False), indent=2))"

# TB-4: Runaway-to-stop (turn control path)
# Usage: just tb4 <issue_id> <repo_path>
tb4 ISSUE_ID REPO_PATH:
    @echo "Running TB-4: Runaway-to-Stop"
    @echo "Issue: {{ISSUE_ID}} | Repo: {{REPO_PATH}}"
    uv run python -c "from devloop.feedback.pipeline import run_tb4; import json; print(json.dumps(run_tb4('{{ISSUE_ID}}', '{{REPO_PATH}}'), indent=2))"

# TB-4 with explicit turn limit override
# Usage: just tb4-turns <issue_id> <repo_path> <max_turns>
tb4-turns ISSUE_ID REPO_PATH MAX_TURNS:
    @echo "Running TB-4: Runaway-to-Stop (turn limit: {{MAX_TURNS}})"
    @echo "Issue: {{ISSUE_ID}} | Repo: {{REPO_PATH}}"
    uv run python -c "from devloop.feedback.pipeline import run_tb4; import json; print(json.dumps(run_tb4('{{ISSUE_ID}}', '{{REPO_PATH}}', turns_override={{MAX_TURNS}}), indent=2))"

# TB-5: Cross-repo cascade (multi-project path)
# Usage: just tb5 <source_issue_id> <source_repo_path> <target_repo_path>
# Example: just tb5 dl-abc ~/prompt-bench ~/omniswipe-backend
tb5 SOURCE_ISSUE SOURCE_REPO TARGET_REPO:
    @echo "Running TB-5: Cross-Repo Cascade"
    @echo "Source: {{SOURCE_ISSUE}} | {{SOURCE_REPO}} → {{TARGET_REPO}}"
    uv run python -c "from devloop.feedback.pipeline import run_tb5; import json; print(json.dumps(run_tb5('{{SOURCE_ISSUE}}', '{{SOURCE_REPO}}', '{{TARGET_REPO}}'), indent=2))"

# TB-6: Session replay (observability path)
# Usage: just tb6 <issue_id> <repo_path>
# Example: just tb6 dl-abc ~/prompt-bench
tb6 ISSUE_ID REPO_PATH:
    @echo "Running TB-6: Session Replay Debug"
    @echo "Issue: {{ISSUE_ID}} | Repo: {{REPO_PATH}}"
    uv run python -c "from devloop.feedback.pipeline import run_tb6; import json; print(json.dumps(run_tb6('{{ISSUE_ID}}', '{{REPO_PATH}}'), indent=2))"

# TB-6 replay: display a saved session
# Usage: just tb6-replay <session_id>
tb6-replay SESSION_ID:
    @echo "Replaying session: {{SESSION_ID}}"
    uv run python -c "from devloop.feedback.pipeline import replay_session; r = replay_session('{{SESSION_ID}}'); print(r['timeline'])"

# TB-7: LLMOps A/B comparison (DSPy optimized vs CLI baseline)
# Usage: just tb7 <repo_path>
# Example: just tb7 ~/prompt-bench
tb7 REPO_PATH:
    @echo "Running TB-7: LLMOps A/B Comparison"
    @echo "Repo: {{REPO_PATH}}"
    uv run python -c "from devloop.feedback.pipeline import run_tb7; import json; print(json.dumps(run_tb7('{{REPO_PATH}}'), indent=2))"

# Run the priority scheduler (multi-issue autonomous mode)
schedule REPO_PATH:
    uv run python -m devloop.orchestration.scheduler {{REPO_PATH}}

# Check scheduler status
schedule-status:
    uv run python -c "from devloop.orchestration.scheduler import count_active_agents, get_budget_usage_pct, load_scheduler_config; c = load_scheduler_config(); print(f'Active: {count_active_agents()}, Budget: {get_budget_usage_pct(c):.1f}%')"

# Stress test: run all 6 TBs N times (default 30)
stress *ARGS:
    uv run python scripts/stress-test.py {{ARGS}}

# Run all passing tracer bullets (using fixtures)
tb-all:
    @echo "Running all tracer bullets with fixtures..."
    just tb1-mock test-fixtures/tickets/tb1-sample.yaml
    just tb1-mock test-fixtures/tickets/tb2-failure.yaml
    just tb1-mock test-fixtures/tickets/tb3-vulnerability.yaml

# Run full smoke suite (all test tiers)
smoke:
    @echo "=== Rust tests ==="
    cd daemon && cargo test --quiet
    @echo "=== Python tests ==="
    uv run pytest tests/ -q
    @echo "=== Conformance ==="
    uv run python scripts/conformance/run_conformance.py tests/conformance/pre_tool_use.yaml tests/conformance/post_tool_use.yaml
    @echo "=== Tier 2 ==="
    uv run pytest tests/tier2/ -q
    @echo "=== All smoke passed ==="

# ─── Feedback Channels ───

# Channel 2: Detect repeated failure patterns
patterns *HOURS:
    uv run python -c "from devloop.feedback.pattern_detector import detect_patterns; import json; print(json.dumps(detect_patterns(hours=int('{{HOURS}}' or '24')), indent=2))"

# Channel 3: Usage summary (turns, tokens)
usage *HOURS:
    uv run python -c "from devloop.feedback.cost_monitor import get_usage_summary, check_budget; import json; s = get_usage_summary(hours=int('{{HOURS}}' or '24')); b = check_budget(s); print(json.dumps({'summary': s, 'budget': b}, indent=2))"

# Channel 5: Generate changelog from closed issues
changelog *DAYS:
    uv run python -c "from devloop.feedback.changelog import generate_changelog; r = generate_changelog(days=int('{{DAYS}}' or '7')); print(r['markdown'])"

# Channel 7: Analyze session efficiency
efficiency SESSION_ID:
    uv run python -c "from devloop.feedback.pipeline import _load_session; from devloop.feedback.efficiency import analyze_efficiency; import json; s = _load_session('{{SESSION_ID}}'); print(json.dumps(analyze_efficiency(s['events']), indent=2))"

# ─── Scoring ───

# Evaluate all tools against scoring rubric (uses feedback score data)
score:
    uv run python scripts/feedback/score.py

# Score a specific tool
score-tool TOOL:
    @echo "Scoring tool: {{TOOL}}"
    uv run python scripts/feedback/score.py --tool "{{TOOL}}"

# ─── Safety ───

# EMERGENCY: Kill all agents, pause intake, preserve worktrees
emergency-stop:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "!!! EMERGENCY STOP !!!"
    echo ""
    echo "Killing all claude processes..."
    pkill -f "claude" 2>/dev/null && echo "  Killed." || echo "  No claude processes found."
    echo ""
    echo "Marking in-progress beads issues as interrupted..."
    ids=$(br list --status in_progress --json 2>/dev/null | python3 -c "import sys,json; print(' '.join(i['id'] for i in json.load(sys.stdin)))" 2>/dev/null || true)
    if [ -n "$ids" ]; then
        for id in $ids; do
            br update "$id" --status open --add-label interrupted 2>/dev/null \
                && echo "  $id → open (interrupted)" \
                || echo "  $id — failed to update"
        done
    else
        echo "  No in-progress issues found."
    fi
    echo ""
    echo "Worktrees preserved for forensics."
    echo "Run 'just status' to see state. Run 'just recover' to clean up."

# Recover from crashed/interrupted runs
recover:
    @echo "=== Recovery scan ==="
    @echo "Checking for orphaned worktrees..."
    @find /tmp/dev-loop/worktrees -name ".dev-loop-metadata.json" -mmin +60 2>/dev/null || echo "  No worktree directory found"
    @echo "Checking for stuck issues..."
    @br stale --days 1 2>/dev/null || echo "  No stale issues"
    @echo "Run 'just worktree-gc' to clean up orphaned worktrees"

# Clean up orphaned worktrees older than 24h
worktree-gc:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "Scanning for orphaned worktrees..."
    orphans=$(find /tmp/dev-loop/worktrees -maxdepth 1 -mmin +1440 -type d 2>/dev/null || true)
    if [ -z "$orphans" ]; then
        echo "  No orphans found"
        exit 0
    fi
    for wt in $orphans; do
        # Check for uncommitted work
        if [ -d "$wt/.git" ] || [ -f "$wt/.git" ]; then
            changes=$(cd "$wt" && git status --porcelain 2>/dev/null | wc -l || echo "0")
            if [ "$changes" -gt 0 ]; then
                echo "  SKIP $wt ($changes uncommitted changes)"
                continue
            fi
        fi
        echo "  DELETE $wt"
        rm -rf "$wt"
    done
    echo "Done."

# ─── Utilities ───

# Bypass beads — run agent directly on a repo
run-direct REPO TASK:
    @echo "Direct run on {{REPO}}: {{TASK}}"
    uv run python -c "from devloop.runtime.server import spawn_agent; import json; print(json.dumps(spawn_agent('{{REPO}}', '{{TASK}}'), indent=2))"

# Run TB-1 with mock intake (beads fixture)
tb1-mock FIXTURE="test-fixtures/tickets/tb1-sample.yaml":
    @echo "Running TB-1 with mock intake: {{FIXTURE}}"
    uv run python -m devloop.cli tb1-mock {{FIXTURE}}

# List all agent sessions
sessions-list *ARGS:
    #!/usr/bin/env bash
    sessions_dir="${HOME}/.local/share/dev-loop/sessions"
    if [ ! -d "$sessions_dir" ]; then
        sessions_dir="/tmp/dev-loop/sessions"
    fi
    if [ ! -d "$sessions_dir" ]; then
        echo "No sessions directory found"
        exit 0
    fi
    echo "Sessions in $sessions_dir:"
    for f in "$sessions_dir"/*.yaml "$sessions_dir"/*.json; do
        [ -f "$f" ] || continue
        echo "  $(basename "$f")  $(stat -c '%y' "$f" 2>/dev/null | cut -d. -f1)"
    done

# View project status
status:
    @echo "=== dev-loop status ==="
    @echo ""
    @echo "Beads:"
    @br count --by-status 2>/dev/null || echo "  NOT INITIALIZED"
    @echo ""
    @echo "Ready to work:"
    @br ready 2>/dev/null | head -5 || echo "  None"
    @echo ""
    @echo "Services:"
    @docker inspect -f '{{{{.State.Status}}}}' dev-loop-openobserve 2>/dev/null || echo "  OpenObserve: NOT RUNNING"

# Generate docs table of contents
docs-toc:
    @echo "# dev-loop Documentation"
    @echo ""
    @echo "## Architecture"
    @echo "- [Architecture Overview](docs/architecture.md)"
    @echo "- [Tracer Bullets](docs/tracer-bullets.md)"
    @echo "- [Scoring Rubric](docs/scoring-rubric.md)"
    @echo "- [Test Repos](docs/test-repos.md)"
    @echo ""
    @echo "## Layers"
    @for f in docs/layers/*.md; do echo "- [$$(head -1 $$f | sed 's/# //')]($$f)"; done
    @echo ""
    @echo "## ADRs"
    @for f in docs/adrs/*.md; do echo "- [$$(head -1 $$f | sed 's/# //')]($$f)"; done

# ─── Calibration ───

# Update semgrep AI security rules
semgrep-rules-update:
    cd ~/.local/share/semgrep-ai-rules && git pull --ff-only

# Run hook conformance tests
conformance:
    uv run python scripts/conformance/run_conformance.py tests/conformance/pre_tool_use.yaml tests/conformance/post_tool_use.yaml

# Replay tool calls from real sessions through check engine (first N, default 500)
replay N="500":
    uv run python scripts/replay/parse_sessions.py ~/.claude/projects/-home-musicofhel/*.jsonl | head -{{N}} | uv run python scripts/replay/run_replay.py --raw --summarize > /dev/null

# Full replay with parallel workers (default 4) — outputs NDJSON + scores
replay-full WORKERS="4":
    uv run python scripts/replay/parse_sessions.py ~/.claude/projects/-home-musicofhel/*.jsonl | \
        uv run python scripts/replay/run_replay.py --raw --json --workers {{WORKERS}} | \
        uv run python scripts/replay/score.py --json

# Generate baseline from full replay (saves to scripts/replay/baselines/)
replay-baseline WORKERS="4":
    mkdir -p scripts/replay/baselines
    uv run python scripts/replay/parse_sessions.py ~/.claude/projects/-home-musicofhel/*.jsonl | \
        uv run python scripts/replay/run_replay.py --raw --json --workers {{WORKERS}} | \
        uv run python scripts/replay/score.py --json > scripts/replay/baselines/$(date +%Y-%m-%d)-baseline.json
    @echo "Baseline saved to scripts/replay/baselines/$(date +%Y-%m-%d)-baseline.json"

# Score replay against baseline (regression detection)
replay-score BASELINE:
    uv run python scripts/replay/parse_sessions.py ~/.claude/projects/-home-musicofhel/*.jsonl | \
        uv run python scripts/replay/run_replay.py --raw --json --workers 4 | \
        uv run python scripts/replay/score.py --json --baseline {{BASELINE}}

# Full replay stats (all tool calls from all sessions)
replay-stats:
    uv run python scripts/replay/parse_sessions.py ~/.claude/projects/-home-musicofhel/*.jsonl --stats

# Run replay harness tests
replay-test:
    uv run pytest tests/replay/test_replay.py -v

# ─── Feedback Loop ───

# Score labeled feedback data (precision/recall/F1 per check type)
feedback-score:
    uv run python scripts/feedback/score.py

# Score feedback as JSON
feedback-score-json:
    uv run python scripts/feedback/score.py --json

# Score + append to history
feedback-score-history:
    uv run python scripts/feedback/score.py --history

# Score against a baseline (regression detection)
feedback-score-baseline BASELINE:
    uv run python scripts/feedback/score.py --baseline {{BASELINE}}

# Suggest config tuning from feedback data
feedback-suggest:
    uv run python scripts/feedback/suggest_tuning.py

# Run feedback tests
feedback-test:
    uv run pytest tests/feedback/ -v

# ─── Calibration Pipeline ───

# Run full calibration pipeline (shadow + replay + tier2 + feedback + rust tests)
# Use --skip-rust to skip the Rust compilation+test stage
calibrate *ARGS:
    bash scripts/calibrate.sh {{ARGS}}

# Save current replay state as a baseline
calibrate-baseline WORKERS="4":
    mkdir -p scripts/replay/baselines scripts/feedback/baselines
    uv run python scripts/replay/parse_sessions.py ~/.claude/projects/-home-musicofhel/*.jsonl | \
        uv run python scripts/replay/run_replay.py --raw --json --workers {{WORKERS}} | \
        uv run python scripts/replay/score.py --json > scripts/replay/baselines/$(date +%Y-%m-%d)-baseline.json
    uv run python scripts/feedback/score.py --json > scripts/feedback/baselines/$(date +%Y-%m-%d)-baseline.json
    @echo "Baselines saved for $(date +%Y-%m-%d)"

# ─── Tier 2 Planted-Defect Suite ───

# Run planted-defect regression suite (13 scenarios)
tier2-test:
    uv run pytest tests/tier2/test_checkpoint_gates.py -v

# Run only secrets gate tests
tier2-secrets:
    uv run pytest tests/tier2/test_checkpoint_gates.py -v -k "TestSecretsGate"

# Run only semgrep gate tests
tier2-semgrep:
    uv run pytest tests/tier2/test_checkpoint_gates.py -v -k "TestSemgrepGate"

# Validate corpus YAML schema and structure
tier2-corpus-validate:
    uv run python scripts/tier2/validate_corpus.py

# ─── Daemon (dl) ───

# Build the Rust daemon (debug)
dl-build:
    cd daemon && cargo build

# Build the Rust daemon (release) and install to ~/.local/bin
dl-install:
    cd daemon && cargo build --release
    cp daemon/target/release/dl ~/.local/bin/dl
    @echo "Installed dl to ~/.local/bin/dl"

# Run daemon tests
dl-test:
    cd daemon && cargo test

# Start the daemon
dl-start:
    dl start

# Stop the daemon
dl-stop:
    dl stop

# Daemon status
dl-status:
    dl status

# ─── LLMOps (Layer 7) ───

# Export training data from session history
llmops-export:
    @echo "Exporting training data..."
    uv run python -m devloop.llmops.training.export_reviews
    uv run python -m devloop.llmops.training.export_retries
    uv run python -m devloop.llmops.training.export_personas
    @echo "Training data exported to ~/.local/share/dev-loop/llmops/training/"

# Export training data, forcing overwrite even if 0 examples
llmops-export-force:
    @echo "Exporting training data (force overwrite)..."
    uv run python -m devloop.llmops.training.export_reviews --force
    uv run python -m devloop.llmops.training.export_retries --force
    uv run python -m devloop.llmops.training.export_personas --force
    @echo "Training data exported (force mode)."

# Run GEPA optimization for a DSPy program
# Usage: just llmops-optimize code_review
llmops-optimize PROGRAM:
    @echo "Running GEPA optimization for {{PROGRAM}}..."
    uv run python -m devloop.llmops.optimize {{PROGRAM}}

# Run metric diagnostic on validation examples
llmops-diagnostic:
    uv run python scripts/llmops/metric_diagnostic.py

# Check optimization status for all programs
llmops-status:
    uv run python -c "from devloop.llmops.server import list_programs; import json; print(json.dumps(list_programs(), indent=2))"

# Start full stack (OpenObserve + Langfuse)
stack-up-full:
    docker compose up -d
    @echo "OpenObserve: http://localhost:5080"
    @echo "Langfuse:    http://localhost:3001"

# Stop full stack
stack-down-full:
    docker compose down

# ─── Python / uv ───

# Install/sync all dependencies
sync:
    uv sync

# Run linter
lint:
    uv run ruff check src/

# Run tests
test:
    uv run pytest

# Run tests with coverage
test-cov:
    uv run pytest --cov=devloop --cov-report=term-missing

# Format code
fmt:
    uv run ruff format src/

# Validate all YAML config files against schemas
validate-config:
    uv run python -c "from devloop.config_schemas import validate_all; r = validate_all(); [print(f'  {k}: {v}') for k,v in r.items()]"

# Validate harness config, imports, and core TB tests
self-test:
    @echo "=== Config validation ==="
    just validate-config
    @echo "=== Import check ==="
    uv run python -c "from devloop.feedback.pipeline import run_tb1, run_tb2, run_tb3, run_tb4, run_tb5, run_tb6, run_tb7; print('  All TB imports OK')"
    @echo "=== Core TB unit tests ==="
    uv run pytest tests/test_tb1.py tests/test_tb5.py tests/test_tb6.py tests/test_config_validation.py -q
    @echo "=== Self-test passed ==="
