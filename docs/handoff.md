# dev-loop Handoff — 2026-03-12

## Status: TB-3 PASSING

TB-1, TB-2, and TB-3 are all passing.

### TB-1 (Golden Path) — PASSING
- Bug fix: 94s, all gates passed first try
- Feature add: 245s, failed Gate 0 → succeeded on retry

### TB-2 (Failure-to-Retry) — PASSING
- **Forced mode**: 202s — forced Gate 0 failure → retry with error context → pass
- **Organic mode**: 134s — pre-seeded test trap caught missing edge case → retry → pass
- **Escalation path**: 41s — max_retries=0 + forced fail → issue status verified as "blocked"

### TB-3 (Security-Gate-to-Fix) — PASSING
- **Seeded mode**: 55s — pre-seeded CWE-89 → Gate 3 caught it → agent fixed → clean scan
- Pre-flight gate scan: Gate 3 detected 2 SQL injection findings (B608 CWE-89)
- Agent used parameterized queries on retry, vulnerability_fixed=true
- 121 unit tests passing

## What Was Done This Session

### 1. Implemented Gate 3: Security SAST Scan
Added `run_gate_3_security()` to `src/devloop/gates/server.py`:
- Uses bandit for Python SAST scanning
- Parses JSON output into `Finding` objects with CWE classification
- Maps bandit severity (HIGH/MEDIUM) to critical findings
- CWE IDs as OTel span attributes for observability
- Gracefully skips if bandit not installed or project is non-Python

### 2. Updated run_all_gates() sequence
Gate order: 0 (sanity) → 2 (secrets) → 3 (security) → 4 (review).
Gate 3 is fail-fast like others. Skipped gates don't block the suite.

### 3. TB-3 pipeline with pre-flight scan
`run_tb3()` in `src/devloop/feedback/pipeline.py`:
- Seeds vulnerable code + commits it before agent runs
- Pre-flight gate scan catches the vulnerability (Gate 3)
- Agent gets security findings as retry context
- Agent fixes with parameterized queries
- Re-run gates verify the fix

### 4. Bug fixes from first e2e attempt
- **Gate 0 merge-base**: `HEAD~10` fails for short git histories — replaced with safe lookback based on `git rev-list --count`
- **retry_agent model**: Added `model` parameter to `retry_agent()` so retries use the correct model (opus for security-fix persona)
- **Seed commit**: `_seed_vulnerable_code()` now commits the seeded file so Gate 0 detects changes and Gate 3 scans committed code

## Architecture (TB-3 flow)

```
just tb3 <issue_id> <repo_path>
    → run_tb3() in feedback/pipeline.py
        → Phase 1:   poll_ready() — br ready --json
        → Phase 2:   claim_issue() — br update --claim
        → Phase 3:   setup_worktree() — git worktree add
        → Phase 3.5: seed_vulnerable_code() — copy CWE-89 fixture + git commit
        → Phase 4:   select_persona() — security-fix persona (retry_max=3)
        → Phase 5:   init_tracing() — OTel → OpenObserve
        → Phase 6:   start_heartbeat() — background thread
        → Phase 7:   run_all_gates() — pre-flight scan (Gate 3 catches vuln)
        → Phase 8:   gates pass? → success (no vulnerability detected)
        → Phase 9:   gates fail → retry: agent gets CWE context → fixes → re-run gates
        → Phase 10:  retries exhausted → escalate to human
        → Phase 11:  cleanup — stop heartbeat, preserve worktree on escalation
        → Flush:     provider.force_flush() for trace verification
```

## What's Next: TB Hardening (before TB-4)

Race condition audit found 3 critical, 10 medium, 8 low issues across TB-1/TB-2/TB-3.
Organized as 6 tracer-bullet slices in `docs/tb-hardening-plan.md`.

| Slice | Issue | Description | Epic |
|-------|-------|-------------|------|
| 1 | `dl-1kz.14` | Subprocess Lifecycle (zombie kill + JSON parse) | TB-1 |
| 2 | `dl-1kz.15` | Heartbeat Thread Safety (join + temp leak + cache) | TB-1 |
| 3 | `dl-ajr.7` | Seed Integrity (git check + vuln_fixed guard) | TB-3 |
| 4 | `dl-1kz.16` | OTel Lifecycle (flush + span links + lock) | TB-1 |
| 5 | `dl-jd4.11` | Retry Resilience (accumulate + cap + unclaim) | TB-2 |
| 6 | `dl-1kz.17` | Low-Severity Polish (8 items) | TB-1 |

Order: 1 → 2 → 3 → 4 → 5 → 6. Each slice validated e2e before moving on.

## After Hardening: TB-4 (Cost Control)

TB-4 proves token spend is visible and controllable. Runaway agents get killed, not just logged.

Key areas:
1. Token proxy between agent and LLM API
2. Cost gate: checks total spend before PR creation
3. Real-time cost dashboard in OpenObserve
4. Kill signal on budget exceeded

After TB-4: TB-5 (cross-repo), TB-6 (session replay).

## Key Gotchas
- `br show --format json` returns a JSON array (list), not a dict
- `br create` uses `--labels` (plural), not `--label`; no `--epic` flag, use `--parent`
- Gate 0: uses `git rev-list --count HEAD` for safe lookback (handles short git histories)
- Gate 3 skips gracefully if bandit not installed or project is non-Python
- bandit exit code 1 = issues found (not an error), exit code 2 = actual error
- Pre-seeded vulnerable code is committed in worktree before agent runs
- `retry_agent()` must pass `model` param to match persona's model
- `provider.force_flush()` needed after pipeline to ensure spans export
