# Layer 2: Orchestration

## Purpose
Takes a work item from intake and turns it into an isolated, configured agent run. Handles worktree creation, agent selection, context loading, and task decomposition. This layer decides WHO works on WHAT in WHERE.

## Primary Tool: git worktree

- Creates isolated git worktrees per agent run via `git worktree add`
- Automatic branching (`dl/<issue_id>`)
- Cleanup on completion via `git worktree remove`

Evaluated dmux (score dropped to 0.65 -- TUI-only, cannot be called programmatically). `git worktree add` used directly instead.

## Orchestration Flow

```
WorkItem from Intake
       │
       ▼
┌─────────────────┐
│ Worktree Setup   │ ← git worktree add, create branch
│                  │   Write .dev-loop-metadata.json
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Agent Config     │ ← Select agent persona based on labels
│                  │   Set max_turns from persona config
│                  │   Build CLAUDE.md overlay with deny list
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Agent Spawn      │ ← Launch Claude Code in worktree (Layer 3)
│                  │   Pass task prompt + context
│                  │   Start OTel span
└─────────────────┘
```

## Agent Personas

Configured via label -> persona mapping in `config/agents.yaml`:

```yaml
personas:
  bug-fix:
    labels: [bug]
    claude_md_overlay: |
      Focus on minimal fix. Read the failing test first.
      Do not refactor surrounding code.
    max_turns_default: 10
    retry_max: 2
    model: sonnet

  feature:
    labels: [feature]
    claude_md_overlay: |
      Implement the feature as described in the ticket.
      Write tests for new code. Follow existing patterns.
    max_turns_default: 15
    retry_max: 1
    model: opus

  refactor:
    labels: [refactor]
    claude_md_overlay: |
      Preserve all existing behavior. Run tests before and after.
      Commit in small increments.
    max_turns_default: 15
    retry_max: 1
    model: opus

  security-fix:
    labels: [security]
    claude_md_overlay: |
      Fix the security vulnerability without changing functionality.
      Reference the CWE/CVE in your commit message.
      Run security scan to verify the fix.
    max_turns_default: 15
    retry_max: 3
    model: opus

  docs:
    labels: [docs]
    claude_md_overlay: |
      Update documentation only. Do not change code.
    max_turns_default: 10
    retry_max: 1
    model: haiku
```

Note: `cost_ceiling_default` exists in the config schema but is not enforced at runtime (on Max subscription, cost is $0). `max_turns_default` IS used and passed to `claude --max-turns`.

### MCP Server: `orchestration`

```
src/devloop/orchestration/
├── __init__.py
├── server.py          # MCP server with orchestration tools
└── types.py           # WorktreeInfo, PersonaConfig, ClaudeOverlay, etc.
```

**Tools exposed:**
- `setup_worktree` — creates isolated git worktree + branch for an issue
- `select_persona` — matches issue labels to agent persona from agents.yaml
- `build_claude_md_overlay` — generates CLAUDE.md overlay from persona + issue context + deny list
- `cleanup_worktree` — removes worktree and branch after completion

### OTel Instrumentation
```
span: orchestration.setup_worktree
attributes:
  orchestration.operation: setup_worktree
  issue.id: dl-1kz
  worktree.repo_path: /home/user/prompt-bench
  worktree.path: /tmp/dev-loop/worktrees/dl-1kz
  worktree.branch: dl/dl-1kz
parent: intake.issue_pickup (trace_id from intake)
```

### Tracer Bullet Coverage
- **TB-1**: Single issue -> single worktree -> single agent. Simplest path.
- **TB-2**: Same setup, agent will fail. Orchestrator handles retry (re-spawn with error context).
- **TB-3**: Security persona selected based on label.
- **TB-4**: Max turns passed from issue metadata to agent config.
- **TB-5**: Orchestrator creates worktrees in TWO repos (source + dependent).
- **TB-6**: Normal orchestration, NDJSON session captured downstream.

### Open Questions
- [ ] Task decomposition: LLM-based or rule-based for MVP? (Status: deferred, not blocking any active TB)
- [ ] How to handle issues that need multiple agents working in sequence (not parallel)? (Status: deferred, not blocking any active TB)
- [x] Worktree cleanup: immediate after merge, or keep for N hours for debugging? (Resolved: immediate cleanup after each TB run via `cleanup_worktree()`. See `src/devloop/orchestration/server.py`.)
