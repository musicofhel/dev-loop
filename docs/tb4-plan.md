# TB-4 Implementation Plan: Runaway-to-Stop

Thin vertical slice through all 6 layers. Proves: runaway agents get stopped,
usage is visible. On Max subscription, turns are the control — not dollars.

## The Slice

| Layer | What | Minimal Change |
|-------|------|----------------|
| **Intake** | Vague issue that will burn turns | Seed beads issue: "refactor the entire codebase" |
| **Orchestration** | Turn limit from persona config | `max_turns_default` field in `agents.yaml` + `PersonaConfig` |
| **Runtime** | CLI-level enforcement + usage parsing | `--max-turns N` + `--output-format json` → parse `num_turns`, tokens |
| **Quality Gates** | Existing gates; skipped when turns exhausted | No new gate. Pipeline checks turns before calling `run_all_gates()` |
| **Observability** | Usage in OTel spans | `runtime.num_turns`, `runtime.input_tokens`, `runtime.output_tokens` |
| **Feedback** | Escalation with usage table | beads comment: "Turn limit: 5/5 turns, 2 attempts" + per-attempt breakdown |

## What Changes Per File

### `runtime/types.py`
- `AgentConfig`: add `max_turns: int | None = None`
- `AgentResult`: add `input_tokens: int = 0`, `output_tokens: int = 0`, `num_turns: int = 0`

### `runtime/server.py`
- `_build_command()`: add `--output-format json`, `--max-turns N` (when set)
- `_run_agent()`: call `_parse_usage_from_output(stdout)` → populate result fields
- New: `_parse_usage_from_output(stdout) -> dict` — scan NDJSON for `{"type":"result"}`, extract usage

### `config/agents.yaml`
- Each persona gets `max_turns_default` (10–25 depending on persona)

### `orchestration/types.py`
- `PersonaConfig`: add `max_turns_default: int = 15`

### `feedback/types.py`
- New: `TB4Result` (extends common pattern + `turns_used_total`, `max_turns_total`, `usage_breakdown`)
- New: `UsageBreakdown` (attempt, num_turns, input_tokens, output_tokens, cumulative_turns)

### `feedback/pipeline.py`
- New: `run_tb4(issue_id, repo_path, turns_override=None)`
- Turn budget: `remaining = max_turns - turns_used` → pass `remaining` as `max_turns` per spawn
- When remaining hits 0: skip retry, escalate

### `feedback/server.py`
- `escalate_to_human()`: accept optional `usage_breakdown` → append turn/token table to comment

### `justfile`
- `tb4` and `tb4-turns` commands

## Implementation Order

1. `runtime/types.py` — `AgentConfig.max_turns` + `AgentResult` usage fields
2. `runtime/server.py` — `_parse_usage_from_output`, `_build_command` flags, wire into `_run_agent`
3. `orchestration/types.py` + `config/agents.yaml` — `max_turns_default`
4. `feedback/types.py` — `TB4Result`, `UsageBreakdown`
5. `feedback/pipeline.py` — `run_tb4()` with turn budget
6. `feedback/server.py` — usage table in escalation comment
7. `justfile` — commands
8. Tests
9. Verify TB-1/2/3 still pass with `--output-format json`

## Exit Criteria

- [ ] `just tb4` with low turn limit → agent stopped → issue blocked with usage comment
- [ ] Turn + token counts in OTel spans (OpenObserve)
- [ ] TB-1/2/3 unbroken
