# Handoff: Dashboard Fix Implementation + Mirror Validation

**Date**: 2026-03-20
**Session**: Fix all dashboard issues from grounding docs → reimport → full mirror pipeline
**Previous session**: `handoff-2026-03-19-initial-pipeline-run.md` (first mirror run, produced fix plan)

---

## What Was Done

### Phase 1: Import Script Fixes (`~/dev-loop/scripts/import-dashboards.py`)

Root-cause fix for field misclassification across all dashboards:

1. **New `_parse_select_columns()`** — returns `(expression, alias)` tuples with parenthesis-depth comma splitting. `_parse_select_aliases()` now delegates to it.
2. **Removed ROUND from `_detect_agg()`** — ROUND is a scalar wrapper, inner aggregates (AVG, SUM) are detected correctly.
3. **Rewrote `_make_fields()`** with 3-axis classification:
   - **x-axis**: time dimensions (`_timestamp`, `day`, `hour`, `week`, `month`)
   - **z-axis**: non-time, non-aggregate aliases in GROUP BY (breakdown dimensions like `gate`, `persona`, `cwe`)
   - **y-axis**: everything else (aggregates, computed metrics)
4. **New `_extract_group_by_columns()`** helper via regex.
5. **Removed ROUND from `_fix_aggregate_timestamp()`** detection list.
6. **Added comment** on `area-stacked` mapping.

### Phase 2: Test Updates (`~/dev-loop/tests/test_import_dashboards.py`)

- Added `TestParseSelectColumns` class (6 tests)
- Added 6 new `TestMakeFields` tests: CASE WHEN→y, multi-GROUP-BY→z, ROUND-wrapped→y, Quality Gates Panel 1 pattern, z-field presence
- Updated existing dimension test for z-axis (was x-axis)
- **30/30 tests pass**

### Phase 3: Dashboard Config Fixes

| File | Changes |
|---|---|
| `agent-performance.json` | Removed panels 2-3 (redundant), COALESCE persona across tb1-6, 7d→30d intervals, added `runtime_output_tokens IS NOT NULL`, renamed "Per Day"→"Over Time". 4 panels (was 6). |
| `dora.json` | Panel 1: `intake.issue_pickup`→`tb%.run`. Panel 4: `feedback.retry` for recovery time (was duplicate of Panel 2). |
| `loop-health.json` | Panels 2,4: NULL+empty-string handling for `gates_first_failure`. Panels 4,5: 7d→30d intervals. |
| `quality-gates.json` | Panels 1,2: 7d→30d intervals. Panel 3: added `security_cwe_ids != ''`. |

### Phase 4: Reimport + Mirror Pipeline

1. `import-dashboards.py --delete-existing` — 6 dashboards imported (32 panels)
2. `dm-schema` — 4 streams, 217 fields captured
3. `dm-chain` — 6 dashboard chain diffs
4. `dm-collect` — Playwright collection, all 6 dashboards
5. **Baseline agent** — cross-dashboard schema/consistency audit
6. **18 analyst agents** (3×6 dashboards, ran in parallel) — Structure, Data, UX
7. **6 synthesizer agents** (ran in parallel) — grounding documents
8. **Total: 25 agents**, all completed successfully (1 retry on cost-tracking data analyst due to corrupt sub-pixel screenshot)

---

## Key Findings from Mirror

### Confirmed Working
- **DORA Panel 1**: `tb%.run` renders data (was dead with `intake.issue_pickup`)
- **DORA Panel 4**: `feedback.retry` gives distinct recovery time chart
- **Field classification**: All computed metrics in y-axis, breakdown dimensions in z-axis
- **Agent Perf**: Clean 4-panel layout, COALESCE persona correct

### New Issue: OO Config Drift (Top Priority)

OpenObserve silently rewrites stored configs, breaking several fixes:

| Dashboard | Panel | OO Mutation | Impact |
|---|---|---|---|
| Loop Health | P2 | Dropped `OR gates_first_failure = ''` | Undercounts successes |
| Loop Health | P4 | Moved `gate` z→x, dropped `_timestamp` | Panel completely empty |
| Loop Health | P4,5 | Narrowed 30d→7d | Time window mismatch |
| Quality Gates | P1,2 | x-axis mapped to `_timestamp` not `gate` | Blank charts despite data |
| Cost Tracking | P2 | Moved `persona` z→x | Will break when data arrives |

### New Issue: Integer Division (Agent Perf Panel 2)

`duration / 1000000` truncates sub-second microsecond values to 0 → invisible bars. Needs `CAST(duration AS DOUBLE) / 1000000`.

### Known Limitations (Unchanged)
- Calibration (8 panels): blocked on `dev-loop-ambient` service + missing columns
- Cost Tracking (5 panels): blocked on `devloop_cost_*` columns
- Hardcoded `INTERVAL '30 DAYS'`: OO doesn't support `$__timeFilter`
- DOM extraction: false `noData` for all canvas-rendered charts (collection artifact)

---

## Rendering Scorecard

| Dashboard | Panels | Rendering | Change |
|---|---|---|---|
| DORA Metrics | 4 | 4/4 | **+2** (P1 fixed, P4 distinct) |
| Loop Health | 6 | 5/6 | 0 (P4 broken by OO drift) |
| Agent Performance | 4 | 2/4 | 0 (P2 int division, P3 intermittent) |
| Quality Gate Insights | 5 | 1/5 | 0 (P1-2 blank from OO drift) |
| Ambient Calibration | 8 | 0/8 | 0 (blocked) |
| Cost Tracking | 5 | 0/5 | 0 (blocked) |
| **Total** | **32** | **12/32** | **+2 net** |

---

## Uncommitted Changes

### `~/dev-loop` (6 modified files)
```
M config/dashboards/agent-performance.json
M config/dashboards/dora.json
M config/dashboards/loop-health.json
M config/dashboards/quality-gates.json
M scripts/import-dashboards.py
M tests/test_import_dashboards.py
```

### `~/dashboard-mirror` (2 new files)
```
?? docs/fix-plan-2026-03-19.md
?? docs/fix-plan-2026-03-20-post-mirror.md
```

### `~/dashboard-mirror/output/` (regenerated, not tracked)
All 6 grounding documents + 18 analyst reports + 1 baseline report.

---

## Next Session Priorities

1. **P0**: Fix Agent Perf P2 integer division → reimport → verify
2. **P1**: Investigate OO config drift — prototype post-import PATCH or chart type workarounds
3. **P2**: Fix mirror collection (canvas wait, screenshot filtering, time range picker)
4. **P3**: Full reimport + re-mirror after P0-P1
5. **P4**: Commit all changes to both repos

---

## Files Created This Session

| File | Repo | Purpose |
|---|---|---|
| `docs/fix-plan-2026-03-20-post-mirror.md` | dashboard-mirror | Updated fix plan with mirror findings |
| `docs/handoff-2026-03-20-mirror-validation.md` | dashboard-mirror | This handoff |
| `output/*/grounding.md` (×6) | dashboard-mirror | Canonical grounding docs (regenerated) |
| `output/*/analyst-{structure,data,ux}.md` (×18) | dashboard-mirror | Analyst reports (regenerated) |
| `output/_baseline/baseline-report.md` | dashboard-mirror | Cross-dashboard baseline (regenerated) |
