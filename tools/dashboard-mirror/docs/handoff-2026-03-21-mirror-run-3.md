# Handoff: Mirror Run #3 — Post-Millisecond Fix + OO SQL Rewrite Discovery

**Date**: 2026-03-21
**Session**: Fix AP P2 ms unit → reimport → full mirror pipeline (run 3) → discover OO SQL rewriting
**Previous session**: `handoff-2026-03-21-mirror-validation-v2.md`

---

## What Was Done

### Code Changes (dev-loop, already committed + pushed)

1. **Categorical x-axis classification** — `_make_fields()` routes non-time GROUP BY to x-axis when no time dimension present. Fixes QG P1-3, LH P4.
2. **Integer division CAST** — `CAST(duration AS DOUBLE)` on all duration panels (AP P2, DORA P2/P4, LH P3)
3. **AP Panel 2 unit** — `/1000` (ms) instead of `/1000000` (s), title "(ms)", alias `avg_ms`
4. **Post-import drift detection** — `_detect_drift()`, `_patch_dashboard()`, `_get_query_fields()` added to import script
5. **Tests** — 30/30 pass, 2 assertions updated for categorical classification

### Mirror Pipeline (run 3)

- `dm-schema` — 4 streams, 217 fields
- `dm-chain` — 6 dashboard chain diffs
- `dm-collect` — 6 dashboards collected
- **25 agents** (1 baseline + 18 analysts + 6 synthesizers) — all completed successfully
- **6 grounding documents** written to `output/*/grounding.md`

### Git

- **dev-loop**: committed `a3bf132`, pushed to `origin/main`
- **dashboard-mirror**: committed `336b16c`, pushed to new private repo `musicofhel/dashboard-mirror`

---

## Critical Discovery: OO Rewrites SQL Queries

**The single most important finding from this run.**

OpenObserve actively rewrites SQL query text on storage — not just field metadata. Confirmed on AP Panel 2:

| Layer | Divisor | Alias | Title |
|---|---|---|---|
| Source config | `/ 1000` | `avg_ms` | "Duration by Persona (ms)" |
| OO stored | `/ 1000000` | `avg_seconds` | "Duration by Persona" |

OO changed the arithmetic, the alias, the y-axis label, AND the panel title. It also stripped `CAST(duration AS DOUBLE)`, leaving bare integer division.

**Why our drift check missed it**: The post-import `_patch_dashboard()` runs immediately after POST. OO appears to rewrite asynchronously — the GET right after POST returns the original, but a later GET (during `dm-collect`) shows the rewritten version.

**Impact**: The ms fix we applied is being reverted by OO at storage time. AP P2 remains zero-height bars.

**Other confirmed OO mutations**:
- LH P2: Drops `OR gates_first_failure = ''` from WHERE
- LH P4, P5: Narrows `INTERVAL '30 DAYS'` → `INTERVAL '7 DAYS'`
- CT P2: Moves `persona` from x→z, injects `_timestamp` on x
- CT P3: Duplicates `utilization_pct` from y into x
- CT P5: Prepends `MIN(_timestamp) as _timestamp` (breaks query semantics)

---

## Rendering Scorecard

| Dashboard | Panels | Rendering | Status |
|---|---|---|---|
| DORA Metrics | 4 | **4/4** | Healthy |
| Loop Health | 6 | **6/6** | Degraded (OO query mutations in stored config) |
| Agent Performance | 4 | **3/4** | Degraded (P2 zero-height — OO reverts ms fix) |
| Quality Gate Insights | 5 | **4/5** | Degraded (P4 correctly empty, color inversion) |
| Calibration | 8 | 0/8 | Blocked (upstream) |
| Cost Tracking | 5 | 0/5 | Blocked (upstream) |
| **Total** | **32** | **17/32** | |

Cumulative from initial (10/34): **+7 rendering, -2 redundant panels removed**.

---

## Key Findings by Dashboard

### DORA (4/4 — Healthy)
- All panels render correctly
- Duration `/1000000` confirmed correct (µs → seconds)
- No OO SQL mutations (dashboard authored directly in OO, no sent.json)
- Unvalidated string literals: `gate_status = 'fail'`, `operation_name = 'feedback.retry'`

### Loop Health (6/6 — Degraded)
- All 6 panels render (P4 restored by categorical x-axis fix)
- OO stored-config mutations: P2 drops OR clause, P4/P5 narrow to 7d
- `customQuery` SQL renders correctly despite metadata mutations
- Yellow line on P5 fails WCAG (~1.1:1 contrast)
- Panels 4-6 below fold with most diagnostic data

### Agent Performance (3/4 — Degraded)
- P1, P3, P4 render. P2 zero-height bars
- **Root cause**: OO rewrites `/1000` → `/1000000` AND strips CAST. Even at ms scale, persona durations (7-17µs) produce 0.007-0.017ms → rounds to 0.0
- Three-way unit contradiction documented in grounding doc
- P4 Input Tokens invisible (15:1 scale ratio vs Output Tokens)

### Quality Gates (4/5 — Degraded)
- **P1-3 now render** (categorical x-axis fix — major improvement from 1/5)
- P4 correctly empty (secrets gate passing)
- Color semantic inversion: orange=Passed, purple=Failed
- `gate_duration_ms` stores µs despite name (26,000 = 26ms)
- P2 outlier compression (gate_4_review dominates y-axis)

### Calibration (0/8 — Blocked)
- Bimodal failure: 6 schema errors (missing fields), 2 zero rows (no service)
- 4 latent bugs for when data arrives (x-axis mismatches, wrong chart type)
- Missing: `verdict`, `check_type`, `reason`, `session_outcome`

### Cost Tracking (0/5 — Blocked)
- Missing: `devloop_cost_spent_usd`, `devloop_cost_budget_usd`, `devloop_agent_persona`
- 3 critical OO mutations to fix before data flows (P2 axis, P3 x-axis dup, P5 SQL rewrite)
- `devloop_agent_persona` should be `persona_name` (already exists in schema)

---

## Uncommitted Changes

### `~/dashboard-mirror` (new files since last commit)
```
?? docs/handoff-2026-03-21-mirror-run-3.md
```
All grounding docs are in `output/` (not tracked).

### `~/dev-loop`
Clean — all changes committed and pushed.

---

## Next Session Priorities

1. **P0**: Investigate OO SQL rewriting mechanism — why does OO rewrite `/1000` to `/1000000`? Is this a duration-aware normalization? Can it be disabled? Test: import a panel with `/500` and see if OO rewrites it.
2. **P1**: Fix AP P2 — if OO always normalizes duration to `/1000000`, accept seconds and use `ROUND(..., 6)` or display raw µs as integers
3. **P2**: Fix mirror collection (canvas wait, scroll-to-panel, full-page screenshot)
4. **P3**: Polish pass (QG color inversion, y-axis labels, LH P5 contrast)
5. **P4**: Address OO stored-config mutations for LH P2/P4/P5 (verify rendering uses query-level SQL, not stored metadata)

---

## Files Created This Session

| File | Repo | Purpose |
|---|---|---|
| `docs/fix-plan-2026-03-21-post-mirror-v2.md` | dashboard-mirror | Fix plan with OO rewrite findings |
| `docs/handoff-2026-03-21-mirror-validation-v2.md` | dashboard-mirror | Mid-session handoff |
| `docs/handoff-2026-03-21-mirror-run-3.md` | dashboard-mirror | This handoff |
| `output/*/grounding.md` (×6) | dashboard-mirror | Canonical grounding docs (run 3) |
| `output/*/analyst-{structure,data,ux}.md` (×18) | dashboard-mirror | Analyst reports (run 3) |
| `output/_baseline/baseline-report.md` | dashboard-mirror | Cross-dashboard baseline (run 3) |

---

## Session Stats

- **3 mirror runs** (run 1 from previous session, runs 2+3 this session)
- **75 agents total** (25 per run × 3 runs)
- **17/32 panels rendering** (up from 10/34 initial)
- **Key blocker shifted**: import script bugs → OO SQL rewriting
