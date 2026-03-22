---
name: collect
description: "Capture mirror bundles from OpenObserve dashboards — screenshots, DOM text, API responses, config chains, layout metrics."
license: MIT
metadata:
  author: musicofhel
  version: "0.1.0"
  category: collection
---

# When to Use

Use this skill when you need to capture or refresh the raw mirror data from OpenObserve dashboards. This is Phase 1 of the pipeline — it produces the data that analysis agents consume.

# Prerequisites

- OpenObserve running and accessible (default: `http://localhost:5080`)
- Python 3.11+ with `uv`
- Playwright chromium installed: `uv run playwright install chromium`
- Dashboard configs in `DM_CONFIG_DIR` (default: `~/dev-loop/config/dashboards`)

# Commands

Run from the `~/dashboard-mirror` directory:

## 1. Schema Capture

Fetch all stream field definitions and sample data from OO:

```bash
uv run dm-schema
```

Output: `output/_baseline/stream-schema.json`

## 2. Config Chain Capture

Trace dashboard configs through 4 transformation stages (source → transformed → sent → stored) and generate diffs:

```bash
uv run dm-chain --config-dir ~/dev-loop/config/dashboards
```

Output per dashboard: `output/<slug>/config/{source,transformed,sent,stored}.json` + `chain-diff.txt`

## 3. Full Mirror Collection

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

# Typical Flow

```bash
cd ~/dashboard-mirror
uv run dm-schema                                    # ~10s
uv run dm-chain --config-dir ~/dev-loop/config/dashboards  # ~5s
uv run dm-collect                                   # ~3-5 min for 6 dashboards
```
