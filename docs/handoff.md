> Note: omniswipe-backend was the secondary test target during this period. It has since been replaced by OOTestProject1.

# dev-loop Handoff — 2026-03-14

## Status: ALL 6 TBs E2E VALIDATED + POST-VALIDATION CLEANUP COMPLETE

TB-1 through TB-6 all validated end-to-end against real repos. 3 bugs found and fixed during validation. 280 unit tests passing (+ 33 in new gate/channel files = 313 actual, pytest-deepeval undercounts).

---

## Session Work (2026-03-14 late): E2E Validation + Bug Fixes + Cleanup

### E2E Validation Results

| TB | Command | Result | Key Metrics |
|----|---------|--------|-------------|
| TB-4 | `just tb4-turns dl-320 ~/prompt-bench 3` | Escalated (correct) | persona=bug-fix, turns=4/3, 32 OTel spans |
| TB-5 | `just tb5 dl-76a ~/prompt-bench ~/omniswipe-backend` | cascade_skipped (correct) | 0.05s, changed_files=[calculator.py], no watch match |
| TB-6 | `just tb6 dl-2xm ~/prompt-bench` | Gates passed | 34 session events, forced gate fail + retry, suggested fix generated |
| OpenObserve | API query | 48 distinct operation names | All TB spans nested correctly, trace_ids verified |

### 3 Bugs Found and Fixed

| Bug | Root Cause | Fix |
|-----|-----------|-----|
| **Token/turn counts = 0** | Claude CLI `--output-format json` emits a JSON array `[{...}]`, not NDJSON. `_parse_usage_from_output` iterated lines expecting one JSON object per line. | Unwrap arrays: when `json.loads()` returns a list, extend objects from its dict elements. (`runtime/server.py`) |
| **Session event count = 0** | Same root cause in `_parse_session_events`. | Same array-unwrapping fix. (`feedback/pipeline.py`) |
| **Persona always "feature"** | `br ready --json` omits the `labels` field. `issue.get("labels", [])` → `[]` → no persona match → fallback to "feature". | Added `get_issue()` to `beads_poller.py` (calls `br show <id> --json` which includes labels). Added label enrichment in all 5 TB poll phases. (`intake/beads_poller.py`, `feedback/pipeline.py`) |

### Commits (5 atomic commits)

```
383d1b9 Update docs, dashboards, alert rules, and beads state for TB-1 through TB-6
e5efda1 Fix OTel spec violations: remove description from set_status(OK) calls
a859dd3 Add unit tests for JSON array parsing, get_issue, and session event array format
b3ca2e3 Add quality gates 0.5/2.5/3, feedback channels 2/3/5/7, and PR creation tool
33eeb61 Fix JSON array parsing in runtime and add get_issue() for label enrichment
```

### OTel Fix (12 violations)

`set_status(StatusCode.OK, "description")` violates OTel spec — description is only for ERROR. Moved descriptions to `span.set_attribute("status.detail", ...)` across pipeline.py (6), feedback/server.py (2), gates/server.py (3), observability/server.py (1).

### New Unit Tests (+10)

| File | Tests Added | What |
|------|------------|------|
| `test_runtime.py` | 3 | JSON array parsing in `_parse_usage_from_output` |
| `test_beads_poller.py` | 5 | `get_issue()` — success, not found, timeout, invalid JSON, empty list |
| `test_tb6.py` | 2 | JSON array format in `_parse_session_events` |

### Prompt-bench Cleanup

- Deleted `dl/dl-76a` branch (TB-5 test artifact)
- Kept `main` branch (pipeline needs it for `git diff main..dl/<id>`)

---

## Previous Session Work (2026-03-14 early): Intent-vs-Implementation Audit

Audited the entire codebase against all 6 intent layer docs. Found ~25 missing items. All addressed in 6 phases:

- **Phase 1**: Wired deny_list.py, capabilities.yaml, --dangerously-skip-permissions
- **Phase 2**: Added gates 0.5 (relevance), 2.5 (dangerous ops), 5 (cost)
- **Phase 3**: PR creation tool in orchestration layer
- **Phase 4**: OpenObserve dashboards + alert rules
- **Phase 5**: Feedback channels 2 (patterns), 3 (cost), 5 (changelog), 7 (efficiency)
- **Phase 6**: Rewrote all 8 stale docs to match implementation

---

## Previous Session Work (2026-03-13)

### TB-1 (Golden Path) — E2E PASSING
### TB-2 (Failure-to-Retry) — E2E PASSING
### TB-3 (Security-Gate-to-Fix) — E2E PASSING
### TB-4 (Runaway-to-Stop) — E2E PASSING (validated 2026-03-14)
### TB-5 (Cross-Repo Cascade) — E2E PASSING (validated 2026-03-14)
### TB-6 (Session Replay Debug) — E2E PASSING (validated 2026-03-14)

---

## Test Count History

| Date | Tests | Delta | What |
|------|-------|-------|------|
| 2026-03-12 | 85 | — | TB-1 passing |
| 2026-03-12 | 121 | +36 | TB-2 + TB-3 passing |
| 2026-03-13 | 207 | +86 | TB-4 + TB-5 code complete |
| 2026-03-13 | 237 | +30 | TB-6 code complete |
| 2026-03-14 | 270 | +33 | Audit remediation (3 gates + 4 channels) |
| 2026-03-14 | 280 | +10 | E2E bug fix tests (JSON array, get_issue, session events) |

Note: pytest reports 280 but 33 additional tests in `test_new_gates.py` and `test_feedback_channels.py` run and pass — pytest-deepeval plugin undercounts. Actual total: **313**.

---

## What's Next

1. **Gate 1 (ATDD)**: Implement when repos have `specs/` directories
2. **Channel 1 (Health Dashboard)**: Real-time success/fail streaming
3. **Channel 4 (Sprint)**: Cross-issue dependency tracking
4. **Channel 6 (DORA)**: Build when enough historical data exists
5. **Dashboard import**: Load `config/dashboards/*.json` into OpenObserve UI
6. **pytest-deepeval count fix**: Investigate why it suppresses new test file counts

## Key Gotchas

- **`br ready --json` omits labels** — always use `get_issue()` to enrich when labels are empty
- **Claude CLI `--output-format json` emits a JSON array**, not NDJSON — all parsers must handle `[{...}]`
- `br show --json` returns a list (array), not a dict — index `[0]` to get the issue
- `br create` uses `--labels` (plural); no `--epic` flag, use `--parent`
- Gate 0: uses `git rev-list --count HEAD` for safe lookback
- Gate 3 skips if bandit not installed or project is non-Python
- Gate 5 (cost) is NOT in `run_all_gates()` — called separately with usage data
- `--dangerously-skip-permissions` required for unattended agent runs
- PR failure does not block pipeline success
- Session files in `/tmp/dev-loop/sessions/` — not persistent across reboots
- **OTel**: never pass description to `set_status(StatusCode.OK)` — only valid for ERROR
- pytest-deepeval plugin undercounts tests from newer files (cosmetic, tests do run)
