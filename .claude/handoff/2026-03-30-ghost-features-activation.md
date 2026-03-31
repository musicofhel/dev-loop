# Handoff: Ghost Features Activation (v7)

**Date:** 2026-03-30
**Status:** Code complete, tests passing (708/708), NOT committed, NOT validated with live TBs

## What Was Done

### Context
The v6 `dm-traces` analysis found 265 columns in the OpenObserve schema. 124 are populated. 141 are ghost columns — attributes set by code that has never been exercised during a TB run. This session wired the ghost code paths into the pipeline.

### Fix 1: Gate 5 (Cost) Wired into `run_all_gates()` — 7 ghost attributes

**`src/devloop/gates/server.py`** — `run_all_gates()` now accepts optional `num_turns`, `input_tokens`, `output_tokens` (default 0). Gate 5 runs after Gate 4 as **informational only** (never triggers fail-fast). `total_gates` updated 6 -> 7.

**5 callers updated** to thread `agent_result` token data:
- `src/devloop/feedback/tb1_golden_path.py:523` — initial gates
- `src/devloop/feedback/tb2_retry.py:437` — initial gates
- `src/devloop/feedback/tb4_runaway.py:538` — initial gates
- `src/devloop/feedback/tb6_replay.py:487` — initial gates
- `src/devloop/feedback/server.py:419` — `retry_agent()` re-run gates

TB-3 uses `run_gate_3_security_standalone`, not `run_all_gates()` — no change needed.

Backwards-compatible: callers that don't pass token args get defaults of 0, which means Gate 5 passes vacuously.

### Fix 2: Post-Pipeline Feedback Channels — 12 ghost attributes

**New file: `src/devloop/feedback/post_pipeline.py`**
- `run_post_pipeline(issue_id, session_events=None, success=False)` -> dict
- Channel 2: `detect_patterns(hours=1)` — sets `post_pipeline.patterns_found`
- Channel 3: `get_usage_summary(hours=24)` + `check_budget()` — sets `post_pipeline.cost_pause_recommended`, `post_pipeline.cost_warnings_count`
- Channel 7: `analyze_efficiency(session_events)` — sets `post_pipeline.efficiency_score`, `post_pipeline.efficiency_waste_ratio` (only runs if `session_events` provided)
- Sets `post_pipeline.channels_run` count
- All best-effort, logged at DEBUG on failure

**Wired into 4 TB finally blocks** (before `force_flush`, after `_unclaim_issue`):
- TB-1 (`tb1_golden_path.py:~893`)
- TB-2 (`tb2_retry.py:~793`)
- TB-4 (`tb4_runaway.py:~837`)
- TB-6 (`tb6_replay.py:~660`)

Channel 5 (changelog) deliberately NOT wired — needs multiple closed issues to be useful, stays as standalone `just changelog`.

Session events not passed yet (would need NDJSON parsing from session_path). Efficiency channel will only fire when explicitly given events. Patterns + cost fire every run.

### Fix 3: Ambient Daemon Built

`cargo build --release` succeeded. Binary at `daemon/target/release/dl` (6.7MB). Ready to start.

### Test Fix

`tests/test_new_gates.py` — two tests (`test_all_gates_run_on_success`, `test_skip_propagation`) updated to mock `run_gate_5_cost` and expect 7 gate results.

## Uncommitted Files

```
Modified:
  src/devloop/gates/server.py          — Gate 5 in run_all_gates
  src/devloop/feedback/server.py       — retry_agent passes tokens
  src/devloop/feedback/tb1_golden_path.py — Gate 5 tokens + post-pipeline
  src/devloop/feedback/tb2_retry.py    — Gate 5 tokens + post-pipeline
  src/devloop/feedback/tb4_runaway.py  — Gate 5 tokens + post-pipeline
  src/devloop/feedback/tb6_replay.py   — Gate 5 tokens + post-pipeline
  tests/test_new_gates.py             — Updated gate count assertions
  .beads/issues.jsonl                  — beads state (ignore)

Untracked:
  src/devloop/feedback/post_pipeline.py — NEW: post-pipeline channel runner
```

## What's Left (Validation Runs)

### Phase 2: Run TBs to generate trace data
```bash
# TB-1 (Gate 5 + post-pipeline)
br create "Ghost: add power function" --labels feature --silent
just tb1 <id> ~/OOTestProject1

# TB-2 (retry + Gate 5)
br create "Ghost: fix rounding" --labels bug --silent
just tb2-force <id> ~/OOTestProject1

# TB-6 (session capture + post-pipeline)
br create "Ghost: add floor division" --labels bug --silent
just tb6 <id> ~/OOTestProject1
```

### Phase 3: Force escalation (2 ghost attributes)
```bash
br create "Ghost: force escalation test" --labels bug --silent
uv run python -c "
from devloop.feedback.tb2_retry import run_tb2
import json
result = run_tb2('<id>', '$HOME/OOTestProject1', max_retries=0, force_gate_fail=True)
print(json.dumps({k: v for k, v in result.items() if k not in ('gate_results',)}, indent=2))
"
```

### Phase 4: Start ambient daemon (~20 ghost attributes)
```bash
cd ~/dev-loop/daemon
./target/release/dl start
./target/release/dl status
# Then run a Claude Code session in OOTestProject1 to generate ambient data
cd ~/OOTestProject1 && claude "Read README.md and tell me what this project does"
```

### Phase 5: Verify in OpenObserve
Query for `gates.gate_5_cost` spans, `feedback.post_pipeline` spans, `feedback.escalate` spans, and `service_name = 'dev-loop-ambient'` spans.

### Phase 6: Re-run dm-traces
```bash
cd ~/dashboard-mirror && uv run dm-traces
```
Target: 145-165 of 265 populated (55-62%), up from 124 (47%).

### Phase 7: Commit + validation report
Commit message: `Activate ghost features: Gate 5, post-pipeline channels, escalation path, ambient layer`
Save report to `docs/validation-report-2026-03-30-v7.md`.

## Expected Attribute Yield

| Group | Attributes | Source |
|-------|-----------|--------|
| Gate 5 (cost) | 7 | `gate.status`, `gate.duration_ms`, `gate.findings_count`, `gate.num_turns`, `gate.input_tokens`, `gate.output_tokens`, `gate.total_tokens` |
| Post-pipeline channels | 12 | pattern, cost, efficiency attributes across 3 channels |
| Escalation path | 2 | `escalate.comment_added`, `escalate.status_updated` |
| Ambient layer | ~20 | session, check, guardrail attributes (daemon must be running) |
| **Total** | **21-41** | **145-165 populated (55-62%)** |
