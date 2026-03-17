# Handoff: Calibration Phase 2 — Replay Harness

**Date**: 2026-03-16
**Session**: Implement Phase 2 from `docs/testing-calibration-plan.yaml`
**Previous**: `handoff-2026-03-16-phase1-shadow-mode.md`

---

## What Was Implemented

### p2.1: Session JSONL Parser (Enhanced)

**File**: `scripts/replay/parse_sessions.py` (existed from calibration step 6)

**Enhancement**: Added `timestamp` field extraction from Claude Code JSONL entries (ISO 8601 format, e.g. `2026-03-16T12:00:00Z`). Previously only extracted tool name, input, and line number.

### p2.2: Replay Runner (Enhanced)

**File**: `scripts/replay/run_replay.py` (existed from calibration step 6)

**Enhancements**:
- `--workers N` flag: parallel replay using `ThreadPoolExecutor` (default: 1, recommended: 4). Order-preserving — results indexed by input position.
- `--json` flag: outputs NDJSON (one line per verdict) + final summary line with `_type: "summary"` marker. Summary includes total/allow/block/warn/error counts, by_check_type breakdown, top patterns, top blocked files.
- Refactored check logic into `check_one()` function for parallelization.

### p2.3: Replay Scoring (NEW)

**File**: `scripts/replay/score.py` (new)

Two modes depending on whether data has `expected_verdict` labels:

**Labeled data** (precision/recall/F1):
- Computes per-check-type and overall precision/recall/F1
- A "positive" = block or warn verdict
- Reports TP/FP/FN/TN counts

**Unlabeled data** (frequency analysis):
- Verdict distribution (allow/block/warn/error)
- By check type and tool breakdown
- Top blocked files, top triggered reasons
- **Likely false positives**: files matching known FP patterns (`.env.example`, `.env.template`, `.env.sample`)
- **Repeat-blocked files**: anything blocked >5 times flagged as potential FP
- **Potential false negatives**: sensitive-looking files (`.env`, `.pem`, `.key`, `credentials`, `secret`, `id_rsa`) that were allowed through

**Baseline comparison** (`--baseline <file>`):
- Detects block rate increases (>50% relative + >1% absolute)
- Detects warn rate increases
- Detects new repeat-blocked files
- Exit code 1 on regression

### p2.4: Baseline Replay Run

**File**: `scripts/replay/baselines/2026-03-baseline.json`

Full replay across all 74 sessions, 9,966 tool calls:

| Verdict | Count | Rate |
|---------|-------|------|
| Allow   | 9,825 | 98.6% |
| Block   | 96    | 1.0% |
| Warn    | 45    | 0.5% |

**Key findings**:
- 3 repeat-blocked files (>5 times = likely FPs):
  - `secrets.rs` (14x) — `*secret*` deny pattern matches source file name
  - `.env.example` in link-forge (6x) — `.env.*` pattern matches example files
  - `.env` in link-forge (6x) — correct block (real .env file)
- Top warn reasons: `rm -rf` (38x), `kill -9` (11x), `git branch -D` (6x), `chmod -R` (4x), `DELETE FROM` (3x) — all correct warns
- 14 known FP instances: all `.env.example` files across repos
- 0 potential false negatives detected

### Tests

**File**: `tests/replay/test_replay.py` (new, 19 tests)

| Category | Tests |
|----------|-------|
| parse_sessions | 7 (extract Write/Bash, skip non-checkable/user, malformed lines, session ID, multi-tool messages) |
| score | 12 (precision/recall perfect/FP/FN/per-check, analyze unlabeled, FP/FN detection, is_sensitive_file, is_known_fp, baseline comparison no-reg/block-increase/new-repeats) |

---

## Justfile Recipes

| Recipe | Purpose |
|--------|---------|
| `just replay N` | Quick replay of first N calls (default 500) |
| `just replay-full [WORKERS]` | Full replay with scoring (default 4 workers) |
| `just replay-baseline [WORKERS]` | Generate dated baseline JSON |
| `just replay-score BASELINE` | Score current state against a baseline |
| `just replay-stats` | Tool call counts without checking |
| `just replay-test` | Run Python replay tests |

---

## Files Created/Modified

| File | Change |
|------|--------|
| `scripts/replay/parse_sessions.py` | Added timestamp extraction |
| `scripts/replay/run_replay.py` | Added `--workers`, `--json`, refactored `check_one()` |
| `scripts/replay/score.py` | **NEW**: precision/recall, frequency analysis, FP/FN detection, baseline comparison |
| `scripts/replay/baselines/2026-03-baseline.json` | **NEW**: baseline across 9,966 tool calls |
| `tests/replay/__init__.py` | **NEW**: package init |
| `tests/replay/test_replay.py` | **NEW**: 19 tests for parser + scorer |
| `justfile` | Added `replay-full`, `replay-baseline`, `replay-score`, `replay-test` recipes |

---

## Test Counts

| Category | Count |
|----------|-------|
| Rust unit tests (lib) | 79 |
| Rust unit tests (bin) | 166 |
| Turmoil integration | 4 |
| Conformance (Python) | 106 |
| Replay harness (Python) | 19 |
| **Total** | **249 + 106 + 19 = 374** |

---

## Usage

```bash
# Parse all sessions (shows stats)
just replay-stats

# Quick replay (500 calls, summary only)
just replay

# Full replay with scoring (4 workers)
just replay-full

# Generate baseline
just replay-baseline

# Check for regressions after config changes
just replay-score scripts/replay/baselines/2026-03-baseline.json

# Run tests
just replay-test
```

---

## Next Session

From the calibration plan, remaining phases:
- **Phase 3**: Planted-defect suite — integration test repos with known vulnerabilities
- **Phase 4**: Per-check feedback — `dl feedback` command, labeled data
- **Phase 5**: Continuous calibration pipeline

Recommended: Phase 3 next (planted-defect suite). The baseline replay data provides the unlabeled frequency analysis; Phase 3 creates the labeled ground truth for precision/recall measurement.
