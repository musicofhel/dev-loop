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
    @echo "=== Beads ===" && br stats --quiet 2>/dev/null && echo "  OK" || echo "  NOT INITIALIZED"
    @echo "=== Anthropic API ===" && echo "TODO: verify Anthropic API key"

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
tb2 *ARGS:
    @echo "Running TB-2: Failure-to-Retry"
    @echo "Requires: TB-1 passing"
    @echo "Args: {{ARGS}}"

# TB-3: Security gate (safety path)
tb3 *ARGS:
    @echo "Running TB-3: Security-Gate-to-Fix"
    @echo "Requires: TB-1 + TB-2 passing"
    @echo "Args: {{ARGS}}"

# TB-4: Cost control (budget path)
tb4 *ARGS:
    @echo "Running TB-4: Cost-Spike-to-Pause"
    @echo "Requires: TB-1 passing"
    @echo "Args: {{ARGS}}"

# TB-5: Cross-repo cascade (multi-project path)
tb5 *ARGS:
    @echo "Running TB-5: Cross-Repo Cascade"
    @echo "Requires: TB-1 passing on 2+ repos"
    @echo "Args: {{ARGS}}"

# TB-6: Session replay (observability path)
tb6 *ARGS:
    @echo "Running TB-6: Session Replay Debug"
    @echo "Requires: TB-2 passing"
    @echo "Args: {{ARGS}}"

# Run all passing tracer bullets
tb-all:
    @echo "Running all tracer bullets..."
    @echo "TODO: run only TBs that have been implemented"

# ─── Scoring ───

# Evaluate all tools against scoring rubric
score:
    @echo "Tool scoring not yet implemented"
    @echo "See docs/scoring-rubric.md for rubric"

# Score a specific tool
score-tool TOOL:
    @echo "Scoring tool: {{TOOL}}"
    @echo "TODO: implement interactive scoring"

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
    @echo "Scanning for orphaned worktrees..."
    @find /tmp/dev-loop/worktrees -maxdepth 1 -mmin +1440 -type d 2>/dev/null || echo "  No orphans found"
    @echo "TODO: prompt before deletion, check for uncommitted work"

# ─── Utilities ───

# Bypass beads — run agent directly on a repo
run-direct REPO TASK:
    @echo "Direct run on {{REPO}}: {{TASK}}"
    @echo "TODO: implement direct agent spawn"

# Run TB-1 with mock intake (beads fixture)
tb1-mock FIXTURE="test-fixtures/tickets/tb1-sample.yaml":
    @echo "Running TB-1 with mock intake: {{FIXTURE}}"
    @echo "TODO: load ticket from YAML fixture, create beads issue, run pipeline"

# List all agent sessions
sessions-list *ARGS:
    @echo "TODO: integrate with AgentLens"

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

# Format code
fmt:
    uv run ruff format src/
