# Handoff: v5 Observability Validation (2026-03-30)

## What was done

Closed the observability gap between instrumented span attributes (~130) and dashboard visibility (~20 previously queried).

### Phase 1-2: New Dashboards + Fix

- **TB Outcomes** (`config/dashboards/tb-outcomes.json`) — 6 panels: outcomes over time, retries per TB, cascade activity, ambiguity scores, timeout rate, phase duration breakdown
- **LLMOps Insights** (`config/dashboards/llmops-insights.json`) — 6 panels: DSPy vs CLI latency, finding overlap, GEPA history, training data volume, Langfuse status, finding counts
- **tb5_persona fix** in `config/dashboards/agent-performance.json` — added to COALESCE (TB-5 delegates to TB-1 for persona, but future-proofed)
- All 8 dashboards imported to OO with zero drift

### Phase 3: Stress Test (30s cooldown)

**30/30 PASS (100%)** — 5 iterations, 6 TBs each, 30s cooldown between TBs.

Bugs fixed in stress test:
1. `br_create` cwd was `~/dev-loop` but pipeline looks in `~/OOTestProject1` — issues created in wrong beads DB. Fixed to `cwd=OOTESTPROJECT1`.
2. Pytest pre-flight timeout 120s→300s (708 tests take ~141s).
3. Added `--cooldown N` flag for configurable spacing.

Key results:
- Zero timeouts (confirms v4 hypothesis: failures were concurrency-induced)
- TB-2 escalated 4/5 times (retry exhaustion is expected but tuning opportunity)
- TB-4 iter 1 outlier (637s) — cold cache, all subsequent runs 17-28s

### Phase 5: Dashboard-Mirror

Full `dm-collect-all` ran (10m 42s): 8 dashboards captured with Playwright screenshots at 30d/7d/1h. Chain diffs written for all 8. Output in `~/dashboard-mirror/output/`.

**Caveat:** Screenshots captured before stress test completed → most panels show "No Data". A fresh `dm-collect-all` after data generation would show populated panels.

### Phase 6: Training Data

182 total examples (152 code_review, 24 persona_select, 6 retry_prompt). All 3 DSPy programs optimized.

## Files changed

| File | Change |
|------|--------|
| `config/dashboards/tb-outcomes.json` | NEW — 6-panel TB outcomes dashboard |
| `config/dashboards/llmops-insights.json` | NEW — 6-panel LLMOps insights dashboard |
| `config/dashboards/agent-performance.json` | Added tb5_persona to COALESCE |
| `scripts/stress-test.py` | --cooldown flag, br_create cwd fix, pytest timeout bump |
| `docs/validation-report-2026-03-30-v5.md` | Full validation report |

## What to do next

1. **Run TB-7** to populate LLMOps Insights dashboard panels (DSPy comparison data)
2. **Re-run `dm-collect-all`** after generating data to get populated dashboard screenshots
3. **Tune TB-2**: Consider increasing `max_retries` or improving the retry prompt — 4/5 escalation rate suggests the single retry isn't enough for the bug-fix persona
4. **persona_select training**: At 0.56 metric score with only 19 examples, this program needs more data
5. **Consider automated dm-collect-all**: Periodic captures would detect dashboard drift over time

## Commit

`f248c75` on `main`, pushed to origin.
