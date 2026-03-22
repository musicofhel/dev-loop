# dev-loop

A safety harness for AI coding agents. It watches every action your AI takes, blocks dangerous ones in real-time, and runs quality checks before code ships.

**100% open-source tool stack. Zero paid services (beyond Anthropic API).**

## Why This Exists

AI coding agents are fast — but they can also delete your `.env`, leak API keys in a commit, run `rm -rf /`, or push broken code. The faster they work, the more damage they can do before you notice.

dev-loop sits between the AI agent and your codebase. It intercepts every action, checks it against safety rules, and blocks anything dangerous — all in under 5 milliseconds. For bigger checks (security scans, running tests), it runs a full gate at commit time.

## How It Works

<picture>
  <img alt="System Overview — 6-layer closed loop" src="docs/diagrams/system-overview.svg" width="400">
</picture>

Six layers form a closed loop — the output of every stage feeds back as input. There is no "end", only cycles that get tighter as the harness learns.

| # | Layer | What it does |
|---|-------|-------------|
| 1 | **Intake** | Pulls issues from the tracker |
| 2 | **Orchestration** | Creates an isolated branch for the agent to work in |
| 3 | **Agent Runtime** | Runs the AI agent with scoped permissions |
| 4 | **Quality Gates** | Scans for secrets, vulnerabilities, and test failures |
| 5 | **Observability** | Records everything for debugging and dashboards |
| 6 | **Feedback Loop** | Retries failures, escalates to a human if stuck |

Three tiers of protection, from instant to thorough:

- **Real-time (< 5ms)** — Every file write, edit, or shell command is checked against deny lists, dangerous patterns, and secret detectors before it executes.
- **Commit-time (~30s)** — Tests, security scanning, secret detection, and spec enforcement run before each commit goes through.
- **Full pipeline (on demand)** — Take a bug report → assign it to an agent → agent writes code → quality gates → retry on failure → PR created.

## Three-Tier Safety Model

<picture>
  <img alt="Three-tier ambient architecture" src="docs/diagrams/ambient-tiers.svg" width="500">
</picture>

| Tier | When it runs | Latency | What it checks |
|------|-------------|---------|---------------|
| **Tier 1** | Every tool call | < 5ms | Blocked files, dangerous commands, leaked secrets |
| **Tier 2** | On `git commit` | ~30s | Security scanner, secret scanner, test runner, spec enforcement |
| **Tier 3** | On demand | ~minutes | Full 6-layer pipeline end-to-end |

### Tier 1: Check Engine

<picture>
  <img alt="Check engine — deny list, dangerous ops, secrets" src="docs/diagrams/check-engine.svg" width="450">
</picture>

Three check modules run on every Write, Edit, or Bash call the agent makes:

- **Deny List** — 15 patterns block writes to sensitive files (`.env`, `.ssh/*`, `*.key`, etc.)
- **Dangerous Ops** — 25 patterns warn or block risky commands (`rm -rf`, `curl | sh`, `git push --force`, etc.)
- **Secret Scanner** — 15 patterns catch API keys, private keys, and database strings in file content.

All patterns are configurable per-project.

### Hook Integration

<picture>
  <img alt="Hook integration — Claude Code to daemon" src="docs/diagrams/hook-flow.svg" width="500">
</picture>

Six hooks connect Claude Code to the safety daemon:

| Hook | When it fires | What it does |
|------|--------------|-------------|
| `PreToolUse` | Before Write, Edit, Bash | Checks for blocked files and dangerous commands |
| `PostToolUse` | After Write, Edit | Scans written content for secrets |
| `SessionStart` | Session begins | Registers the session, injects context from prior sessions |
| `SessionEnd` | Session ends | Saves session state, exports telemetry |
| `Stop` | After each turn | Warns if the agent is using too much context (85% threshold) |
| `PreCompact` | Before context compaction | Saves session state before context is trimmed |

Hooks are **fail-open** — if the daemon is unavailable, all tool calls proceed normally.

### Tier 2: Checkpoint Gates

<picture>
  <img alt="Checkpoint gates — 5 sequential gates on commit" src="docs/diagrams/checkpoint-gates.svg" width="450">
</picture>

Five gates run in sequence on `git commit`. The first failure blocks the commit:

1. **Sanity** — Auto-detects the test runner and runs it
2. **Semgrep** — Security scanning for known vulnerability patterns
3. **Secrets** — Scans the staged diff for leaked credentials
4. **ATDD** — Checks that code matches acceptance specs (if configured)
5. **Review** — Placeholder for human or LLM code review

On pass, a `Dev-Loop-Gate: <sha256>` trailer is added to the commit message.

### Config System

<picture>
  <img alt="3-layer config merge" src="docs/diagrams/config-merge.svg" width="350">
</picture>

Three layers merge to produce the final configuration:

1. **Built-in defaults** — Hardcoded in the Rust daemon
2. **Global config** — `~/.config/dev-loop/ambient.yaml` (applies to all projects)
3. **Per-project config** — `.devloop.yaml` in the project root

Pattern lists are additive (you can add or remove patterns at each level). Both global and project configs must have `enabled: true` for checks to run.

### Session Lifecycle

<picture>
  <img alt="Session lifecycle state diagram" src="docs/diagrams/session-lifecycle.svg" width="500">
</picture>

Sessions are tracked from start to end:

- **Handoff file** — Session state is saved between sessions so the next session picks up where you left off
- **Telemetry export** — Spans are sent to OpenObserve for dashboards and alerting
- **Context guard** — Warns at 85% context usage and saves state before context is lost
- **Temporary overrides** — `dl allow-once ".env" --ttl 600` bypasses a block for 10 minutes

### Observability

<picture>
  <img alt="Observability data flow" src="docs/diagrams/observability-flow.svg" width="550">
</picture>

- **Event log** — Append-only JSONL log with automatic rotation
- **Live stream** — Real-time event feed via `dl stream`
- **Telemetry** — OpenTelemetry spans exported to OpenObserve
- **Dashboards** — Loop health, agent performance, and calibration panels
- **Alerts** — Gate failure spikes, stuck agents, cost anomalies

## What Gets Blocked

| The AI tries to... | What happens |
|---|---|
| Write to `.env` or `.ssh/config` | Blocked before the write executes (< 5ms) |
| Run `rm -rf /` or `curl ... \| sh` | Blocked before the command executes (< 5ms) |
| Force-push to main | Blocked before the push executes (< 5ms) |
| Commit code containing an API key | Blocked at commit time (~30s) |
| Commit code with a known vulnerability | Blocked at commit time (Semgrep scan) |
| Commit code that fails tests | Blocked at commit time (auto-detected test runner) |

## Calibration

<picture>
  <img alt="5-stage calibration pipeline" src="docs/diagrams/calibration-pipeline.svg" width="450">
</picture>

`just calibrate` runs a 5-stage regression detection pipeline to make sure safety checks haven't degraded. It produces a dated report at `docs/calibration/YYYY-MM-DD.md` and exits with an error if it detects a regression.

Six end-to-end test paths ("tracer bullets") validate every critical workflow — from the happy path through security catches, retries, cross-repo cascades, and session replay. See [Tracer Bullets](docs/tracer-bullets.md) for details.

## Quick Start

```bash
# Build and install the daemon
cd daemon && cargo build --release
cp target/release/dl ~/.local/bin/dl

# Hook into Claude Code
dl install

# Start the daemon
dl start

# Check status
dl status
```

Once running, dev-loop silently protects every Claude Code session. No changes to your workflow needed.

## CLI Reference

### Daemon Management

| Command | Purpose |
|---------|---------|
| `dl start` | Start daemon (background, Unix socket) |
| `dl stop` | Graceful shutdown |
| `dl status` | Active sessions, uptime, event counts |
| `dl stream` | Tail live event stream |
| `dl reload` | Hot-reload config |

### Hook & Check

| Command | Purpose |
|---------|---------|
| `dl install` / `dl uninstall` | Manage Claude Code hooks |
| `dl enable` / `dl disable` | Toggle the ambient safety layer |
| `dl check` | Offline check engine test |
| `dl checkpoint [--dir] [--json]` | Run Tier 2 gates offline |
| `dl allow-once <pattern>` | Temporary block override (5min TTL) |
| `dl kill <gate>` / `dl unkill [gate]` | Temporarily disable/re-enable a checkpoint gate |

### Observability

| Command | Purpose |
|---------|---------|
| `dl traces --last N` | Tail the event log |
| `dl shadow-report` | Analyze shadow-mode verdicts |
| `dl feedback <id> correct\|false-positive\|missed` | Label events for scoring |
| `dl feedback --stats` | Precision/recall/F1 per check type |
| `dl outcome <session-id> success\|partial\|fail` | Record session outcome |
| `dl dashboard-validate` | Validate dashboard SQL queries |

### Configuration

| Command | Purpose |
|---------|---------|
| `dl config [dir]` | Show merged config |
| `dl config-lint [--dir]` | Validate configuration |
| `dl rules` | Print active rules |

## Stats

| Metric | Value |
|--------|-------|
| Tier 1 latency | < 5ms |
| Hook latency | ~6ms (incl. process startup) |
| Binary size | 6.5 MB |
| **Total tests** | **764** |
| Python tests | 386 |
| Rust tests | 195 |
| Conformance tests | 106 |
| Tier 2 tests | 31 |
| Feedback tests | 27 |
| Replay tests | 19 |

## Agents

Claude Code agents that ship with dev-loop (`.claude/agents/`):

| Agent | What it does |
|-------|-------------|
| `@dashboard-mirror` | Grounds OpenObserve dashboard state — Playwright capture → 3-analyst pipeline (structure, data, UX) → synthesis into a canonical grounding doc. Source at [`tools/dashboard-mirror/`](tools/dashboard-mirror/). |

## Documentation

| Doc | What it covers |
|-----|---------------|
| [Architecture](docs/architecture.md) | System diagram, data flow, multi-project model |
| [Tracer Bullets](docs/tracer-bullets.md) | All 6 end-to-end test paths with entry/exit criteria |
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
