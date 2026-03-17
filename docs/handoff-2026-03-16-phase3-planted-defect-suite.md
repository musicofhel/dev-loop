# Handoff: Calibration Phase 3 — Planted-Defect Regression Suite

**Date**: 2026-03-16
**Session**: Implement Phase 3 from `docs/testing-calibration-plan.yaml`
**Previous**: `handoff-2026-03-16-phase2-replay-harness.md`

---

## What Was Implemented

### p3.1: Test Wrapper (pytest + tmp_path, no Docker)

Used the fallback approach: plain pytest fixtures with `git init` in `tmp_path`. No Docker/Harbor needed — the `dl checkpoint` CLI command runs offline.

**New CLI command**: `dl checkpoint [--dir DIR] [--json]` — runs Tier 2 gate suite offline without requiring the daemon. Similar to `dl check` for Tier 1.

**Files**:
- `daemon/src/cli.rs` — Added `Checkpoint { dir, json }` variant
- `daemon/src/main.rs` — Added `run_checkpoint_cli()` dispatch (human-readable + JSON output)
- `tests/tier2/__init__.py` — Package init
- `tests/tier2/conftest.py` — `create_git_repo()` fixture (creates temp git repo from corpus, stages files, writes `.devloop.yaml` overrides), `run_checkpoint()` helper (shells out to `dl checkpoint --json`)

### p3.2: Defect Corpus (13 scenarios)

**Directory**: `tests/tier2/corpus/` — each scenario has `files/` and `expected.yaml`

| Scenario | Gate | Expected | Status |
|----------|------|----------|--------|
| `has_leaked_aws_key` | secrets | FAIL | Detected (AKIA pattern) |
| `has_private_key` | secrets | FAIL | Detected (RSA private key) |
| `has_slack_token` | secrets | FAIL | Detected (xoxb- pattern) |
| `has_sql_injection` | semgrep | FAIL | Detected (f-string SQL) |
| `has_xss_vulnerability` | semgrep | FAIL | Detected (innerHTML) |
| `missing_tests` | sanity | FAIL | Detected (no test files) |
| `clean_python_project` | semgrep, secrets | PASS | No findings |
| `clean_rust_project` | semgrep, secrets | PASS | No findings |
| `placeholder_secret` | secrets, semgrep | PASS | No findings |
| `commented_secret` | secrets | PASS | Not flagged |
| `has_leaked_github_pat` | secrets | PASS | **Known gap** |
| `has_hardcoded_password` | secrets | PASS | **Known gap** |
| `has_db_connection_string` | secrets | PASS | **Known gap** |

### Known Gaps in Secrets Gate

gitleaks v8.30 and betterleaks v1.1 do **not** detect:
- GitHub PATs (`ghp_` prefix)
- Hardcoded passwords in YAML
- Database connection strings with embedded passwords

These are documented as `known_gap: true` in the expected.yaml and tested inversely — if a future tool update starts catching them, the test will fail, alerting us to update expectations.

### p3.3: Gate Assertion Framework

**File**: `tests/tier2/test_checkpoint_gates.py` (28 tests)

| Test Class | Tests | Purpose |
|------------|-------|---------|
| `test_checkpoint_scenario` | 13 | Parametrized over all corpus scenarios |
| `TestSecretsGate` | 9 | 3 should-fail + 3 known-gaps + 3 should-pass |
| `TestSemgrepGate` | 5 | 2 should-fail + 3 should-pass |
| `TestSanityGate` | 1 | Missing tests scenario |

**Config approach**: Per-repo `.devloop.yaml` uses `skip_gates` (not `gates`) to isolate individual gates. This matches the `RepoCheckpointOverrides` schema which only supports `skip_gates`, `test_command`, and `atdd_required`.

### p3.4: CI Integration

**Justfile recipes**:
- `just tier2-test` — Run all 13 scenarios (28 tests)
- `just tier2-secrets` — Run only secrets gate tests
- `just tier2-semgrep` — Run only semgrep gate tests
- `just tier2-corpus-validate` — Validate corpus YAML schema

**Corpus validation**: `scripts/tier2/validate_corpus.py` checks that every scenario has `expected.yaml` with required fields (`description`, `expected_gates`), valid gate names, and non-empty `files/` directory.

---

## Key Discovery: Semgrep Catches Secrets Too

Semgrep's `auto` config includes secret detection rules (`generic.secrets.security.*`) that overlap with gitleaks. In the AWS key scenario, semgrep detected the leaked key before gitleaks even ran. This means the `semgrep` gate provides partial coverage for secrets, even without gitleaks.

---

## Files Created/Modified

| File | Change |
|------|--------|
| `daemon/src/cli.rs` | Added `Checkpoint` variant |
| `daemon/src/main.rs` | Added `run_checkpoint_cli()` function + dispatch |
| `tests/tier2/__init__.py` | **NEW**: package init |
| `tests/tier2/conftest.py` | **NEW**: git repo fixtures, checkpoint runner |
| `tests/tier2/test_checkpoint_gates.py` | **NEW**: 28 tests across 13 scenarios |
| `tests/tier2/corpus/` | **NEW**: 13 scenario directories with files + expected.yaml |
| `scripts/tier2/validate_corpus.py` | **NEW**: corpus YAML schema validator |
| `justfile` | Added `tier2-test`, `tier2-secrets`, `tier2-semgrep`, `tier2-corpus-validate` recipes |

---

## Test Counts

| Category | Count |
|----------|-------|
| Rust unit tests (lib) | 79 |
| Rust unit tests (bin) | 166 |
| Turmoil integration | 4 |
| Conformance (Python) | 106 |
| Replay harness (Python) | 19 |
| Tier 2 planted-defect (Python) | 28 |
| **Total** | **249 + 106 + 19 + 28 = 402** |

---

## Usage

```bash
# Validate corpus structure
just tier2-corpus-validate

# Run all planted-defect tests
just tier2-test

# Run only secrets gate tests
just tier2-secrets

# Run only semgrep gate tests
just tier2-semgrep

# Run checkpoint manually on a directory
dl checkpoint --dir /path/to/repo
dl checkpoint --json --dir /path/to/repo
```

---

## Next Session

From the calibration plan, remaining phases:
- **Phase 4**: Per-check feedback — `dl feedback` command, labeled data scoring
- **Phase 5**: Continuous calibration pipeline — `just calibrate` aggregate script

Recommended: Phase 4 next (per-check feedback). The planted-defect corpus provides ground truth; Phase 4 creates the feedback loop for ongoing labeled data collection.
