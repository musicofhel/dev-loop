# Handoff: Calibration Phase 4 — Per-Check Feedback Loop

**Date**: 2026-03-16
**Session**: Implement Phase 4 from `docs/testing-calibration-plan.yaml`
**Previous**: `handoff-2026-03-16-phase3-planted-defect-suite.md`

---

## What Was Implemented

### p4.1: `dl feedback` Command (Rust)

**New file**: `daemon/src/feedback.rs` — feedback annotation, listing, and stats.

Three modes:
- `dl feedback <event-id> <label> [--notes "..."]` — annotate an event
- `dl feedback --list [--last N]` — show recent unlabeled block/warn events
- `dl feedback --stats` — show precision/recall/F1 per check type

**Event identification**: Line numbers in the JSONL event log (1-indexed). E.g., `L42` or just `42`.

**Labels**: `correct`, `false-positive`, `missed`.

**Storage**: YAML files at `/tmp/dev-loop/feedback/L<n>.yaml` containing:
- Event metadata (check_type, tool_name, verdict, reason, pattern_matched)
- Label and optional notes
- Timestamps (original event ts + feedback ts)

**CLI changes**:
- `daemon/src/cli.rs` — Added `Feedback` variant with optional positional args + flags
- `daemon/src/main.rs` — Added `feedback` module + dispatch logic

### p4.2: Feedback Scoring Script (Python)

**New file**: `scripts/feedback/score.py`

- Reads all feedback YAML, groups by check_type
- Computes per-type: TP, FP, FN, precision, recall, F1
- `--json` for machine-readable output
- `--history` appends to `scripts/feedback/history.jsonl` for tracking over time
- `--baseline FILE` compares against a previous run, detects F1 regressions (>5% drop)

### p4.3: Config Tuning Suggestions (Python)

**New file**: `scripts/feedback/suggest_tuning.py`

- Reads feedback, analyzes FPs and FNs
- For deny_list FPs: suggests `remove_patterns`
- For dangerous_ops FPs: suggests `allow_patterns`
- For secrets FPs: suggests `file_allowlist` entries
- For missed detections: groups by notes, suggests `extra_patterns`
- Generates a `.devloop.yaml` diff snippet for manual review
- `--json` for machine-readable output

### Justfile Recipes

| Recipe | Purpose |
|--------|---------|
| `feedback-score` | Score labeled data (human-readable) |
| `feedback-score-json` | Score as JSON |
| `feedback-score-history` | Score + append to history |
| `feedback-score-baseline BASELINE` | Score against baseline (regression detection) |
| `feedback-suggest` | Show config tuning suggestions |
| `feedback-test` | Run Python feedback tests |

---

## Files Created/Modified

| File | Change |
|------|--------|
| `daemon/src/feedback.rs` | **NEW**: feedback annotation, listing, stats (8 tests) |
| `daemon/src/cli.rs` | Added `Feedback` variant |
| `daemon/src/main.rs` | Added `feedback` module + dispatch |
| `scripts/feedback/score.py` | **NEW**: scoring + history + baseline comparison |
| `scripts/feedback/suggest_tuning.py` | **NEW**: config tuning from feedback |
| `tests/feedback/__init__.py` | **NEW**: package init |
| `tests/feedback/test_feedback.py` | **NEW**: 27 Python tests |
| `justfile` | Added 6 feedback recipes |

---

## Test Counts

| Category | Count |
|----------|-------|
| Rust unit tests (lib) | 79 |
| Rust unit tests (bin) | 174 |
| Turmoil integration | 4 |
| Conformance (Python) | 106 |
| Replay harness (Python) | 19 |
| Tier 2 planted-defect (Python) | 28 |
| Feedback (Python) | 27 |
| **Total** | **257 + 106 + 19 + 28 + 27 = 437** |

---

## Usage

```bash
# List unlabeled block/warn events for review
dl feedback --list

# Annotate an event
dl feedback L42 correct --notes "Correctly blocked .env write"
dl feedback L261 false-positive --notes "secrets.rs is code, not a secret"
dl feedback L100 missed --notes "GitHub PAT not detected"

# View per-check-type precision/recall/F1
dl feedback --stats

# Python scoring (same data, more options)
just feedback-score
just feedback-score-json

# Save baseline for regression detection
just feedback-score-json > scripts/feedback/baselines/2026-03-16.json

# Compare against baseline
just feedback-score-baseline scripts/feedback/baselines/2026-03-16.json

# Get config tuning suggestions
just feedback-suggest
```

---

## Key Design Decisions

1. **Line-number event IDs** — Simple, stable (event log is append-only), no need for UUID generation in events.

2. **YAML feedback files** — One file per annotation, human-readable, easy to inspect/edit. Alternative was a single JSONL file, but per-file makes dedup and partial updates trivial.

3. **Dual scoring** — Rust `dl feedback --stats` for quick terminal use, Python `scripts/feedback/score.py` for history tracking, baseline comparison, and JSON output. Same underlying YAML data.

4. **`--list` filters to block/warn only** — Allow verdicts are uninteresting to label (too many, almost always correct). Focus labeling effort on blocks and warns where FPs are likely.

---

## Binary

- Size: 6.4MB (from 6.5MB — release build variance)
- CLI commands: 26 (up from 25)

---

## Next Session

From the calibration plan, remaining:
- **Phase 5**: Continuous Calibration Pipeline — `just calibrate` aggregate script, OpenObserve dashboard

Recommended: Phase 5 ties everything together (shadow + replay + planted-defect + feedback into a single `just calibrate` command).
