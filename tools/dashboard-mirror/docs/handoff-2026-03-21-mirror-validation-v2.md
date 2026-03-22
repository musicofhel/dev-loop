# Handoff: Dashboard Fixes v2 + Mirror Validation

**Date**: 2026-03-21
**Session**: Implement fix plan from 2026-03-20 → reimport → full mirror pipeline
**Previous session**: `handoff-2026-03-20-mirror-validation.md`

---

## What Was Done

### Phase 1: Root Cause Fix — Categorical x-axis Classification

The previous session's "OO config drift" was a misdiagnosis. The real bug was in `_make_fields()`:

- **Problem**: ALL non-time GROUP BY columns went to z-axis, even without a time dimension. For categorical queries like `SELECT gate, COUNT(*) GROUP BY gate`, this put `gate` in z-axis with a fallback `_timestamp` in x — but the SQL doesn't SELECT `_timestamp`, so OO rendered blank charts.
- **Fix**: z-axis only when time dimension is also present. Without time, categorical GROUP BY → x-axis.
- **Result**: +4 panels rendering (QG P1-3, LH P4)

### Phase 2: Integer Division Fix

- Applied `CAST(duration AS DOUBLE) / 1000000` across all duration panels (AP P2, DORA P2/P4, LH P3)
- AP P2 further changed to `/ 1000` (milliseconds) after mirror revealed persona durations are 7-17µs

### Phase 3: Post-Import Drift Detection

Added to `import-dashboards.py`:
- `_get_query_fields()` — reads from `queries[0].fields` (where OO stores them)
- `_detect_drift()` — compares query-level fields between sent and stored
- `_patch_dashboard()` — GET→overlay→PUT with hash for optimistic concurrency
- **Result**: Zero drift on all 6 reimports. OO preserves `customQuery` field definitions correctly.

### Phase 4: Full Mirror Pipeline (25 agents)

1. `dm-schema` — 4 streams, 217 fields
2. `dm-chain` — 6 dashboard chain diffs
3. `dm-collect` — Playwright collection, all 6 dashboards
4. **1 baseline agent** — cross-dashboard schema/consistency audit
5. **18 analyst agents** (3×6 dashboards: Structure, Data, UX) — ran in parallel
6. **6 synthesizer agents** — grounding documents, ran in parallel
7. **Total: 25 agents**, all completed successfully

### Phase 5: AP Panel 2 Millisecond Fix

After mirror revealed persona durations are 7-17µs, changed P2 from seconds to milliseconds. Reimported, verified zero drift.

---

## Rendering Scorecard

| Dashboard | Panels | Previous | Current | Delta |
|---|---|---|---|---|
| DORA Metrics | 4 | 4/4 | 4/4 | 0 |
| Loop Health | 6 | 5/6 | **6/6** | **+1** |
| Agent Performance | 4 | 2/4 | **3/4** | **+1** |
| Quality Gate Insights | 5 | 1/5 | **4/5** | **+3** |
| Ambient Calibration | 8 | 0/8 | 0/8 | 0 |
| Cost Tracking | 5 | 0/5 | 0/5 | 0 |
| **Total** | **32** | **12/32** | **17/32** | **+5** |

Cumulative from initial (10/34): **+7 rendering, -2 redundant panels removed**.

---

## Key Findings from Mirror

### Confirmed Working
- **QG Panels 1-3**: Categorical bar charts now render with gate names on x-axis (was blank)
- **LH Panel 4**: Gate failure breakdown bar chart restored (was empty from z-axis misclassification)
- **All 4 DORA panels**: Confirmed rendering, including P4 (collector timing race gives false "No Data" in DOM)
- **All 6 LH panels**: Confirmed rendering (DOM `noData` is collector artifact)
- **AP Panels 1, 3, 4**: Confirmed rendering
- **Zero drift**: All 6 dashboards pass post-import verification

### Remaining Issues
- **AP Panel 2**: Bars still zero-height. `tb%.phase.persona` spans are 7-17µs — even millisecond display gives 0.007-0.017ms. May need different span type.
- **Color semantic inversion**: QG P1 orange=passed, purple=failed
- **OO stored-config mutations**: Cosmetic (drops CAST, remaps axes in metadata). Rendering unaffected.
- **Y-axis labels**: Raw SQL aliases as labels across most dashboards
- **WCAG contrast**: Yellow-on-white lines fail accessibility
- **Time picker**: Non-functional (hardcoded SQL intervals, known limitation)

---

## Uncommitted Changes

### `~/dev-loop` (6 modified files)
```
M config/dashboards/agent-performance.json  — P2 ms fix, CAST
M config/dashboards/dora.json               — CAST duration fix
M config/dashboards/loop-health.json        — CAST duration fix
M config/dashboards/quality-gates.json      — (from previous session)
M scripts/import-dashboards.py              — categorical x-axis, drift detection
M tests/test_import_dashboards.py           — updated assertions
```

### `~/dashboard-mirror` (4 new files)
```
?? docs/fix-plan-2026-03-20-post-mirror.md
?? docs/fix-plan-2026-03-21-post-mirror-v2.md
?? docs/handoff-2026-03-20-mirror-validation.md
?? docs/handoff-2026-03-21-mirror-validation-v2.md
```

### `~/dashboard-mirror/output/` (regenerated, not tracked)
All 6 grounding documents + 18 analyst reports + 1 baseline report.

---

## Next Session Priorities

1. **P0**: Investigate AP P2 span selection — `tb%.phase.persona` may be wrong span; try `tb%.run` grouped by persona
2. **P1**: Fix mirror collection (canvas wait, scroll-to-panel, full-page screenshot, time range picker)
3. **P2**: Polish pass (color semantics, y-axis labels, dual-axis for AP P4)
4. **P3**: Commit all changes to both repos

---

## Files Created This Session

| File | Repo | Purpose |
|---|---|---|
| `docs/fix-plan-2026-03-21-post-mirror-v2.md` | dashboard-mirror | Updated fix plan with v2 mirror findings |
| `docs/handoff-2026-03-21-mirror-validation-v2.md` | dashboard-mirror | This handoff |
| `output/*/grounding.md` (×6) | dashboard-mirror | Canonical grounding docs (regenerated) |
| `output/*/analyst-{structure,data,ux}.md` (×18) | dashboard-mirror | Analyst reports (regenerated) |
| `output/_baseline/baseline-report.md` | dashboard-mirror | Cross-dashboard baseline (regenerated) |
