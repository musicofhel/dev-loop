# Handoff: Calibration & Testing Infrastructure Planning

**Date**: 2026-03-16
**Session**: Deep analysis + implementation planning for ambient layer calibration
**Previous**: `handoff-2026-03-16-completion-pass.md`

---

## What Was Done

This session was pure research and planning — no code changes. Produced two plan documents and a comprehensive architectural analysis.

### 1. Deep Architectural Analysis

Full read of all 24 daemon source files + tests + configs. Key findings:

**Critical silent failures identified:**
- OTel export (`otel.rs`): raw TcpStream, zero error handling, spans lost silently
- Checkpoint gates (`checkpoint.rs`): ALL fail-open if semgrep/gitleaks not installed — user gets zero protection with no warning
- Daemon startup (`daemon.rs`): socket bind failure doesn't propagate — `dl start` says "started" but daemon crashed
- Event log (`event_log.rs`): bounded channel (1000), drops events silently, unbounded JSONL growth

**Testing gaps:**
- 412 total tests (280 Python + 132 Rust) but zero integration tests for: server HTTP endpoints, hook stdin/stdout protocol, checkpoint gates with real tools, OTel export, daemon lifecycle
- Check engine core is well-tested (30+ unit tests, benchmarks, concurrent safety)
- 937K lines of real Claude Code session data available for replay testing

### 2. Tool Evaluation

Evaluated 12 external tools/repos for calibration infrastructure:

| Tool | Verdict | Use |
|------|---------|-----|
| semgrep/ai-best-practices | **Adopt** | 58 AI security rules including 9 Claude Code hook rules. Add to checkpoint gate |
| betterleaks | **Evaluate** | Gitleaks replacement with CEL live validation + BPE token efficiency filter |
| rgx | **Install** | Terminal regex tester for interactive pattern debugging |
| pyrefly conformance pattern | **Steal pattern** | Annotated test file format for hook conformance tests |
| promptfoo test case pattern | **Steal pattern** | Declarative YAML assertion format for replay harness |
| vibe-diff | **Study** | Similar architecture (PreToolUse/PostToolUse hooks, risk scoring) |
| agentura | **Skip** | Validates per-check feedback design but nothing to import |
| Kolega | **Park** | SaaS security scanner, future evaluation |
| deepagents/Harbor | **Use for Tier 2** | Docker sandbox lifecycle for planted-defect suite |
| ACuRL | **Skip** | RL for GUI agents, wrong domain |
| k8s-ai-conformance | **Skip** | No automated tests, just methodology |
| attyx | **Skip** | Terminal emulator, not relevant |

### 3. Plan Documents Created

**`docs/testing-calibration-plan.yaml`** — 5-phase calibration roadmap:
- Phase 0: Fix silent failures (OTel, fail-open, daemon startup, event log)
- Phase 1: Shadow mode (log verdicts without blocking)
- Phase 2: Replay harness (937K lines through check engine)
- Phase 3: Planted-defect suite (12 scenarios, Harbor/Docker)
- Phase 4: Per-check feedback (dl feedback, labeled data, F1 tracking)
- Phase 5: Continuous calibration pipeline
- Plus: 10-item tool wishlist (fuzzer, conformance tester, mock server, etc.)

**`docs/calibration-implementation-plan.yaml`** — concrete implementation for first 6 steps:
- Step 1: semgrep/ai-best-practices integration (~30 min)
- Step 2: betterleaks evaluation + migration (~1 hr)
- Step 3: rgx installation (~5 min)
- Step 4: Hook conformance tests, 130+ cases (~2 hrs)
- Step 5: Check engine fuzzer with proptest (~2 hrs)
- Step 6: Replay harness with declarative YAML assertions (~3 hrs)
- Total: ~9 hours across 2-3 sessions

---

## Files Created

| File | Purpose |
|------|---------|
| `docs/testing-calibration-plan.yaml` | 5-phase calibration roadmap + tool wishlist |
| `docs/calibration-implementation-plan.yaml` | Concrete implementation plan with tool integrations |

## Files NOT Modified

No code changes. No daemon modifications. No config changes. Pure planning session.

---

## Key Decisions Made

1. **Shadow mode before enforcement** — deploy hooks in log-only mode for a week before blocking
2. **betterleaks over gitleaks** — CEL validation (checks if secrets are live) justifies migration, keep gitleaks as fallback
3. **semgrep/ai-best-practices** — clone to `~/.local/share/semgrep-ai-rules/`, add as second `--config` flag, graceful fallback
4. **Conformance test format** — steal pyrefly's annotated test file pattern (`# expect: block/allow/warn` comments above JSON)
5. **proptest for fuzzing** — property-based testing on all 55 regex patterns, invariants: no panic, deterministic, no backtracking
6. **Harbor for Tier 2 only** — Docker sandbox testing for planted defects, plain pytest for Tier 1 replay
7. **Success metrics defined** — Tier 1 precision >= 95%, recall >= 90%, Tier 2 gate accuracy 100%

---

## Next Session: Start Implementation

Recommended order:
1. Step 1: Clone semgrep/ai-best-practices, update checkpoint.rs, scan own codebase
2. Step 2: Install betterleaks, run comparison against gitleaks, migrate if passes
3. Steps 4-6 in parallel: conformance tests, fuzzer, replay harness

All details in `docs/calibration-implementation-plan.yaml`.
