---
name: analyze
description: "Run multi-agent analysis pipeline on collected mirror data — baseline, 3 per-dashboard analysts (structure, data, UX), and synthesizer."
license: MIT
metadata:
  author: musicofhel
  version: "0.1.0"
  category: analysis
---

# When to Use

Use this skill after collection is complete and `output/` contains mirror bundles. This runs Phases 2-4 of the pipeline.

# Pipeline Stages

## Phase 2: Baseline Analysis (1 agent)

Runs once across ALL dashboards. Reads:
- `output/_baseline/stream-schema.json`
- `output/*/config/source.json`, `sent.json`, `chain-diff.txt`

Prompt template: `prompts/baseline.md`

Output: `output/_baseline/baseline-report.md`

Tasks:
1. Schema coverage audit (every column in every query vs stream schema)
2. Spec compliance check
3. Cross-dashboard consistency (naming, colors, queries, granularity)
4. Transformation drift (config mutations)

## Phase 3: Per-Dashboard Analysis (3 agents per dashboard, parallel)

For each dashboard, spawn 3 independent analysts:

### Analyst A — Structure (`prompts/analyst-structure.md`)
- Layout, sizing, visibility, grid integrity
- Config drift (sent vs stored)
- Output: `output/<slug>/analyst-structure.md`

### Analyst B — Data & Labels (`prompts/analyst-data.md`)
- Query correctness, schema coverage, data presence
- Labels, axes, legends, time range behavior
- Output: `output/<slug>/analyst-data.md`

### Analyst C — UX & Polish (`prompts/analyst-ux.md`)
- Readability, error states, information hierarchy
- Chart effectiveness, loading performance, color coherence
- Output: `output/<slug>/analyst-ux.md`

## Phase 4: Synthesis (1 agent per dashboard, after Phase 3)

Reads all 3 analyst reports + baseline + screenshots. Resolves contradictions with visual evidence.

Prompt template: `prompts/synthesizer.md`

Output: `output/<slug>/grounding.md` — the canonical grounding document

# Invocation Pattern

Each agent receives its full prompt template + specific file paths:

```
You are Analyst B (Data & Labels). Here is your prompt:
[contents of prompts/analyst-data.md]

Analyze this dashboard:
- Screenshots: output/loop-health/screenshots/
- DOM: output/loop-health/dom/
- API: output/loop-health/api/
- Baseline schema: output/_baseline/stream-schema.json

Write your analysis to: output/loop-health/analyst-data.md
```

# Parallelism

- Baseline must complete before per-dashboard analysts start
- The 3 analysts for each dashboard run in parallel (independent lenses)
- Synthesis runs after all 3 analysts complete for that dashboard
- Multiple dashboards can be analyzed in parallel
