# Tracer Bullets

Every feature is a vertical slice through all seven layers. No horizontal building. Each TB has a single `just` command that runs it end-to-end.

---

## TB-1: Issue-to-PR (The Golden Path)

**What it proves:** The entire loop works. An issue goes in, a PR comes out, every layer is touched.

### Vertical Slice

| Layer | What happens | Minimal implementation |
|-------|-------------|----------------------|
| Intake | beads issue with no blockers detected | Poll `br ready --json` via MCP server |
| Orchestration | Worktree created, agent assigned | `git worktree add` + persona selection from `config/agents.yaml` |
| Runtime | Agent reads issue, modifies code, commits | `claude --print` via stdin pipe in worktree with scoped CLAUDE.md |
| Quality Gates | Gate 0 sanity (tests), Gate 2 gitleaks (secrets), Gate 4 LLM review | Claude Code CLI `--json-schema` for review + gitleaks scan |
| Observability | Full trace visible in OpenObserve | OTel spans at each layer boundary |
| Feedback Loop | On gate failure, error fed back to agent for 1 retry | Simple retry with error context appended to prompt |

### Entry Criteria
- beads workspace initialized with a test issue
- OOTestProject1 repo cloned and configured as test target
- OpenObserve running (Docker)
- gitleaks installed (`shutil.which` or `~/.local/bin`)

### Exit Criteria
- Issue moves from open → closed without human intervention
- All gates pass (Gate 0 sanity + Gate 2 secrets + Gate 4 review)
- Full trace visible in OpenObserve (intake → orchestration → runtime → gate → outcome)
- On gate failure, agent retries once with error context

### Command
```bash
just tb1 <issue_id> <repo_path>    # full run
```

### Status: PASSING (2026-03-12)
- 2 successful e2e runs: bug fix (94s), feature add (245s with 1 retry)
- 85 unit tests passing

---

## TB-2: Failure-to-Retry (The Feedback Path)

**What it proves:** The loop actually loops. Failures don't dead-end — they feed back and self-correct.

### Vertical Slice

| Layer | What happens | Minimal implementation |
|-------|-------------|----------------------|
| Intake | Issue that will intentionally fail gates | Seed issue with pre-seeded tricky test in OOTestProject1 |
| Orchestration | Same as TB-1 | `git worktree add` + persona selection |
| Runtime | Agent attempts work, produces code that fails pre-seeded tests | `claude --print` via stdin pipe |
| Quality Gates | Gate 0 fails with structured pytest error | Gate 0 catches test failures, returns error context |
| Observability | Failure trace captured, linked across attempts | OTel spans with explicit links between retry attempts |
| Feedback Loop | Error parsed, context injected, agent retried | Retry with accumulated gate failures in prompt |

### Entry Criteria
- TB-1 passes (golden path works)
- OpenObserve running (Docker)

### Exit Criteria
- Agent fails → retries with error context → succeeds on retry
- Both attempts visible as linked OTel traces (shared trace_id, span links)
- Agent stdout and gate results captured per attempt (retry_history)
- After max retries, issue correctly moves to "blocked" (verified programmatically)

### Command
```bash
just tb2 <issue_id> <repo_path>          # organic mode (tricky issue)
just tb2-force <issue_id> <repo_path>    # forced first-attempt failure
```

### Status: PASSING (2026-03-12)
- 3 successful e2e runs:
  - Forced failure mode: 202s (forced Gate 0 fail → retry → pass)
  - Organic mode: 134s (pre-seeded test trap caught missing edge case → retry → pass)
  - Escalation path: 41s (max_retries=0 + forced fail → blocked_verified=true)
- OTel span linking works (attempt_span_ids captured per run)
- `_verify_blocked_status()` confirms beads status = "blocked" after escalation

---

## TB-3: Security-Gate-to-Fix (The Safety Path)

**What it proves:** Security scanning is in the loop, not bolted on. Agents can self-remediate security findings.

### Vertical Slice

| Layer | What happens | Minimal implementation |
|-------|-------------|----------------------|
| Intake | Issue that will produce code with a known vulnerability | Seed issue: "add user search with raw SQL" |
| Orchestration | `git worktree add` + security-fix persona | `git worktree add` + `config/agents.yaml` security-fix persona |
| Runtime | Agent writes vulnerable code (SQL injection) | `claude --print` via stdin pipe, follows ticket literally |
| Quality Gates | Gate 3 (bandit SAST) catches vulnerability | `bandit -r src/ -f json` → structured Finding with CWE-89 |
| Observability | Security finding logged with CWE classification | OTel span with `security.cwe_ids`, `security.finding.B608` attributes |
| Feedback Loop | Finding fed back to agent with CWE context | Agent re-generates code using parameterized queries |

### Entry Criteria
- TB-1 and TB-2 pass
- bandit installed (`pip install bandit` or `uv sync`)

### Exit Criteria
- Agent produces vulnerable code → Gate 3 catches it → agent fixes it → clean scan
- Security finding appears in OpenObserve with CWE classification
- Fix diff is minimal (agent uses parameterized queries, not a rewrite)
- After max retries, issue correctly moves to "blocked"

### Command
```bash
just tb3 <issue_id> <repo_path>          # seeded mode (deterministic)
just tb3-organic <issue_id> <repo_path>  # organic mode (relies on agent)
```

### Status: PASSING (2026-03-12)
- 1 successful e2e run: seeded mode (55s, Gate 3 caught CWE-89 → retry → agent fixed → clean scan)
- Pre-flight scan detects 2 SQL injection findings (B608 CWE-89 at lines 24, 43)
- Agent uses parameterized queries on retry, vulnerability_fixed=true
- 121 unit tests passing

---

## TB-4: Runaway-to-Stop (The Resource Control Path)

**What it proves:** Runaway agents get stopped, not just logged. Resource usage is visible and controllable.

On Claude Code Max (flat subscription), dollar cost is always 0. The real
runaway controls are **turn limits** and **timeouts**. TB-4 gates on turns.

### Vertical Slice

| Layer | What happens | Minimal implementation |
|-------|-------------|----------------------|
| Intake | Issue with intentionally vague/large scope | Seed issue: "refactor the entire codebase" |
| Orchestration | Agent assigned with turn limit from persona | `git worktree add` + `max_turns_default` in agents.yaml |
| Runtime | `--max-turns` + `--output-format json` on CLI | CLI stops at turn limit; parse `num_turns` + token usage from NDJSON |
| Quality Gates | Existing gates run if turns remain; skipped if exhausted | Same gates (0→2→3→4); turn check happens in pipeline before gate call |
| Observability | Turn + token counts visible in OTel spans | `runtime.num_turns`, `runtime.input_tokens`, `runtime.output_tokens` span attrs |
| Feedback Loop | On turns exhausted: issue marked blocked, human gets usage breakdown | beads comment with per-attempt turn/token table |

### Entry Criteria
- TB-1 passes
- Claude CLI supports `--max-turns` and `--output-format json`

### Exit Criteria
- Agent runs until turn limit hit → gracefully stopped (not crashed)
- Turn + token counts visible per-attempt in OTel (OpenObserve)
- beads issue gets a comment: "Turn limit reached: N/M turns across K attempts"
- Human can adjust `max_turns_default` in agents.yaml and re-run

### Command
```bash
just tb4 <issue_id> <repo_path>              # default turns from persona
just tb4-turns <issue_id> <repo_path> 5      # override turn limit
```

### Status: PASSING (2026-03-28)
- 2 e2e runs against OOTestProject1:
  - Default turns (bd-339): 11/10 turns, 2 attempts, 49.7s → escalated with usage breakdown
  - Forced exhaustion (bd-2dw, max_turns=2): 3/2 turns, 8.35s → escalated
- Per-attempt usage breakdown: turn counts, token counts, context_pct_at_exit
- Escalation comment posted to beads with usage table
- 38 unit tests passing

---

## TB-5: Cross-Repo Cascade (The Multi-Project Path)

**What it proves:** Changes in one repo trigger downstream work in dependent repos.

### Vertical Slice

| Layer | What happens | Minimal implementation |
|-------|-------------|----------------------|
| Intake | Source issue branch has files matching dependency watches | `br show` + `git diff main..dl/<id> --name-only` |
| Orchestration | Detect dependency via `config/dependencies.yaml`, create cascade issue | `_load_dependency_map()` + `br create --parent --labels cascade` |
| Runtime | TB-1 runs on target repo with cascade issue | Delegates to `run_tb1(target_issue_id, target_repo_path)` |
| Quality Gates | Target repo's gates run via TB-1 | Same gates (0→2→3→4) through `run_tb1()` |
| Observability | Cross-repo trace: TB-5 spans parent TB-1 spans via context propagation | `tb5.phase.cascade_tb1` → `tb1.run` (automatic OTel child spans) |
| Feedback Loop | Outcome reported back to source issue via `br comments add` | Success/failure/skip comment on source issue |

### Entry Criteria
- TB-1 passes on at least 2 repos independently
- Dependency map defined in `config/dependencies.yaml`

### Exit Criteria
- Change in source repo → dependency match → cascade issue in beads → TB-1 on target repo
- Both repos linked via OTel trace in OpenObserve (single trace_id)
- If target repo can't adapt, source issue gets a failure comment
- "No match" is a success (cascade_skipped=True), not a failure

### Command
```bash
just tb5 <source_issue_id> <source_repo_path> <target_repo_path>
# Example:
just tb5 dl-abc ~/OOTestProject1 ~/OOTestProject2
```

### Status: PASSING (2026-03-29)
TB-5 validates cross-repo cascade: OOTestProject1 (source) → OOTestProject2 (target).
When files matching `src/oo_test_project/db/**` change in OOTestProject1, a cascade
issue is created in OOTestProject2 and TB-1 runs there.

**Historical run (2026-03-28, prompt-bench era):**
- 1 e2e run: OOTestProject1 (bd-tab) → prompt-bench cascade (pre-purge)
  - Changed files detected: src/oo_test_project/db/users.py
  - Watch matched: src/oo_test_project/db/** (data-model dependency)
  - Cascade issue created in target beads workspace
  - TB-1 delegated on target repo
- 40 unit tests passing

---

## TB-6: Session Replay Debug (The Observability Path)

**What it proves:** When something goes wrong, you can replay and inspect every decision the agent made.

### Vertical Slice

| Layer | What happens | Minimal implementation |
|-------|-------------|----------------------|
| Intake | Any issue (reuse TB-2's failure case) | Poll + claim via beads |
| Orchestration | Normal flow | `git worktree add` + persona |
| Runtime | Agent NDJSON stdout saved as session file | `_save_session()` writes to `/tmp/dev-loop/sessions/` |
| Quality Gates | Gate failure triggers session analysis | `_parse_session_events()` + `_suggest_claude_md_fix()` |
| Observability | Session timeline linked to OTel trace via trace_id | Session metadata includes trace_id; `just tb6-replay` shows timeline |
| Feedback Loop | Rule-based CLAUDE.md fix suggestion from gate failure | Gate name → fix template (sanity→tests, secrets→env vars, etc.) |

### Entry Criteria
- TB-2 passes (we have a failure to debug)

### Exit Criteria
- Agent session saved to disk as NDJSON + metadata
- Session replayable via `just tb6-replay <session_id>`
- Timeline shows all NDJSON events with types
- Gate failure produces a suggested CLAUDE.md fix
- Session linked to OTel trace via trace_id in metadata

### Command
```bash
just tb6 <issue_id> <repo_path>    # run with session capture + forced gate fail
just tb6-replay <session_id>       # replay a saved session
```

### Status: PASSING (2026-03-28)
- 1 e2e run against OOTestProject1 (bd-3ss):
  - Session captured: 70 events (38 assistant, 29 user, 1 system, 1 rate_limit, 1 result)
  - Session file: 89KB NDJSON + metadata at ~/.local/share/dev-loop/sessions/
  - Forced gate fail → retry → gates passed (185.38s total)
  - Suggested CLAUDE.md fix generated for gate_0_sanity
  - Session replayable via `just tb6-replay bd-3ss-1774738946`
- 27 unit tests passing

---

## TB-7: LLMOps A/B Comparison (The Optimization Path)

**What it proves:** Layer 7 (LLMOps) works end-to-end. The GEPA-optimized prompt produces measurably different output than the CLI baseline, and both paths are observable.

### Vertical Slice

| Layer | What happens | Minimal implementation |
|-------|-------------|----------------------|
| Intake | Training data exists (`code_review.jsonl` >= 5 examples) | Check JSONL line count |
| Orchestration | Load optimized artifact from `artifacts/code_review_latest.json` | `load_program("code_review")` |
| Runtime | DSPy module executes on a diff via OpenRouter/Opus 4.6 | `dspy.LM()` + `CodeReviewModule.forward()` |
| Quality Gates | Run Gate 4 twice: DSPy path vs CLI baseline (`claude --print`) | Both paths produce `findings` JSON |
| Observability | OTel spans emitted for both paths (tb7.phase.dspy_path, tb7.phase.cli_path) | Standard OTel tracing |
| Feedback Loop | Comparison report: finding count, severity match, latency delta | `_compare_findings()` with Jaccard overlap |
| LLMOps | Artifact metadata + metric score reported in result | `_latest_artifact()` metadata |

### Entry Criteria
- `OPENROUTER_API_KEY` set (or `ANTHROPIC_API_KEY` depending on provider)
- `config/llmops.yaml` has `enabled: true`
- At least 5 training examples in `code_review.jsonl`
- `claude` CLI on PATH (for baseline comparison)

### Exit Criteria
- Both DSPy and CLI paths return valid JSON findings
- Comparison metrics computed: finding count delta, message overlap, severity agreement, latency ratio
- OTel spans visible for all 5 phases (validate, get_diff, dspy_path, cli_path, compare)
- Result includes artifact version and metric score

### Command
```bash
just tb7 ~/OOTestProject1    # uses repo diff or test fixture
```

### Status: PASSING (2026-03-28)

Validated with `run_tb7("/home/musicofhel/OOTestProject1")`:
- DSPy path: 4 findings (0.06s, cached artifact v20260328-141039, score 0.771)
- CLI path: 5 findings (24.27s)
- Overlap: 37.2%, severity agreement: 66.7%
- All 3 programs optimized: code_review (0.771), retry_prompt (0.733), persona_select (0.933)
- Langfuse bridge wired into all inference paths

---

## Implementation Order

```
TB-1 (golden path) ──► TB-2 (failure/retry) ──► TB-3 (security gate)
                                                       │
TB-4 (cost control) ◄────────────────────────────────┘
       │
       ▼
TB-5 (cross-repo) ──► TB-6 (session replay)

TB-7 (llmops A/B) ◄── Layer 7 enabled + training data exported
```

TB-1 is the spine. Everything else builds on it. Do NOT start TB-2 until TB-1 passes end-to-end on OOTestProject1. TB-7 is independent — it only requires Layer 7 (LLMOps) to be enabled with training data.
