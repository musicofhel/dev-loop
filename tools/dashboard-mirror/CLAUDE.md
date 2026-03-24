# dashboard-mirror

## What This Is

A standalone tool that captures a complete mirror of OpenObserve dashboards (screenshots, DOM, API, config diffs, alerts, traces, functions, health, supplementary objects) and runs multi-agent analysis to produce grounding documents — canonical references for what each dashboard looks like.

## Quick Start

```bash
cd ~/dashboard-mirror
uv sync && uv run playwright install chromium
```

## Running the Pipeline

### Phase 1: Collection (automated scripts)

**Recommended — run everything at once:**

```bash
cd ~/dashboard-mirror
uv run dm-collect-all              # Full pipeline (~4-6 min)
uv run dm-collect-all --skip-playwright  # API-only (~30-40s)
```

**Or run individual collectors:**

```bash
uv run dm-health          # OO health, config, cluster
uv run dm-schema          # Stream schemas + cross-dashboard map
uv run dm-alerts           # Alerts, incidents, destinations, drift
uv run dm-functions        # VRL functions, pipelines, VRL field analysis
uv run dm-traces           # Trace structure, durations, attributes
uv run dm-supplementary    # Views, reports, annotations, folders
uv run dm-chain            # Config transformation diffs
uv run dm-collect          # Playwright screenshots + DOM
```

All output goes to `./output/`.

### Phase 2-4: Analysis (agent-driven)

Run from this directory in a Claude Code session. The pipeline is:

1. **Baseline agent** — reads `output/_baseline/` + all `config/` dirs, writes `output/_baseline/baseline-report.md`
2. **Per-dashboard: 3 analysts in parallel** — each reads one dashboard's mirror bundle
   - Analyst A (Structure): `prompts/analyst-structure.md`
   - Analyst B (Data): `prompts/analyst-data.md`
   - Analyst C (UX): `prompts/analyst-ux.md`
3. **Per-dashboard: 1 synthesizer** — reads 3 analyst reports + baseline, writes `output/<slug>/grounding.md`

Prompt templates are in `prompts/`. Each analyst should be given the full prompt from its template plus the paths to the mirror data files.

## Project Structure

```
src/dashboard_mirror/
  api.py             — Shared OO API client (GET, POST, v2, noauth, root, search)
  config.py          — Shared configuration (env vars)
  health.py          — OO instance health, config, cluster info
  schema.py          — Stream schema capture + cross-map trigger
  alerts.py          — Alert rules, history, incidents, drift detection, schema coverage
  functions.py       — VRL functions, pipelines, VRL field analysis
  traces.py          — Service inventory, operations, attributes, hierarchy, DAGs, durations
  supplementary.py   — Saved views, enrichment tables, reports, annotations, folders
  collect.py         — Playwright-based mirror data collection
  transform_chain.py — Config transformation chain diffing
  cross_map.py       — Cross-dashboard metric/column mapping
  collect_all.py     — Orchestrator for all 8 collectors

prompts/
  baseline.md        — Baseline analyst prompt
  analyst-structure.md — Structure analyst prompt
  analyst-data.md    — Data & labels analyst prompt
  analyst-ux.md      — UX & polish analyst prompt
  synthesizer.md     — Grounding doc synthesizer prompt
```

## Environment Variables

- `OPENOBSERVE_URL` (default: `http://localhost:5080`)
- `OPENOBSERVE_USER` (default: `admin@dev-loop.local`)
- `OPENOBSERVE_PASS` (default: `devloop123`)
- `OPENOBSERVE_ORG` (default: `default`)
- `DM_OUTPUT` (default: `./output`)
- `DM_CONFIG_DIR` (default: `~/dev-loop/config/dashboards`)
- `DM_ALERTS_CONFIG` (default: `~/dev-loop/config/alerts/rules.yaml`)

## Dependencies

- Python 3.11+
- uv
- Playwright (chromium)
- PyYAML (for alert drift detection)
- OpenObserve running with dashboards imported
