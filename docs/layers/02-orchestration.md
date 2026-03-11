# Layer 2: Orchestration

## Purpose
Takes a work item from intake and turns it into an isolated, configured agent run. Handles worktree creation, agent selection, context loading, and task decomposition. This layer decides WHO works on WHAT in WHERE.

## Primary Tools

### dmux (Dev Agent Multiplexer)
- Creates isolated git worktrees per agent run
- Automatic branching (`dev-loop/dl-1kz-fix-auth-bug`)
- One-key merge back to main
- Cleanup on completion

### Gastown (Multi-Agent Scale)
- Multi-agent workspace manager for Claude Code
- Persistent identity and git-backed work state
- Scales to 20-30 concurrent agents
- Evaluate alongside dmux — Gastown may be better for high-concurrency

### JAT (Agentic IDE)
- Visual dashboard: live sessions, task management, terminal
- Already uses beads + agent mail natively
- Epic Swarm parallel workflows
- Evaluate for visual orchestration layer

### Symphony Pattern (Reference Architecture)
OpenAI's Symphony does exactly what we're building: tickets → isolated runs → PRs with CI checks. Study the architecture, don't adopt (Codex-specific).

## Orchestration Flow

```
WorkItem from Intake
       │
       ▼
┌─────────────────┐
│ Task Analysis    │ ← Read issue, determine repo, scope, complexity
│                  │   If complex: decompose into sub-tasks
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Worktree Setup   │ ← dmux: git worktree add, create branch
│                  │   Copy .claude/ config from dev-loop template
│                  │   Inject project-specific CLAUDE.md
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Agent Config     │ ← Select agent persona based on labels
│                  │   Set cost ceiling from issue metadata
│                  │   Load context from memory layer
│                  │   Configure MCP server access
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Agent Spawn      │ ← Launch Claude Code in worktree
│                  │   Pass task prompt + context
│                  │   Start OTel span
└─────────────────┘
```

## Agent Personas

Configured via label → persona mapping in `config/agents.yaml`:

```yaml
personas:
  bug-fix:
    labels: [bug]
    claude_md_overlay: |
      Focus on minimal fix. Read the failing test first.
      Do not refactor surrounding code.
    cost_ceiling_default: 1.00
    retry_max: 2
    model: sonnet

  feature:
    labels: [feature]
    claude_md_overlay: |
      Implement the feature as described in the ticket.
      Write tests for new code. Follow existing patterns.
    cost_ceiling_default: 5.00
    retry_max: 1
    model: opus

  refactor:
    labels: [refactor]
    claude_md_overlay: |
      Preserve all existing behavior. Run tests before and after.
      Commit in small increments.
    cost_ceiling_default: 3.00
    retry_max: 1
    model: opus

  security-fix:
    labels: [security]
    claude_md_overlay: |
      Fix the security vulnerability without changing functionality.
      Reference the CWE/CVE in your commit message.
      Run security scan to verify the fix.
    cost_ceiling_default: 2.00
    retry_max: 3
    model: opus

  docs:
    labels: [docs]
    claude_md_overlay: |
      Update documentation only. Do not change code.
    cost_ceiling_default: 0.50
    retry_max: 1
    model: haiku
```

### MCP Server: `orchestrator`

```
src/devloop/orchestration/
├── __init__.py
├── analyzer.py        # Issue → task analysis (complexity, decomposition)
├── worktree.py        # dmux integration (create, cleanup, merge)
├── config_loader.py   # Load agent persona, inject CLAUDE.md
├── spawner.py         # Launch Claude Code in worktree
└── types.py           # WorkItem → AgentRun mapping
```

**Tools exposed:**
- `analyze_issue` — returns complexity estimate, suggested persona, decomposition
- `create_worktree` — sets up isolated env for agent
- `spawn_agent` — launches agent with full config
- `merge_worktree` — merge completed work back to main branch
- `cleanup_worktree` — remove worktree after merge or abandonment

### OTel Instrumentation
```
span: orchestration.setup
attributes:
  agent.persona: bug-fix
  agent.cost_ceiling: 1.00
  worktree.branch: dev-loop/dl-1kz-fix-auth-bug
  worktree.path: /tmp/dev-loop/worktrees/dl-1kz
  task.complexity: low
  task.decomposed: false
parent: intake.issue_pickup (trace_id from intake)
```

### Tracer Bullet Coverage
- **TB-1**: Single issue → single worktree → single agent. Simplest path.
- **TB-2**: Same setup, agent will fail. Orchestrator handles retry (re-spawn with error context).
- **TB-3**: Security persona selected based on label.
- **TB-4**: Cost ceiling passed from issue metadata to agent config.
- **TB-5**: Orchestrator creates worktrees in TWO repos (source + dependent).
- **TB-6**: Normal orchestration, AgentLens captures the spawn.

## Scheduling & Priority

When multiple issues are ready simultaneously:
```yaml
# config/scheduling.yaml
max_concurrent_agents: 3
priority_order: [P0, P1, P2, P3, P4]
budget_throttle:
  80_percent: P1_and_above_only
  95_percent: P0_only
  100_percent: pause_all
```

## Ambiguity Detection

Before assigning an agent, the orchestration layer checks for ambiguous issues:
- No specific file/function mentioned → flag
- Vague verbs only ("improve", "clean up", "make better") → flag
- No acceptance criteria AND no ATDD spec → flag

Flagged issues → deferred in beads with `needs-clarification` label, not assigned.

### Open Questions
- [ ] dmux vs Gastown — evaluate both for TB-1, pick winner by TB-3
- [ ] JAT integration — does it add value on top of dmux, or is it a replacement?
- [ ] Task decomposition: LLM-based or rule-based for MVP?
- [ ] How to handle issues that need multiple agents working in sequence (not parallel)?
- [ ] Worktree cleanup: immediate after merge, or keep for N hours for debugging?
- [ ] EnCompass (checkpoint/rewind) — can we rewind to last good state instead of full retry?
