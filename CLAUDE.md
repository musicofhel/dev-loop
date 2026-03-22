# dev-loop — Harness Rules

## Project Type
Developer tooling harness. Not an app. Not a library. A composition of tools wired together with config, MCP servers, and thin glue code.

## Build Philosophy
- **TRACER BULLETS ONLY.** Never build a layer horizontally. Every change must cut vertically through all six layers: intake → orchestration → runtime → quality gates → observability → feedback loop.
- To add a feature: define the tracer bullet first (docs/tracer-bullets.md), then implement the thinnest possible vertical slice.
- Each tracer bullet should be runnable end-to-end within a single `just tb<N>` command.

## Architecture
- Six layers (see docs/layers/*.md for intent)
- MCP servers are the integration boundary between layers
- OpenTelemetry is the instrumentation standard — every layer emits spans
- Every tool must be individually bypassable (escape hatches, not locked gates)

## Code Standards
- Glue code in Python, managed by uv (pyproject.toml, `uv run`, `uv sync`)
- MCP servers via fastmcp — one per layer under `src/devloop/<layer>/`
- Config in YAML or TOML — no JSON for human-edited config
- Justfile for all commands — `just` delegates to `uv run` for Python tasks
- Issue tracking via beads (br) — replaces Linear as intake layer
- `br ready` = what's ready to work on, `br graph` = dependency visualization

## Testing
- Test against real repos (prompt-bench, omniswipe-backend, enterprise-pipeline)
- No synthetic benchmarks until real-repo tracer bullets pass
- Scoring rubric at docs/scoring-rubric.md — every tool gets evaluated

## Agents
- **Dashboard debugging**: `@dashboard-mirror` grounds OpenObserve dashboard state via `~/dashboard-mirror` (Playwright capture → 3-analyst pipeline → grounding doc)

## What NOT To Do
- Don't build abstractions until you have 3+ concrete uses
- Don't add tools to the stack without running them through the scoring rubric first
- Don't write integration tests for layers — test tracer bullets end-to-end
- Don't optimize for speed until the loop works correctly
