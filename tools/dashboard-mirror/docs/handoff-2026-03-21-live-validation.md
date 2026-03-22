# Handoff: Live Dashboard Validation — 2026-03-21

## Session Summary

Spun up the full stack and ran live queries against all 32 OO panels to validate the current state. No code changes made — investigation only.

## Key Discoveries

### Phase 0 Investigations (all resolved)

**0a. Timestamp filter is CORRECT** — not a no-op.
- `CAST(NOW() AS BIGINT)` returns **nanoseconds** (19 digits: `1774104650363061488`)
- `/1000` converts to **microseconds** (16 digits: `1774104650363061`)
- `_timestamp` is microseconds (16 digits: `1773889270274991`)
- Same magnitude → filter works correctly
- The AP grounding doc's claim that it's a "semantic no-op" was **wrong**

**0b. AP P2 span durations confirmed**
- `tb%.phase.persona` spans: 2–31µs (sub-millisecond, explains zero-height bars)
- `tb%.run` spans: 17s–112s average (multi-second, good for display)
- BUT: `tb%.run` spans do NOT carry persona fields (`tb1_persona` through `tb6_persona` are all NULL on run spans)
- Persona data only exists on `tb%.phase.persona` spans
- **Conclusion**: AP P2 must display raw microseconds since it can't switch to `tb%.run`
- Fix: `ROUND(AVG(CAST(duration AS DOUBLE)), 0) as avg_us`, title "Duration by Persona (µs)"

**0c. String literals validated**
- `gate_status` values: `pass`, `fail` — exact match confirmed
- `feedback.retry` exists as exact operation name — confirmed
- Also found: `feedback.build_retry_prompt`, `feedback.escalate`

### Calibration Dashboard Now Live (was 0/8, now 8/8)

Major surprise: `dev-loop-ambient` is now emitting data. All 8 Calibration panels return rows. This means the Calibration "prep" fixes from the plan are now live fixes that need to be applied and verified visually.

## Current Scorecard — Live Query Results

| Dashboard | Panels | Returning Data | Notes |
|---|---|---|---|
| DORA Metrics | 4 | **4/4** | Fully healthy |
| Loop Health | 6 | **6/6** | Fully healthy |
| Quality Gate Insights | 5 | **4/5** | P4 correctly empty (no secret gate failures) |
| Agent Performance | 4 | **3/4** | P2 EMPTY (zero-height bars — µs durations) |
| Ambient Calibration | 8 | **8/8** | ALL alive (was 0/8 at mirror run #3) |
| Cost Tracking | 5 | **0/5** | All blocked (`devloop_cost_*` fields missing) |
| **Total** | **32** | **25/32** | +8 from mirror run #3 (17→25) |

## Remaining Issues

### Must Fix (1 panel not rendering)
- **AP P2**: Change to raw µs display: `ROUND(AVG(CAST(duration AS DOUBLE)), 0) as avg_us`, title "Duration by Persona (µs)"

### Blocked on Upstream (5 panels)
- **Cost Tracking**: All 5 panels need `devloop_cost_spent_usd`, `devloop_cost_budget_usd`, `devloop_agent_persona` telemetry fields

### Quality/UX Issues (from grounding docs, still valid)
- QG P1: Color semantic inversion (orange=Passed, purple=Failed)
- QG P2: `gate_duration_ms` stores µs — label mismatch
- LH P5: Yellow line on white (#FFEB3B) — WCAG contrast failure (~1.1:1)
- LH P2: OO drops `OR gates_first_failure = ''` from stored metadata
- LH P4/P5: OO narrows 30d→7d in stored metadata
- CT P2/P3/P5: OO axis restructuring + SQL rewrite (prep for when data arrives)
- Import script: `_fix_aggregate_timestamp()` injects `MIN(_timestamp)` into flat listings (CT P5)
- Import script: label override map needed (raw SQL aliases as labels)
- Calibration: 6/8 panels use 7d interval vs 30d dashboard default
- All dashboards: time picker non-functional (hardcoded intervals) — known limitation

## Infrastructure State

- **OO**: `dev-loop-openobserve` container up 2 days, port 5080
- **Credentials**: `admin@dev-loop.local` / `devloop123`
- **Data**: 5,435 traces in `default` stream, service_name `dev-loop`
- **Alert error in logs**: DataFusion `ExprBoundaries` column index bug — alert scheduler, not dashboard issue
- **Dashboard IDs**:
  - Agent Performance: `7440921977243566080`
  - Ambient Calibration: `7440921977264537600`
  - Cost Tracking: `7440921977285509120`
  - DORA Metrics: `7440921977306480640`
  - Loop Health: `7440921977335840768`
  - Quality Gate Insights: `7440921977352617984`
  - UI Test: `7440512147903217664`

## Git State

- `~/dev-loop`: clean at `01a671b`, 2 untracked (`.claude/`, old handoff doc)
- `~/dashboard-mirror`: clean at `e65e821`

## Plan File

`~/.claude/plans/virtual-tinkering-journal.md` — comprehensive 4-phase plan. Phase 0 investigation is now complete. Ready to proceed with Phase 1 (config JSON fixes).

## Next Session Priority

1. Fix AP P2 to display raw µs (one config change + reimport)
2. Apply QG color/unit fixes
3. Apply Calibration interval standardization (now that it's live)
4. Re-mirror to validate visual rendering
