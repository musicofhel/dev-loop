# Layer 4: Quality Gates

## Purpose
Every agent output passes through a gauntlet of automated checks before it becomes a PR. Gates run sequentially — fail fast, fail cheap. Each gate produces structured output that the feedback loop can parse and act on.

**All gates are open source or built in-house. Zero paid tools.**

### TB-1 Minimal
TB-1 wires only 2-3 gates, not all 8:
- **Gate 0 (Sanity)** — compile + test (free, fast)
- **Gate 2 (Secrets)** — gitleaks scan (critical, must run before code leaves machine)
- **Gate 4 (Review)** — DeepEval LLM-as-judge (validates the concept)

Gates 0.5, 1, 2.5, 3, and 5 are wired progressively in TB-2 through TB-4. The full 8-gate pipeline described below is the target state, not the TB-1 state.

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
         │ pass
         ▼
┌─────────────────┐
│ Gate 0.5: Relevance │ ← Does the diff match the ticket?
└────────┬────────┘
         │ pass
         ▼
┌─────────────────┐
│ Gate 1: ATDD     │ ← Do acceptance tests pass (if spec exists)?
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
│ Gate 3: Security │ ← SAST/SCA scan (VibeForge + npm/pip audit)
└────────┬────────┘
         │ pass
         ▼
┌─────────────────┐
│ Gate 4: Review   │ ← LLM-as-judge code review (DeepEval)
└────────┬────────┘
         │ pass
         ▼
┌─────────────────┐
│ Gate 5: Cost     │ ← Did the agent stay within budget?
└────────┬────────┘
         │ pass
         ▼
       PR Created
```

**Why this order:**
- Gate 0 is free and fast — catches garbage before spending money on scans
- Gate 0.5 is cheap (one LLM call) — catches off-topic work early
- Gate 1 (ATDD) catches behavioral regressions early
- Gate 2 (secrets) is critical — must run before any code leaves the machine
- Gate 2.5 (dangerous ops) catches migrations, destructive commands
- Gate 3 (security) catches vulns before human reviewers see the PR
- Gate 4 (review) is the most expensive gate — runs last on clean code
- Gate 5 (cost) is a bookkeeping check, not a code check

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

### Gate 0.5: Task Relevance Check
Tool: **DeepEval** LLM-as-judge (via Claude API — already paid for)

LLM-as-judge compares the diff against the issue description. Scores how well the change addresses the stated requirements. Catches agents that did good work on the wrong thing.

### Gate 1: ATDD (Acceptance Test Driven Development)
Tool: `swingerman/atdd` Claude Code plugin (open source)

- Reads Given/When/Then specs from `specs/` directory
- Generates and runs acceptance tests against the agent's changes
- Two test streams: acceptance tests (behavioral) + unit tests (structural)
- **Only runs if spec exists** — no spec = gate skipped with warning

Output format:
```json
{
  "gate": "atdd",
  "status": "fail",
  "specs_run": 3,
  "specs_passed": 2,
  "specs_failed": 1,
  "failures": [
    {
      "spec": "specs/user-auth.feature",
      "scenario": "Given expired token When refresh Then new token issued",
      "error": "Expected 200, got 401",
      "file": "src/auth/refresh.ts",
      "line": 42
    }
  ]
}
```

### Gate 2: Secret Scanner
Tool: `gitleaks` (open source) + custom entropy detection

```
src/devloop/gates/
├── secrets.py         # Run gitleaks on diff + custom entropy check
└── allowlist.yaml     # Known false positives (test fixtures, etc.)
```

Patterns scanned:
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
Tools: **VibeForge Scanner** (2000+ rules, open source) + `npm audit` + `pip-audit` + **Claude Code Security** (when available)

Coverage:
- **SAST** — Static analysis for code vulnerabilities (SQL injection, XSS, path traversal, etc.)
- **SCA** — Dependency vulnerability scanning (`npm audit`, `pip-audit`)
- **Container** — Base image scanning (if Dockerfile in diff)

Output: structured findings with CWE classification, severity, file:line, and suggested fix.

### Gate 4: Code Review (LLM-as-Judge)
Tool: **DeepEval** (open source Python framework) via Claude API

Replaces CodeRabbit. Uses Claude API (already paid for) as LLM-as-judge with DeepEval metrics:
- Hallucination detection
- Code relevancy scoring
- Step efficiency analysis
- Custom criteria: race conditions, memory leaks, missing error handling

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

### Gate 5: Cost Check
Tool: Token proxy data (OTel metrics)

```json
{
  "gate": "cost",
  "status": "pass",
  "budget_usd": 2.00,
  "spent_usd": 0.87,
  "remaining_usd": 1.13,
  "calls": 12,
  "tokens_input": 45000,
  "tokens_output": 8500
}
```

### MCP Server: `quality-gates`

```
src/devloop/gates/
├── __init__.py
├── runner.py          # Sequential gate execution with fail-fast
├── sanity.py          # Gate 0: compile + test
├── relevance.py       # Gate 0.5: LLM-as-judge task relevance
├── atdd.py            # Gate 1: acceptance tests
├── secrets.py         # Gate 2: gitleaks + entropy
├── dangerous_ops.py   # Gate 2.5: migration/CI/auth detection
├── security.py        # Gate 3: VibeForge + npm/pip audit
├── review.py          # Gate 4: DeepEval LLM-as-judge
├── cost.py            # Gate 5: budget check
├── reporter.py        # Aggregate gate results into structured report
└── types.py
```

**Tools exposed:**
- `run_all_gates` — sequential execution, fail-fast
- `run_gate` — run a single gate (for debugging)
- `get_gate_results` — retrieve results for a specific run
- `skip_gate` — mark a gate as skipped (escape hatch)

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
  gates.total: 8
  gates.passed: 7
  gates.failed: 1
  gates.skipped: 0
  gates.first_failure: security
  gates.total_duration_ms: 12400
parent: runtime.output
```

### Gate Configuration Per Project

```yaml
# config/projects/prompt-bench.yaml
quality_gates:
  sanity:
    enabled: true
    commands: ["npm test", "npm run lint"]
  relevance:
    enabled: true
    model: claude-sonnet-4-6
  atdd:
    enabled: false  # no specs yet
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
    ceiling_usd: 2.00
```

### Fallback: semgrep
If VibeForge Scanner is unmaintained or rule quality is insufficient, **semgrep** (14k+ stars, Semgrep Inc backing, LGPL-2.1) is the drop-in replacement:
- 3,000+ community rules
- `semgrep scan --config auto` works out of the box
- Python + JSON output, trivial to wrap as MCP tool
- Actively maintained with weekly rule updates

Evaluate VibeForge vs semgrep during TB-3 scoring. Pick the winner by rule quality and false positive rate.

### Open Questions
- [ ] VibeForge Scanner vs semgrep — evaluate both during TB-3, pick winner by rule quality and FP rate
- [ ] Claude Code Security: when does research preview become GA?
- [ ] Should Gate 4 (review) use DeepEval or a simpler custom LLM-as-judge prompt?
- [ ] How to handle flaky gates? (gate passes sometimes, fails sometimes on same code)
- [ ] Should gate results be posted as PR comments or stored separately?
