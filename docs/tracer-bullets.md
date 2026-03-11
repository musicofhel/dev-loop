# Tracer Bullets

Every feature is a vertical slice through all six layers. No horizontal building. Each TB has a single `just` command that runs it end-to-end.

---

## TB-1: Issue-to-PR (The Golden Path)

**What it proves:** The entire loop works. An issue goes in, a PR comes out, every layer is touched.

### Vertical Slice

| Layer | What happens | Minimal implementation |
|-------|-------------|----------------------|
| Intake | beads issue with no blockers detected | Poll `br ready --json` via MCP server |
| Orchestration | Worktree created, agent assigned | `dmux` creates branch + worktree from issue metadata |
| Runtime | Agent reads issue, modifies code, commits | Claude Code in worktree with scoped CLAUDE.md |
| Quality Gates | DeepEval reviews the diff, gitleaks scans for secrets | LLM-as-judge review + secret scan (two gates, not all) |
| Observability | Full trace visible in OpenObserve | OTel spans at each layer boundary, one dashboard |
| Feedback Loop | On gate failure, error fed back to agent for 1 retry | Simple retry with error context appended to prompt |

### Entry Criteria
- beads workspace initialized with a test issue
- prompt-bench repo cloned and configured as test target
- OpenObserve running (Docker)
- dmux installed
- DeepEval + gitleaks configured

### Exit Criteria
- Issue moves from open → closed without human intervention
- PR exists on GitHub with DeepEval review comments
- Full trace visible in OpenObserve (intake → orchestration → runtime → gate → outcome)
- On intentional failure (bad issue), agent retries once then marks issue "blocked"

### Command
```bash
just tb1                    # full run
just tb1 --dry-run          # trace the path without executing
just tb1 --skip-review      # bypass DeepEval review gate
```

---

## TB-2: Failure-to-Retry (The Feedback Path)

**What it proves:** The loop actually loops. Failures don't dead-end — they feed back and self-correct.

### Vertical Slice

| Layer | What happens | Minimal implementation |
|-------|-------------|----------------------|
| Intake | Issue that will intentionally fail (e.g., references nonexistent file) | Seed issue in beads with known-bad description |
| Orchestration | Same as TB-1 | dmux worktree |
| Runtime | Agent attempts work, produces broken output | Agent runs, generates code that won't pass gate |
| Quality Gates | Gate fails with structured error | DeepEval or ATDD returns failure reason |
| Observability | Failure trace captured with full context | OTel span marked as ERROR, AgentLens session saved |
| Feedback Loop | Error parsed, context injected, agent retried | Retry orchestrator extracts failure → re-prompts agent |

### Entry Criteria
- TB-1 passes (golden path works)
- AgentLens configured for session capture

### Exit Criteria
- Agent fails → retries with error context → succeeds on retry
- Both attempts visible as linked traces in OpenObserve
- AgentLens shows side-by-side comparison of attempt 1 vs attempt 2
- After max retries, issue correctly moves to "blocked"

### Command
```bash
just tb2                    # run with seeded failure issue
just tb2 --max-retries 3    # override retry count
```

---

## TB-3: Security-Gate-to-Fix (The Safety Path)

**What it proves:** Security scanning is in the loop, not bolted on. Agents can self-remediate security findings.

### Vertical Slice

| Layer | What happens | Minimal implementation |
|-------|-------------|----------------------|
| Intake | Issue that will produce code with a known vulnerability | Seed issue: "add user input to SQL query" |
| Orchestration | Same worktree flow | dmux |
| Runtime | Agent writes vulnerable code (SQL injection, XSS, etc.) | Agent follows issue literally |
| Quality Gates | VibeForge catches the vulnerability | VibeForge Scanner on the diff, returns structured finding |
| Observability | Security finding logged with CWE/OWASP classification | OTel span with security.finding attributes |
| Feedback Loop | Finding fed back to agent with fix guidance | Agent re-generates code addressing the specific CWE |

### Entry Criteria
- TB-1 and TB-2 pass
- VibeForge Scanner configured with security rules

### Exit Criteria
- Agent produces vulnerable code → VibeForge catches it → agent fixes it → clean scan
- Security finding appears in OpenObserve with CWE classification
- Fix diff is minimal (agent didn't rewrite everything, just fixed the vuln)

### Command
```bash
just tb3                    # run with seeded vuln issue
just tb3 --vuln sqlinjection  # specific vulnerability type
```

---

## TB-4: Cost-Spike-to-Pause (The Budget Path)

**What it proves:** Token spend is visible and controllable. Runaway agents get killed, not just logged.

### Vertical Slice

| Layer | What happens | Minimal implementation |
|-------|-------------|----------------------|
| Intake | Issue with intentionally vague/large scope | Seed issue: "refactor the entire codebase" |
| Orchestration | Agent assigned with cost ceiling | dmux + cost limit passed to runtime config |
| Runtime | Token proxy tracks spend per API call | OTel-instrumented proxy between agent and LLM API |
| Quality Gates | Cost gate checks total spend before PR creation | Threshold comparison (spent vs budget) |
| Observability | Real-time cost dashboard in OpenObserve | Token counts + model pricing → dollar amounts |
| Feedback Loop | On budget exceeded: agent killed, issue marked, human alerted | Kill signal → beads comment with cost breakdown |

### Entry Criteria
- TB-1 passes
- Token proxy deployed (even if just logging, not blocking)

### Exit Criteria
- Agent runs until cost ceiling hit → gracefully stopped
- Cost breakdown visible in OpenObserve (per-call, per-model, cumulative)
- beads issue gets a comment: "Budget exceeded: $X.XX spent of $Y.YY limit"
- Human can approve budget increase and restart

### Command
```bash
just tb4                    # run with low budget ($0.50)
just tb4 --budget 5.00      # override budget
```

---

## TB-5: Cross-Repo Cascade (The Multi-Project Path)

**What it proves:** Changes in one repo trigger downstream work in dependent repos.

### Vertical Slice

| Layer | What happens | Minimal implementation |
|-------|-------------|----------------------|
| Intake | PR merged in repo A that affects repo B's API contract | Watch for merged PRs via GitHub webhook |
| Orchestration | Detect dependency, create issue in beads for repo B | Dependency map config + beads issue creation |
| Runtime | Agent in repo B makes compatible changes | Claude Code in repo B worktree |
| Quality Gates | Both repos' test suites pass | Run tests in both worktrees |
| Observability | Cross-repo trace links both PRs to same root cause | OTel trace spans both repos with shared trace_id |
| Feedback Loop | If repo B fails, repo A PR gets a warning comment | GitHub comment on source PR |

### Entry Criteria
- TB-1 passes on at least 2 repos independently
- Dependency map defined (even if manual YAML for now)

### Exit Criteria
- Change in repo A → auto-issue in beads for repo B → auto-PR in repo B
- Both PRs linked via trace in OpenObserve
- If repo B can't adapt, repo A PR gets a comment warning about breakage

### Command
```bash
just tb5                    # trigger with test change in prompt-bench
just tb5 --source prompt-bench --target omniswipe-backend
```

---

## TB-6: Session Replay Debug (The Observability Path)

**What it proves:** When something goes wrong, you can replay and inspect every decision the agent made.

### Vertical Slice

| Layer | What happens | Minimal implementation |
|-------|-------------|----------------------|
| Intake | Any issue (reuse TB-2's failure case) | Existing issue |
| Orchestration | Normal flow | dmux |
| Runtime | Agent session fully captured | AgentLens recording every tool call, context state, decision |
| Quality Gates | Gate failure triggers session save | On failure, session marked for review |
| Observability | Session browsable in AgentLens, linked to OTel trace | AgentLens UI shows timeline, tool calls, context window |
| Feedback Loop | Human reviews session → adjusts CLAUDE.md or harness config | Manual step: review → config change → re-run confirms fix |

### Entry Criteria
- TB-2 passes (we have a failure to debug)
- AgentLens capturing sessions

### Exit Criteria
- Failed session is fully replayable in AgentLens
- Can identify exactly which tool call / decision led to failure
- CLAUDE.md change based on session analysis prevents the same failure on re-run

### Command
```bash
just tb6                    # re-run TB-2 failure with full capture
just tb6 --session <id>     # replay a specific session
```

---

## Implementation Order

```
TB-1 (golden path) ──► TB-2 (failure/retry) ──► TB-3 (security gate)
                                                       │
TB-4 (cost control) ◄────────────────────────────────┘
       │
       ▼
TB-5 (cross-repo) ──► TB-6 (session replay)
```

TB-1 is the spine. Everything else builds on it. Do NOT start TB-2 until TB-1 passes end-to-end on prompt-bench.
