#!/usr/bin/env node
// Renders architecture diagrams using beautiful-mermaid
// Usage: node scripts/render-diagrams.mjs

import { renderMermaidSVG } from 'beautiful-mermaid';
import { writeFileSync, mkdirSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const outDir = join(__dirname, '..', 'docs', 'diagrams');
mkdirSync(outDir, { recursive: true });

const theme = {
  bg: '#0d1117',
  fg: '#e6edf3',
  accent: '#58a6ff',
  muted: '#8b949e',
  surface: '#161b22',
  border: '#30363d',
  line: '#8b949e',
};

const diagrams = {};

// ─── Diagram 1: System Overview (6-Layer Loop) ─────────────────
diagrams['system-overview'] = `graph TD
    INTAKE["1. Intake\\n(beads polling)"]
    ORCH["2. Orchestration\\n(worktree isolation)"]
    RUNTIME["3. Agent Runtime\\n(Claude Code CLI)"]
    GATES["4. Quality Gates\\n(SAST + secrets + review)"]
    OBS["5. Observability\\n(OTel + OpenObserve)"]
    FEEDBACK["6. Feedback Loop\\n(retry + tuning)"]

    INTAKE --> ORCH
    ORCH --> RUNTIME
    RUNTIME --> GATES
    GATES --> OBS
    OBS --> FEEDBACK
    FEEDBACK --> INTAKE`;

// ─── Diagram 2: Three-Tier Ambient Architecture ────────────────
diagrams['ambient-tiers'] = `graph LR
    CC["Claude Code\\nSession"]
    T1["Tier 1\\nAlways-On\\n< 5ms"]
    T2["Tier 2\\nCheckpoint\\n~30s"]
    T3["Tier 3\\nFull Pipeline\\n~minutes"]

    CC --> T1
    T1 --> T2
    T2 --> T3`;

// ─── Diagram 3: Tier 1 Check Engine ────────────────────────────
diagrams['check-engine'] = `graph TD
    HOOK["Hook\\n(PreToolUse / PostToolUse)"]
    ENGINE["Check Engine"]
    DENY["Deny List\\n15 glob patterns"]
    OPS["Dangerous Ops\\n25 regex patterns"]
    SEC["Secret Scanner\\n15 regex patterns"]
    ALLOW["ALLOW"]
    BLOCK["BLOCK"]
    WARN["WARN"]

    HOOK --> ENGINE
    ENGINE --> DENY
    ENGINE --> OPS
    ENGINE --> SEC
    DENY --> BLOCK
    DENY --> ALLOW
    OPS --> BLOCK
    OPS --> WARN
    OPS --> ALLOW
    SEC --> WARN
    SEC --> ALLOW`;

// ─── Diagram 4: Hook Integration Flow ─────────────────────────
diagrams['hook-flow'] = `graph TD
    CC["Claude Code"]
    SETTINGS["~/.claude/settings.json\\n6 hook matchers"]
    PRE["PreToolUse\\n(Write/Edit/Bash)"]
    POST["PostToolUse\\n(Write/Edit)"]
    START["SessionStart"]
    END["SessionEnd"]
    STOP["Stop\\n(context guard)"]
    DAEMON["dl daemon\\nUnix socket"]
    LOG["Event Log\\n(JSONL)"]
    SSE["SSE Broadcast"]

    CC --> SETTINGS
    SETTINGS --> PRE
    SETTINGS --> POST
    SETTINGS --> START
    SETTINGS --> END
    SETTINGS --> STOP
    PRE --> DAEMON
    POST --> DAEMON
    START --> DAEMON
    END --> DAEMON
    DAEMON --> LOG
    DAEMON --> SSE`;

// ─── Diagram 5: Checkpoint Gates (Tier 2) ─────────────────────
diagrams['checkpoint-gates'] = `graph TD
    COMMIT["git commit detected"]
    STAGED["Get Staged Files"]
    SANITY["Gate: Sanity\\n(compile + test)"]
    SEMGREP["Gate: Semgrep\\n(SAST scanning)"]
    SECRETS["Gate: Secrets\\n(gitleaks)"]
    ATDD["Gate: ATDD\\n(spec enforcement)"]
    REVIEW["Gate: Review\\n(placeholder)"]
    PASS["PASS\\n+ trailer hash"]
    FAIL["FAIL\\n(block commit)"]

    COMMIT --> STAGED
    STAGED --> SANITY
    SANITY --> SEMGREP
    SEMGREP --> SECRETS
    SECRETS --> ATDD
    ATDD --> REVIEW
    REVIEW --> PASS
    SANITY --> FAIL
    SEMGREP --> FAIL
    SECRETS --> FAIL
    ATDD --> FAIL`;

// ─── Diagram 6: Config 3-Layer Merge ──────────────────────────
diagrams['config-merge'] = `graph TD
    BUILTIN["Built-in Defaults\\n(hardcoded)"]
    GLOBAL["Global Config\\n~/.config/dev-loop/ambient.yaml"]
    REPO["Per-Repo Config\\n.devloop.yaml"]
    MERGED["Merged Config"]
    ENGINE["Check Engine"]

    BUILTIN --> MERGED
    GLOBAL --> MERGED
    REPO --> MERGED
    MERGED --> ENGINE`;

// ─── Diagram 7: Session Lifecycle ─────────────────────────────
diagrams['session-lifecycle'] = `stateDiagram-v2
    [*] --> Started: SessionStart hook
    Started --> Checking: PreToolUse
    Checking --> Started: allow
    Checking --> Blocked: block
    Blocked --> Started: allow-once
    Started --> ContextWarn: Stop hook > 85%
    ContextWarn --> Handoff: write YAML
    Started --> Ended: SessionEnd hook
    Handoff --> Ended: session complete
    Ended --> OTelExport: export spans
    OTelExport --> [*]`;

// ─── Diagram 8: Calibration Pipeline ──────────────────────────
diagrams['calibration-pipeline'] = `graph TD
    START["just calibrate"]
    S1["Stage 1: Shadow Report\\n(last 7 days)"]
    S2["Stage 2: Replay Harness\\n(10K+ tool calls)"]
    S3["Stage 3: Tier 2 Suite\\n(13 planted defects)"]
    S4["Stage 4: Feedback Scoring\\n(P/R/F1 metrics)"]
    S5["Stage 5: Rust Tests\\n(257 tests)"]
    REPORT["Calibration Report\\ndocs/calibration/YYYY-MM-DD.md"]
    BASELINE["Baseline Comparison\\n(regression detection)"]

    START --> S1
    S1 --> S2
    S2 --> S3
    S3 --> S4
    S4 --> S5
    S5 --> REPORT
    S2 --> BASELINE
    S4 --> BASELINE
    BASELINE --> REPORT`;

// ─── Diagram 9: Observability Data Flow ───────────────────────
diagrams['observability-flow'] = `graph LR
    HOOKS["Hooks"]
    DAEMON["Daemon"]
    JSONL["JSONL Event Log\\n/tmp/dev-loop/events.jsonl"]
    OTEL["OTel Spans\\n(OTLP/HTTP)"]
    OO["OpenObserve"]
    DASH["Dashboards"]
    ALERTS["Alert Rules"]

    HOOKS --> DAEMON
    DAEMON --> JSONL
    DAEMON --> OTEL
    OTEL --> OO
    OO --> DASH
    OO --> ALERTS`;

// ─── Diagram 10: Tracer Bullet Flow (TB-1) ───────────────────
diagrams['tracer-bullet-flow'] = `graph TD
    ISSUE["Beads Issue\\n(br ready)"]
    POLL["Intake Polling\\n(MCP server)"]
    WORKTREE["Git Worktree\\n(isolation)"]
    AGENT["Agent Runtime\\n(Claude Code)"]
    GATE0["Gate 0: Sanity\\n(compile + test)"]
    GATE2["Gate 2: Secrets\\n(gitleaks)"]
    GATE3["Gate 3: Security\\n(bandit SAST)"]
    GATE4["Gate 4: Review\\n(LLM-as-judge)"]
    PR["PR Created"]
    RETRY["Feedback: Retry\\n(max 2 attempts)"]
    ESCALATE["Escalate to Human"]

    ISSUE --> POLL
    POLL --> WORKTREE
    WORKTREE --> AGENT
    AGENT --> GATE0
    GATE0 --> GATE2
    GATE2 --> GATE3
    GATE3 --> GATE4
    GATE4 --> PR
    GATE0 --> RETRY
    GATE2 --> RETRY
    GATE3 --> RETRY
    GATE4 --> RETRY
    RETRY --> AGENT
    RETRY --> ESCALATE`;

// ─── Render all diagrams ──────────────────────────────────────

let rendered = 0;
let failed = 0;

for (const [name, mermaid] of Object.entries(diagrams)) {
  try {
    const svg = renderMermaidSVG(mermaid, theme);
    const outPath = join(outDir, `${name}.svg`);
    writeFileSync(outPath, svg);
    console.log(`  ${name}.svg`);
    rendered++;
  } catch (err) {
    console.error(`  FAIL: ${name} — ${err.message}`);
    failed++;
  }
}

console.log(`\nRendered ${rendered}/${rendered + failed} diagrams to docs/diagrams/`);
if (failed > 0) process.exit(1);
