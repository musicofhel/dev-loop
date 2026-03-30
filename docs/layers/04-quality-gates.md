# Layer 4: Quality Gates

## Purpose
Every agent output passes through a gauntlet of automated checks before it becomes a PR. Gates run sequentially -- fail fast, fail cheap. Each gate produces structured output that the feedback loop can parse and act on.

**All gates are open source or built in-house. Zero paid tools.**

### TB-1 Coverage
TB-1 wires Gate 0 (sanity), Gate 0.5 (relevance), Gate 2 (secrets), Gate 2.5 (dangerous ops), Gate 3 (security/bandit), and Gate 4 (review). Gate 5 (cost/usage) is implemented but called separately, not in the fail-fast chain.

## In-Process Backpressure (Pre-Gate)

Gates run AFTER the agent finishes. But the cheapest feedback happens DURING agent work. The agent's CLAUDE.md overlay mandates in-process checks:

```
For TypeScript repos:
  After every file edit → tsc --noEmit (type check)
  After all edits → npm test (affected tests only)
  Only commit when local checks pass

For Python repos:
  After every file edit → mypy / pyright (if configured)
  After all edits → pytest (affected tests only)
  Only commit when local checks pass
```

This catches 80% of problems at 10% of the cost. Gates become a safety net, not the primary check.

## Gate Execution Order

```
Agent Output (diff + commits)
       │
       ▼
┌─────────────────┐
│ Gate 0: Sanity   │ ← Does the code compile/parse? Are tests passing?
└────────┬────────┘
         │ pass ──────────────────────────┐
         │ FAIL + differential enabled    │
         ▼                                │
┌─────────────────────┐                   │
│ Gate 0.1: Differential │ ← Baseline vs HEAD test comparison (opt-in)
└────────┬────────────┘                   │
         │ pass (fewer failures than      │
         │       baseline = progress)     │
         ▼◄───────────────────────────────┘
┌─────────────────┐
│ Gate 0.5: Relevance │ ← Does the diff match the ticket?
└────────┬────────┘
         │ pass
         ▼
┌─────────────────┐
│ Gate 2: Secrets  │ ← Any leaked credentials in the diff?
└────────┬────────┘
         │ pass
         ▼
┌─────────────────┐
│ Gate 2.5: Danger │ ← Migrations, CI config, auth changes?
└────────┬────────┘
         │ pass
         ▼
┌─────────────────┐
│ Gate 3: Security │ ← SAST scan (bandit for Python)
└────────┬────────┘
         │ pass
         ▼
┌─────────────────┐
│ Gate 4: Review   │ ← LLM-as-judge code review (Claude CLI)
└────────┬────────┘
         │ pass
         ▼
       PR Created

Gate 0.1 (Differential) only runs when Gate 0 fails AND differential is enabled.
Gate 5 (Cost/Usage) is called separately after PR creation.
Gate 1 (ATDD) is not yet implemented.
```

**Why this order:**
- Gate 0 is free and fast -- catches garbage before spending money on scans
- Gate 0.5 is cheap (keyword overlap) -- catches off-topic work early
- Gate 2 (secrets) is critical -- must run before any code leaves the machine
- Gate 2.5 (dangerous ops) catches migrations, destructive commands
- Gate 3 (security) catches vulns before human reviewers see the PR
- Gate 4 (review) is the most expensive gate -- runs last on clean code
- Gate 5 (cost) is a bookkeeping check, not a code check -- called separately

## Gate Details

### Gate 0: Sanity Check
Tools: Standard linters (ruff, eslint, tsc, pytest, npm test)

```bash
# Per-language sanity
npm run build          # TypeScript/JS
npm test               # Unit tests
ruff check src/        # Python
python -m py_compile   # Python
cargo check            # Rust
```
- **Pass**: exit code 0
- **Fail**: structured error with file:line:message

### Gate 0.1: Differential Test Check
Tool: Custom baseline-vs-HEAD test comparison (built in-house)

Opt-in gate that only runs when Gate 0 fails. Compares test results between the merge-base (baseline) and the agent's HEAD to determine whether the agent's changes made things better or worse.

- Enabled per-project via `quality_gates.differential.enabled: true` in project config
- Finds the merge-base commit, runs tests on both baseline and HEAD
- If HEAD has fewer failures than baseline, the agent is making progress -- gate passes
- If HEAD introduced new failures, gate fails with a diff of which tests regressed
- Skips gracefully if project type is unknown or test output is unparsable

This prevents the feedback loop from retrying an agent that is actually fixing pre-existing test failures. Without it, Gate 0 would reject any commit to a repo that already had broken tests.

Implementation: `gates/server.py:run_gate_01_differential`

### Gate 0.5: Task Relevance Check
Tool: Keyword overlap analysis between issue description and diff content.

Compares words in the issue title/description against words in the diff. Scores how well the change relates to the stated requirements. Catches agents that did good work on the wrong thing.

### Gate 1: ATDD (Acceptance Test Driven Development)
**Not yet implemented.** Planned: reads Given/When/Then specs from `specs/` directory, generates and runs acceptance tests against the agent's changes. Only runs if spec exists.

### Gate 2: Secret Scanner
Tool: `gitleaks` (open source)

Scans the diff for leaked credentials:
- API keys (AWS, GCP, Azure, Anthropic, OpenAI, etc.)
- Private keys (RSA, EC, Ed25519)
- Passwords in config files
- Connection strings with embedded credentials
- JWT tokens
- `.env` files in diff

### Gate 2.5: Dangerous Operations
Tool: Custom diff scanner (built in-house)

Scans the diff for operations that require human approval regardless of quality:
- **Database migrations** with destructive SQL (DROP, DELETE, TRUNCATE, RENAME)
- **Lock file** inconsistency (package.json changed but lock file doesn't match)
- **CI/CD config** changes (.github/workflows, Dockerfile, deploy scripts)
- **Permission/auth** changes (RBAC rules, OAuth config, API key rotation)

If detected: gate pauses and escalates to human. Never auto-passes.

### Gate 3: Security Scan
Tool: **bandit** (Python SAST, open source)

Runs `bandit -r` on Python files in the diff. Reports findings with CWE classification, severity, file:line, and suggested fix.

Note: bandit is Python-only. Non-Python projects skip this gate gracefully (gate passes with a "skipped: no Python files" message). SCA scanning (`npm audit`, `pip-audit`) is detected by project type but not yet wired into the gate.

### Gate 4: Code Review (LLM-as-Judge)
Tool: **Claude CLI** with `--print` and structured JSON schema output

Uses `claude --print --output-format json` with a review prompt and JSON schema for structured output. Evaluates the code change against review criteria from `config/review-gate.yaml`:
- Race conditions
- Memory leaks
- Logic errors
- Missing error handling at boundaries
- Performance antipatterns

Critical findings fail the gate. Warnings and suggestions are included in the output but do not block.

Configuration:
```yaml
# config/review-gate.yaml
review:
  model: claude-sonnet-4-6  # cheaper than opus for review
  criteria:
    - race_conditions
    - memory_leaks
    - logic_errors
    - missing_error_handling_at_boundaries
    - performance_antipatterns
  severity_levels:
    critical: fail    # gate FAILS
    warning: pass     # attached to PR as warnings
    suggestion: pass  # attached to PR as suggestions
```

### Gate 5: Usage Check
Tool: Parsed NDJSON usage data (turns, tokens)

Checks that the agent run stayed within reasonable resource bounds. On Claude Code Max, cost is always $0; this gates on turn count and token usage against configurable thresholds.

Implemented but not in the fail-fast chain -- called separately after the main gate sequence.

### MCP Server: `quality-gates`

```
src/devloop/gates/
├── __init__.py
├── server.py          # All gate implementations + MCP server
└── types.py           # Finding, GateResult, GateSuiteResult
```

**Tools exposed:**
- `run_gate_0_sanity` — compile + test
- `run_gate_05_relevance` — keyword overlap check between issue and diff
- `run_gate_2_secrets` — gitleaks scan
- `run_gate_25_dangerous_ops` — migration/CI/auth detection
- `run_gate_3_security` — bandit SAST scan
- `run_gate_4_review` — Claude CLI LLM-as-judge code review
- `run_gate_5_cost` — turn/token usage check
- `run_all_gates` — sequential execution, fail-fast (runs gates 0 → 0.5 → 2 → 2.5 → 3 → 4)

### OTel Instrumentation
Each gate emits its own span:
```
span: quality_gates.gate_2_secrets
attributes:
  gate.name: secrets
  gate.order: 2
  gate.status: pass
  gate.duration_ms: 340
  gate.findings_count: 0
parent: quality_gates.run_all
```

Aggregate span:
```
span: quality_gates.run_all
attributes:
  gates.total: 6
  gates.passed: 6
  gates.failed: 0
  gates.skipped: 0
  gates.first_failure: null
  gates.total_duration_ms: 12400
parent: runtime.output
```

### Gate Configuration Per Project

```yaml
# config/projects/OOTestProject1.yaml
quality_gates:
  sanity:
    enabled: true
    commands: ["npm test", "npm run lint"]
  relevance:
    enabled: true
  secrets:
    enabled: true
    allowlist: ["tests/fixtures/fake-key.pem"]
  dangerous_ops:
    enabled: true
  security:
    enabled: true
    severity_threshold: medium  # low findings don't block
  review:
    enabled: true
    block_on: critical  # only critical findings block
  cost:
    enabled: true
    max_turns: 25
    max_input_tokens: 200000
```

### Open Questions
- [x] Claude Code Security: when does research preview become GA? (Resolved: no longer applicable — dev-loop uses `--dangerously-skip-permissions` for unattended runs; sandbox mode is not part of the architecture.)
- [ ] How to handle flaky gates? (gate passes sometimes, fails sometimes on same code) (Status: deferred, not blocking any active TB)
- [ ] Should gate results be posted as PR comments or stored separately? (Status: deferred, not blocking any active TB)
