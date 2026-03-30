# Layer 1: Intake

## Purpose
Single entry point for all work. Every task enters the system as a beads issue. No back-channel requests, no "just run this real quick." If it doesn't have an issue, it doesn't get an agent.

## Primary Tool: beads (br)

### Why beads
- Agent-first, CLI-native — designed for automated workflows
- Local SQLite + JSONL — no network dependency, no API key, no rate limits
- `br ready` returns exactly what we need: unblocked, non-deferred issues
- Dependencies with `br dep` — TB ordering is native
- Epics for tracer bullet grouping
- `br graph --all` for dependency visualization
- `br changelog` for auto-generated changelogs
- JSON output for programmatic consumption (`--json`)
- Issue state lives in the repo (`.beads/`) — version controlled

### What beads Tracks
- **Issues** — one issue per unit of work (bug fix, feature, refactor)
- **Labels** — map to agent config: `bug`, `feature`, `refactor`, `security`, `docs`
- **Priority** — P0-P4, maps to scheduling order
- **Dependencies** — `br dep add` models TB ordering and task prerequisites
- **Status flow**: open → in_progress → closed (or deferred, blocked)
- **DORA metrics** — computed from `br changelog` + git log → OpenObserve

### Optional: Beads-Kanban-UI
Visual Kanban board for beads (Next.js + Rust). Real-time file sync, epic tracking, GitOps (PR creation/merge from UI), multi-project dashboards. Deploy if stakeholder visibility is needed.

### MCP Server: `beads-intake`

```
src/devloop/intake/
├── __init__.py
├── beads_poller.py    # Poll br ready --json for unblocked issues
├── server.py          # MCP server exposing beads tools
└── types.py           # beads issue → internal WorkItem type
```

**Tools exposed:**
- `poll_ready_issues` — returns all issues with no blockers via `br ready --json`
- `get_issue_detail` — full issue with metadata, comments, dependencies
- `update_issue_status` — move issue to new status via `br update`
- `add_issue_comment` — post agent status updates via `br comments add`

### OTel Instrumentation
Every issue pickup emits a span:
```
span: intake.issue_pickup
attributes:
  issue.id: dl-1kz
  issue.repo: OOTestProject1
  issue.labels: [bug, backend]
  issue.priority: 0
```
This span becomes the root of the full trace for this work item.

### Tracer Bullet Coverage
- **TB-1**: Polling loop picks up a ready issue, starts the trace
- **TB-2**: Seed issue with bad data, same intake path
- **TB-3**: Seed issue that will produce a security vuln
- **TB-4**: Seed issue with intentionally large scope
- **TB-5**: Detects changed files via git diff, matches against dependency map watches, creates downstream cascade issues
- **TB-6**: Same intake, full session captured downstream

### Escape Hatches
- `just run-direct --repo OOTestProject1 --task "fix the typo in README"` — bypass beads entirely for quick one-offs
- Mock intake mode: `just tb1-mock` loads from YAML fixture, creates beads issue, runs pipeline

### Open Questions
- [ ] Beads-Kanban-UI maturity — is it production-ready? (Status: deferred, not blocking any active TB)
- [ ] Do we create sub-issues for decomposed tasks, or keep it flat? (Status: deferred, not blocking any active TB)
- [x] How do we handle issues that span multiple repos? (Resolved: TB-5 implements cross-repo cascade via dependency map watches and downstream issue creation. See `src/devloop/feedback/tb5_cascade.py`.)
