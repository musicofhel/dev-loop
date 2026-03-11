# Network Requirements

## Design Principle

dev-loop is designed to be **maximally local**. The only required external API is the Anthropic API for LLM calls. All other tools (beads, OpenObserve, gitleaks, VibeForge, DeepEval) run locally with zero network dependency.

## External APIs Called

| Service | URL | Required For | Fallback If Unreachable |
|---------|-----|-------------|------------------------|
| Anthropic API | api.anthropic.com | Agent runtime (all TBs), DeepEval LLM-as-judge | None — agents can't work without it |
| GitHub API | api.github.com | PR creation, cascade detection | Manual PR creation |

## Local Services

| Port | Service | Purpose |
|------|---------|---------|
| 5080 | OpenObserve | Traces, metrics, logs, dashboards, alerts |
| 4318 | OTel Collector (optional) | OTLP HTTP receiver (if using collector) |

## Fully Local Tools (No Network)

| Tool | Layer | Why It's Local |
|------|-------|---------------|
| beads (br) | Intake | SQLite + JSONL in `.beads/` directory |
| dmux | Orchestration | Git worktree operations only |
| gitleaks | Quality Gates | Regex + entropy scanning on local diff |
| VibeForge Scanner | Quality Gates | Static analysis, 2000+ rules, runs on local files |
| pip-audit / npm audit | Quality Gates | Checks local lockfiles against advisory DB (one HTTP call) |
| AgentLens | Observability | Local session capture and replay |
| OpenObserve | Observability | Docker container, local storage |

## What Happens When Things Are Down

### Anthropic API down
**Impact:** Total stop. No agents can run. No DeepEval LLM-as-judge gates.
**Detection:** Health check in `just stack-health`.
**Response:** Queue issues in beads, resume when API returns.

### GitHub API down
**Impact:** PRs can't be created. Agent work completes but isn't published.
**Detection:** PR creation fails, logged.
**Response:** Worktree with completed work persists. PR created when GitHub returns.

### OpenObserve down
**Impact:** Traces queue in memory (OTel SDK buffering). No dashboards. No alerts.
**Detection:** `just stack-health` reports unhealthy.
**Response:** Loop continues. Traces flush when OpenObserve recovers. Buffer overflow drops oldest spans.

### beads unavailable
**Impact:** Cannot happen — beads is a local binary reading local files.
**Detection:** N/A.
**Response:** N/A.
