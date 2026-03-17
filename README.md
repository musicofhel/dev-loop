# dev-loop

A tracer-bullet-driven developer tooling harness that wires beads issues through agent orchestration, quality gates, observability, and feedback loops — across multiple projects.

**100% open-source tool stack. Zero paid services (beyond Anthropic API).**

## Philosophy

**Tracer bullets, not horizontal layers.** Every feature cuts vertically through all six layers before we widen anything. The first tracer bullet is a single issue flowing through the entire system end-to-end with visibility at every step.

**Loop-first.** The system is a loop, not a pipeline. Every output feeds back as input. Failed PRs feed back to agents. Cost spikes feed back to throttles. Trace analysis feeds back to harness tuning.

**Multi-project by default.** This harness manages N repos simultaneously. Isolation via worktrees, shared config via MCP servers, unified observability via OpenTelemetry.

## System Overview

<picture>
  <img alt="System Overview — 6-layer closed loop" src="docs/diagrams/system-overview.svg" width="400">
</picture>

Six layers form a closed loop. The output of every stage feeds back as input to an earlier stage — there is no "end", only cycles that get tighter as the harness learns.

| # | Layer | Purpose | Core Tools |
|---|-------|---------|-----------|
| 1 | **Intake** | Issue entry point | beads (`br`), SQLite |
| 2 | **Orchestration** | Task isolation | git worktree, persona config |
| 3 | **Agent Runtime** | Agent execution | Claude Code CLI, CLAUDE.md overlay |
| 4 | **Quality Gates** | Automated checks | gitleaks, Semgrep, bandit, Claude CLI review |
| 5 | **Observability** | Instrumentation | OpenTelemetry, OpenObserve, NDJSON |
| 6 | **Feedback Loop** | Learning loop | retry logic, cost monitor, changelog |

## Architecture

### Two Implementations

dev-loop has two complementary implementations:

- **Python Layer** (`src/devloop/`) — Six MCP servers (one per layer), feedback pipelines, gate implementations. 313 tests passing.
- **Rust Daemon** (`daemon/`) — `dl` binary. Ambient layer hooks for Claude Code, real-time check engine, session management. 257 tests passing.

### Three-Tier Safety Model

<picture>
  <img alt="Three-tier ambient architecture" src="docs/diagrams/ambient-tiers.svg" width="500">
</picture>

| Tier | Trigger | Latency | What it checks |
|------|---------|---------|---------------|
| **Tier 1** | Every tool call | < 5ms | Deny list, dangerous ops, secret patterns |
| **Tier 2** | `git commit` | ~30s | Semgrep SAST, gitleaks, test runner, ATDD specs |
| **Tier 3** | On demand (`just tb1`) | ~minutes | Full pipeline: all 6 layers end-to-end |

### Tier 1: Check Engine

<picture>
  <img alt="Check engine — deny list, dangerous ops, secrets" src="docs/diagrams/check-engine.svg" width="450">
</picture>

Three pre-compiled check modules run on every Write, Edit, or Bash tool call:

- **Deny List** — 15 glob patterns block writes to `.env`, `.ssh/*`, `*.key`, etc.
- **Dangerous Ops** — 25 regex patterns warn/block `rm -rf`, `curl | sh`, `git push --force`, etc.
- **Secret Scanner** — 15 regex patterns warn on API keys, private keys, DB connection strings in file content.

All checks are configurable with `extra_patterns`, `remove_patterns`, and `allow_patterns`.

### Hook Integration

<picture>
  <img alt="Hook integration — Claude Code to daemon" src="docs/diagrams/hook-flow.svg" width="500">
</picture>

Six hooks installed into `~/.claude/settings.json` connect Claude Code to the daemon:

| Hook | Fires on | Action |
|------|----------|--------|
| `PreToolUse` | Write, Edit, Bash | Deny list + dangerous ops check |
| `PostToolUse` | Write, Edit | Secret detection in content |
| `SessionStart` | Session begins | Register session, inject handoff context |
| `SessionEnd` | Session ends | Write handoff YAML, export OTel spans |
| `Stop` | After each turn | Context guard (warn at 85% usage) |
| `PreCompact` | Before compaction | Write handoff before context is lost |

Hooks are **fail-open** — if the daemon is unavailable, all tool calls proceed normally.

### Tier 2: Checkpoint Gates

<picture>
  <img alt="Checkpoint gates — 5 sequential gates on commit" src="docs/diagrams/checkpoint-gates.svg" width="450">
</picture>

Five gates run sequentially on `git commit` (fail-fast — first failure blocks the commit):

1. **Sanity** — Auto-detects test runner (`cargo test`, `pytest`, `npm test`), runs it
2. **Semgrep** — SAST scanning with `--config auto` + custom AI rules
3. **Secrets** — gitleaks/betterleaks on staged diff
4. **ATDD** — Spec-before-code enforcement (Given/When/Then)
5. **Review** — Placeholder for human/LLM review

On pass, a `Dev-Loop-Gate: <sha256>` trailer is injected into the commit message.

### Config System

<picture>
  <img alt="3-layer config merge" src="docs/diagrams/config-merge.svg" width="350">
</picture>

Three layers merge to produce the final check engine configuration:

1. **Built-in defaults** — Hardcoded patterns in Rust
2. **Global config** — `~/.config/dev-loop/ambient.yaml`
3. **Per-repo config** — `.devloop.yaml` in project root

Lists are additive (`extra_patterns` appends, `remove_patterns` subtracts). Scalars are last-wins. Both global and repo configs must have `enabled: true` for checks to run.

### Session Lifecycle

<picture>
  <img alt="Session lifecycle state diagram" src="docs/diagrams/session-lifecycle.svg" width="500">
</picture>

Sessions are tracked from start to end with full observability:

- **Handoff YAML** at `/tmp/dev-loop/sessions/<id>.yaml` persists session state (~400 tokens) for context injection on resume
- **OTel spans** exported to OpenObserve on session end (3x retry with exponential backoff)
- **Context guard** warns at 85% usage and writes handoff before context is lost
- **Allow-once** overrides let developers bypass a block temporarily (`dl allow-once ".env" --ttl 600`)

### Observability

<picture>
  <img alt="Observability data flow" src="docs/diagrams/observability-flow.svg" width="550">
</picture>

- **JSONL event log** — Append-only at `/tmp/dev-loop/events.jsonl` with rotation (50MB, 3 files)
- **SSE broadcast** — Real-time event streaming via `dl stream`
- **OTel export** — OTLP/HTTP JSON to OpenObserve (manual TCP, no heavy SDK)
- **Dashboards** — Loop health + calibration panels in OpenObserve
- **Alert rules** — Gate failure spikes, stuck agents, cost anomalies

## Tracer Bullets

<picture>
  <img alt="Tracer bullet flow — TB-1 issue to PR" src="docs/diagrams/tracer-bullet-flow.svg" width="500">
</picture>

Six vertical slices, each proving one critical path end-to-end. All validated against real repos.

| TB | Name | Status |
|----|------|--------|
| TB-1 | Issue-to-PR (the golden path) | **PASSING** |
| TB-2 | Failure-to-retry (the feedback path) | **PASSING** |
| TB-3 | Security-gate-to-fix (the safety path) | **PASSING** |
| TB-4 | Runaway-to-stop (the resource path) | **PASSING** |
| TB-5 | Cross-repo cascade (the multi-project path) | **PASSING** |
| TB-6 | Session replay debug (the observability path) | **PASSING** |

See [docs/tracer-bullets.md](docs/tracer-bullets.md) for entry/exit criteria.

## Calibration Pipeline

<picture>
  <img alt="5-stage calibration pipeline" src="docs/diagrams/calibration-pipeline.svg" width="450">
</picture>

`just calibrate` runs a 5-stage regression detection pipeline:

| Stage | What it does | Key metric |
|-------|-------------|-----------|
| Shadow Report | Analyze shadow-mode verdicts (last 7 days) | Verdict counts |
| Replay Harness | Replay 2000 tool calls from 74 real sessions | Block rate (baseline: 0.5%) |
| Tier 2 Suite | 13 planted-defect scenarios | 100% detection rate |
| Feedback Scoring | Precision/recall from labeled events | F1 per check type |
| Rust Tests | 257 daemon unit + integration tests | 100% pass |

Produces a dated report at `docs/calibration/YYYY-MM-DD.md`. Exits 1 on regression.

## Test Repos

| Repo | Purpose |
|------|---------|
| prompt-bench | Python calculator — validates simple issue resolution |
| omniswipe-backend | Fastify + PostgreSQL + Redis — validates production patterns |
| enterprise-pipeline | Python/FastAPI + Qdrant RAG — validates cross-language support |

## Quick Start

```bash
# Install the ambient daemon
cd daemon && cargo build --release
cp target/release/dl ~/.local/bin/dl

# Install hooks into Claude Code
dl install

# Start the daemon
dl start

# Check status
dl status

# Run a tracer bullet end-to-end
just tb1 <issue_id> <repo_path>

# Run the calibration pipeline
just calibrate
```

## CLI Reference

### Daemon Management

| Command | Purpose |
|---------|---------|
| `dl start` | Start daemon (background, Unix socket) |
| `dl stop` | Graceful shutdown |
| `dl status` | Active sessions, uptime, event counts |
| `dl stream` | Tail SSE event stream |
| `dl reload` | Hot-reload config (SIGHUP) |

### Hook & Check

| Command | Purpose |
|---------|---------|
| `dl install` / `dl uninstall` | Manage Claude Code hooks |
| `dl enable` / `dl disable` | Toggle ambient layer |
| `dl check` | Offline check engine test |
| `dl checkpoint [--dir] [--json]` | Offline Tier 2 gates |
| `dl allow-once <pattern>` | Temporary block override (5min TTL) |

### Observability

| Command | Purpose |
|---------|---------|
| `dl traces --last N` | Tail JSONL event log |
| `dl shadow-report` | Analyze shadow-mode verdicts |
| `dl feedback <id> correct\|false-positive\|missed` | Annotate events |
| `dl feedback --stats` | Precision/recall/F1 per check type |
| `dl outcome <session-id> success\|partial\|fail` | Record session outcome |

### Configuration

| Command | Purpose |
|---------|---------|
| `dl config [dir]` | Show merged config |
| `dl config-lint [--dir]` | Validate configuration |
| `dl rules` | Print active rules (markdown) |

## Stats

| Metric | Value |
|--------|-------|
| Rust tests | 257 |
| Conformance tests | 106 |
| Replay tests | 19 |
| Tier 2 tests | 28 |
| Feedback tests | 27 |
| **Total tests** | **437** |
| Binary size | 6.4 MB |
| CLI commands | 26 |
| Tier 1 latency | < 5ms |
| Hook latency | ~6ms (incl. process startup) |

## Documentation

| Doc | What it covers |
|-----|---------------|
| [Architecture](docs/architecture.md) | System diagram, data flow, multi-project model |
| [Tracer Bullets](docs/tracer-bullets.md) | All 6 vertical slices with entry/exit criteria |
| [Edge Cases — Pass 1](docs/edge-cases.md) | 25 failure modes: races, crashes, security |
| [Edge Cases — Pass 2](docs/edge-cases-pass2.md) | 16 design gaps: context scaling, backpressure |
| [Scoring Rubric](docs/scoring-rubric.md) | 7-dimension tool evaluation matrix |
| [Test Repos](docs/test-repos.md) | Validation targets and pass criteria |
| [Network Requirements](docs/network-requirements.md) | External APIs, ports, degradation behavior |
| [Ambient Layer Plan](docs/ambient-layer-plan.md) | Full daemon design spec (~900 lines) |
| [ADRs](docs/adrs/) | Architecture decision records |

## License

MIT

---

*Diagrams rendered with [beautiful-mermaid](https://github.com/lukilabs/beautiful-mermaid) (github-dark theme).*
