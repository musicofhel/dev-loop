> Note: prompt-bench and omniswipe-backend were test targets during this period. They have been consolidated into OOTestProject1.

# Handoff: Phase 5 — Session Registration + OTel

**Date**: 2026-03-15
**Session**: Implementation (Phase 5 of ambient layer plan)
**Previous**: `handoff-2026-03-15-phase4-config-system.md`

---

## What Was Built

Phase 5 deliverable complete: session registration with daemon, check attribution per session, OTLP/HTTP span export to OpenObserve.

### Session tracking

Daemon now tracks active sessions in a concurrent `DashMap<String, SessionInfo>`:

- `session_start` hook registers session via `POST /session/start` to daemon
- `session_end` hook deregisters via `POST /session/end`, triggering OTel span flush
- Pre/post tool use hooks fire check events to daemon for session counter tracking
- `dl status` displays active sessions with cwd, duration, check/block/warn counts

### OTel span export

Lightweight OTLP/HTTP JSON exporter (no heavy SDK — raw `TcpStream` + `serde_json`):

- Session end triggers `ambient.session` root span + `ambient.session.summary` child span
- Spans include: session_id, cwd, repo, duration, check/block/warn counts
- Export POSTs to OpenObserve at `{openobserve_url}/api/{org}/v1/traces`
- Basic auth from config (`openobserve_user:openobserve_password`)
- Fire-and-forget: logs errors but never blocks

### Span hierarchy

```
ambient.session (root span — full session lifetime)
└── ambient.session.summary (on end — aggregated stats)
    Per-check ambient.check spans available via build_check_span() — deferred to Phase 6+
```

### New API endpoints

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/session/start` | Register session, returns `{trace_id, root_span_id}` |
| POST | `/session/end` | Deregister, flush OTel spans, returns summary |

### Enhanced status display

```
$ dl status
Status:   running
PID:      13709
Uptime:   00:00:43
Sessions: 2 active
  s-backend: /home/musicofhel/omniswipe-backend (5m12s, 47 checks, 1 blocks, 3 warns)
  s-devloop: /home/musicofhel/dev-loop (2m30s, 23 checks, 0 blocks, 0 warns)
Socket:   /tmp/dev-loop/dl.sock
```

### Hook-to-daemon communication

Hooks communicate with daemon via Unix socket HTTP (fail-open):

- `session_start`: `POST /session/start` (500ms timeout)
- `session_end`: `POST /session/end` (500ms timeout)
- `pre/post_tool_use`: `POST /event` (fire-and-forget, check attribution)
- If daemon not running: silently skip all daemon communication

---

## Performance

| Operation | Latency |
|-----------|---------|
| Hook with daemon event posting (process startup + config + check + daemon POST) | ~6ms |
| Session register/deregister (daemon-side) | <1ms |
| OTel span export (on session end) | <5ms (non-blocking) |

Binary size: 5.8MB (up from 5.6MB — dashmap + getrandom + base64 add ~200KB).

---

## Tests

66 tests, all passing (up from 55 in Phase 4):

| Category | Count | New |
|----------|-------|-----|
| Deny list (built-in + from_config) | 10 | - |
| Dangerous ops (built-in + from_config) | 11 | - |
| Secrets (built-in + from_config) | 11 | - |
| Config (schema + merge + repo root) | 14 | - |
| Hook | 1 | - |
| Install | 6 | - |
| Session | 6 | +6 |
| OTel | 5 | +5 |
| **Total** | **66** | **+11** |

New tests cover:
- Session register/deregister lifecycle
- Missing session deregister returns None
- Check counter tracking (allow/block/warn)
- Missing session check recording is no-op
- Multiple concurrent sessions
- Random hex uniqueness
- OTLP JSON envelope structure
- Session span hierarchy (root + summary with parent)
- Check span construction with parent
- Attribute helper functions (string + int)
- Span ID format (16 hex chars)

---

## Files Created

| File | Purpose |
|------|---------|
| `daemon/src/session.rs` | Session state tracking: `SessionInfo`, `SessionMap`, register/deregister/record_check |
| `daemon/src/otel.rs` | OTLP/HTTP JSON span builder + exporter: session spans, check spans, TcpStream POST |

## Files Modified

| File | Change |
|------|--------|
| `daemon/Cargo.toml` | Added `dashmap = "6"`, `getrandom = "0.3"`, `base64 = "0.22"` |
| `daemon/src/main.rs` | Added `mod otel; mod session;` |
| `daemon/src/server.rs` | Added `SessionMap` + `ObservabilityConfig` to `ServerState`, added `/session/start` and `/session/end` endpoints, updated `/status` to show sessions, updated `/event` to track check counters, updated `/check` to track per-session |
| `daemon/src/daemon.rs` | Init `SessionMap` + load `ObservabilityConfig`, updated status display with session count + details |
| `daemon/src/hook.rs` | Implemented `session_start` (register with daemon), `session_end` (deregister + OTel flush), added `post_to_daemon()` + `fire_event_to_daemon()` helpers, pre/post tool use now fire check events to daemon |

---

## Source Layout After Phase 5

```
daemon/src/
├── main.rs              # CLI dispatch (14 commands)
├── cli.rs               # Clap: Command + HookCommand + Config
├── daemon.rs            # Start/stop/status/stream (session-aware status)
├── server.rs            # Unix socket HTTP server (session endpoints)
├── sse.rs               # SSE broadcast channel
├── event_log.rs         # JSONL event log writer
├── config.rs            # Full config system: schema, merge, load, dump
├── session.rs           # Session lifecycle: register, deregister, counters [NEW]
├── otel.rs              # OTLP/HTTP JSON span export [NEW]
├── hook.rs              # Hook handlers (session + config-aware)
├── install.rs           # settings.json merger
└── check/
    ├── mod.rs           # CheckEngine: new() + from_config()
    ├── deny_list.rs     # 15 built-in patterns + from_config(extra, remove)
    ├── dangerous_ops.rs # 25 built-in patterns + from_config(extra, allow)
    └── secrets.rs       # 16 built-in patterns + from_config(extra, allowlist)
```

---

## Not Implemented (Deferred)

1. **Per-check OTel spans from hooks** — `build_check_span()` is implemented but not wired into hook→daemon flow. Currently only session-level spans are exported. Wire when per-check granularity is needed.
2. **OTel span batching** — Currently exports all session spans immediately on session end. For high-volume scenarios, add a background batch exporter.
3. **AgentLens integration** — Listed in plan as "install alongside". No integration work done — AgentLens reads Claude's native JSONL, so zero-integration is the design.
4. **`ambient-sessions.json` dashboard** — Dashboard definition file for OpenObserve. Need to create panel queries using the span data now being exported.
5. **Auto-start daemon on session-start** — Currently hooks require `dl start` manually. Could auto-start if socket not found.

---

## Next: Phase 6 — Tier 2 Checkpoint

1. Commit interception in `dl hook pre-tool-use` (detect `git commit` in Bash)
2. Install Semgrep, wire into `/checkpoint` endpoint
3. Gitleaks integration for diff scanning
4. ATDD spec check and tracer-bullet enforcement
5. Git trailer injection (`Dev-Loop-Gate: <hash>`)
