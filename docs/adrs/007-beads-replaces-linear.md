# ADR-007: Beads Replaces Linear as Intake Layer

## Status
Accepted

## Context
The original design used Linear as the intake layer — polling an external API for "Ready" tickets. This requires:
- Linear account setup and API key
- Network dependency for every polling cycle
- Custom webhook infrastructure for real-time events
- Learning a third-party API

Meanwhile, we already use `br` (beads_rust) as an agent-first issue tracker with:
- Local SQLite + JSONL storage (no network dependency)
- `br ready` returns unblocked, non-deferred issues (exact semantic we need)
- Dependency tracking with `br dep` (TB-2 depends on TB-1, etc.)
- Epic management for tracer bullet grouping
- `br graph --all` for dependency visualization
- `br changelog` for auto-generated changelogs
- JSON output for programmatic consumption (`--json`)
- No API key, no rate limits, no cost

## Decision
Replace Linear with beads as the intake layer. The `linear-intake` MCP server becomes `beads-intake` — polling `br ready --json` instead of the Linear API.

## Consequences

### Positive
- Zero external dependencies for intake (pure local)
- Beads is already installed and familiar
- Dependency graph natively models TB ordering
- `br ready` is exactly the semantic we need (unblocked + not deferred)
- No API key management, no rate limits
- Issue state lives in the repo (`.beads/`) — version controlled
- Emergency stop can use `br update` to mark issues interrupted
- Recovery uses `br stale` to find stuck issues

### Negative
- No web UI for non-technical stakeholders (beads is CLI/TUI only)
- No built-in DORA metrics (must compute from beads + git history)
- Linear's webhook model was cleaner for real-time; beads requires polling
- Loses Linear's project management features (sprints, roadmaps, cycles)

### Mitigation
- DORA metrics computed from `br changelog` + git log — stored in OpenObserve
- If a web UI is needed later, `bv` (beads viewer TUI) or a custom dashboard can be added
- Polling `br ready` is effectively instant (local SQLite query) — no latency concern
- Linear can be re-added as a *sync target* (beads → Linear) if stakeholder visibility is needed

## Migration
- `docs/layers/01-intake.md` updated to reference beads instead of Linear
- `src/devloop/intake/beads_poller.py` replaces `src/mcp/linear-intake/`
- ADR-005 (Linear as intake) superseded by this ADR
- `justfile` commands updated to use `br` instead of Linear API calls
