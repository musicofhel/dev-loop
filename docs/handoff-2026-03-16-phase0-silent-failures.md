# Handoff: Phase 0 — Fix Silent Failures

**Date**: 2026-03-16
**Session**: Fix all 4 silent failure categories from `docs/testing-calibration-plan.yaml` Phase 0
**Previous**: `handoff-2026-03-16-calibration-implementation.md`

---

## What Was Fixed

### p0.1: OTel Export Error Handling

**Problem**: Raw TcpStream POST with no read timeout, no response status check, no retry. Any network issue = silent span loss.

**Fix** (`daemon/src/otel.rs`):
- `post_json()` now uses `TcpStream::connect_timeout()` (5s), `set_read_timeout()` (10s)
- Reads HTTP response status line, parses status code
- Returns `Result<u16, String>` (status code or error message)
- 401 Unauthorized detected with credential hint
- `export_spans()` retries 3 times with exponential backoff (1s, 2s, 4s)
- Returns `Result<(), String>` instead of silently swallowing errors
- Caller (`handle_session_end` in server.rs) logs failures to event log as `otel_export_error` events

**New tests**: `export_spans_empty_is_ok`, `export_spans_connection_refused`, `post_json_rejects_https`

### p0.2: Checkpoint Gate Fail-Closed Option

**Problem**: Missing semgrep/gitleaks = gate silently passes. User thinks code was scanned.

**Fix** (`daemon/src/checkpoint.rs`, `daemon/src/config.rs`):
- Added `checkpoint.fail_mode: "open" | "closed"` config (default: "open" for backward compat)
- All 3 external-tool gates (semgrep, secrets, sanity) check `config.fail_mode`:
  - `"open"`: tool missing → gate passes with skip message (existing behavior)
  - `"closed"`: tool missing → gate FAILS, commit blocked
- Gate error messages include `(fail_mode=open/closed)` for clarity
- Added `checkpoint.gate_timeout_s` (default: 60s) — overall checkpoint timeout via `tokio::time::timeout` in server.rs. Timeout = gate failure (don't pass uncommitted code if checks didn't complete)
- `config-lint` now checks tool availability:
  - Warning level when `fail_mode=open`, Error level when `fail_mode=closed`
  - Checks: semgrep, betterleaks/gitleaks
- Invalid `fail_mode` values detected by lint

**New tests**: `lint_invalid_fail_mode`, `parse_checkpoint_fail_mode`, `checkpoint_fail_mode_defaults_to_open`

### p0.3: Daemon Startup Failure Propagation

**Problem**: `dl start` said "started" but daemon may have crashed on socket bind.

**Fix** (`daemon/src/daemon.rs`):
- Parent now waits up to 2s after spawning child, polling for socket existence every 100ms
- If socket appears: prints "Daemon started (pid N)" (success)
- If socket not bound after 2s: prints error, checks child process status (`try_wait()`), exits 1
- No more false success messages

### p0.4: Event Log Reliability

**Problem**: 1000-event channel, silent drops, no rotation, no monitoring.

**Fix** (`daemon/src/event_log.rs`, `daemon/src/config.rs`):
- **Configurable capacity**: `event_log.channel_capacity` (default: 10,000, was hardcoded 1,000)
- **Drop tracking**: `AtomicU64` counter, incremented on every failed `try_send()`, warns via tracing every 100th drop
- **Log rotation**: writer checks file size every 100 writes, rotates when exceeding `event_log.max_file_size_mb` (default: 50MB), keeps `event_log.max_rotated_files` (default: 3) rotated copies (events.jsonl.1, .2, .3)
- **Stats exposure**: `EventLogWriter::events_logged()` and `events_dropped()` methods
- **Status display**: `dl status` now shows `Events: N logged, N dropped`
- **Server endpoint**: `/status` JSON includes `events_logged` and `events_dropped` fields

**New tests**: `rotate_log_creates_numbered_files`, `rotate_log_shifts_existing`, `rotate_log_drops_oldest`, `event_log_tracks_counts`, `event_log_tracks_drops`

---

## Config Changes

New fields in `~/.config/dev-loop/ambient.yaml`:

```yaml
checkpoint:
  fail_mode: "open"       # or "closed"
  gate_timeout_s: 60      # overall checkpoint timeout

event_log:
  channel_capacity: 10000
  max_file_size_mb: 50
  max_rotated_files: 3
```

All fields have backward-compatible defaults — no config changes required.

---

## Files Modified

| File | Change |
|------|--------|
| `daemon/src/config.rs` | `EventLogConfig` struct, `fail_mode`/`gate_timeout_s` in CheckpointConfig, `event_log` in AmbientConfig/MergedConfig, `is_tool_on_path()` lint helper, tool availability lint rules, 5 new tests |
| `daemon/src/event_log.rs` | Full rewrite: AtomicU64 counters, configurable capacity, log rotation, stats methods, 5 new tests |
| `daemon/src/otel.rs` | `connect_timeout`, `read_timeout`, response status parsing, retry with backoff, `Result` return type, 3 new tests |
| `daemon/src/checkpoint.rs` | `run_secrets_gate` takes config, all gates check `fail_mode`, error messages include mode |
| `daemon/src/daemon.rs` | Startup socket verification (2s poll), mutable child handle, EventLogWriter::spawn with config params, status shows events |
| `daemon/src/server.rs` | `/status` includes `events_logged`/`events_dropped`, checkpoint timeout via `tokio::time::timeout`, OTel export error logging to event log |

---

## Test Counts

| Category | Count |
|----------|-------|
| Rust unit tests (lib) | 75 |
| Rust unit tests (bin) | 158 |
| Turmoil integration | 4 |
| Conformance (Python) | 106 |
| **Total** | **237 + 106 = 343** |

Binary: 6.3MB, 23 CLI commands (unchanged).

---

## Next Session

From the calibration plan, remaining phases:
- **Phase 1**: Shadow mode — `ambient_mode: "enforce"|"shadow"|"disabled"`, shadow verdict logging, `dl shadow-report`
- **Phase 2**: Replay scoring — labeled data, precision/recall computation
- **Phase 3**: Planted-defect suite — integration test repos with known vulnerabilities
- **Phase 4**: Per-check feedback — `dl feedback` command
- **Phase 5**: Continuous calibration pipeline

Recommended: Phase 1 next (shadow mode). Phase 0 prerequisites are now satisfied.
