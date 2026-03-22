# Dashboard Fix Plan v2 — Post-Mirror Validation

**Date**: 2026-03-20
**Previous plan**: `fix-plan-2026-03-19.md` (original 16 critical / 14 warning / 15 minor)
**Source**: 6 grounding documents from post-fix mirror run (25 agents: 1 baseline + 18 analysts + 6 synthesizers)
**Scope**: 32 panels across 6 OpenObserve dashboards (down from 34 — removed 2 redundant Agent Perf panels)

---

## What Was Fixed (v1 Plan → This Run)

| Phase | Fix | Status |
|---|---|---|
| 1a | `_parse_select_columns()` + rewritten `_make_fields()` with z-axis | **Done** — all computed metrics (failure_pct, avg_lead_time_s, block_pct, etc.) now correctly in y-axis |
| 1b | Removed ROUND from `_detect_agg()` | **Done** — ROUND-wrapped aggregates detected via inner AVG/SUM |
| 1c | Added `_extract_group_by_columns()` + z-axis classification | **Done** — `gate`, `persona`, `cwe` correctly in z-axis for breakdown |
| 1d | Removed ROUND from `_fix_aggregate_timestamp()` | **Done** |
| 1e | Comment on `area-stacked` mapping | **Done** |
| 3a | Removed panels 2 & 3 (redundant token panels) | **Done** — 4 panels now |
| 3b | COALESCE persona across tb1-tb6 | **Done** |
| 3c | Standardized 7d → 30d intervals | **Done** |
| 3d | Added `runtime_output_tokens IS NOT NULL` | **Done** |
| 3e | Renamed "Per Day" → "Over Time" | **Done** |
| 4a | Fixed Panel 1: `intake.issue_pickup` → `tb%.run` | **Done** — Panel 1 now renders data |
| 4b | Distinct Panel 4: `feedback.retry` for recovery time | **Done** — no longer duplicates Panel 2 |
| 5a | NULL + empty-string handling for `gates_first_failure` | **Done in source** — but OO silently drops the filter (see below) |
| 5b | Standardized loop-health intervals to 30d | **Done in source** — but OO rewrites to 7d (see below) |
| 6a | Standardized quality-gates intervals to 30d | **Done** |
| 6b | Added `security_cwe_ids != ''` filter | **Done** |

### Tests
- 30/30 pass (`uv run pytest tests/test_import_dashboards.py -v`)
- Dry-run field classification verified all aliases in correct axes

---

## New Issues Discovered by Mirror

### Critical: OO Config Drift (Stored ≠ Sent)

**This is the single biggest finding.** OpenObserve silently rewrites dashboard configs on save, breaking several fixes we applied.

| Dashboard | Panel | Mutation | Impact |
|---|---|---|---|
| Loop Health | 2 (Success Rate) | Dropped `OR gates_first_failure = ''` from WHERE | Undercounts successes — empty strings treated as failures |
| Loop Health | 4 (Gate Failure Breakdown) | Moved `gate` from z-axis to x-axis, dropped `_timestamp` | Panel renders completely empty |
| Loop Health | 4, 5 | Narrowed `INTERVAL '30 DAYS'` back to `INTERVAL '7 DAYS'` | Time window mismatch |
| Quality Gates | 1, 2 | x-axis mapped to `_timestamp` instead of SQL output column `gate` | Panels 1-2 show blank charts despite data existing |
| Cost Tracking | 2 | Moved `persona` from z-axis to x-axis | Bar chart grouping will be wrong when data arrives |
| Cost Tracking | 3 | Duplicated `utilization_pct` into x-axis alongside `day` | Dual x-axis line chart — broken semantics |
| Agent Perf | all | Stripped `aggregationFunction` from field defs | Cosmetic — OO still auto-detects |

**Root cause**: OO v8 format has its own ideas about axis mapping. When `customQuery: true`, it still re-analyzes the SQL and overrides our field definitions. The z-axis array (breakdown dimension) gets collapsed into x-axis for some chart types.

**Recommended fix approaches** (investigate in priority order):
1. **OO API version**: Check if a newer OO release respects `customQuery: true` field definitions
2. **Chart type workaround**: OO may handle z-axis differently for `line` vs `bar` — test if switching chart types preserves breakdown
3. **Post-import patch**: After `import-dashboards.py`, immediately GET the stored config, detect drift, PATCH back the correct values
4. **Accept and document**: If OO always rewrites, document which panels are affected and what the correct behavior should be

### Critical: Agent Performance Panel 2 — Integer Division

Panel 2 (Duration by Persona) is persistently empty. Panel 3 uses the identical WHERE clause and returns data, proving rows exist.

**Root cause**: `duration / 1000000` on sub-second microsecond durations truncates to 0.

**Fix**: `CAST(duration AS DOUBLE) / 1000000` to preserve fractional seconds.

**File**: `config/dashboards/agent-performance.json`, panel 2 query.

### Warning: Hardcoded Time Intervals (All Dashboards)

All 32 panels hardcode `INTERVAL '30 DAYS'` (or `7 DAYS` after OO rewrites). OO does NOT support `$__timeFilter` — hardcoded intervals are the required pattern. But this means the time picker is non-functional.

**Status**: Known limitation. No fix available without OO upstream changes. Document in grounding docs.

### Warning: Quality Gates Panels 1-2 Blank Despite Data

Panel 5 (Gate Failures Over Time) renders data proving `gate_status` and `gates.gate_%` spans exist. But Panels 1-2 (Pass/Fail Rates, Gate Duration) show blank charts.

**Root cause**: OO remaps x-axis to `_timestamp` instead of the SQL output column `gate`. The field definition we send has `gate` in z-axis, but OO moves it to x-axis as `_timestamp`.

**Fix**: Same as OO Config Drift investigation above. May need to restructure queries to work with OO's auto-mapping behavior.

### Warning: DOM Extraction False Negatives

The mirror collection's DOM scraper reports `noData: true` for ALL canvas-rendered charts (including ones that clearly show data in screenshots). This affects analysis accuracy.

**Fix in dashboard-mirror**:
- Add `waitForSelector('canvas')` or `waitForFunction` with canvas paint check before DOM extraction
- Use screenshot-based data presence detection as fallback
- Flag `noData` as unreliable in analyst prompts when `canvasPresent: true`

---

## Remaining Issues from v1 Plan (Not Yet Fixed)

### Deferred — Blocked on Upstream

| Issue | Status | Blocker |
|---|---|---|
| Calibration: all 8 panels empty | Blocked | `dev-loop-ambient` service + 4 missing columns |
| Cost Tracking: all 5 panels empty | Blocked | `devloop_cost_*` columns not instrumented |
| Test span exclusion | Deferred | Need `devloop_test` attribute in spans |

### Deferred — Polish (Lower Priority)

| Issue | Severity | Notes |
|---|---|---|
| KPI summary stat rows | Polish | No at-a-glance numbers on any dashboard |
| Color palette design | Polish | OO ignores configured colors anyway — needs upstream fix |
| Y-axis decimal formatting on COUNTs | Polish | OO control — may not be configurable |
| Month context on x-axis dates | Polish | OO control — investigate `axis.format` config |
| Gate name humanization (Loop Health P4) | Polish | SQL CASE for friendly names |
| CWE splitting (Quality Gates P3) | Known limitation | OO/DataFusion doesn't support UNNEST |
| Utf8 cast safety (gate_duration_ms) | Warning | Add regex guard `~ '^[0-9]'` |

---

## Next Steps — Priority Order

### P0: Fix integer division (Agent Performance Panel 2)
```
File: ~/dev-loop/config/dashboards/agent-performance.json
Panel 2: CAST(duration AS DOUBLE) / 1000000
```
Quick fix, high-confidence, restores a dead panel.

### P1: Investigate OO config drift
```
1. Import dashboard
2. GET stored config via API
3. Diff sent.json vs stored.json programmatically
4. Identify which mutations break rendering
5. Prototype post-import PATCH to correct drift
```
This affects Loop Health P4, Quality Gates P1-2, and will affect Cost Tracking P2-3 when data arrives.

### P2: Fix mirror collection pipeline
```
File: ~/dashboard-mirror/src/dashboard_mirror/collect.py
- Canvas paint wait before DOM extraction
- Filter <500 byte panel screenshots
- Scroll to lazy-loaded panels before API interception
- Fix time range picker label matching
```
Improves future validation runs.

### P3: Reimport + re-mirror after P0-P1
```
cd ~/dev-loop && uv run python scripts/import-dashboards.py --delete-existing
cd ~/dashboard-mirror && uv run dm-schema && uv run dm-chain && uv run dm-collect
# Then re-run 25-agent analysis pipeline
```
Validates P0-P1 fixes landed.

---

## Scorecard

| Dashboard | Panels | Rendering | Pre-Fix | Post-Fix | Delta |
|---|---|---|---|---|---|
| DORA Metrics | 4 | 4/4 | 2/4 (P1 dead, P4 duplicate) | 4/4 | **+2** |
| Loop Health | 6 | 5/6 | 5/6 (P4 empty) | 5/6 (P4 still empty — OO drift) | **0** |
| Agent Performance | 4 | 2/4 | 2/6 (P2-3 redundant, P4-5 scope bug) | 2/4 (P2 int division) | **0** (but cleaner) |
| Quality Gate Insights | 5 | 1/5 | 1/5 | 1/5 (P1-2 OO drift) | **0** |
| Ambient Calibration | 8 | 0/8 | 0/8 (blocked) | 0/8 (blocked) | **0** |
| Cost Tracking | 5 | 0/5 | 0/5 (blocked) | 0/5 (blocked) | **0** |
| **Total** | **32** | **12/32** | **10/34** | **12/32** | **+2 rendering, -2 panels** |

Net: +2 rendering panels (DORA P1 + P4), -2 redundant panels removed. Key blocker is now OO config drift, not import script bugs.
