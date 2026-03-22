---
name: ground
description: "Full end-to-end pipeline — collect mirror data, run all analysis agents, produce grounding documents."
license: MIT
metadata:
  author: musicofhel
  version: "0.1.0"
  category: pipeline
---

# When to Use

Use this skill for a complete end-to-end run: collection through grounding document production. This combines the `collect` and `analyze` skills into a single orchestrated pipeline.

# Full Pipeline

```
Phase 1: Collection (automated)
  ├── dm-schema         → output/_baseline/stream-schema.json
  ├── dm-chain          → output/*/config/{source,transformed,sent,stored,chain-diff}
  └── dm-collect        → output/*/screenshots, dom, api, timing, meta

Phase 2: Baseline Analysis (1 agent)
  └── baseline agent    → output/_baseline/baseline-report.md

Phase 3: Per-Dashboard Analysis (3 agents × N dashboards)
  ├── Analyst A (Structure) → output/*/analyst-structure.md
  ├── Analyst B (Data)      → output/*/analyst-data.md
  └── Analyst C (UX)        → output/*/analyst-ux.md

Phase 4: Synthesis (1 agent × N dashboards)
  └── Synthesizer           → output/*/grounding.md
```

# Agent Count

For N dashboards: 1 baseline + 3N analysts + N synthesizers = **4N + 1 agents**

Example: 6 dashboards = 25 agents total

# Prerequisites

- OpenObserve running at `OPENOBSERVE_URL`
- `uv sync && uv run playwright install chromium` completed
- Dashboard configs available at `DM_CONFIG_DIR`

# Running

From `~/dashboard-mirror`:

```bash
# Phase 1 (shell commands)
uv run dm-schema
uv run dm-chain --config-dir ~/dev-loop/config/dashboards
uv run dm-collect

# Phases 2-4 (agent-driven from Claude Code session)
# Baseline first, then per-dashboard analysis, then synthesis
```

# Output

The final deliverable per dashboard is `output/<slug>/grounding.md` — a comprehensive, fact-based, screenshot-referenced document describing exactly what the dashboard looks like and what state it's in.

# Re-running

To refresh a single dashboard:
```bash
uv run dm-collect --dashboard <slug>
```
Then re-run the 3 analysts + synthesizer for that slug only.
