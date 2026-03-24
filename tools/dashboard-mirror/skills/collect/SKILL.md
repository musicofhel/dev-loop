---
name: collect
description: "Capture mirror bundles from OpenObserve — health, schemas, alerts, functions, traces, supplementary data, config chains, screenshots, and DOM."
license: MIT
metadata:
  author: musicofhel
  version: "0.2.0"
  category: collection
---

# When to Use

Use this skill when you need to capture or refresh the raw mirror data from OpenObserve. This is Phase 1 of the pipeline — it produces the data that analysis agents consume.

# Prerequisites

- OpenObserve running and accessible (default: `http://localhost:5080`)
- Python 3.11+ with `uv`
- Playwright chromium installed: `uv run playwright install chromium`
- Dashboard configs in `DM_CONFIG_DIR` (default: `~/dev-loop/config/dashboards`)

# Commands

Run from the `~/dashboard-mirror` directory.

## Recommended: Run Everything

```bash
uv run dm-collect-all                   # Full pipeline (~4-6 min)
uv run dm-collect-all --skip-playwright # API-only (~30-40s)
```

`dm-collect-all` orchestrates all 8 collection steps in sequence and produces a summary report at the end showing OO version, health, stream/alert/function/pipeline/trace counts.

## Individual Collectors

### 1. OO Health & Config

Fetch healthz, config, runtime config, org settings, org summary, and cluster info:

```bash
uv run dm-health
```

Output: `output/_baseline/oo-health.json`, `oo-config.json`, `oo-org-settings.json`, `oo-org-summary.json`, `oo-cluster.json`

### 2. Stream Schema

Fetch all stream field definitions, sample data, and cross-dashboard metric map:

```bash
uv run dm-schema
```

Output: `output/_baseline/stream-schema.json`, `cross-dashboard-map.json`

### 3. Alerts & Incidents

Fetch alert rules, firing history, incidents, templates, destinations, dedup config. Compares against source YAML for drift detection and validates SQL columns against stream schema:

```bash
uv run dm-alerts
uv run dm-alerts --alerts-config ~/dev-loop/config/alerts/rules.yaml
```

Output: `output/_baseline/alerts.json`, `alert-history.json`, `alert-incidents.json`, `alert-templates.json`, `alert-destinations.json`, `alert-dedup.json`, `alert-drift.json`, `alert-schema-coverage.json`

### 4. Functions & Pipelines

Fetch VRL functions, pipelines, pipeline-stream associations, and modification history. Each function gets automatic VRL field analysis (reads, writes, deletes, possible renames):

```bash
uv run dm-functions
```

Output: `output/_baseline/functions.json`, `pipelines.json`, `pipeline-streams.json`, `pipeline-history.json`

### 5. Trace Analysis

Deep trace structure analysis — service inventory, operation catalog, attribute coverage, trace hierarchy validation, DAG structure, and duration distributions:

```bash
uv run dm-traces
```

Output: `output/_baseline/trace-services.json`, `trace-operations.json`, `trace-attributes.json`, `trace-structure.json`, `trace-dag.json`, `trace-durations.json`

### 6. Supplementary Data

Fetch saved views, enrichment tables, reports, dashboard annotations, and folder structure:

```bash
uv run dm-supplementary
```

Output: `output/_baseline/saved-views.json`, `enrichment-tables.json`, `reports.json`, `annotations.json`, `folders.json`

### 7. Config Chain Capture

Trace dashboard configs through 4 transformation stages (source → transformed → sent → stored) and generate diffs:

```bash
uv run dm-chain --config-dir ~/dev-loop/config/dashboards
```

Output per dashboard: `output/<slug>/config/{source,transformed,sent,stored}.json` + `chain-diff.txt`

### 8. Full Mirror Collection (Playwright)

Launch Playwright to capture screenshots, DOM text, API responses, layout metrics, and timing for all dashboards:

```bash
uv run dm-collect
```

To target specific dashboards:

```bash
uv run dm-collect --dashboard dora-metrics-proxy --dashboard loop-health
```

Output per dashboard:
- `screenshots/` — full-page, viewport stops, per-panel PNGs
- `screenshots-{1h,7d}/` — same at different time ranges
- `dom/` — text-content.json, layout-metrics.json, chart-data.json
- `api/` — queries-executed.json, errors.json
- `timing.json`, `meta.json`

# Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `OPENOBSERVE_URL` | `http://localhost:5080` | OO base URL |
| `OPENOBSERVE_USER` | `admin@dev-loop.local` | Login username |
| `OPENOBSERVE_PASS` | `devloop123` | Login password |
| `OPENOBSERVE_ORG` | `default` | OO organization |
| `DM_OUTPUT` | `./output` | Output directory |
| `DM_CONFIG_DIR` | `~/dev-loop/config/dashboards` | Source config directory |
| `DM_ALERTS_CONFIG` | `~/dev-loop/config/alerts/rules.yaml` | Alert rules source YAML |

# Typical Flow

```bash
cd ~/dashboard-mirror
uv run dm-collect-all                  # Everything (~4-6 min)
# OR for fast API-only refresh:
uv run dm-collect-all --skip-playwright  # ~30-40s
```

# API Client

All collectors share `api.py` which provides:
- `api_get(path, org)` — authenticated GET to `/api/{org}/{path}`
- `api_get_v2(path, org)` — authenticated GET to `/v2/{org}/{path}` (alerts)
- `api_post(path, data, org)` — authenticated POST
- `api_get_noauth(path)` — unauthenticated GET (healthz, config)
- `api_get_root(path)` — authenticated GET to non-org-scoped paths
- `search(sql, stream_type, size)` — OO SQL search with 30-day window
- `is_error(result)` — check if result is an error string
