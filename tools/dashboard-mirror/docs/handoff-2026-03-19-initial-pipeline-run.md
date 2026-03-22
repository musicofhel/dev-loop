# Handoff: dashboard-mirror — Initial Pipeline Run

**Date**: 2026-03-19
**Session**: First run of the full dashboard-mirror pipeline

---

## What Was Built

A standalone project at `~/dashboard-mirror` that captures a complete mirror of every OpenObserve dashboard (screenshots, DOM text, API responses, config round-trip diffs, layout metrics, timing) and runs a multi-agent analysis pipeline to produce grounding documents — canonical references for what each dashboard looks like.

### Project Structure
```
~/dashboard-mirror/
├── pyproject.toml              # uv-managed, playwright dependency
├── CLAUDE.md                   # Project instructions for future sessions
├── README.md                   # Full architecture docs
├── src/dashboard_mirror/
│   ├── collect.py              # Playwright collection (dm-collect)
│   ├── schema.py               # OO stream schema capture (dm-schema)
│   ├── transform_chain.py      # Config transformation chain diffs (dm-chain)
│   ├── cross_map.py            # Cross-dashboard metric mapping
│   └── config.py               # Shared env var config
├── prompts/
│   ├── baseline.md             # Cross-dashboard baseline analyst
│   ├── analyst-structure.md    # Analyst A: layout, grid, config drift
│   ├── analyst-data.md         # Analyst B: queries, labels, schema coverage
│   ├── analyst-ux.md           # Analyst C: readability, errors, coherence
│   └── synthesizer.md          # Grounding document author
└── output/                     # gitignored — regenerated each run
```

### Pipeline Execution (25 agents total)
1. **Collection** (3 scripts): `dm-schema`, `dm-chain`, `dm-collect` → 353 files
2. **Baseline analysis** (1 agent): Cross-dashboard schema/consistency validation
3. **Per-dashboard analysis** (18 agents): 3 analysts × 6 dashboards, run in parallel
4. **Synthesis** (6 agents): 1 per dashboard, produces the grounding document

---

## What Was Found

### Cross-Dashboard Issues (from baseline + all grounding docs)

| Issue | Impact | Dashboards Affected |
|---|---|---|
| **Hardcoded SQL time intervals** | Dashboard time picker is non-functional | All 6 |
| **13 missing schema columns** | Cost Tracking (5/5 dead), Calibration (6/8 dead) | 2 |
| **`fields.x` auto-detection bug** | Metric columns placed on both x and y axes | DORA panel 3 (garbled x-axis) |
| **Canvas DOM timing** | Collection reports false "No Data" for rendered panels | All with canvas charts |
| **Uniform `#5960b2` color** | OO overrides configured colors on most panels | All 6 |
| **`area` → `area-stacked` mutation** | Import script maps area to area-stacked (by design, OO lacks plain area) | 10 panels across 4 dashboards |
| **Panels 2 & 4 identical** on DORA | Same SQL query, different aliases | DORA Metrics |
| **`tb1_persona` scope bug** | Persona panels exclude TB2-TB6 data | Agent Performance |
| **Duration divisor bug** | `duration / 1000000` may yield wrong units from nanosecond OTel spans | Agent Performance |

### Per-Dashboard Health

| Dashboard | Panels | Data | Health | Critical Issues |
|---|---|---|---|---|
| Agent Performance | 6 | 6/6 | Degraded | Duration divisor bug, persona scope, panel redundancy |
| Loop Health | 6 | 6/6 | Degraded | Hardcoded 7d intervals on 2 panels, outlier compression |
| Quality Gate Insights | 5 | 4/5 | Degraded | Pass series omitted, x-axis conflation, CWE duplication |
| DORA Metrics (Proxy) | 4 | 3/4 | Degraded | Panel 1 dead (no intake spans), duplicate panels 2/4, garbled x-axis |
| Cost Tracking | 5 | 0/5 | Broken | All panels dead — `devloop_cost_spent_usd` not in schema |
| Calibration | 8 | 0/8 | Broken | All panels dead — `dev-loop-ambient` service not emitting, 6 missing columns |

### Collection Infrastructure Gaps Discovered
1. **Canvas timing**: DOM extraction runs before canvas charts paint → false `noData: true`
2. **Tiny panel screenshots**: Sub-pixel GridStack child elements produce <500 byte PNGs that fail image processing → filter by size >1KB
3. **API response parsing**: Some OO search responses couldn't be parsed → `"error": "Could not parse response body"`
4. **Time range picker**: UI button text doesn't match hardcoded labels (`"Past 7 Days"` vs OO's actual text) → time range switching failed silently
5. **Below-fold lazy loading**: Panels outside viewport report `visible: false` and may not have API calls captured

---

## Files Created

### In `~/dashboard-mirror/` (committed to git)
- `pyproject.toml`, `CLAUDE.md`, `README.md`, `.gitignore`
- `src/dashboard_mirror/` — 5 Python modules
- `prompts/` — 5 agent prompt templates

### In `~/dashboard-mirror/output/` (gitignored, regenerated)
- `_baseline/baseline-report.md` — cross-dashboard analysis
- `_baseline/stream-schema.json` — 4 streams, 217 fields
- `_baseline/cross-dashboard-map.json` — 26 shared columns
- `<slug>/grounding.md` × 6 — the deliverables
- `<slug>/analyst-{structure,data,ux}.md` × 6 — intermediate analyst reports
- `<slug>/screenshots/`, `dom/`, `api/`, `config/` — raw mirror data

### In memory
- `~/.claude/projects/-home-musicofhel/memory/dashboard-mirror.md`
- Updated `MEMORY.md` repo list

---

## How to Re-Run

```bash
cd ~/dashboard-mirror

# Phase 1: Collection (requires OO running with dashboards imported)
uv run dm-schema
uv run dm-chain --config-dir ~/dev-loop/config/dashboards
uv run dm-collect

# Phase 2-4: Analysis (in a Claude Code session)
# Launch baseline agent, then 3 analysts per dashboard in parallel,
# then 1 synthesizer per dashboard after its 3 analysts complete.
# See prompts/ for agent instructions.
```

---

## Recommended Next Steps

1. **Fix hardcoded time intervals** in all dashboard SQL queries (replace `INTERVAL '30 DAYS'` with OO time picker binding)
2. **Fix `_make_fields()` x-axis detection** in `import-dashboards.py` — the heuristic places non-aggregate columns on x-axis even when they're metric values
3. **Fix duration divisor** on Agent Performance — verify if OTel `duration` is nanoseconds or microseconds
4. **Fix persona scope** — change `tb1_persona` to a broader attribute or use `LIKE 'tb%_persona'`
5. **Deduplicate DORA panels** 2 and 4
6. **Add `passed` series** to Quality Gate Insights panel 1
7. **Improve collection script** — wait longer for canvas paint before DOM extraction, fix time range picker button matching
8. **Instrument cost telemetry** in dev-loop to unblock Cost Tracking dashboard
9. **Deploy ambient layer** to unblock Calibration dashboard
