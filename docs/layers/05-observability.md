# Layer 5: Observability

## Purpose
See everything. Every issue, every agent run, every tool call, every dollar spent, every gate result — visible in one place. Not just logging — tracing (causal chains), metrics (trends), and replay (debugging).

**All tools are open source. Zero paid services.**

## The Three Pillars + One

| Pillar | Tool | What it answers |
|--------|------|----------------|
| **Traces** | OpenTelemetry → OpenObserve | "What happened in this run, step by step?" |
| **Metrics** | OpenTelemetry → OpenObserve | "Are we getting faster? Cheaper? More reliable?" |
| **Logs** | OpenObserve | "What did the agent print/say/error?" |
| **Replay** | AgentLens | "Show me exactly what the agent saw and did." |

## OpenTelemetry (Instrumentation Standard)

Every layer in dev-loop emits OTel spans. This is non-negotiable — if a layer doesn't emit spans, it's invisible.

### Span Hierarchy
```
trace: T-abc123 (one per issue)
├── span: intake.issue_pickup
│   └── attributes: issue.id, issue.repo, issue.labels
├── span: orchestration.setup
│   └── attributes: agent.persona, worktree.branch, task.complexity
├── span: runtime.execution
│   ├── span: runtime.tool_call (N times)
│   │   └── attributes: tool.name, tool.duration_ms
│   ├── span: runtime.llm_call (N times)
│   │   └── attributes: model, tokens_in, tokens_out, cost_usd
│   └── attributes: total_tool_calls, total_tokens, total_cost
├── span: quality_gates.run_all
│   ├── span: quality_gates.gate_0_sanity
│   ├── span: quality_gates.gate_05_relevance
│   ├── span: quality_gates.gate_1_atdd
│   ├── span: quality_gates.gate_2_secrets
│   ├── span: quality_gates.gate_25_dangerous_ops
│   ├── span: quality_gates.gate_3_security
│   ├── span: quality_gates.gate_4_review
│   └── span: quality_gates.gate_5_cost
├── span: feedback.outcome_routing
│   └── attributes: outcome (pr_created | retry | blocked)
└── span: feedback.retry (if applicable)
    └── (entire runtime + gates subtree repeated)
```

### Semantic Conventions
Custom attribute namespace: `devloop.*`

```
devloop.issue.id           # beads issue ID (dl-1kz)
devloop.issue.repo         # Target repository
devloop.agent.id           # Unique agent run ID
devloop.agent.persona      # bug-fix, feature, refactor, security-fix
devloop.tracer_bullet      # tb1, tb2, etc.
devloop.cost.budget_usd    # Budget for this run
devloop.cost.spent_usd     # Actual spend
devloop.gate.name          # Gate name
devloop.gate.status        # pass, fail, skip
devloop.retry.attempt      # 0, 1, 2
devloop.retry.reason       # Why the previous attempt failed
```

## OpenObserve (Storage + Dashboards + Alerts)

Replaces Datadog/Splunk/Elasticsearch AND OneUptime. Single binary, 140x cheaper storage.

### Deployment
```bash
docker run -d \
  --name openobserve \
  -p 5080:5080 \
  -v openobserve-data:/data \
  -e ZO_ROOT_USER_EMAIL=admin@dev-loop.local \
  -e ZO_ROOT_USER_PASSWORD=devloop123 \
  public.ecr.aws/zinclabs/openobserve:latest
```

### Dashboards

**Dashboard 1: Loop Health**
- Issues processed (today/week/month)
- Success rate (PRs created / issues attempted)
- Average lead time (issue pickup → PR created)
- Average cost per issue
- Gate failure breakdown (which gates fail most)

**Dashboard 2: Agent Performance**
- Token usage per run (trend)
- Tool calls per run (trend)
- Cost per run by persona type
- Retry rate by persona type
- Time-to-completion distribution

**Dashboard 3: Quality Gate Insights**
- Gate pass/fail rates over time
- Most common failure reasons
- Security findings by CWE category
- LLM-as-judge critical findings trend
- Secret scanner catches (should be zero in steady state)

**Dashboard 4: DORA Metrics**
- Deployment frequency: PRs merged per week per repo
- Lead time: issue created → PR merged
- Change failure rate: PRs reverted or causing incidents
- MTTR: incident detected → resolved

**Dashboard 5: Cost Tracking**
- Total spend (daily/weekly/monthly)
- Spend by project
- Spend by agent persona
- Spend by model (if using multiple)
- Budget utilization (spent/budget ratio)

### Alerts (Replaces OneUptime)

OpenObserve has built-in alert rules. No separate incident management tool needed.

When alerts trigger:
- 3+ gate failures in 10 minutes → investigate
- Agent stuck for > 5 minutes with no tool calls → kill
- Cost ceiling exceeded across all projects → pause all
- Service health check fails (Anthropic API, OpenObserve) → notify

## AgentLens (Session Replay)

Open source (github.com/RobertTLange/agentlens). Local observability for coding-agent sessions.

### What it captures
- Every tool call the agent made (with arguments and results)
- Every LLM call (prompt + response)
- Context window state over time
- Decision points (where the agent chose path A over path B)
- File reads and writes
- Time between actions

### Integration
AgentLens runs alongside the agent in the worktree. It hooks into Claude Code's tool execution layer.

```
src/devloop/observability/
├── __init__.py
├── otel_setup.py      # Initialize OTel SDK, configure exporters
├── span_factory.py    # Helper to create properly attributed spans
├── dashboards/        # Dashboard definitions (JSON/YAML)
├── alerts/            # Alert rule definitions
└── types.py
```

### Usage
```bash
# After a failed run, find the session
just sessions list --status failed
# Replay it
just sessions replay <session-id>
# Compare two attempts of the same issue
just sessions diff <session-id-1> <session-id-2>
```

## Optional: Agent Trace
Open RFC specification for tracking and attributing AI-generated code contributions. Vendor-neutral trace records at file and line granularity. Evaluate for TB-6.

### Open Questions
- [ ] OpenObserve retention policy — how long to keep traces? (30 days default)
- [ ] AgentLens storage — local files or ship to OpenObserve?
- [ ] Alert fatigue — what's the right threshold before we tune out notifications?
- [ ] How to correlate AgentLens sessions with OTel traces? (trace_id as link)
- [ ] Agent Trace RFC — is it mature enough to adopt?
