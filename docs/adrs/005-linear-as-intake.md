# ADR-005: Linear as the Intake Layer

## Status
**Superseded** by [ADR-007: beads Replaces Linear](007-beads-replaces-linear.md)

## Context
Need a task management system that serves as the single entry point for all work. Options:

| Tool | Pros | Cons |
|------|------|------|
| Linear | Fast, dev-focused, great API, webhooks, keyboard-driven | Paid past free tier |
| GitHub Issues | Free, already where code lives | Weak project management, no sprint boards |
| Jira | Enterprise standard, extremely configurable | Slow, complex, over-engineered for us |
| Plain text/YAML | Zero dependencies, fully offline | No collaboration, no webhooks, no dashboards |

## Decision
Linear as the primary intake mechanism.

## Rationale
- Symphony (OpenAI) already proves the Linear → agent → PR pattern works
- API-first design — polling and webhooks both well-supported
- Labels and custom fields map cleanly to agent personas and cost ceilings
- Built-in cycle/sprint tracking for DORA metrics without extra tooling
- Clean enough that agents can read and update tickets without parsing HTML

## Consequences
**Good:**
- Proven pattern (Symphony)
- DORA metrics come nearly for free
- Humans and agents share the same interface
- Status flow maps directly to loop states

**Bad:**
- Another SaaS dependency
- Free tier may be limiting (need to verify)
- If Linear goes down, the entire intake layer stops

## Escape Hatch
`just run-direct` command bypasses Linear entirely for quick one-off agent runs. This ensures the system works without Linear for testing and emergencies.

## Why This Was Superseded
beads (br) provides agent-first, CLI-native issue tracking with:
- Local SQLite + JSONL (zero network dependency)
- `br ready` returns exactly what we need without polling an API
- Dependencies with `br dep` enforce TB ordering natively
- No paid service, no rate limits, no API keys
- Version-controlled issue state in `.beads/`

See [ADR-007](007-beads-replaces-linear.md) for full rationale.
