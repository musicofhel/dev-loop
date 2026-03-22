# dashboard-mirror

## What This Is

A standalone tool that captures a complete mirror of OpenObserve dashboards (screenshots, DOM, API, config diffs) and runs multi-agent analysis to produce grounding documents — canonical references for what each dashboard looks like.

## Quick Start

```bash
cd ~/dashboard-mirror
uv sync && uv run playwright install chromium
```

## Running the Pipeline

### Phase 1: Collection (automated scripts)

```bash
# 1. Capture stream schema (baseline)
uv run dm-schema

# 2. Capture transformation chain diffs
uv run dm-chain --config-dir ~/dev-loop/config/dashboards

# 3. Collect mirror data from all dashboards (Playwright)
uv run dm-collect
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
  collect.py         — Playwright-based mirror data collection
  schema.py          — OO stream schema capture
  transform_chain.py — Config transformation chain diffing
  cross_map.py       — Cross-dashboard metric/column mapping
  config.py          — Shared configuration (env vars)

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

## Dependencies

- Python 3.11+
- uv
- Playwright (chromium)
- OpenObserve running with dashboards imported
