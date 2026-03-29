# dev-loop Ambient Layer: Full Implementation Plan (v2)

> Transform dev-loop from a batch pipeline (`just tb1`) into an ambient layer that wraps every Claude Code session with quality gates, observability, and feedback.

**Status**: PLAN — not yet implemented
**Date**: 2026-03-15 (v2 — post-research revision)

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Three-Tier Model](#three-tier-model)
3. [The `dl` Binary (Rust)](#the-dl-binary-rust)
4. [Daemon Architecture](#daemon-architecture)
5. [Hook Registration](#hook-registration)
6. [Toggle Mechanism](#toggle-mechanism)
7. [Config System](#config-system)
8. [Tier 1: Always-On Checks](#tier-1-always-on-checks)
9. [Tier 2: Checkpoint Gates](#tier-2-checkpoint-gates)
10. [Tier 3: Full Pipeline (existing)](#tier-3-full-pipeline-existing)
11. [OTel Integration](#otel-integration)
12. [Session Continuity](#session-continuity)
13. [Session Transcript Parsing](#session-transcript-parsing)
14. [Aggregate Learning](#aggregate-learning)
15. [Override / Escape Hatch](#override--escape-hatch)
16. [External Tool Integrations](#external-tool-integrations)
17. [Dashboard Validation](#dashboard-validation)
18. [Source Layout](#source-layout)
19. [Implementation Phases](#implementation-phases)
20. [Design Decisions Log](#design-decisions-log)
21. [Open Questions](#open-questions)
22. [Research Sources](#research-sources)
23. [Steps From Human](#steps-from-human)

---

## Architecture Overview

```
┌──────────────────── Claude Code Session ─────────────────────┐
│                                                               │
│  SessionStart ──► dl hook session-start                       │
│       │           ├─ Start daemon if not running              │
│       │           ├─ Register session (id, cwd, repo)        │
│       │           ├─ Init OTel root span                     │
│       │           ├─ Write ambient-rules.md                  │
│       │           └─ Inject handoff state (resume/compact)   │
│       │                                                       │
│       ▼                                                       │
│  PreToolUse ───► dl hook pre-tool-use                         │
│  [Write|Edit]    ├─ Deny list check (in-process, <1ms)       │
│                  └─ BLOCK or ALLOW                            │
│                                                               │
│  PreToolUse ───► dl hook pre-tool-use                         │
│  [Bash]          ├─ Dangerous ops scan (in-process, <1ms)    │
│                  ├─ Commit interception (triggers Tier 2)    │
│                  └─ BLOCK, WARN, or ALLOW                    │
│                                                               │
│       ▼                                                       │
│  Claude acts (tool executes)                                  │
│       │                                                       │
│       ▼                                                       │
│  PostToolUse ──► dl hook post-tool-use                        │
│  [Write|Edit]    ├─ Secret pattern scan (in-process, <1ms)   │
│                  └─ additionalContext warning if found        │
│                                                               │
│  PreToolUse ───► dl hook pre-tool-use                         │
│  [Bash]          ├─ Detects "git commit" command              │
│  (commit)        ├─ Triggers Tier 2 checkpoint                │
│                  ├─ Semgrep + gitleaks + ATDD + tests         │
│                  ├─ Injects Dev-Loop-Gate trailer on pass     │
│                  └─ BLOCK commit if gates fail                │
│                                                               │
│  PreCompact ───► dl hook pre-compact                          │
│                  └─ Auto-serialize session state to YAML      │
│                                                               │
│  Stop ─────────► dl hook stop                                 │
│                  └─ 85% context guard (block + force handoff) │
│                                                               │
│  SessionEnd ───► dl hook session-end                          │
│                  ├─ Parse transcript (tokens, tools, files)   │
│                  ├─ Grade outcome (SUCCESS/PARTIAL/FAIL)      │
│                  ├─ Flush OTel traces                        │
│                  └─ Emit session summary span                │
│                                                               │
└───────────────────────────────────────────────────────────────┘
         │ OTLP/HTTP               │ JSONL event log
         ▼                         ▼
   ┌─────────────┐     ┌──────────────────────┐
   │ OpenObserve  │     │ /tmp/dev-loop/       │
   │   :5080      │     │   events.jsonl       │
   └─────────────┘     │   sessions/*.yaml    │
         │              └──────────────────────┘
         ▼                         │
   ┌─────────────┐                 ▼
   │ AgentLens    │     ┌──────────────────────┐
   │ (free rider) │     │ Entire CLI           │
   │ reads native │     │ session → git commit │
   │ Claude JSONL │     │ attribution tracking │
   └─────────────┘     └──────────────────────┘
```

---

## Three-Tier Model

| Tier | When | Latency Target | What Runs |
|------|------|---------------|-----------|
| **1: Always-On** | Every tool call | <5ms | Deny list, dangerous ops, secret patterns (all in-process regex/glob) |
| **2: Checkpoint** | Before `git commit` | ~30s | Semgrep, gitleaks, ATDD spec check, tests, optional PR-Agent review |
| **3: Full Pipeline** | On demand (`just tb1`) | Minutes | Existing TB flow: worktree → agent → gates → PR/retry |

Tier 1 is the ambient safety net. Tier 2 is the quality gate. Tier 3 is the autonomous pipeline. All three coexist — Tier 1 runs during Tier 3 sessions too (unless inside a worktree, see [worktree detection](#worktree-detection)).

---

## The `dl` Binary (Rust)

Single Rust binary with dual roles: **CLI tool** and **daemon server**.

### Installation

```
~/.local/bin/dl          # binary location
~/dev-loop/daemon/       # source (Rust workspace inside dev-loop repo)
```

### CLI Commands

```bash
# Daemon lifecycle
dl start                          # Start daemon in background
dl stop                           # Stop daemon gracefully
dl status                         # Health + active sessions + orphan worktrees

# Hook integration (called by Claude Code hooks)
dl hook session-start             # Reads stdin JSON, registers session
dl hook session-end               # Reads stdin JSON, flushes + summarizes
dl hook pre-tool-use              # Reads stdin JSON, returns verdict
dl hook post-tool-use             # Reads stdin JSON, returns verdict
dl hook pre-compact               # Auto-serialize session state to YAML handoff
dl hook stop                      # 85% context guard

# Installation
dl install                        # Merge hooks into ~/.claude/settings.json
dl uninstall                      # Remove hooks cleanly (preserves other hooks)

# Toggle
dl enable                         # All tiers on
dl enable --tier 1                # Tier 1 only (hooks)
dl enable --tier 2                # Tier 2 only (checkpoints)
dl disable                        # All off (hooks installed but no-op)

# Override
dl allow-once ".env"              # Temporary override for one match or 5 minutes

# Diagnostics
dl check '{"tool_name":"Write","tool_input":{"file_path":".env"}}'
                                  # Manual test: would this be blocked?
dl dashboard-validate             # Run every panel SQL query, report results
dl traces --last 10               # Query recent OTel spans from terminal
dl config                         # Dump merged config (built-in + global + repo)
dl config reload                  # Hot-reload config (also triggered by SIGHUP)
dl config lint                    # Validate all config files
dl stream                         # SSE event stream (tail -f for the daemon)
```

### Cargo Dependencies

```toml
[dependencies]
tokio = { version = "1", features = ["full"] }
hyper = { version = "1", features = ["server", "http1"] }
hyper-util = "0.1"
serde = { version = "1", features = ["derive"] }
serde_json = "1"
serde_yaml = "0.9"
regex = "1"                     # dangerous ops + secret patterns (pre-compiled)
glob = "0.3"                    # deny list matching
opentelemetry = "0.27"          # OTel spans
opentelemetry-otlp = "0.27"    # OTLP/HTTP exporter
tracing = "0.1"                 # Rust internal logging
clap = { version = "4", features = ["derive"] }  # CLI arg parsing
dirs = "6"                      # ~/.config resolution
base64 = "0.22"                 # OpenObserve auth
dashmap = "6"                   # Lock-free concurrent session map

[dev-dependencies]
turmoil = "0.6"                 # Deterministic simulation testing for async Rust
criterion = "0.5"               # Benchmarks
```

---

## Daemon Architecture

### State Management

```
/tmp/dev-loop/dl.sock            # Unix domain socket
/tmp/dev-loop/dl.pid             # PID file
/tmp/dev-loop/events.jsonl       # Append-only event log (Axel pattern)
/tmp/dev-loop/sessions/*.yaml    # Per-session handoff state
~/.config/dev-loop/ambient.yaml  # Enable/disable state + global config
```

### In-Memory State

```rust
struct DaemonState {
    config: Arc<RwLock<Config>>,
    sessions: DashMap<String, SessionState>,
    deny_patterns: Vec<CompiledGlob>,       // pre-compiled at startup
    dangerous_ops: Vec<CompiledRegex>,       // pre-compiled at startup
    secret_patterns: Vec<CompiledRegex>,     // pre-compiled at startup
    active_overrides: DashMap<String, Override>,
    event_tx: tokio::sync::broadcast::Sender<Event>,  // SSE fan-out (100 buffer)
    log_tx: tokio::sync::mpsc::Sender<Event>,          // JSONL writer (bounded 1000)
}

struct SessionState {
    session_id: String,
    source: SessionSource,          // Startup, Resume, Compact, Clear
    trace_id: TraceId,
    root_span: SpanContext,
    started_at: Instant,
    cwd: PathBuf,
    repo_config: Option<RepoConfig>,
    events: Vec<CheckEvent>,
    blocked_count: u32,
    warned_count: u32,
    handoff: Option<SessionHandoff>,  // loaded from YAML on resume
}

enum SessionSource { Startup, Resume, Compact, Clear }

struct CheckEvent {
    timestamp: Instant,
    tool_name: String,
    action: Action,         // Allow, Block, Warn
    check_type: CheckType,  // DenyList, DangerousOps, Secrets, Checkpoint
    reason: Option<String>,
    duration_us: u64,
}
```

### API Endpoints (Unix socket, HTTP/JSON)

| Method | Path | Purpose | Latency |
|--------|------|---------|---------|
| POST | `/session/start` | Register session, init OTel trace | <50ms |
| POST | `/session/end` | Flush traces, parse transcript, emit summary | <500ms |
| POST | `/check` | Tier 1 pre/post tool check | <5ms |
| POST | `/checkpoint` | Tier 2 full gate run | ~30s |
| POST | `/handoff` | Write session handoff YAML (PreCompact) | <50ms |
| POST | `/override` | Register temporary allow-once | <5ms |
| GET | `/status` | Health + active sessions + orphan worktrees | <10ms |
| GET | `/inbox` | SSE event stream (real-time inspection) | streaming |
| POST | `/config/reload` | Hot-reload from disk | <50ms |

### SSE Event Stream (from Axel research)

The `/inbox` endpoint broadcasts all daemon events via Server-Sent Events:

```
$ curl --no-buffer --unix-socket /tmp/dev-loop/dl.sock http://localhost/inbox

data: {"ts":"14:30:01","type":"check","session":"abc123","tool":"Write","file":".env","action":"block","reason":"deny_list","us":312}
data: {"ts":"14:30:02","type":"check","session":"abc123","tool":"Bash","cmd":"npm test","action":"allow","us":180}
data: {"ts":"14:32:15","type":"checkpoint","session":"abc123","trigger":"commit","gates_passed":5,"gates_failed":0,"duration_s":22}
```

Implementation: `tokio::sync::broadcast` channel with 100-event buffer. Any client can connect — `curl`, `dl stream`, future TUI.

### JSONL Event Log (from Axel research)

All events also append to `/tmp/dev-loop/events.jsonl` via a bounded mpsc channel (capacity 1000) with `try_send` backpressure. Events silently dropped if writer can't keep up — correct tradeoff for telemetry.

```
$ tail -f /tmp/dev-loop/events.jsonl
{"ts":"2026-03-15T14:30:01Z","type":"block","session":"abc123","tool":"Write","file":".env","pattern":".env","check":"deny_list"}
```

### Lifecycle

1. **Start**: On first `SessionStart` hook, or manual `dl start`
2. **Run**: Stays alive, handles multiple concurrent sessions
3. **Auto-stop**: After 30 minutes of no active sessions (configurable)
4. **Crash recovery**: Hooks fail-open (daemon down = allow everything)

### Fail-Open Design

The daemon is an **enhancement**, not a lock. If it crashes:
- Hook binary detects missing socket → exit 0 (allow)
- No Claude session is ever blocked by daemon failure
- Tier 2 checkpoints still catch issues (Python gates don't need daemon)
- User is never locked out of their own tools

---

## Hook Registration

### `dl install` Output

Merges into `~/.claude/settings.json` (preserves existing hooks):

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [{ "type": "command", "command": "dl hook session-start" }]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Write|Edit",
        "hooks": [{ "type": "command", "command": "dl hook pre-tool-use" }]
      },
      {
        "matcher": "Bash",
        "hooks": [{ "type": "command", "command": "dl hook pre-tool-use" }]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Write|Edit",
        "hooks": [{ "type": "command", "command": "dl hook post-tool-use" }]
      }
    ],
    "PreCompact": [
      {
        "hooks": [{ "type": "command", "command": "dl hook pre-compact" }]
      }
    ],
    "Stop": [
      {
        "hooks": [{ "type": "command", "command": "dl hook stop" }]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [{ "type": "command", "command": "dl hook session-end" }]
      }
    ]
  }
}
```

### `dl uninstall`

Removes entries whose `command` starts with `dl hook`. Preserves all other hooks (like the existing image resize hooks in `~/.claude/settings.json`).

### No Read Hook

Reading denied files is NOT blocked. Claude reads `.env` to understand config shape — that's fine. Only **writes** to sensitive files are blocked.

### No Shell Wrapper Scripts

The `dl` binary IS the hook command. No bash scripts, no curl, no jq. Single binary reads stdin, does the check, writes stdout. ~2ms total.

---

## Toggle Mechanism

### State File: `~/.config/dev-loop/ambient.yaml`

```yaml
enabled: true
tier1: true
tier2: true
```

### How It Works

Every `dl hook *` subcommand checks enable state **before** any other work:

```rust
fn is_enabled(tier: Tier) -> bool {
    // Fast path: read 3 lines from a small YAML file (<1ms)
    let config = read_ambient_yaml();
    if !config.enabled { return false; }
    match tier {
        Tier::One => config.tier1,
        Tier::Two => config.tier2,
    }
}
```

If disabled: `dl hook pre-tool-use` reads stdin (mandatory, Claude sends it), immediately exits 0. No socket connection, no daemon contact. Sub-millisecond.

### CLI UX

```bash
$ dl enable
✓ Ambient layer enabled (Tier 1 + Tier 2)

$ dl disable
✗ Ambient layer disabled (all hooks no-op)

$ dl status
dev-loop ambient layer
  Enabled:    true
  Tier 1:     active (deny list + dangerous ops + secrets)
  Tier 2:     active (checkpoint gates on commit)
  Daemon:     running (PID 12345, uptime 2h 14m)
  Sessions:   1 active
    session abc123: ~/OOTestProject1 (47 checks, 1 block, 0 warns)
  Orphan worktrees: 0
  Socket:     /tmp/dev-loop/dl.sock
  Event log:  /tmp/dev-loop/events.jsonl (1,247 events)
```

---

## Config System

### Three Layers (merged at runtime)

#### Layer 1: Built-In Defaults (compiled into `dl`)

Ported from `src/devloop/runtime/deny_list.py`:

```
Deny patterns:
  .env, .env.*, *.key, *.pem, *.p12, *.pfx,
  credentials.*, *secret*, .aws/*, .ssh/*,
  *.keystore, *.jks, .netrc, .npmrc, .pypirc

Dangerous ops patterns:
  rm -rf with / or ~ or * targets
  git push --force
  git reset --hard
  git clean -f
  DROP TABLE/DATABASE/SCHEMA
  TRUNCATE
  chmod 777
  curl ... | bash/sh

Secret patterns:
  AKIA[0-9A-Z]{16}                          (AWS access keys)
  -----BEGIN (RSA |EC |DSA )?PRIVATE KEY---- (private keys)
  ghp_[A-Za-z0-9_]{36,}                     (GitHub PATs)
  sk-[A-Za-z0-9]{40,}                       (OpenAI/Anthropic keys)
  (token|secret|password|api_key)\s*[:=]\s*['"][^\s]{20,}  (generic secrets)
```

#### Layer 2: Global Config — `~/.config/dev-loop/ambient.yaml`

```yaml
enabled: true
tier1: true
tier2: true

daemon:
  socket: /tmp/dev-loop/dl.sock
  pid_file: /tmp/dev-loop/dl.pid
  auto_stop_minutes: 30
  log_level: info

deny_list:
  extra_patterns: ["*.vault"]
  remove_patterns: []

dangerous_ops:
  extra_patterns: []
  allow_patterns:
    - "rm -rf node_modules"
    - "rm -rf dist"
    - "rm -rf build"
    - "rm -rf .next"
    - "rm -rf __pycache__"
    - "rm -rf .pytest_cache"
    - "rm -rf .ruff_cache"

secrets:
  extra_patterns: []
  file_allowlist: []

observability:
  openobserve_url: "http://localhost:5080"
  openobserve_org: "default"
  openobserve_user: "admin@dev-loop.local"
  openobserve_password: "devloop123"
  service_name: "dev-loop-ambient"

checkpoint:
  gates: [sanity, semgrep, secrets, atdd, review]
  skip_review: false
  atdd_required: false    # Set true to require specs/ before code
```

#### Layer 3: Per-Repo Config — `.devloop.yaml` (in repo root)

```yaml
ambient: true

deny_list:
  extra_patterns: ["config/production.yaml"]
  remove_patterns: [".npmrc"]

dangerous_ops:
  allow_patterns:
    - "docker compose down"
    - "prisma migrate"

checkpoint:
  skip_gates: [review]
  test_command: "npm test"
  atdd_required: true     # This repo requires specs

workflow: tracer-bullet    # Enforce: changes must touch test + src
spec_required: true        # ATDD: must write spec before code

secrets:
  file_allowlist: ["tests/fixtures/fake-key.pem"]
```

### Config Loading

1. Built-in defaults (compiled into binary)
2. Merge global `~/.config/dev-loop/ambient.yaml`
3. On first check for a repo, detect `.devloop.yaml` from `cwd`, merge
4. Cache repo config in daemon (keyed by repo root path)
5. Reload on `SIGHUP` or `dl config reload`

### Merge Rules

- `extra_patterns`: appended to existing list
- `remove_patterns`: subtracted from existing list
- `allow_patterns`: appended to allowlist
- `skip_gates`: subtracted from active gates
- Scalars (booleans, strings): later layer wins

---

## Tier 1: Always-On Checks

### Check Flow (in-process, no daemon round-trip needed)

```
dl hook pre-tool-use
  │
  ├─ Read stdin JSON from Claude Code
  │  {
  │    "session_id": "abc123",
  │    "tool_name": "Write",
  │    "tool_input": { "file_path": "/path/to/file.py", "content": "..." },
  │    "cwd": "/home/user/repo"
  │  }
  │
  ├─ Check enable state (ambient.yaml) → bail if disabled
  ├─ Check worktree detection → bail if inside /tmp/dev-loop/worktrees/
  │
  ├─ If tool is Write|Edit:
  │    └─ Match file_path against deny_patterns
  │       ├─ MATCH → exit 2 + stderr reason
  │       └─ NO MATCH → exit 0
  │
  ├─ If tool is Bash:
  │    ├─ Match command against allow_patterns → skip if matched
  │    ├─ Match command against dangerous_ops patterns
  │    │   ├─ severity=block → exit 2 + stderr reason
  │    │   └─ severity=warn → permissionDecision: "ask"
  │    │
  │    └─ If command matches "git commit" and Tier 2 enabled:
  │         ├─ Trigger checkpoint (contact daemon /checkpoint)
  │         ├─ Checkpoint passes → exit 0 (inject gate trailer)
  │         └─ Checkpoint fails → exit 2 + gate failure details
  │
  └─ Notify daemon (async, non-blocking):
       POST /check { session_id, tool, action, duration }
       → daemon records event + emits OTel span + broadcasts SSE
```

### PostToolUse Flow

```
dl hook post-tool-use
  │
  ├─ Read stdin JSON (includes tool_name, tool_input)
  │
  ├─ If tool is Write|Edit:
  │    ├─ Extract file content from tool_input
  │    ├─ Match content against secret_patterns (in-process regex)
  │    ├─ If secrets found:
  │    │    └─ Output JSON with additionalContext:
  │    │       "WARNING: Possible secret detected in {file}. Pattern: {match}.
  │    │        Do NOT commit this file. Use .env.example with placeholders."
  │    └─ If clean → exit 0
  │
  └─ Notify daemon (async)
```

### Worktree Detection

If `cwd` starts with `/tmp/dev-loop/worktrees/`, the ambient layer stands down entirely. The TB pipeline has its own gates — double-gating wastes time and pollutes traces.

### Performance Budget

| Operation | Target | How |
|-----------|--------|-----|
| Read stdin JSON | <1ms | serde_json from stdin |
| Check enable state | <1ms | Read small YAML file |
| Deny list matching | <0.5ms | Pre-compiled glob patterns |
| Dangerous ops matching | <0.5ms | Pre-compiled regex set |
| Secret pattern matching | <1ms | Pre-compiled regex set |
| Total (Tier 1 check) | <5ms | All in-process, no IPC |
| Daemon notification | async | Non-blocking Unix socket POST |

---

## Tier 2: Checkpoint Gates

### Trigger: Commit Interception

When `dl hook pre-tool-use` detects a Bash command matching `git commit`:

```rust
fn is_commit_command(command: &str) -> bool {
    let cmd = command.trim();
    cmd.starts_with("git commit")
        || cmd.contains("&& git commit")
        || cmd.contains("; git commit")
}
```

Un-bypassable from Claude's perspective — fires before the Bash tool executes.

### Gate Suite (replaces bandit-only pipeline)

| Gate | Tool | What It Catches | Speed |
|------|------|----------------|-------|
| **Sanity** | `npm test` / `pytest` / auto-detect | Broken code, failing tests | ~5-15s |
| **Secrets** | gitleaks (full diff scan) | API keys, credentials, private keys | ~2s |
| **SAST** | **Semgrep** (replaces bandit) | SQL injection, XSS, insecure crypto, 30+ languages | ~3-5s |
| **ATDD** | spec check | Spec exists before code (if `atdd_required`) | <1s |
| **Dangerous Ops** | diff scanner | DB migrations, CI changes, auth changes | <1s |
| **Review** | PR-Agent or Claude (optional) | Logic bugs, race conditions, architectural issues | ~10-20s |

**Semgrep replaces bandit** because:
- Bandit is Python-only. Semgrep covers 30+ languages.
- Same rule-based SAST approach, same class of findings.
- CISA-recommended. Free open-source community rules.
- Pre-commit hook support built in.

### Checkpoint Flow

```
dl hook pre-tool-use (Bash: "git commit -m ...")
  │
  ├─ Detect commit command
  ├─ Contact daemon: POST /checkpoint { cwd, session_id }
  │
  ▼
dl daemon /checkpoint handler
  │
  ├─ Get staged diff: git diff --cached
  ├─ Get changed files: git diff --cached --name-only
  ├─ Load repo checkpoint config (.devloop.yaml)
  │
  ├─ If atdd_required and no spec files in diff:
  │    └─ Return { "passed": false, "reason": "ATDD: spec required before code" }
  │
  ├─ If workflow=tracer-bullet and no test files in diff:
  │    └─ Return { "passed": false, "reason": "Tracer bullet: test + src required" }
  │
  ├─ Run gate suite (sequential, fail-fast):
  │    ├─ semgrep --config auto --json <changed_files>
  │    ├─ gitleaks detect --no-git --source <repo>
  │    ├─ <test_command from config>
  │    └─ (optional) PR-Agent review
  │
  ├─ If all gates pass:
  │    └─ Return { "passed": true, "trailer": "Dev-Loop-Gate: <sha256-of-results>" }
  │
  └─ If any gate fails:
       └─ Return { "passed": false, "first_failure": "semgrep", "details": {...} }

  ▼
dl hook pre-tool-use (continued)
  │
  ├─ If passed → exit 0 (Claude can also inject the gate trailer into commit msg)
  └─ If failed → exit 2 + stderr with failure summary
```

### Git Trailer Injection (from Entire CLI research)

On checkpoint pass, the daemon returns a `Dev-Loop-Gate: <hash>` trailer. Claude can append this to the commit message, creating an auditable chain linking every commit to its gate results. The hash is `sha256(gate_results_json)`.

### ATDD Enforcement (from swingerman/atdd)

When `atdd_required: true` in `.devloop.yaml`:
- Checkpoint checks for `specs/` or `*.spec.md` files in the staged diff
- If code changes exist without corresponding spec files, the commit is blocked
- Message: "ATDD: Write a Given/When/Then spec before implementing code"

### Tracer Bullet Enforcement

When `workflow: tracer-bullet` in `.devloop.yaml`:
- Checkpoint checks that the diff includes BOTH test files and source files
- If only source files changed (no tests), the commit is blocked
- Message: "Tracer bullet: changes must include tests alongside implementation"

---

## Tier 3: Full Pipeline (existing)

Unchanged. `just tb1 <issue> <repo>` continues to work exactly as before.

**Worktree improvements adopted from dmux**:
- `git worktree prune` before create (prevents stale ref errors)
- 3 retries over 5 seconds (handles timing race conditions)
- Queued async cleanup (singleton queue prevents concurrent git corruption)
- Orphan detection in `dl status` (scans `/tmp/dev-loop/worktrees/` for stale entries)

---

## OTel Integration

### Service Name

`dev-loop-ambient` — distinct from `dev-loop` (TB pipeline). Both export to the same OpenObserve instance.

### Span Hierarchy

```
ambient.session (root span — full session lifetime)
├── ambient.check (per tool call, <5ms)
│   { tool, action, check_type, reason, duration_us, tier:1 }
│
├── ambient.checkpoint (Tier 2, ~30s)
│   { tier:2, gates_total, gates_passed, gates_failed, trigger }
│
├── ambient.handoff (PreCompact auto-serialize)
│   { handoff_path, token_count, files_modified }
│
└── ambient.session.summary (SessionEnd)
    { total_checks, blocked, warned, checkpoints, duration_s,
      repo, tokens_in, tokens_out, outcome }
```

### New Dashboard: `config/dashboards/ambient-sessions.json`

| Panel | Type | What It Shows |
|-------|------|--------------|
| Active Sessions | metric | Count of sessions in last 24h |
| Blocks by Tool Type | bar | Write vs Bash vs Edit block counts |
| Most Blocked Patterns | bar | Which deny/dangerous patterns fire most |
| Check Latency (p50/p95/p99) | line | Hook performance over time |
| Checkpoint Pass/Fail | bar | Tier 2 gate results by gate name |
| Sessions by Repo | pie | Which repos get the most ambient coverage |
| Token Usage by Session | line | Cost tracking from transcript parsing |
| Outcome Distribution | pie | SUCCESS vs PARTIAL vs FAIL across sessions |

---

## Session Continuity

### Problem (from Continuous-Claude-v3 research)

Claude Code's context compaction destroys session state. Without continuity, each compaction or resume starts from scratch.

### Differentiated SessionStart (from CC-v3)

The `session-start` hook receives `source: startup | resume | compact | clear`:

- **startup**: One-line notification: "dev-loop ambient active. Last session: 47 checks, 1 block."
- **resume | compact | clear**: Full state injection via `additionalContext` — load the most recent handoff YAML from `/tmp/dev-loop/sessions/`.

### PreCompact Auto-Handoff (from CC-v3)

Before Claude compacts, `dl hook pre-compact`:
1. Reads the session transcript (path from hook JSON)
2. Extracts: tool calls, modified files, errors, gate results
3. Writes structured YAML to `/tmp/dev-loop/sessions/<session-id>.yaml`

```yaml
---
session: abc123
date: 2026-03-15
source: compact
outcome: partial
---

goal: Implementing user authentication
now: Fix the JWT token validation bug in auth.py
test: pytest tests/test_auth.py -v

done_this_session:
  - task: Added JWT middleware
    files: [src/auth.py, src/middleware.py]
  - task: Fixed CORS headers
    files: [src/app.py]

blockers: []
gate_results:
  last_checkpoint: passed
  semgrep_findings: 0
  tests_passing: true

files:
  modified: [src/auth.py, src/middleware.py, src/app.py]

ambient_stats:
  checks: 47
  blocked: 1
  warned: 3
```

Target: ~400 tokens (YAML, not markdown — from CC-v3 research).

### 85% Context Guard (from CC-v3)

The `dl hook stop` fires when Claude completes a turn. If context usage exceeds 85%:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "Stop",
    "decision": "block",
    "reason": "Context at 87%. Auto-handoff written to /tmp/dev-loop/sessions/abc123.yaml. Run /compact or start a new session."
  }
}
```

This forces a structured state dump before context death.

### Outcome Tracking (from CC-v3)

On `SessionEnd`, the daemon prompts the user to grade the session:

```
Session abc123 ending. Outcome?
  [1] SUCCESS — completed the goal
  [2] PARTIAL — made progress
  [3] FAIL — blocked or wrong direction
  [Enter] skip
```

Outcome stored in the session YAML and emitted as an OTel attribute. Feeds the aggregate learning loop.

---

## Session Transcript Parsing

### Problem

Hooks don't receive token counts. But Claude Code's hook JSON includes `transcript_path`.

### Solution

On `SessionEnd`, the daemon parses the transcript for:
- Total input/output tokens (from `{"type":"result"}` entries)
- Number of turns
- Tools used and frequency
- Files created/modified/read
- Whether any commits were made
- Agent vs human contribution ratio

### Transcript Flush Sentinel (from Entire CLI research)

Before parsing, poll the last 4KB of the transcript file for a completion marker with a 3-second timeout. Ensures async writes complete before parsing.

```rust
fn wait_for_transcript_flush(path: &Path, timeout: Duration) -> bool {
    let start = Instant::now();
    while start.elapsed() < timeout {
        if let Ok(content) = read_last_4kb(path) {
            if content.contains("\"type\":\"result\"") {
                return true;
            }
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    false // parse anyway, best-effort
}
```

---

## Aggregate Learning

### Structured Events to OpenObserve

```json
{
  "event": "ambient.block",
  "session_id": "abc123",
  "repo": "OOTestProject1",
  "tool": "Write",
  "check_type": "deny_list",
  "pattern": ".env",
  "file_path": "/home/user/repo/.env",
  "timestamp": "2026-03-15T14:30:00Z"
}
```

### Attribution Tracking (from Entire CLI research)

On SessionEnd, calculate:
- Lines added by agent vs human (from transcript tool_use entries)
- % of commit that was agent-generated
- Store in OTel span attributes for aggregate analysis

### Questions to Answer After 100+ Sessions

```sql
-- Most triggered deny patterns (false positive candidates)
SELECT pattern, COUNT(*) as hits FROM ambient_events
WHERE event = 'ambient.block' AND check_type = 'deny_list'
GROUP BY pattern ORDER BY hits DESC

-- Gate failure hotspots
SELECT repo, first_failure, COUNT(*) as fails FROM ambient_events
WHERE event = 'ambient.checkpoint' AND passed = false
GROUP BY repo, first_failure ORDER BY fails DESC
```

---

## Override / Escape Hatch

### `dl allow-once`

```bash
dl allow-once ".env"           # Allow one write to .env (expires after match or 5 min)
```

### Warn Severity (Ask the User)

For "warn" severity items (like `git reset --hard`):

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "ask",
    "permissionDecisionReason": "dev-loop: git reset --hard detected. This will discard all uncommitted changes."
  }
}
```

Shows the user a confirmation prompt — not a hard block, but a speed bump.

---

## External Tool Integrations

### Install Alongside (zero integration code)

| Tool | What It Does | Integration |
|------|-------------|------------|
| **[AgentLens](https://github.com/RobertTLange/agentlens)** | Session replay/inspection for Claude, Codex, Cursor, Gemini | Reads Claude's native JSONL from `~/.claude/projects/`. Just install it. |
| **[Entire CLI](https://github.com/entireio/cli)** | Links AI sessions to git commits with attribution tracking | Installs its own hooks alongside ours. Captures transcripts on push. |
| **[Semgrep](https://github.com/semgrep/semgrep)** | Multi-language SAST (replaces bandit). 30+ languages, CISA-recommended. | Called by Tier 2 checkpoint: `semgrep --config auto --json` |

### Adopt Patterns From (no direct dependency)

| Source | Pattern Adopted |
|--------|----------------|
| **[dmux](https://github.com/standardagents/dmux)** | Worktree prune+retry, queued cleanup, orphan detection, 3-tier hook resolution |
| **[Continuous-Claude-v3](https://github.com/parcadei/Continuous-Claude-v3)** | Differentiated SessionStart, PreCompact auto-handoff, 85% context guard, YAML handoffs, outcome tracking |
| **[Axel](https://github.com/txtx/axel-app)** | SSE broadcast via tokio broadcast channel, JSONL EventLogger with mpsc backpressure |
| **[Entire CLI](https://github.com/entireio/cli)** | Git trailer injection, transcript flush sentinel, attribution tracking |
| **[ATDD plugin](https://github.com/swingerman/atdd)** | Spec-before-code enforcement, Given/When/Then acceptance tests |
| **[everything-claude-code](https://github.com/affaan-m/everything-claude-code)** | Settings.json hook structure reference |

### Evaluate Later (v2+)

| Tool | Why Wait |
|------|----------|
| **[agent-vault](https://github.com/botiverse/agent-vault)** | Placeholder substitution for secrets in reads — smarter than deny list. v2 feature. |
| **[Shannon](https://github.com/KeygraphHQ/shannon)** | Autonomous pentesting agent. Could be a deep security gate. Needs evaluation. |
| **[Headroom](https://github.com/chopratejas/headroom)** | Context compression 47-92%. Test if additionalContext gets verbose. |
| **[PR-Agent](https://github.com/qodo-ai/pr-agent)** | Free AI PR review (self-hosted, own API key). Could replace our LLM Gate 4. |
| **[Tracecat](https://github.com/TracecatHQ/tracecat)** | Security automation workflows. Could orchestrate complex gate pipelines. |

### Testing Tools

| Tool | Purpose |
|------|---------|
| **[Turmoil](https://github.com/tokio-rs/turmoil)** | DST for async Rust — test daemon's concurrent session handling deterministically |
| **[LangChain deepagents](https://github.com/langchain-ai/deepagents)** | Harness engineering patterns — analyze failure traces at scale |
| **[Utah/Inngest](https://github.com/inngest/utah)** | Durable event-driven harness pattern — validates our daemon + hook architecture |

---

## Dashboard Validation

### `dl dashboard-validate`

Reads `config/dashboards/*.json`, runs each panel's SQL against OpenObserve search API:

```
$ dl dashboard-validate

Loop Health (config/dashboards/loop-health.json)
  Panel 1 "Issues Processed (24h)":     ✅ 8 rows
  Panel 2 "Success Rate":               ✅ 1 row (87.5%)
  Panel 3 "Average Lead Time":          ❌ ERROR: column not found
  ...

Summary: 15/17 panels valid, 1 error, 1 empty
```

---

## Source Layout

```
~/dev-loop/
├── daemon/                              # NEW: Rust workspace
│   ├── Cargo.toml
│   ├── Cargo.lock
│   └── src/
│       ├── main.rs                      # CLI entrypoint (clap)
│       ├── cli.rs                       # Subcommand dispatch
│       ├── daemon.rs                    # Daemon start/stop/lifecycle
│       ├── server.rs                    # Unix socket HTTP server (hyper)
│       ├── sse.rs                       # SSE broadcast (/inbox endpoint)
│       ├── event_log.rs                 # JSONL append-only writer (mpsc)
│       ├── hooks.rs                     # Hook stdin/stdout handlers
│       ├── check/
│       │   ├── mod.rs
│       │   ├── deny_list.rs             # Glob-based file deny list
│       │   ├── dangerous_ops.rs         # Regex-based command scanner
│       │   └── secrets.rs              # Regex-based secret pattern scanner
│       ├── checkpoint.rs               # Tier 2: Semgrep + gitleaks + ATDD
│       ├── config.rs                    # Config loading + merging (3 layers)
│       ├── session.rs                   # Session lifecycle + state
│       ├── continuity.rs               # Handoff YAML read/write, context guard
│       ├── transcript.rs               # Session transcript JSONL parser
│       ├── otel.rs                      # OTel span management + export
│       ├── install.rs                   # settings.json merge/unmerge
│       ├── override_mgr.rs             # dl allow-once tracking
│       ├── dashboard.rs                # dl dashboard-validate
│       └── rules_md.rs                 # ambient-rules.md generator
│   └── tests/
│       ├── deny_list_test.rs
│       ├── dangerous_ops_test.rs
│       ├── secrets_test.rs
│       ├── config_merge_test.rs
│       ├── hook_roundtrip_test.rs
│       ├── checkpoint_test.rs
│       ├── continuity_test.rs
│       ├── turmoil_concurrent.rs       # DST: concurrent session races
│       └── fixtures/
│           ├── hook-write-env.json
│           ├── hook-bash-rm-rf.json
│           ├── hook-bash-commit.json
│           └── handoff-sample.yaml
│
├── config/
│   ├── ambient.yaml                    # NEW: default ambient config template
│   └── dashboards/
│       ├── loop-health.json            # existing
│       ├── quality-gates.json          # existing
│       ├── agent-performance.json      # existing
│       └── ambient-sessions.json       # NEW: ambient layer dashboard
│
├── src/devloop/                        # Existing Python
│   ├── gates/server.py                 # Tier 2 reuses existing gates
│   ├── runtime/deny_list.py            # Reference for Rust port
│   └── ...
│
├── justfile                            # Add: dl-build, dl-install, dl-test
│
└── docs/
    └── ambient-layer-plan.md           # THIS FILE
```

---

## Implementation Phases

### Phase 1: Daemon Skeleton + SSE + Event Log
- `cargo init ~/dev-loop/daemon`
- CLI with clap: `dl start`, `dl stop`, `dl status`, `dl stream`
- Unix socket listener with hyper
- `/status` endpoint, `/inbox` SSE endpoint
- JSONL event log with mpsc backpressure
- PID file management
- **Deliverable**: `dl start` → `dl stream` shows live events

### Phase 2: Deny List + Dangerous Ops + Secrets Engine
- Port `DENIED_PATTERNS` from `deny_list.py` to Rust
- Implement `DANGEROUS_BASH_PATTERNS` (Bash command scanning)
- Implement `SECRET_PATTERNS` (in-process regex)
- `/check` endpoint with all three check types
- Turmoil tests for concurrent session handling
- **Deliverable**: `dl check '{"tool_name":"Write","tool_input":{"file_path":".env"}}'` → `block`

### Phase 3: Hook Integration
- `dl hook pre-tool-use` / `dl hook post-tool-use` subcommands
- Read stdin JSON, perform checks, write verdict to stdout/stderr
- `dl install` / `dl uninstall` (settings.json merger)
- Enable/disable toggle (`ambient.yaml`)
- Worktree detection
- **Deliverable**: Install hooks, open Claude session, Write `.env` is blocked

### Phase 4: Config System
- Global config loading (`~/.config/dev-loop/ambient.yaml`)
- Per-repo config (`.devloop.yaml`)
- Three-layer merge logic
- Config caching in daemon
- Hot-reload via SIGHUP
- **Deliverable**: `.devloop.yaml` in a repo changes behavior for that repo only

### Phase 5: OTel + Observability
- Session start/end span management
- Per-check span emission (async, non-blocking)
- OTLP/HTTP export to OpenObserve
- `ambient-sessions.json` dashboard definition
- Install AgentLens for free session replay
- **Deliverable**: Ambient spans visible in OpenObserve

### Phase 6: Tier 2 Checkpoint
- Commit interception in `dl hook pre-tool-use`
- Install Semgrep, wire into `/checkpoint` endpoint
- Gitleaks integration for diff scanning
- ATDD spec check and tracer-bullet enforcement
- Git trailer injection (`Dev-Loop-Gate: <hash>`)
- **Deliverable**: `git commit` blocked if Semgrep finds SQL injection

### Phase 7: Session Continuity
- `dl hook pre-compact` → auto-handoff YAML
- `dl hook stop` → 85% context guard
- Differentiated SessionStart (startup vs resume/compact)
- Handoff YAML read/write in `continuity.rs`
- Transcript parsing with flush sentinel
- Outcome tracking on SessionEnd
- **Deliverable**: Compaction preserves pipeline state, session grades in OTel

### Phase 8: External Integrations + Polish
- Install Entire CLI for session→commit linking
- `dl dashboard-validate` (SQL query validation)
- `dl traces --last N` (terminal span viewer)
- `dl allow-once` override mechanism
- CLAUDE.md ambient-rules.md generation
- Performance benchmarks (criterion)
- Turmoil stress test: concurrent sessions + races
- **Deliverable**: Full system operational, all `dl` subcommands working

---

## Design Decisions Log

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| 1 | Hook mechanism | `dl` binary IS the hook | Single process, no subprocess chain, <5ms |
| 2 | Daemon IPC | Unix domain socket + hyper | Fastest IPC, filesystem auth, no port conflicts (validated against Axel's TCP approach) |
| 3 | Fail behavior | Fail-open | Enhancement, not a lock — never block the user |
| 4 | Read hook | Not included | Reading .env is fine, writing secrets is the problem |
| 5 | Pre-commit trigger | Commit interception in PreToolUse[Bash] | Un-bypassable (unlike git hooks with --no-verify) |
| 6 | Secrets (Tier 1) | In-process regex | gitleaks is 100-200ms startup; regex is <1ms |
| 7 | Secrets (Tier 2) | gitleaks (full diff scan) | Correct tool for comprehensive scanning |
| 8 | SAST tool | Semgrep (replaces bandit) | Multi-language (30+), same rule-based approach, CISA-recommended, free |
| 9 | Session replay | AgentLens (install alongside) | Reads Claude's native JSONL — zero integration, free |
| 10 | Session→commit | Entire CLI (install alongside) | Proven linkage via git hooks + trailers, no custom code |
| 11 | State persistence | YAML handoff files (~400 tokens) | Token-efficient, deterministic parsing (from CC-v3) |
| 12 | Context guard | 85% Stop hook block | Prevents silent context death (from CC-v3) |
| 13 | SessionStart | Differentiated by source type | startup=one-liner, resume/compact=full injection (from CC-v3) |
| 14 | Event streaming | SSE /inbox endpoint | Inspectable with curl, debuggable (from Axel) |
| 15 | Audit trail | Append-only JSONL event log | mpsc backpressure, fail-silent drops (from Axel) |
| 16 | Worktree lifecycle | Prune+retry, queued cleanup | Handles stale refs and concurrent ops (from dmux) |
| 17 | Daemon testing | Turmoil DST | Deterministic simulation of concurrent async behavior |
| 18 | Spec enforcement | ATDD at checkpoint | Given/When/Then before code (from swingerman/atdd) |
| 19 | Git trailer | `Dev-Loop-Gate: <hash>` | Auditable gate-pass chain (from Entire CLI) |
| 20 | Outcome tracking | Grade on SessionEnd | Feeds aggregate learning (from CC-v3) |

---

## Open Questions

1. **Should subagent tool calls fire ambient hooks?** The `agent_id` differs from the parent. Should the daemon nest sub-sessions under the parent, or treat them independently?

2. **Should Tier 2 run on `git commit --amend`?** Amend commits change existing commits — gate differently?

3. **How to handle piped/complex Bash commands?** `echo "data" | git commit` — how far does commit detection go?

4. **Per-repo opt-out vs opt-in?** Current: on globally, repos opt out. Alternative: off by default, repos opt in.

5. **Should Entire CLI and AgentLens be mandatory or optional?** They're "install alongside" but do we bundle the install in `dl install`?

6. **Context percentage detection for Stop hook**: How does the daemon know context is at 85%? CC-v3 reads it from a temp file. Is this in the hook JSON?

7. **PR-Agent vs built-in LLM review gate**: PR-Agent is battle-tested but adds a dependency. Keep our Gate 4 as fallback?

---

## Research Sources

These projects were studied to inform the design:

| Project | Key Insight Adopted |
|---------|-------------------|
| [AgentLens](https://github.com/RobertTLange/agentlens) | Reads Claude's native JSONL — free session replay with zero integration |
| [Entire CLI](https://github.com/entireio/cli) | Git trailer for commit-to-session linkage, transcript flush sentinel |
| [dmux](https://github.com/standardagents/dmux) | Worktree prune+retry, queued cleanup, orphan detection |
| [Continuous-Claude-v3](https://github.com/parcadei/Continuous-Claude-v3) | PreCompact handoff, 85% context guard, differentiated SessionStart, YAML format |
| [Axel](https://github.com/txtx/axel-app) | SSE broadcast pattern, JSONL EventLogger with mpsc backpressure |
| [ATDD plugin](https://github.com/swingerman/atdd) | Spec-before-code enforcement |
| [everything-claude-code](https://github.com/affaan-m/everything-claude-code) | Settings.json hook structure patterns |
| [Semgrep](https://github.com/semgrep/semgrep) | Multi-language SAST replacing bandit |
| [Turmoil](https://github.com/tokio-rs/turmoil) | Deterministic simulation testing for async Rust |
| [PR-Agent](https://github.com/qodo-ai/pr-agent) | Self-hosted AI code review (evaluate for v2) |
| [agent-vault](https://github.com/botiverse/agent-vault) | Placeholder substitution for secrets (evaluate for v2) |
| [Shannon](https://github.com/KeygraphHQ/shannon) | Autonomous pentesting agent (evaluate for v2) |
| [Don't Waste Your Back Pressure](https://banay.me/dont-waste-your-backpressure/) | Theoretical foundation for structured feedback loops |
| [Harness Engineering](https://x.com/i/status/2023805578561060992) | Analyze failure traces at scale, engineer the harness not the model |
| [The Intent Layer](https://intent-systems.com/blog/intent-layer) | Embed senior engineering mental models as persistent codebase context |
| [Gastown](https://github.com/steveyegge/gastown) | Persistent agent identity concept (reference only) |
| [LangChain deep agents](https://github.com/langchain-ai/deepagents) | Evaluation patterns for deep agents |
| [Utah/Inngest](https://github.com/inngest/utah) | Durable event-driven harness pattern |

---

## Steps From Human

After implementation:

1. **Install Semgrep**: `pip install semgrep` or `brew install semgrep`
2. **Install AgentLens**: Follow setup at https://github.com/RobertTLange/agentlens
3. **Install Entire CLI**: Follow setup at https://github.com/entireio/cli
4. **Build daemon**: `cd ~/dev-loop/daemon && cargo build --release && cp target/release/dl ~/.local/bin/`
5. **Install hooks**: `dl install`
6. **Verify Tier 1**: Open Claude session, try writing to `.env` — should be blocked
7. **Test toggle**: `dl disable` → retry `.env` → allowed. `dl enable` to restore.
8. **Test Tier 2**: Make a code change with a security issue, try to commit — should be blocked
9. **Check SSE stream**: `dl stream` in another terminal — see events flow in real time
10. **Check OpenObserve**: http://localhost:5080, look for `dev-loop-ambient` service
11. **Review ambient-rules.md**: Check `~/.claude/dev-loop-ambient-rules.md` is generated
12. **Visual dashboard check**: Open dashboards in OpenObserve, report rendering issues
13. **Add `.devloop.yaml`**: Per-repo configs in repos that need custom rules
14. **Stress test**: Run several Claude sessions across repos, check `dl status`
15. **Test continuity**: Start a session, let it compact, verify handoff state loads on resume
