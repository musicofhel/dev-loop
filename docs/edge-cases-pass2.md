# Edge Cases — Pass 2 (Fresh Eyes)

Problems that exist in the *structure* of the system, not just individual failure modes. Pass 1 caught race conditions and crashes. This pass catches design gaps.

---

## The Large Repo Problem

### 26. CLAUDE.md Doesn't Scale
**Problem:** A single CLAUDE.md works for a 5k LOC repo. For a 50k+ LOC repo (like omniswipe-backend), the agent can't hold the whole codebase in context. Our harness gives the agent an issue and says "go" — but the agent doesn't know WHERE in the codebase to look.

Research from the graph backs this up: the "Codified Context" paper argues for three-tier memory (hot constitution, domain-expert agents, cold knowledge base). Single-file instructions hit a wall.

**Fix:** Tiered context loading in the runtime layer:
- **Tier 1 (hot):** CLAUDE.md + issue description + agent persona overlay. Always loaded.
- **Tier 2 (warm):** File map / codebase summary generated once per repo. Agent scans this to narrow scope. Regenerated on major changes.
- **Tier 3 (cold):** Full file contents. Agent pulls specific files on demand via tool calls.

The orchestration layer should generate a "scope hint" from the issue — e.g., issue mentions "auth" → pre-load `src/auth/**` file list into context.

**Also:** Headroom (0.88 in graph) is a transparent proxy that compresses LLM context by 47-92% (strips boilerplate from tool outputs, DB queries, file reads). This belongs in the runtime layer between agent and LLM API, alongside the token proxy.

**TB-1 action:** Start simple (prompt-bench is small). Add scope hints for TB-5 when targeting omniswipe-backend.

### 27. Context Compression Missing from Stack
**Problem:** We have a token *proxy* (metering) but no token *optimizer*. Every file read, every tool output goes to the LLM at full size. This wastes money and eats context window.

**Fix:** Add Headroom to the runtime layer stack:
```
Agent ──► Headroom (compress) ──► Token Proxy (meter) ──► Anthropic API
```

Headroom compresses tool outputs by removing boilerplate, truncating irrelevant sections, and deduplicating repeated content. The agent sees the same information with fewer tokens.

**Scoring rubric:** Evaluate Headroom before TB-4 (cost control).

---

## Feedback Quality

### 28. Backpressure Is Wasted
**Problem:** Our quality gates run AFTER the agent finishes. But the richest feedback sources — compiler errors, type checker, test failures — are available DURING agent work. Right now the agent might code for 3 minutes, produce a diff with 10 type errors, then fail at Gate 0. That's 3 minutes and $0.50 wasted.

The "Don't waste your backpressure" essay (0.72 in graph) argues: type systems, build tools, and linters ARE feedback loops. Wire them into the agent's working process, not just the exit check.

**Fix:** Agent CLAUDE.md overlay should mandate:
```
After every file edit:
1. Run `tsc --noEmit` on the changed file
2. If errors: fix before moving to next file
3. After all edits: run `npm test` for affected tests
4. Only commit when the local feedback loop passes
```

This is "in-process backpressure" vs "post-process gates." Both are needed, but in-process catches problems 10x cheaper.

**TB-1 action:** Add in-process type-check/test rules to the CLAUDE.md overlay for TypeScript repos. Gate 0 becomes a safety net, not the primary check.

### 29. Flaky Tests Cause False Gate Failures
**Problem:** Agent's code is correct. But a pre-existing flaky test (unrelated to the agent's changes) fails intermittently. Gate 0 fails. Agent "fixes" the flaky test — now the test is actually broken.

**Fix:** Gate 0 should run a **differential test**:
1. Run tests on the UNMODIFIED main branch (baseline)
2. Run tests on the agent's branch
3. Only fail on tests that NEWLY fail (pass in baseline, fail in agent's branch)
4. Pre-existing failures are logged as warnings but don't block

This requires checking out main briefly before the agent's branch, or caching recent main test results.

**TB-1 action:** For prompt-bench (small, presumably stable tests), this is low risk. Must be in place before TB-5 targets omniswipe-backend (which has more complex test suites).

---

## Partial and Ambiguous Outcomes

### 30. Partial Success
**Problem:** Issue says "fix the auth bug and update the related tests." Agent fixes the bug but doesn't update the tests. Is this a success or failure? Current design is all-or-nothing — gates pass or fail.

**Fix:** Two approaches:
- **Decomposition (preferred):** Orchestration layer breaks the issue into sub-tasks. Each sub-task is independently gateable. Partial credit = some sub-tasks pass, some don't.
- **Acceptance criteria parsing:** ATDD specs define what "done" means. If the issue has Given/When/Then specs, partial success = some specs pass.

What the system should NOT do: merge a partial fix and hope someone finishes it. Either the issue is fully done or it stays open.

**TB-1 action:** TB-1 uses simple issues (one clear change). Decomposition becomes relevant when issues get complex.

### 31. Agent Says "Already Fixed" or "Not Reproducible"
**Problem:** Agent reads the codebase, determines the bug is already fixed (or can't reproduce it), and reports completion with zero changes. Is this a legitimate outcome or a hallucination?

**Fix:** This is a valid outcome — but it needs verification:
1. "Done-but-empty" check (edge case #15) catches the zero-diff
2. Route to human with agent's explanation: "I investigated and found the bug was fixed in commit abc123"
3. Human confirms or reopens with more context

Never auto-close an issue based on agent's say-so. Move to "blocked" with "needs-verification" label.

**TB-1 action:** Add "needs-verification" label for zero-diff completions.

### 32. Issue Is Ambiguous or Underspecified
**Problem:** Issue says "improve performance." Agent doesn't know what to optimize, guesses wrong, spends budget on irrelevant changes.

**Fix:** Orchestration analysis step should flag ambiguous issues:
- No specific file/function mentioned → flag
- Vague verbs ("improve", "clean up", "make better") → flag
- No acceptance criteria and no ATDD spec → flag

Flagged issues get moved to "deferred" with "needs-clarification" label instead of being assigned to an agent.

**TB-1 action:** Basic ambiguity detection (word list check). More sophisticated NLP later.

---

## Dangerous Operations

### 33. Database Migrations
**Problem:** Agent working on omniswipe-backend could create a Prisma migration with `DROP COLUMN` or `ALTER TABLE`. Migrations are IRREVERSIBLE in production. An automated agent creating destructive migrations is a nightmare.

**Fix:** Migration-specific quality gate:
- Detect any new migration files in the diff
- Parse migration SQL for destructive operations: `DROP`, `DELETE`, `TRUNCATE`, `ALTER ... DROP`
- Destructive migrations → ALWAYS escalate to human. Never auto-merge.
- Additive migrations (CREATE TABLE, ADD COLUMN) → normal gate flow

```yaml
# config/dangerous-operations.yaml
migrations:
  patterns:
    - "prisma/migrations/**"
    - "alembic/versions/**"
    - "db/migrate/**"
  block_on:
    - DROP
    - DELETE
    - TRUNCATE
    - RENAME
  allow:
    - CREATE
    - ADD
    - INSERT
```

**TB-1 action:** Not relevant for prompt-bench. MUST be in place before any TB targets omniswipe-backend.

### 34. Lock File Inconsistency
**Problem:** Agent updates `package.json` but doesn't run `npm install`, leaving `package-lock.json` out of sync. Or agent runs `npm install` which pulls in unrelated dependency updates.

**Fix:** Gate 0 sanity check:
- If `package.json` changed: verify `package-lock.json` also changed
- If lock file changed: verify the diff only contains changes related to the packages the agent intentionally modified (not a full lock file refresh)
- CLAUDE.md rule: "When modifying package.json, run `npm install` immediately. Do not run `npm update` or `npm audit fix` without explicit approval."

**TB-1 action:** Add lock file consistency check to Gate 0.

### 35. Agent Hallucination (Phantom Code)
**Problem:** Vercel incident from graph — Claude hallucinated a GitHub repo ID and deployed unknown code. Our agents could hallucinate: function names that don't exist, API endpoints that aren't real, import paths that resolve to nothing.

Gate 0 (compilation) catches import/reference errors. But it doesn't catch SEMANTIC hallucination — calling a real function with wrong assumptions about what it does.

**Fix:**
- Gate 0 catches structural hallucination (compilation errors)
- ATDD (Gate 1) catches behavioral hallucination (function exists but does wrong thing)
- DeepEval review (Gate 4) may catch logical hallucination ("this function doesn't do what the comment says")
- The real defense: agent CLAUDE.md rule — "Always read a function's implementation before calling it. Never assume what a function does from its name alone."

**TB-1 action:** Add "read before call" rule to CLAUDE.md overlay. Monitor for hallucination in AgentLens traces.

---

## Operational Gaps

### 36. Priority Queuing
**Problem:** 10 issues are "open" simultaneously. Which gets picked first? Current intake polls `br ready` and grabs whatever it returns. No priority logic.

**Fix:**
- Respect beads priority field (P0 > P1 > P2 > P3 > P4)
- Within same priority: FIFO (oldest first)
- Concurrent agent limit: configurable max (default 3). Queue the rest.
- Cost-aware scheduling: if weekly budget is 80% spent, only process P0/P1 issues

```yaml
# config/scheduling.yaml
max_concurrent_agents: 3
priority_order: [P0, P1, P2, P3, P4]
budget_throttle:
  80_percent: P1_and_above_only
  95_percent: P0_only
  100_percent: pause_all
```

**TB-1 action:** Single issue, no queuing needed. Must be in place before running multiple issues.

### 37. Model Selection Strategy
**Problem:** Not all tasks need Opus. A typo fix doesn't need the most expensive model. A complex refactor does. Currently no model routing.

**Fix:** Agent persona config includes model selection:
```yaml
personas:
  bug-fix:
    model: sonnet  # cheaper, fast enough for targeted fixes
  feature:
    model: opus    # needs deep understanding
  refactor:
    model: opus    # high-stakes, needs careful reasoning
  docs:
    model: haiku   # cheapest, docs are low-risk
```

Override per issue via label: `model:opus`

**TB-4 action:** This directly impacts cost control. Evaluate during TB-4.

### 38. Project Onboarding
**Problem:** How does a new repo "join" the harness? No checklist, no automation. Currently a person reads test-repos.md and manually configures everything.

**Fix:** `just onboard <repo-path>` command that:
1. Detects language/framework (package.json → Node, pyproject.toml → Python, Cargo.toml → Rust)
2. Generates `config/projects/<repo-name>.yaml` with sensible defaults
3. Creates beads issues for initial test runs
4. Copies CLAUDE.md template to repo
5. Runs Gate 0 sanity check to verify the repo builds/tests cleanly
6. Seeds one test issue
7. Runs TB-1 dry run

**TB-1 action:** Manual onboarding is fine. Automate when adding the third repo.

### 39. Harness Self-Testing
**Problem:** dev-loop is a repo with config files, YAML, templates, and Python glue code. Who tests the harness itself?

**Fix:**
- Config validation: `just validate-config` — YAML schema check on all config files
- Dry-run mode: every `just tb<N> --dry-run` traces the path without executing
- Integration test: `just self-test` — runs TB-1 with mock intake against a test fixture repo
- CLAUDE.md changes in dev-loop should go through the same PR review process

**TB-1 action:** Add `just validate-config` and `--dry-run` mode.

### 40. Reproducibility
**Problem:** LLMs are non-deterministic. Same issue, same code, different result. When debugging: "it worked yesterday, fails today" is meaningless without the full prompt.

**Fix:**
- AgentLens captures the full prompt for every LLM call (already planned)
- Add `temperature: 0` to agent config for maximum reproducibility
- Seed issue fixtures (already created) allow re-running the same scenario
- OTel traces + AgentLens sessions = full replay of every decision

What we CAN'T guarantee: exact same output. What we CAN guarantee: full visibility into why the output differed.

**TB-6 action:** Session replay is the answer here.

---

## Licensing / Legal

### 41. Tool Licensing Compatibility — UPDATED for OSS Stack
**Problem:** We're composing tools with different licenses. Need to verify nothing conflicts.

| Tool | License | Concern |
|------|---------|---------|
| OpenObserve | AGPL-3.0 | AGPL requires source disclosure if you modify and serve it. We're using it as-is (Docker), so no issue unless we fork. |
| OpenFang | MIT | No concerns |
| dmux | MIT | No concerns |
| DeepEval | Apache-2.0 | No concerns |
| VibeForge Scanner | TBD | Verify license before committing. Fallback: semgrep (LGPL-2.1) |
| ATDD | MIT | No concerns |
| AgentLens | MIT | No concerns |
| Headroom | Apache-2.0 | No concerns |
| gitleaks | MIT | No concerns |
| beads (br) | TBD | Verify license |

**TB-1 action:** All tools are open source. Verify VibeForge and beads licenses before committing.

---

## Updated Priority Matrix (Pass 2)

| Edge Case | Severity | TB Affected | When to Fix |
|-----------|----------|-------------|-------------|
| #33 Database migrations | CRITICAL | TB-5 (backend) | Before any TB targets a DB repo |
| #28 In-process backpressure | HIGH | TB-1 | During TB-1 (CLAUDE.md overlay) |
| #26 Large repo context | HIGH | TB-5 | Before TB-5 |
| #35 Agent hallucination | HIGH | All | During TB-1 (CLAUDE.md rule) |
| #36 Priority queuing | HIGH | Multi-issue | Before running concurrent issues |
| #29 Flaky tests | MEDIUM | TB-5 | Before TB-5 targets complex repos |
| #34 Lock file consistency | MEDIUM | TB-1 | During TB-1 (Gate 0) |
| #32 Ambiguous issues | MEDIUM | TB-1 | During TB-1 (intake analysis) |
| #30 Partial success | MEDIUM | TB-2+ | When issues get complex |
| #37 Model selection | MEDIUM | TB-4 | During TB-4 (cost control) |
| #27 Context compression | LOW | TB-4 | Evaluate during TB-4 |
| #31 "Already fixed" | LOW | TB-1 | During TB-1 |
| #38 Project onboarding | LOW | TB-5 | When adding third repo |
| #39 Harness self-testing | LOW | All | After TB-1 |
| #40 Reproducibility | LOW | TB-6 | During TB-6 |
| #41 Licensing | LOW | All | Before going public |
