# dashboard-mirror

Visual and structural grounding for OpenObserve dashboards.

## What This Does

Dashboard-mirror captures a **complete mirror** of every dashboard in an OpenObserve instance — screenshots, DOM text, API responses, config diffs, timing data, and stream schema — then feeds that bundle through a multi-agent analysis pipeline to produce **grounding documents**: canonical references that describe exactly what each dashboard looks like, what data it shows, and what's wrong.

### Why

Screenshots alone are lossy. Labels get truncated, colors compress, empty panels look identical to loading panels, and queries can silently return nothing without any visible error. A human glancing at a dashboard misses nuance. Three independent analysts reading the full data bundle catch what one pair of eyes won't.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Phase 1: Collect                      │
│                                                         │
│  Playwright logs into OO, navigates each dashboard,     │
│  and captures:                                          │
│    • Screenshots (full-page, viewport stops, per-panel) │
│    • DOM text (titles, labels, legends, axes, errors)   │
│    • API responses (queries executed, row counts, data) │
│    • Config round-trip (source → transform → OO stored) │
│    • Layout metrics (pixel dims, visibility, overflow)  │
│    • Console errors and network failures                │
│    • Per-panel load timing                              │
│    • Multiple time ranges (1h, 7d, 30d)                │
│                                                         │
│  Also captures baseline data (once, not per-dashboard): │
│    • Stream schema (all columns, types, samples)        │
│    • Spec requirements (from project docs)              │
│    • Cross-dashboard metric map                         │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│               Phase 2: Baseline Analysis                 │
│                                                         │
│  One agent reads the baseline bundle and validates:     │
│    • Which queries reference columns that exist         │
│    • Spec compliance gaps                               │
│    • Cross-dashboard naming/color/query consistency     │
│                                                         │
│  Output: baseline-report.md                             │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│           Phase 3: Per-Dashboard Analysis                │
│                    (3 agents × N dashboards)             │
│                                                         │
│  Analyst A — Structure                                  │
│    Grid layout, panel sizes, spacing, overflow,         │
│    config drift between sent and stored                 │
│                                                         │
│  Analyst B — Data & Labels                              │
│    Query correctness, data presence, axis labels,       │
│    legend entries, series colors, actual values,        │
│    schema coverage                                      │
│                                                         │
│  Analyst C — UX & Polish                                │
│    Readability, error states, contrast, information     │
│    density, "No Data" handling, console health          │
│                                                         │
│  All three run in parallel per dashboard.               │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│            Phase 4: Synthesis                            │
│            (1 agent per dashboard)                       │
│                                                         │
│  Reads: baseline report + 3 analyst reports +           │
│         all screenshots for that dashboard              │
│                                                         │
│  Produces: grounding document (the source of truth)     │
│    • Dashboard overview                                 │
│    • Per-panel ground truth                             │
│    • Cross-cutting observations                         │
│    • Known "No Data" states vs actual bugs              │
│    • Actionable issues list                             │
└─────────────────────────────────────────────────────────┘
```

## Output Structure

```
output/
├── _baseline/
│   ├── stream-schema.json
│   ├── cross-dashboard-map.json
│   └── baseline-report.md            ← Agent 0 output
├── agent-performance/
│   ├── screenshots/
│   │   ├── full-page.png
│   │   ├── viewport-01.png ... viewport-N.png
│   │   └── panel-<id>.png
│   ├── screenshots-1h/
│   ├── screenshots-7d/
│   ├── dom/
│   │   ├── text-content.json
│   │   ├── layout-metrics.json
│   │   └── chart-data.json
│   ├── api/
│   │   ├── queries-executed.json
│   │   └── errors.json
│   ├── config/
│   │   ├── source.json
│   │   ├── transformed.json
│   │   ├── sent.json
│   │   ├── stored.json
│   │   └── chain-diff.txt
│   ├── timing.json
│   ├── meta.json
│   ├── analyst-structure.md           ← Agent A output
│   ├── analyst-data.md                ← Agent B output
│   ├── analyst-ux.md                  ← Agent C output
│   └── grounding.md                   ← Synthesizer output
├── loop-health/
│   └── ...
├── quality-gate-insights/
│   └── ...
├── dora-metrics/
│   └── ...
├── cost-tracking/
│   └── ...
└── calibration/
    └── ...
```

## Usage

### Prerequisites

```bash
cd ~/dashboard-mirror
uv sync
uv run playwright install chromium
```

OpenObserve must be running with dashboards imported.

### Step 1: Collect mirror data

```bash
# Collect from local OO instance (defaults)
uv run dm-collect

# Custom OO instance
uv run dm-collect --url http://oo.example.com:5080 --user admin@example.com --pass secret

# Collect only specific dashboards
uv run dm-collect --dashboard agent-performance --dashboard loop-health

# Custom output directory
uv run dm-collect --output /path/to/output
```

### Step 2: Capture stream schema

```bash
uv run dm-schema
```

### Step 3: Capture transform chain diffs

```bash
# Point at dev-loop config directory
uv run dm-chain --config-dir ~/dev-loop/config/dashboards
```

### Step 4: Run analysis agents

This step is run from within a Claude Code session:

```
> Run the dashboard-mirror analysis pipeline against output/ in ~/dashboard-mirror
```

The agent orchestration (baseline → 3 analysts × N dashboards → synthesis) is driven by Claude Code's Agent tool using the prompt templates in `prompts/`.

## Configuration

Environment variables (with defaults for dev-loop):

| Variable | Default | Description |
|---|---|---|
| `OPENOBSERVE_URL` | `http://localhost:5080` | OO base URL |
| `OPENOBSERVE_USER` | `admin@dev-loop.local` | OO username |
| `OPENOBSERVE_PASS` | `devloop123` | OO password |
| `OPENOBSERVE_ORG` | `default` | OO organization |
| `DM_OUTPUT` | `./output` | Mirror output directory |
| `DM_CONFIG_DIR` | `~/dev-loop/config/dashboards` | Source dashboard configs |

## Grounding Document Format

Each `grounding.md` follows this structure:

```markdown
# <Dashboard Title> — Grounding Document

Generated: <timestamp>
Source: <OO URL>/dashboards/<id>

## Overview
- Panel count: N
- Default time range: 30d
- Data status: X/N panels with data

## Panels

### Panel 1: <Title>
- **Type**: area-stacked
- **Query**: `SELECT ...`
- **Schema coverage**: all columns exist / missing: `col_name`
- **Data state**: 47 rows returned, 12 data points rendered
- **X-axis**: "Day" — values from 2026-03-01 to 2026-03-18
- **Y-axis**: "Total Runs" — range 0 to 156
- **Legend**: 1 series, color #5960b2
- **Labels**: all readable / truncated: "Avg Gate Durat..."
- **Layout**: 1847×306px, no overflow, fully visible
- **Issues**: none / [list]

### Panel 2: ...

## Cross-Cutting Observations
- ...

## Known "No Data" States
- ...

## Actionable Issues
1. ...
```

## Project Philosophy

- **Capture everything, analyze later.** The collection phase is dumb and thorough. Intelligence lives in the agents.
- **Three eyes beat one.** Independent analysts with different lenses catch what a single reviewer misses.
- **Ground truth, not opinion.** Grounding documents state facts (pixel widths, row counts, exact text) not subjective assessments.
- **Reproducible.** Run collect again after fixes to see what changed.
