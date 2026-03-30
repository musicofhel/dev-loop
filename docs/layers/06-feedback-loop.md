# Layer 6: Feedback Loop

## Purpose
Close the loop. Every output from the system feeds back as input. Failed runs get retried. Cost spikes get throttled. Trace patterns get turned into harness improvements. Nothing dead-ends.

**All tools are open source or built in-house. Zero paid services.**

### TB-1 Minimal
TB-1 wires only Channel 1 (Agent Retry). Channel 4 (Cross-Repo Cascade) is wired in TB-5. All other channels are not yet implemented. The 7-channel system described below is the target state.

## Feedback Channels

### Channel 1: Agent Retry (Automatic) -- IMPLEMENTED

When a quality gate fails, the feedback loop:
1. Extracts the structured failure from the gate result
2. Builds a retry prompt with the failure context (capped to last 2 attempts to bound prompt size)
3. Re-spawns the agent in the same worktree with the error appended
4. Tracks retry count (max from agent persona config)

```
Gate Failure
    │
    ▼
Parse failure reason ──► Build retry context ──► Re-spawn agent
                                                      │
                                                      ▼
                                               Run gates again
                                                      │
                                          ┌───────────┴───────────┐
                                          │                       │
                                       Pass → PR              Fail again
                                                                  │
                                                         Retry limit?
                                                      ┌─────┴─────┐
                                                      No          Yes
                                                      │           │
                                                   Retry      Escalate
                                                              (issue → Blocked)
```

Retry context template:
```
## Issue: {{issue_title}}

### Failure 1: {{gate_name}} quality gate

Failure details:
  - [CRITICAL] in src/db/query.ts:42 [B608]: SQL injection

Please fix the issues listed above and try again. Do not start over — your
previous changes are still in the worktree. Make the minimal change needed
to pass the gates.
```

### Channel 2: Harness Tuning (Semi-Automatic) -- NOT IMPLEMENTED

When the same failure pattern repeats across multiple issues, collect sessions, analyze patterns, and suggest CLAUDE.md/config improvements. Requires human review before applying.

### Channel 3: Cost Alerts (Automatic) -- NOT IMPLEMENTED

Turns and tokens are tracked per run (parsed from NDJSON output), but no alerting or throttling is wired yet. Gate 5 checks usage bounds but does not trigger alerts.

### Channel 4: Cross-Repo Cascade (Automatic) -- IMPLEMENTED (TB-5)

```
PR merged in repo A
    │
    ▼
Dependency map check: does any other repo depend on changed files?
    │
    ├── No dependencies → done
    └── Repo B depends on changed API
         │
         ▼
    Detect changed files via git diff
    Match against dependency map watches
    Create downstream cascade issue in beads for repo B
         │
         ▼
    Issue enters intake → normal loop for repo B
```

Dependency map (manual for now):
```yaml
# config/dependencies.yaml
dependencies:
  - source: OOTestProject1
    target: OOTestProject2
    watches:
      - "src/oo_test_project/db/**"
    type: data-model
```

### Channel 5: Changelog Generation (Automatic) -- NOT IMPLEMENTED

Planned: extract issue title, PR description, files changed, gate results. Accumulate and generate changelog via `br changelog` + enrichment.

### Channel 6: DORA Feedback (Dashboard) -- NOT IMPLEMENTED

Not automatic -- this is for the human to monitor and adjust the system via OpenObserve dashboards.

### Channel 7: Step Efficiency -- NOT IMPLEMENTED

Planned: analyze tool call patterns to detect wasteful agent execution paths. Feed back as CLAUDE.md overlay tuning.

## MCP Server: `feedback-loop`

```
src/devloop/feedback/
├── __init__.py
├── pipeline.py        # Full pipeline functions (run_tb1 through run_tb6, replay_session)
├── server.py          # MCP server with retry + escalation tools
└── types.py           # RetryPrompt, RetryResult, EscalationResult
```

**Tools exposed:**
- `build_retry_prompt` — parse gate failures, build a retry context prompt with structured failure details
- `retry_agent` — re-spawn agent with failure context, re-run quality gates, track attempt count
- `escalate_to_human` — mark beads issue as "blocked", add failure summary comment with usage breakdown

Note: The pipeline functions (`run_tb1` through `run_tb6`, `replay_session`) are in `pipeline.py` and orchestrate full end-to-end tracer bullet runs. They are not exposed as MCP tools -- they are called directly via `just tb1`, `just tb2`, etc.

### OTel Instrumentation
```
span: feedback.retry
attributes:
  retry.attempt: 1
  retry.reason: "security gate: SQL injection in src/db/query.ts:42"
  retry.gate_failed: security
  retry.agent_exit_code: 0
  runtime.num_turns: 5
  runtime.input_tokens: 8000
  runtime.output_tokens: 1200
parent: trace root (same trace as original attempt)

span: feedback.escalate
attributes:
  escalate.attempts: 3
  escalate.status_updated: true
  escalate.comment_added: true
  issue.id: dl-1kz
parent: trace root
```

### Open Questions
- [x] Retry prompt: should we include the full gate output or a summary? (Resolved: implemented as "last 2 detailed, older summarized" in `build_retry_prompt()`.)
- [x] Cascade: how to detect breaking vs non-breaking changes? (Resolved: started with "all changes cascade" as documented. TB-5 validates.)
- [ ] How does the human "approve" a harness tuning suggestion? (Status: deferred, not blocking any active TB)
