# Validation Report: Ghost Features Activation (v7)

**Date:** 2026-03-30
**Commit:** f413e03 (Activate ghost features: Gate 5, post-pipeline channels, ambient daemon)

## Summary

Activated 4 groups of ghost code paths that were wired into the codebase but never exercised during TB runs. Ran live validation against OOTestProject1 to confirm trace data flows through to OpenObserve.

## Validation Results

### Phase 2: TB Runs

| TB | Issue | Result | Duration | PR | Notes |
|----|-------|--------|----------|----|-------|
| TB-1 | bd-2y3 | PASS | 95.8s | [#40](https://github.com/musicofhel/OOTestProject1/pull/40) | Gate 5 cost: 15 turns, 1896 total tokens. All 7 gates passed. |
| TB-2 | bd-5k5 | PASS | 235.7s | [#41](https://github.com/musicofhel/OOTestProject1/pull/41) | Forced first failure, 2 retries. Gate 5 tokens threaded through retry path. |
| TB-6 | bd-2ft | PASS | 221.4s | — | 68 session events captured. 1 retry (forced). Post-pipeline ran in finally block. |

### Phase 3: Escalation

| Issue | Result | Duration | Notes |
|-------|--------|----------|-------|
| bd-3li | ESCALATED | 111.5s | `max_retries=0, force_gate_fail=True`. Escalation path fired. `blocked_verified=false` (expected: beads cwd mismatch, harmless). |

### Phase 4: Ambient Daemon

- Binary: `daemon/target/release/dl` (6.7MB, built from Rust)
- Daemon started: pid 74282
- Hooks installed: PreToolUse (Write/Edit deny list, Bash dangerous ops), PostToolUse (secret scan), SessionStart/End, Stop (context guard + handoff)
- Check events captured: 4 (2 allow, 1 block for `rm -rf`, 1 allow)
- **Note:** `service_name = 'dev-loop-ambient'` spans require a separate Claude Code session to appear in OO. Daemon is running and intercepting hooks from this session, but events are logged locally, not yet exported as OTLP spans with the ambient service name.

### Phase 5: OpenObserve Verification

**Schema growth:** 265 -> 279 fields (14 new columns from ghost activation)

**Newly activated ghost attributes confirmed in OO schema:**

| Attribute | Group | Status |
|-----------|-------|--------|
| `gate_input_tokens` | Gate 5 (cost) | LIVE |
| `gate_output_tokens` | Gate 5 (cost) | LIVE |
| `gate_num_turns` | Gate 5 (cost) | LIVE |
| `gate_total_tokens` | Gate 5 (cost) | LIVE |
| `post_pipeline_channels_run` | Post-pipeline | LIVE |
| `post_pipeline_patterns_found` | Post-pipeline | LIVE |
| `post_pipeline_cost_pause_recommended` | Post-pipeline | LIVE |
| `post_pipeline_cost_warnings_count` | Post-pipeline | LIVE |
| `cost_pause_recommended` | Cost monitor | LIVE |
| `cost_warnings_count` | Cost monitor | LIVE |
| `escalate_comment_added` | Escalation | LIVE |
| `escalate_status_updated` | Escalation | LIVE |
| `escalate_comment_error` | Escalation | LIVE |

### Phase 6: dm-traces Coverage

**Custom field coverage:** 246/246 (100%)

All custom fields in the OO schema have at least one span with non-null data. The schema is append-only — OO creates field entries only when data arrives.

**Operations catalog:** 113 distinct operations across 2 services (dev-loop, validation-test), 4322 total spans.

## Expected vs Actual

| Group | Expected Attrs | Actual Confirmed | Status |
|-------|---------------|------------------|--------|
| Gate 5 (cost) | 7 | 4 new + 3 existing (gate_name, gate_status, gate_duration_ms already populated) | PASS |
| Post-pipeline channels | 12 | 4 new (patterns, cost_pause, cost_warnings, channels_run) + cost_pause/warnings at cost_monitor level | PARTIAL — efficiency attrs not yet populated (needs session_events) |
| Escalation path | 2 | 3 (comment_added, status_updated, comment_error) | PASS |
| Ambient layer | ~20 | 0 in OO (daemon running, hooks active, needs separate session) | PENDING |

## What's Still Pending

1. **Ambient daemon OTLP export:** The daemon logs events to `/tmp/dev-loop/events.jsonl` but needs a full Claude Code session (not this one) to emit `dev-loop-ambient` OTLP spans to OO. Run: `cd ~/OOTestProject1 && claude "Read README.md and tell me what this project does"`
2. **Efficiency channel:** `post_pipeline.efficiency_score` and `post_pipeline.efficiency_waste_ratio` require `session_events` to be passed (NDJSON parsing not yet wired). These fire only when explicitly given events.
3. **Channel 5 (changelog):** Deliberately NOT wired — needs multiple closed issues.

## Test Status

708/708 tests passing (no regressions).
