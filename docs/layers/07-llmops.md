# Layer 7: LLMOps — DSPy/GEPA Prompt Optimization

## Purpose

Optimize dev-loop's own LLM prompts against measured metrics. Uses DSPy + GEPA (MIPROv2) to iteratively improve prompt quality based on session history as training data.

**Target prompts**: Gate 4 code review, retry prompts (Channel 1), persona selection.

## Architecture

```
Session History (L6)
    │
    ▼
Training Data Export ──► JSONL files
    │
    ▼
GEPA Optimization ──► Optimized DSPy Artifacts
    │
    ▼
Feature-Flagged Gate 4 ──► Findings (same schema)
    │
    ▼
Langfuse Traces ──► Prompt Versioning + Eval Datasets
```

## Components

### DSPy Programs (`src/devloop/llmops/programs/`)

Each program defines a DSPy Signature, Module, and metric function:

- **code_review** — Gate 4 LLM review. Signature: `(diff, issue_context, review_criteria) → findings_json`. Metric: F1-based with severity accuracy bonus.
- **retry_prompt** — Channel 1 retry instructions. Signature: `(failure_log, original_task, gate_results) → retry_instructions`. Metric: outcome-based.
- **persona_select** — Persona assignment. Signature: `(issue_labels, issue_description, repo_type) → (persona_id, custom_guidelines)`. Metric: exact match + outcome signal.

### Training Data Pipeline (`src/devloop/llmops/training/`)

Extracts training examples from Claude Code session JSONLs:

- `export_reviews.py` — Gate 4 review prompts and structured findings
- `export_retries.py` — Retry prompts with success/failure outcomes
- `export_personas.py` — Persona selection events

Output: JSONL files at `~/.local/share/dev-loop/llmops/training/`.

### Optimization (`src/devloop/llmops/optimize.py`)

Runs GEPA/MIPROv2 optimization:
1. Loads training JSONL
2. Splits 80/20 train/val
3. Evaluates baseline (unoptimized module)
4. Runs MIPROv2 with program-specific config
5. Evaluates optimized module
6. Saves artifact to `~/.local/share/dev-loop/llmops/artifacts/`

### MCP Server (`src/devloop/llmops/server.py`)

4 tools: `optimize_program`, `get_program_status`, `get_optimized_prompt`, `list_programs`.

### Langfuse Bridge (`src/devloop/llmops/langfuse_bridge.py`)

OpenInference instrumentation captures DSPy LM calls and forwards to self-hosted Langfuse (port 3001) via OTLP/HTTP.

## Feature Flag

Controlled by `config/llmops.yaml`:

```yaml
llmops:
  enabled: false  # flip after first successful GEPA optimization
```

- `enabled: false` — Gate 4 uses existing CLI path (`claude --print`). Zero behavior change.
- `enabled: true` — Gate 4 uses DSPy path via Anthropic API. Falls back to CLI on any failure.

Both paths produce the same `Finding` objects and feed into the same severity pass/fail logic.

## Infrastructure

Self-hosted via `docker-compose.yaml`:
- **Langfuse** on port 3001 (prompt versioning, eval datasets, traces)
- **OpenObserve** on port 5080 (existing L5 observability)
- **PostgreSQL 16** backing Langfuse

## Justfile Recipes

```
just llmops-export          # Export training data from session history
just llmops-optimize <prog> # Run GEPA optimization (code_review|retry_prompt|persona_select)
just llmops-status          # Check optimization status for all programs
just stack-up-full          # Start OpenObserve + Langfuse
just stack-down-full        # Stop full stack
```

## Dependencies

- `dspy>=2.6` (v3.1.3 resolved)
- `openinference-instrumentation-dspy>=0.1` (v0.1.34 resolved)
- `langfuse>=2.0` (v4.0.1 resolved)
- `ANTHROPIC_API_KEY` env var required for DSPy path (direct API, not CLI auth)
