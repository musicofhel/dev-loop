# dev-loop

A tracer-bullet-driven developer tooling harness that wires beads issues through agent orchestration, quality gates, observability, and feedback loops — across multiple projects.

**100% open-source tool stack. Zero paid services (beyond Anthropic API).**

## Philosophy

**Tracer bullets, not horizontal layers.** Every feature cuts vertically through all six layers before we widen anything. The first tracer bullet is a single issue flowing through the entire system end-to-end with visibility at every step.

**Loop-first.** The system is a loop, not a pipeline. Every output feeds back as input. Failed PRs feed back to agents. Cost spikes feed back to throttles. Trace analysis feeds back to harness tuning.

**Multi-project by default.** This harness manages N repos simultaneously. Isolation via worktrees, shared config via MCP servers, unified observability via OpenTelemetry.

## Layers

```
BEADS (intake) ──► ORCHESTRATION ──► AGENT RUNTIME ──► QUALITY GATES ──► OBSERVABILITY ──► FEEDBACK LOOP
     ▲                                                                                          │
     └──────────────────────────────────────────────────────────────────────────────────────────┘
```

| # | Layer | Core Tools | Doc |
|---|-------|-----------|-----|
| 1 | Intake | beads (br), Beads-Kanban-UI | [docs/layers/01-intake.md](docs/layers/01-intake.md) |
| 2 | Orchestration | dmux, Gastown, JAT | [docs/layers/02-orchestration.md](docs/layers/02-orchestration.md) |
| 3 | Agent Runtime | OpenFang, zsh-tool MCP, Continuous-Claude-v3, Letta, Headroom, EnCompass | [docs/layers/03-runtime.md](docs/layers/03-runtime.md) |
| 4 | Quality Gates | DeepEval (LLM-as-judge), VibeForge Scanner, gitleaks, ATDD | [docs/layers/04-quality-gates.md](docs/layers/04-quality-gates.md) |
| 5 | Observability | OpenTelemetry, OpenObserve, AgentLens | [docs/layers/05-observability.md](docs/layers/05-observability.md) |
| 6 | Feedback Loop | DeepEval Step Efficiency, harness tuning, changelog | [docs/layers/06-feedback-loop.md](docs/layers/06-feedback-loop.md) |

## Tracer Bullets

See [docs/tracer-bullets.md](docs/tracer-bullets.md) for all vertical slices.

| TB | Name | Status |
|----|------|--------|
| TB-1 | Issue-to-PR (the golden path) | NOT STARTED |
| TB-2 | Failure-to-retry (the feedback path) | NOT STARTED |
| TB-3 | Security-gate-to-fix (the safety path) | NOT STARTED |
| TB-4 | Cost-spike-to-pause (the budget path) | NOT STARTED |
| TB-5 | Cross-repo cascade (the multi-project path) | NOT STARTED |
| TB-6 | Session replay debug (the observability path) | NOT STARTED |

## Test Repos

| Repo | Purpose |
|------|---------|
| prompt-bench | Prompt evaluation benchmark — validates agent output quality |
| omniswipe-backend | Real Fastify API — validates against production patterns |
| enterprise-pipeline | Python/FastAPI RAG — validates cross-language support |

## Quick Start

```bash
# (after TB-1 is wired)
just stack up        # stand up all services
just tb1             # run tracer bullet 1 end-to-end
just score           # evaluate all tools against rubric
```

## Documentation

| Doc | What it covers |
|-----|---------------|
| [Architecture](docs/architecture.md) | System diagram, data flow, multi-project model |
| [Tracer Bullets](docs/tracer-bullets.md) | All 6 vertical slices with entry/exit criteria |
| [Edge Cases — Pass 1](docs/edge-cases.md) | 25 failure modes: races, crashes, security |
| [Edge Cases — Pass 2](docs/edge-cases-pass2.md) | 16 design gaps: context scaling, backpressure, dangerous ops |
| [Scoring Rubric](docs/scoring-rubric.md) | 7-dimension tool evaluation matrix |
| [Test Repos](docs/test-repos.md) | Validation targets and pass criteria |
| [Network Requirements](docs/network-requirements.md) | External APIs, ports, degradation behavior |
| [ADRs](docs/adrs/) | Architecture decision records |

## Architecture Decisions

See [docs/adrs/](docs/adrs/) for all ADRs.
