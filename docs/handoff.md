# dev-loop Handoff — 2026-03-11

## What This Is
A tracer-bullet-driven developer tooling harness. 100% open-source stack. TB-1 is code-complete — 6 MCP servers + pipeline orchestrator, ready for first end-to-end run.

## What Was Built (This Session)

### 25 Python Files — 6 MCP Servers + Pipeline
All under `src/devloop/`:

| Layer | Server Name | Key Files | Tools |
|-------|------------|-----------|-------|
| 1. Intake | `beads-intake` | `intake/server.py`, `types.py`, `beads_poller.py` | `poll_ready_issues`, `get_issue_detail`, `update_issue_status`, `add_issue_comment` |
| 2. Orchestration | `orchestration` | `orchestration/server.py`, `types.py` | `setup_worktree`, `select_persona`, `build_claude_md_overlay`, `cleanup_worktree` |
| 3. Runtime | `agent-runtime` | `runtime/server.py`, `types.py`, `deny_list.py` | `spawn_agent`, `kill_agent`, `get_agent_output` |
| 4. Quality Gates | `quality-gates` | `gates/server.py`, `types.py` | `run_gate_0_sanity`, `run_gate_2_secrets`, `run_gate_4_review`, `run_all_gates` |
| 5. Observability | `observability` | `observability/server.py`, `tracing.py`, `heartbeat.py`, `types.py` | `health_check`, `get_trace_url`, `query_recent_traces`, `init_tracing`, `start_heartbeat` |
| 6. Feedback | `feedback-loop` | `feedback/server.py`, `types.py`, **`pipeline.py`** | `build_retry_prompt`, `retry_agent`, `escalate_to_human`, **`run_tb1`** |

### Tools Installed
- **dmux v5.4.0** — `npm install -g dmux` (TUI worktree multiplexer, scored 0.80)
- **gitleaks v8.30.0** — `~/.local/bin/gitleaks` (secret scanner, scored 0.86)
- **DeepEval v3.8.9** — in pyproject.toml via `uv add` (LLM-as-judge, scored 0.73)
- **OpenObserve** — Docker container `dev-loop-openobserve` on :5080 (scored 0.83)
- **beads (br)** — already installed (scored 0.92)

### Config Files Created
```
config/
├── agents.yaml              # 5 personas: bug-fix, feature, refactor, security-fix, docs
├── capabilities.yaml        # Per-project tool/path scoping
├── dependencies.yaml        # Cross-repo cascade map (TB-5)
├── review-gate.yaml         # DeepEval review criteria + severity levels
├── scheduling.yaml          # Priority queuing + budget throttle
└── projects/
    └── prompt-bench.yaml    # Per-project gate thresholds
```

### Edge Cases Implemented
- **Optimistic locking** — `claim_issue()` in beads_poller.py prevents duplicate pickup
- **Emergency stop** — `just emergency-stop` kills agents, marks issues interrupted
- **Secrets deny list** — `deny_list.py` with 15 patterns + `is_path_denied()`
- **Crash recovery** — `heartbeat.py` with background heartbeat thread + `find_stale_runs()`

### Documentation Fully Updated
All docs rewritten for 100% OSS stack:
- 6 layer docs, README, architecture, tracer-bullets, scoring-rubric, network-requirements
- edge-cases (25 items), edge-cases-pass2 (16 items) — all updated
- test-repos, handoff, .env.example, test fixtures
- ADR-005 marked superseded, ADR-007 + ADR-008 added
- All beads issues cleaned of stale tool references (Linear→beads, CodeRabbit→DeepEval, Aikido→VibeForge)

### Beads Issues
- 48 total created, all 48 now closed
- Issue prefix: `dl`

## What Must Happen Next (In Order)

### 1. Wire justfile tb1 command
The `just tb1` recipe still prints TODO. Change it to:
```
just tb1:
    uv run python -c "from devloop.feedback.pipeline import run_tb1; run_tb1('ISSUE_ID', '/home/musicofhel/prompt-bench')"
```

### 2. Populate prompt-bench
`~/prompt-bench` is a placeholder (1 commit, just README.md). Needs real code so gates have something to test:
- Add a simple Python or Node project with at least 1 test
- Seed a beads issue targeting it

### 3. Lint pass
Run `uv run ruff check src/` and fix any errors. The agents wrote code in isolation — may have minor issues.

### 4. First end-to-end run
```bash
cd ~/dev-loop
# Create a test issue
br add --title "Fix typo in README.md" --labels bug --labels "repo:prompt-bench"
# Run TB-1
uv run python -c "from devloop.feedback.pipeline import run_tb1; run_tb1('dl-XXX', '/home/musicofhel/prompt-bench')"
```
This will exercise: poll → claim → worktree → persona → heartbeat → claude --print → gates → retry/escalate → cleanup.

### 5. Commit everything
Nothing is committed to git in the dev-loop repo yet. All 25 Python files, 6 configs, and updated docs need to be committed.

### 6. Re-score tools with real data
After TB-1 runs, update scoring-rubric.md with actual experience data.

## Key Architecture Decisions
- **git worktree add** (not dmux) for programmatic orchestration — dmux scored 2/5 on MCP integration (TUI-only)
- **anthropic SDK directly** for Gate 4 review — not DeepEval's evaluation framework (simpler, fewer deps)
- **init_tracing() sets global provider** — all existing `trace.get_tracer()` calls across every layer auto-export to OpenObserve
- **Fail-fast gates**: Gate 0 (sanity) → Gate 2 (gitleaks) → Gate 4 (review). Cheapest first.
- **TB-1 minimal**: only 3 gates, 1 feedback channel. Full 8-gate + 7-channel system is target state.

## File Map
```
~/dev-loop/
├── README.md
├── CLAUDE.md
├── pyproject.toml                         # Python deps: fastmcp, otel, httpx, deepeval, anthropic
├── .env.example                           # ANTHROPIC_API_KEY + GITHUB_TOKEN only
├── .gitignore
├── justfile
├── config/                                # 6 YAML configs
├── src/devloop/
│   ├── __init__.py, cli.py
│   ├── intake/          (server.py, types.py, beads_poller.py)
│   ├── orchestration/   (server.py, types.py)
│   ├── runtime/         (server.py, types.py, deny_list.py)
│   ├── gates/           (server.py, types.py)
│   ├── observability/   (server.py, types.py, tracing.py, heartbeat.py)
│   └── feedback/        (server.py, types.py, pipeline.py)
├── test-fixtures/tickets/                 # 3 mock YAML tickets
├── docs/                                  # 28+ doc files
│   ├── layers/ (01-06), architecture, tracer-bullets, scoring-rubric, etc.
│   └── adrs/ (001-008)
└── .beads/                                # Issue tracking data (48 issues, all closed)
```

## Docker
- **OpenObserve**: `docker start dev-loop-openobserve` → :5080 (must start Docker Desktop on Windows first)
- Login: `admin@dev-loop.local` / `devloop123`

## After TB-1 Passes
- TB-2: Failure-to-retry (feedback path) — test retry loop with intentional gate failure
- TB-3: Security gate (VibeForge or semgrep) — evaluate both during scoring
- TB-4: Cost control — token proxy + budget enforcement
- TB-5: Cross-repo cascade — prompt-bench → omniswipe-backend
- TB-6: Session replay — AgentLens integration
