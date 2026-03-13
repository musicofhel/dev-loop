# TB Hardening Plan — Race Conditions & Resource Leaks

Audit of TB-1/TB-2/TB-3 found 3 critical, 10 medium, and 8 low issues.
Organized as tracer-bullet slices — each slice is testable e2e before moving on.

---

## Slice 1: Subprocess Lifecycle (C1 + M2)

**What it proves:** Subprocesses are properly killed on timeout and never leave zombies.

| Issue | Severity | Description |
|-------|----------|-------------|
| C1 | Critical | `subprocess.run` timeout leaves Claude CLI running — zombie + worktree race |
| M2 | Medium | Uncaught `JSONDecodeError` in `poll_ready()` crashes pipeline |

**Changes:**
- `runtime/server.py`: Switch `_run_agent` to `Popen` + `communicate(timeout=)` + `proc.kill()` on timeout
- `beads_poller.py`: Wrap `json.loads` in try/except, return empty list on parse failure

**Validation:** `uv run pytest` + manually trigger a timeout (set timeout=5s, give agent a long task) and confirm the process is killed (no zombie in `ps aux`).

---

## Slice 2: Heartbeat Thread Safety (C2 + M3 + M4)

**What it proves:** The heartbeat thread stops cleanly before worktree cleanup, no file races.

| Issue | Severity | Description |
|-------|----------|-------------|
| C2 | Critical | `stop_heartbeat` doesn't `join()` thread — races with cleanup |
| M3 | Medium | Temp file leak in `_touch_metadata` on non-OSError |
| M4 | Medium | `rglob` scan on every tick instead of caching path |

**Changes:**
- `heartbeat.py`: Return `(event, thread)` from `start_heartbeat()`, `join(timeout=interval+5)` in `stop_heartbeat()`
- `heartbeat.py`: Catch `Exception` (not just `OSError`) in `_touch_metadata` cleanup
- `heartbeat.py`: Accept `worktree_path` param, compute metadata path directly instead of `rglob`
- `pipeline.py` (all 3 TBs): Update `start_heartbeat`/`stop_heartbeat` call sites for new signature

**Validation:** Unit test that starts heartbeat, stops it, and confirms `thread.is_alive() == False` before cleanup runs.

---

## Slice 3: Seed Integrity (C3 + M10 + M9 + L-seed-silent)

**What it proves:** Vulnerable code seeding is atomic — commit succeeds or pipeline fails fast.

| Issue | Severity | Description |
|-------|----------|-------------|
| C3 | Critical | Silent `git add`/`commit` failure in `_seed_vulnerable_code` |
| M10 | Medium | `_extract_security_findings` silently returns empty on structure mismatch |
| M9 | Medium | `vuln_fixed` false positive when Gate 3 skipped on retry |
| L | Low | Seed failure in forced mode proceeds silently to false success |

**Changes:**
- `pipeline.py _seed_vulnerable_code`: Check return codes, add `timeout=30`, return `False` on git failure
- `pipeline.py run_tb3`: Fail fast if `force_vuln_seed=True` but `_seed_vulnerable_code` returns `False`
- `pipeline.py _extract_security_findings`: Log warning if `gate_3_security` not found when expected
- `pipeline.py`: Check Gate 3 `skipped` field in retry — `vuln_fixed=False` if Gate 3 was skipped

**Validation:** `just tb3 <issue> ~/prompt-bench` — seeded mode must show `vulnerability_fixed: true` with non-empty `security_findings` and `cwe_ids`. Unit test: mock git add failure → seed returns False → pipeline aborts.

---

## Slice 4: OTel Lifecycle (M1 + M5 + L-tracing-race + L-finally-span)

**What it proves:** Traces are complete, spans are linked, and no spans are lost on exit.

| Issue | Severity | Description |
|-------|----------|-------------|
| M1 | Medium | `BatchSpanProcessor` never shut down; TB-1 never calls `force_flush` |
| M5 | Medium | Span link chain missing — retry 1 not linked to attempt 0 |
| L | Low | `init_tracing` race on `_provider` (no lock) |
| L | Low | OTel span in `finally` block could throw, skipping cleanup |

**Changes:**
- `pipeline.py run_tb1`: Add `provider.force_flush()` in finally block (matching TB-2/TB-3)
- `pipeline.py` (TB-2/TB-3): Set `previous_span_context` from initial gates span before retry loop
- `tracing.py`: Add `threading.Lock` around `_provider` init
- `pipeline.py` (all TBs): Wrap span creation in `finally` blocks with try/except so cleanup always runs

**Validation:** Run TB-1, query OpenObserve for the trace — verify all spans present and flush succeeds. Run TB-2 forced mode — verify span links chain from last retry back to attempt 0.

---

## Slice 5: Retry Resilience (M6 + M7 + M8 + L-forced-synthetic)

**What it proves:** Retries accumulate proper context, prompts stay bounded, and failed issues get unclaimed.

| Issue | Severity | Description |
|-------|----------|-------------|
| M6 | Medium | Agent spawn failures not accumulated into `all_gate_failures` |
| M7 | Medium | Retry prompt grows unbounded across attempts |
| M8 | Medium | Claimed issue never unclaimed on early pipeline failure |
| L | Low | Forced-mode synthetic failure pollutes retry prompts |

**Changes:**
- `pipeline.py` (TB-2/TB-3): When agent spawn fails during retry, append synthetic failure record to `all_gate_failures`
- `server.py build_retry_prompt`: Cap included failures to last 2 attempts (summarize older ones)
- `pipeline.py` (all TBs): Add `br update <id> --status open` in finally block on non-success
- `pipeline.py run_tb2`: Filter synthetic forced failure from retry prompts on attempt 2+

**Validation:** TB-2 forced mode — verify retry prompt includes agent crash context but stays under reasonable size. Verify issue status is `open` (not stuck `in_progress`) after a pipeline crash.

---

## Slice 6: Remaining Low-Severity Hardening

| Issue | Description |
|-------|-------------|
| L | `kill_agent` PID validation — check `/proc/{pid}/cmdline` |
| L | Worktree cleanup condition readability — use explicit `escalated` flag |
| L | TOCTOU on stale worktree removal |
| L | Bandit `TimeoutExpired` not caught as structured gate failure |
| L | NDJSON truncation edge case in Gate 4 parser |
| L | `_run_cmd` doesn't clear `CLAUDECODE` (inconsistency) |
| L | Heartbeat spans inherit pipeline context (pollutes trace tree) |
| L | Stale `gate_suite.first_failure` in retry log messages |

**Changes:** Address each individually after slices 1-5 pass.

**Validation:** Unit tests for each fix.

---

## Implementation Order

```
Slice 1 (subprocess) → Slice 2 (heartbeat) → Slice 3 (seed) → Slice 4 (otel) → Slice 5 (retry) → Slice 6 (low)
```

Slice 1 and 2 are the foundation — they fix the two resource races that affect ALL tracer bullets.
Slice 3 is TB-3 specific. Slices 4-5 improve all TBs. Slice 6 is polish.
