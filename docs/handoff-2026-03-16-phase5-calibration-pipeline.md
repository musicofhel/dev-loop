# Handoff: Calibration Phase 5 — Continuous Calibration Pipeline

**Date**: 2026-03-16
**Session**: Implement Phase 5 from `docs/testing-calibration-plan.yaml`
**Previous**: `handoff-2026-03-16-phase4-feedback-loop.md`

---

## What Was Implemented

### p5.1: Calibration Pipeline Script

**New file**: `scripts/calibrate.sh` — aggregate pipeline running all 5 calibration stages.

Five stages:
1. **Shadow Report** — `dl shadow-report --last 168 --csv` (last 7 days)
2. **Replay Harness** — parse all session JSONLs, replay 2000 tool calls, score, compare against baseline
3. **Tier 2 Suite** — `pytest tests/tier2/` (28 planted-defect scenarios)
4. **Feedback Scoring** — `scripts/feedback/score.py --json` (precision/recall/F1 per check type)
5. **Rust Tests** — `cargo test` (257 tests across lib+bin+integration)

**Features**:
- Dated markdown report at `docs/calibration/YYYY-MM-DD.md`
- Baseline regression detection (replay + feedback)
- `--skip-rust` flag for faster runs
- Exit 1 if any regressions detected
- Regression tracking in report

**CLI changes**:
- `just calibrate [--skip-rust]` — run full pipeline
- `just calibrate-baseline` — save current state as replay + feedback baselines

### p5.2: Calibration Dashboard

**New file**: `config/dashboards/calibration.json` — 8-panel OpenObserve dashboard.

Panels:
1. Shadow Verdicts (7d) — metric
2. Shadow Block Rate — metric
3. Check Verdicts by Type — bar chart
4. Verdict Distribution Over Time — line chart
5. Top Blocked Patterns — table
6. Replay Block Rate Trend — line chart (30d)
7. Sessions by Outcome — pie chart
8. Feedback Labels (cumulative) — metric

Import via `just stack-import` (existing recipe already updated).

---

## Files Created/Modified

| File | Change |
|------|--------|
| `scripts/calibrate.sh` | **NEW**: 5-stage calibration pipeline |
| `config/dashboards/calibration.json` | **NEW**: 8-panel OO dashboard |
| `justfile` | Added `calibrate` and `calibrate-baseline` recipes |
| `docs/calibration/2026-03-16.md` | **NEW**: first calibration report (auto-generated) |

---

## Validated Results

```
Stage 1: 1 shadow verdict collected
Stage 2: 10,093 tool calls parsed, 2000 replayed — 9 blocked, 8 warned
         No regressions vs 2026-03-baseline.json
Stage 3: 28 planted-defect tests passed
Stage 4: 3 labels, F1=0.8
Stage 5: 257 Rust tests passed
RESULT: PASS — no regressions detected
```

---

## Key Design Decisions

1. **5 stages, not 6** — Conformance tests (`scripts/conformance/`) could be a 6th stage, but they test hook protocol conformance (a build-time concern), not calibration accuracy. Keep the pipeline focused on calibration.

2. **Max 2000 replayed calls** — Full replay of 10K+ calls takes too long for a routine pipeline. 2000 is statistically representative. Use `just replay-full` for exhaustive runs.

3. **Parse to temp file** — Avoids `pipefail` + `head` SIGPIPE interaction. The parser writes all 10K+ tool calls to a temp NDJSON, then `head -2000` feeds the replay runner.

4. **`--skip-rust` flag** — Rust compilation can take 2+ minutes if incremental cache is cold. Skip for rapid calibration during development; include for CI/nightly runs.

5. **`find` instead of `ls` for baselines** — `ls *.json` fails with `set -e` when no files match. `find` returns empty gracefully.

---

## Usage

```bash
# Run full calibration pipeline
just calibrate

# Skip Rust tests for faster feedback
just calibrate --skip-rust

# Save baselines for future regression detection
just calibrate-baseline

# Import calibration dashboard into OpenObserve
just stack-import
```

---

## All Calibration Phases Complete

| Phase | Name | Status |
|-------|------|--------|
| 0 | Fix Silent Failures | DONE |
| 1 | Shadow Mode | DONE |
| 2 | Replay Harness | DONE |
| 3 | Planted-Defect Suite | DONE |
| 4 | Per-Check Feedback Loop | DONE |
| 5 | Continuous Calibration Pipeline | DONE |

The testing calibration plan (`docs/testing-calibration-plan.yaml`) is fully implemented.
