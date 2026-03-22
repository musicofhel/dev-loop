# Dashboard Fix Plan v3 — Post-Mirror Validation #2

**Date**: 2026-03-21
**Previous plan**: `fix-plan-2026-03-20-post-mirror.md` (identified OO config drift + integer division)
**Source**: 6 grounding documents from post-fix mirror run (25 agents: 1 baseline + 18 analysts + 6 synthesizers)
**Scope**: 32 panels across 6 OpenObserve dashboards

---

## What Was Fixed This Session

| Fix | Impact | Status |
|---|---|---|
| Categorical x-axis classification: non-time GROUP BY without time dimension → x-axis (was z-axis with fallback `_timestamp`) | **+4 panels rendering** (QG P1-3, LH P4) | **Done** |
| Integer division CAST: `CAST(duration AS DOUBLE) / 1000000` across all duration panels | Prevents truncation for multi-second durations | **Done** |
| AP Panel 2 unit: `/ 1000000` (seconds) → `/ 1000` (milliseconds) | Persona phase durations are 7-17µs — seconds rounds to 0 | **Done** |
| Post-import drift detection: compare query-level fields, report + PATCH with hash | Zero drift detected after classification fix | **Done** |
| "OO config drift" root cause: **our classification was wrong**, not OO rewriting | OO preserves `queries[0].fields` correctly | **Resolved** |

### Root Cause: Categorical vs Time-Series Classification

The previous session's "OO config drift" finding was a misdiagnosis. The real bug:

- `_make_fields()` put ALL non-time GROUP BY columns into z-axis (breakdown), regardless of whether a time dimension existed
- For categorical queries (e.g., `SELECT gate, COUNT(*) ... GROUP BY gate`), this produced z-axis `gate` with a fallback `_timestamp` in x-axis
- OO renders x-axis from the field definition, but the SQL doesn't SELECT `_timestamp` → blank chart
- **Fix**: z-axis only when time dimension is also present; otherwise categorical GROUP BY → x-axis
- OO actually preserves our field definitions correctly in `queries[0].fields` — the "drift" was comparing panel-level `fields` (which OO empties for `customQuery` panels, by design)

---

## Scorecard

| Dashboard | Panels | Pre-Session | Post-Session | Delta |
|---|---|---|---|---|
| DORA Metrics | 4 | 4/4 | 4/4 | 0 |
| Loop Health | 6 | 5/6 | **6/6** | **+1** |
| Agent Performance | 4 | 2/4 | **3/4** | **+1** |
| Quality Gate Insights | 5 | 1/5 | **4/5** | **+3** |
| Ambient Calibration | 8 | 0/8 | 0/8 | 0 (blocked) |
| Cost Tracking | 5 | 0/5 | 0/5 | 0 (blocked) |
| **Total** | **32** | **12/32** | **17/32** | **+5** |

Cumulative from initial: 10/34 → 17/32 (+7 rendering, -2 redundant panels removed).

---

## Remaining Issues from Mirror

### Critical

| Issue | Dashboard | Panel | Details |
|---|---|---|---|
| AP P2 zero-height bars | Agent Perf | 2 | OO strips `CAST(duration AS DOUBLE)` from stored metadata. Query SQL preserved, but bars still zero-height with 7-17µs durations even at ms scale (0.007-0.017ms). **May need `tb%.run` spans instead of `tb%.phase.persona`** — persona phases are sub-millisecond. |
| Time picker non-functional | All | All 32 | Hardcoded `INTERVAL '30 DAYS'` SQL. OO doesn't support `$__timeFilter`. Known limitation. |

### Warning

| Issue | Dashboard | Panel | Details |
|---|---|---|---|
| Color semantic inversion | Quality Gates | 1 | Orange=Passed, Purple=Failed. Needs explicit color mapping or query column reorder. |
| OO stored-config mutations | Loop Health | 2, 4, 5 | OO rewrites stored metadata (drops OR clause, narrows 30d→7d, remaps axes). **Cosmetic** — `customQuery` SQL is preserved and renders correctly. Risk: re-editing panels in OO UI could lose fixes. |
| Y-axis labels are SQL aliases | DORA, LH, AP | Most | "Avg Lead Time S", "Failure Pct", "Avg Recovery S" instead of human-readable labels. OO control — may need `config.axis_labels` if supported. |
| Panel 4 y-axis scale | Agent Perf | 4 | Input Tokens (~20K) invisible next to Output Tokens (~300K) on shared y-axis. Consider dual-axis or separate panels. |
| WCAG contrast | Loop Health, QG | P5, P2-3 | Yellow-on-white (#FFEB3B) at ~1.1:1 contrast. OO ignores configured colors — auto-palette issue. |
| Panel 2 outlier | Quality Gates | 2 | `gate_4_review` at ~26,000ms compresses all other gate durations to near-zero. |
| Decimal formatting | DORA P1, LH P1/5 | - | Integer COUNTs display as "210.00". OO formatting control. |
| Day-only x-axis | All time-series | - | "11, 12, 13" without month context. Ambiguous near month boundaries. |

### Deferred — Blocked on Upstream

| Issue | Blocker |
|---|---|
| Calibration: all 8 panels empty | `dev-loop-ambient` service + 4 missing schema columns |
| Cost Tracking: all 5 panels empty | `devloop_cost_*` columns not instrumented |
| Calibration P3/5/7: x-axis/SQL mismatch | Latent bug — will fail even when data arrives |
| Calibration P2: area-stacked for single series | Should be `line` |

### Mirror Pipeline Issues (P2)

| Issue | Details |
|---|---|
| DOM `noData` false positives | Canvas-rendered charts always report `noData: true`. Use screenshots as authoritative. |
| Below-fold panels not captured | Panels 4+ use lazy-load — queries don't fire until scrolled. Add scroll-to-panel before API interception. |
| Screenshot viewport-only | Full-page screenshot is viewport-sized (1568×882), not scrolled. Panels below fold lack full-page coverage. |
| Time range picker labels | "Past 7 Days" / "Past 1 Hours" presets not found by collector. |
| Sub-element screenshot noise | ~7 files per panel (drag handle, refresh icon, chevron crops). Filter to chart-body only. |

---

## Next Steps — Priority Order

### P0: Investigate AP Panel 2 span selection
The `tb%.phase.persona` spans have 7-17µs durations — too small to display meaningfully even in milliseconds. Options:
1. Use `tb%.run` spans grouped by persona (these have multi-second durations)
2. If `tb%.phase.persona` is correct, display in microseconds: `ROUND(AVG(duration), 0) as avg_us`
3. Verify span data with direct OO query to understand duration distribution

### P1: Fix mirror collection pipeline
- Canvas paint wait before DOM extraction
- Scroll to lazy-loaded panels before API interception
- Full-page scrolled screenshot (not viewport-only)
- Filter sub-element screenshots
- Fix time range picker label matching

### P2: Polish pass
- Color semantic mapping (QG P1 pass/fail)
- Human-readable y-axis labels where OO supports it
- Investigate dual-axis for AP P4 token panel

### P3: Calibration latent bugs
When `dev-loop-ambient` starts emitting:
- Fix P3/5/7 x-axis column bindings
- Change P2 to `line` type
- Standardize all 8 panels to 30d intervals

---

## Files Modified This Session

| File | Repo | Changes |
|---|---|---|
| `scripts/import-dashboards.py` | dev-loop | Categorical x-axis fix in `_make_fields()`, drift detection (`_detect_drift`, `_patch_dashboard`, `_get_query_fields`), hash-based PUT |
| `tests/test_import_dashboards.py` | dev-loop | Updated 2 test assertions for categorical classification |
| `config/dashboards/agent-performance.json` | dev-loop | P2: `/ 1000` (ms), title "(ms)", alias `avg_ms` |
| `config/dashboards/dora.json` | dev-loop | P2, P4: `CAST(duration AS DOUBLE)` |
| `config/dashboards/loop-health.json` | dev-loop | P3: `CAST(duration AS DOUBLE)` |
| `docs/fix-plan-2026-03-21-post-mirror-v2.md` | dashboard-mirror | This plan |
