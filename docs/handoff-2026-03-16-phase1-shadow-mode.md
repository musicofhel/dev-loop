# Handoff: Calibration Phase 1 — Shadow Mode

**Date**: 2026-03-16
**Session**: Implement Phase 1 from `docs/testing-calibration-plan.yaml`
**Previous**: `handoff-2026-03-16-phase0-silent-failures.md`

---

## What Was Implemented

### p1.1: Shadow Mode Config Toggle

**Problem**: No way to run hooks in observation-only mode for FP calibration before enforcing.

**Fix** (`daemon/src/config.rs`, `daemon/src/server.rs`, `daemon/src/daemon.rs`):
- Added `ambient_mode: "enforce" | "shadow" | "disabled"` to `AmbientConfig` (default: "enforce")
- Threaded through `MergedConfig` and `merge()` function
- `/status` JSON endpoint includes `ambient_mode` field
- `dl status` CLI shows `Mode: enforce/shadow/disabled`
- `dl config-lint` validates `ambient_mode` values (Error on invalid)

**New tests**: `parse_ambient_mode_shadow`, `ambient_mode_defaults_to_enforce`, `lint_invalid_ambient_mode`, `merged_config_includes_ambient_mode`

### p1.2: Shadow Verdict Logging

**Problem**: Need to collect would-have-blocked/warned data without disrupting user workflow.

**Fix** (`daemon/src/hook.rs`):
- `pre_tool_use()`: when `ambient_mode == "shadow"`, checks run normally but:
  - Block/warn verdicts logged as `shadow_verdict` events via `fire_shadow_verdict()` to daemon
  - Hook always exits 0 (allow) — never blocks
  - Event fields: `type`, `session_id`, `tool`, `verdict`, `check`, `reason`, `us`
- `post_tool_use()`: same shadow behavior for secrets — warns logged but not shown to user
- `session_start()`: notification includes "(SHADOW MODE — logging only, not blocking)" when active
- `disabled` mode: both hooks exit immediately without running checks
- New helper: `fire_shadow_verdict()` — POSTs `shadow_verdict` event to daemon

### p1.3: Shadow Analysis Command — `dl shadow-report`

**Problem**: Need to analyze collected shadow verdicts to identify FPs before switching to enforce.

**Fix** (`daemon/src/shadow_report.rs` — new file, `daemon/src/cli.rs`, `daemon/src/main.rs`):
- `dl shadow-report [--last N] [--csv]` reads JSONL event log
- Filters `type=shadow_verdict` events
- Groups by check type, counts blocks vs warns
- Human-readable report includes:
  - Total would-have-acted verdicts
  - Per-check-type breakdown (total/blocks/warns)
  - Top 10 triggered reasons
  - Likely false positives: files blocked >5 times
- CSV output for piping to analysis tools
- Handles empty event log gracefully with actionable message

**New tests**: `verdict_stats_default`, `parse_shadow_verdict_event`, `csv_output_format`, `report_from_events_file`

---

## Config Changes

New field in `~/.config/dev-loop/ambient.yaml`:

```yaml
ambient_mode: "enforce"   # default — blocks/warns as before
# ambient_mode: "shadow"  # log verdicts without blocking (for calibration)
# ambient_mode: "disabled" # skip all checks entirely
```

Backward-compatible — defaults to "enforce" if not specified.

---

## Files Modified

| File | Change |
|------|--------|
| `daemon/src/config.rs` | `ambient_mode` field in AmbientConfig + Default impl, MergedConfig, merge(), lint rule, default fn, 4 new tests |
| `daemon/src/hook.rs` | Shadow mode in pre_tool_use (log + allow) and post_tool_use (log + suppress), `fire_shadow_verdict()` helper, mode tag in session_start notifications, `disabled` mode early-exit |
| `daemon/src/server.rs` | `/status` response includes `ambient_mode` from config via `blocking_read()` |
| `daemon/src/daemon.rs` | `dl status` CLI displays `Mode: <mode>` line |
| `daemon/src/cli.rs` | `ShadowReport` command variant with `--last` (hours) and `--csv` flags |
| `daemon/src/main.rs` | `mod shadow_report` declaration, dispatch for `ShadowReport` command |
| `daemon/src/shadow_report.rs` | **NEW**: JSONL reader, shadow_verdict filter, check-type grouping, report + CSV formatters, FP detection heuristic, 4 tests |

---

## Test Counts

| Category | Count |
|----------|-------|
| Rust unit tests (lib) | 79 |
| Rust unit tests (bin) | 166 |
| Turmoil integration | 4 |
| Conformance (Python) | 106 |
| **Total** | **249 + 106 = 355** |

Binary: 6.5MB, 24 CLI commands.

---

## Shadow Mode Event Format

Events logged to `/tmp/dev-loop/events.jsonl`:

```json
{"ts":"14:32:01","type":"shadow_verdict","session":"abc-123","tool":"Write","verdict":"block","check":"deny_list","reason":"Blocked: matches deny pattern '.env'","pattern":null,"us":42}
```

Fields:
- `type`: always `"shadow_verdict"`
- `tool`: tool name (Write, Edit, Bash)
- `verdict`: what would have happened — `"block"` or `"warn"`
- `check`: check type (deny_list, dangerous_ops, secrets)
- `reason`: human-readable reason string
- `pattern`: reserved for future use (currently null — pattern info is in reason)
- `us`: check duration in microseconds

---

## Usage

```bash
# Enable shadow mode
# Edit ~/.config/dev-loop/ambient.yaml:
#   ambient_mode: "shadow"

# Or if daemon is running, edit config and reload:
dl reload

# Work normally — all hooks run but nothing is blocked

# Review what would have been blocked:
dl shadow-report
dl shadow-report --csv > shadow-data.csv
dl shadow-report --last 24  # last 24 hours only

# Check status:
dl status  # shows Mode: shadow

# When satisfied with FP rate, switch back:
# Edit ambient.yaml: ambient_mode: "enforce"
dl reload
```

---

## Next Session

From the calibration plan, remaining phases:
- **Phase 2**: Replay harness — session JSONL parser, replay runner, scoring (precision/recall)
- **Phase 3**: Planted-defect suite — integration test repos with known vulnerabilities
- **Phase 4**: Per-check feedback — `dl feedback` command, labeled data
- **Phase 5**: Continuous calibration pipeline

Recommended: Phase 2 next (replay harness). Shadow mode provides the verdict format that replay output will match.
