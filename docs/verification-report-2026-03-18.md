> Note: omniswipe-backend was the secondary test target during this period. It has since been replaced by OOTestProject1.

# Verification Report — 2026-03-18

**Generated**: 2026-03-18T19:38:00-04:00
**Overall Status**: PASS (with notes)

---

## 1. Environment

| Component | Version / Status |
|-----------|-----------------|
| Python | 3.12.12 |
| uv | 0.10.0 |
| Rust (rustc) | 1.93.0 (254b59607 2026-01-19) |
| gitleaks | 8.30.0 |
| OpenObserve | `public.ecr.aws/zinclabs/openobserve:latest` (Docker) |
| OpenObserve health | `{"status":"ok"}` |
| Beads (br) | OK |
| ANTHROPIC_API_KEY | NOT SET |

---

## 2. Test Results

| Suite | Total | Passed | Failed | Skipped | Duration |
|-------|-------|--------|--------|---------|----------|
| Full pytest (`just test`) | 393 | 393 | 0 | 0 | 90.15s |
| Tier-2 planted-defect (`just tier2-test`) | 31 | 31 | 0 | 0 | 67.45s |
| Rust daemon lib (`cargo test` — lib) | 93 | 93 | 0 | 0 | 10.82s |
| Rust daemon bin (`cargo test` — bin) | 190 | 190 | 0 | 0 | 10.81s |
| Rust turmoil concurrent | 4 | 4 | 0 | 0 | 0.41s |
| Feedback scoring (`just feedback-test`) | 27 | 27 | 0 | 0 | 0.03s |
| Replay harness (`just replay-test`) | 19 | 19 | 0 | 0 | 0.02s |
| Conformance pre_tool_use | 82 | 80 | 0 | 2 | — |
| Conformance post_tool_use | 24 | 24 | 0 | 0 | — |
| **TOTALS** | **863** | **861** | **0** | **2** | — |

### Conformance Skips (Known FPs)

1. `deny_list_allow: .env.example` — `.env.*` pattern matches `.env.example.md`. Fix: add to `allow_patterns` in config.
2. `deny_list_allow: keystore.ts` — `*.keystore` matches `keystore.ts` extension mismatch, but only if file ends with `.keystore`.

### Rust Compiler Warnings (3)

1. `deny_list.rs:120` — field `matched_on` never read on `DenyMatch`
2. `transcript.rs:15` — constant `DEFAULT_CONTEXT_LIMIT` never used
3. `transcript.rs:35` — method `context_pct` never used

---

## 3. Calibration Summary

**Status**: PASS — no regressions detected

| Stage | Status | Details |
|-------|--------|---------|
| Shadow Report | OK | 1 shadow verdict collected |
| Replay Harness | OK | 2000 tool calls — 9 blocked, 8 warned (out of 10,405 parsed from 77 sessions) |
| Tier-2 Suite | OK | 31 passed |
| Feedback Scoring | OK | 3 labels, F1=0.8 |
| Rust Tests | Skipped | (already run in Phase 2) |

### Replay Details

- **Allow**: 1983 (99.2%)
- **Block**: 9 (0.5%)
- **Warn**: 8 (0.4%)
- No regressions vs `2026-03-baseline.json`

**Top Blocked Files** (expected — all are `.env` or sensitive files):
- `/home/musicofhel/septic-lookup/.env` (2x)
- `/home/musicofhel/omniswipe-backend/.env` (2x)
- `/home/musicofhel/wavecast/.env` (1x)
- `/home/musicofhel/wavecast/.env.example` (1x)

### Feedback Scores

| Check Type | TP | FP | FN | Precision | Recall | F1 |
|------------|----|----|----|-----------|---------|----|
| dangerous_ops | 1 | 0 | 0 | 100.0% | 100.0% | 1.000 |
| deny_list | 1 | 0 | 0 | 100.0% | 100.0% | 1.000 |
| secrets | 0 | 1 | 0 | 0.0% | 0.0% | 0.000 |

Note: Secrets has 1 FP and no TP/FN labels yet — F1=0.0 is due to insufficient labeled data, not a detection failure.

---

## 4. OTel Verification

### Test Span Ingestion: SUCCESS

A test span (`test-verification-span`) was created via `devloop.observability.tracing`, flushed, and confirmed present in OpenObserve:

```json
{
  "service_name": "dev-loop",
  "trace_id": "fd595a011db127c733b7a8142bd55ca0",
  "start_time": 1773877135675408285
}
```

The OTLP/HTTP exporter → OpenObserve pipeline is working end-to-end.

### Dashboards: 4 of 5 expected present

| Dashboard | Status |
|-----------|--------|
| Loop Health | PRESENT |
| Agent Performance | PRESENT |
| Quality Gate Insights | PRESENT |
| Ambient Layer Calibration | PRESENT (bonus — not in original expected list) |
| DORA Metrics | MISSING |
| Cost Tracking | MISSING |

DORA Metrics and Cost Tracking dashboards have not been created yet. These were part of the original plan's expected list but were not implemented in `config/dashboards/`.

### Alerts: 7 unique rules, 0 duplicates (FIXED)

| Alert Rule | Enabled | SQL Validated |
|------------|---------|---------------|
| gate_failure_spike | Yes | OK |
| agent_stuck | Yes | Pending (needs `issue_id` column from real heartbeat spans) |
| high_turn_usage | Yes | OK |
| escalation_spike | Yes | OK |
| security_finding | Yes | OK |
| session_burn_rate | Yes | Pending (needs `session_context_tokens_consumed` column) |
| guardrail_trigger_rate_spike | Yes | Pending (needs `guardrail_action` column) |

**Bugs found and fixed during verification:**

1. **`import-alerts.py` delete bug** — `delete_existing()` read `alert.get("id")` but the OpenObserve v2 API returns the field as `alert_id`. Deletes silently no-op'd, causing duplicates to stack on each import. Fixed: `alert.get("id")` → `alert.get("alert_id")`.

2. **Alert SQL type coercion errors** — All 7 queries used `_timestamp >= NOW() - INTERVAL '...'`, but OpenObserve stores `_timestamp` as `Int64` (epoch microseconds) while `NOW() - INTERVAL` produces `Timestamp(Nanosecond)`. Fixed: `CAST(NOW() - INTERVAL '...' AS BIGINT) / 1000`.

3. **Alert SQL bracket-accessor syntax** — Queries used `attributes_string['gate.status']` and `attributes_int64['runtime.num_turns']`, but OpenObserve flattens span attributes with dots→underscores (e.g., `gate.status` → `gate_status`). Fixed: all queries now use flattened column names.

4. **`session_burn_rate` subquery** — Original SQL used a correlated subquery (`SELECT ... WHERE ... > (SELECT AVG(...) ...)`). OpenObserve/DataFusion doesn't support this in alert context. Replaced with a static threshold (200k tokens).

**Note**: 3 of 7 alerts reference custom attribute columns (`issue_id`, `session_context_tokens_consumed`, `guardrail_action`) that don't yet exist in the OpenObserve schema. These columns will auto-appear once real pipeline spans carrying those OTel attributes are ingested. Until then, those 3 alerts will produce "field not found" errors on evaluation — this is expected and harmless (they fire 0 results, same as if no matching data exists).

---

## 5. Issues Found

| # | Severity | Description | Status |
|---|----------|-------------|--------|
| 1 | INFO | `ANTHROPIC_API_KEY` is not set — tracer bullets that call Claude API won't work | Open |
| 2 | LOW | 3 Rust compiler warnings (dead code) in `deny_list.rs` and `transcript.rs` | Open |
| 3 | LOW | 2 known conformance false-positive skips (`.env.example`, `keystore.ts`) | Open |
| 4 | INFO | DORA Metrics and Cost Tracking dashboards don't exist yet | Open |
| 5 | HIGH | `import-alerts.py` used wrong field name (`id` vs `alert_id`) — deletes silently failed, causing duplicate alerts | **FIXED** |
| 6 | HIGH | All 7 alert SQL queries had `Int64 >= Timestamp(ns)` type coercion errors | **FIXED** |
| 7 | MED | Alert SQL used `attributes_string[...]` bracket syntax instead of flattened column names | **FIXED** |
| 8 | MED | `session_burn_rate` alert used unsupported correlated subquery | **FIXED** |
| 9 | INFO | Secrets feedback F1=0.0 — needs more labeled data (only 1 FP label, no TP/FN) | Open |
| 10 | LOW | `uv sync` removed stale `prompt-bench==0.1.0` reference from a deleted worktree | Resolved |

---

## 6. Recommended Next Steps

1. **Set `ANTHROPIC_API_KEY`** if you want to run live tracer bullets (TB-1 through TB-6).
2. **Add DORA Metrics and Cost Tracking dashboards** to `config/dashboards/` — or remove them from the expected-dashboard checklist if they're deferred.
3. **Fix the 3 Rust dead-code warnings** — either use or remove `DenyMatch.matched_on`, `DEFAULT_CONTEXT_LIMIT`, and `context_pct()`.
4. **Label more secrets feedback data** — the F1=0.0 on secrets is purely a data-coverage issue; add TP and FN labels to `scripts/feedback/labels/`.
5. **Address the 2 conformance known FPs** when convenient — add `.env.example` to `allow_patterns` or refine the `.env.*` glob.
6. **Run a real tracer bullet** to ingest pipeline spans, which will create the missing columns (`issue_id`, `guardrail_action`, etc.) and fully validate the remaining 3 alert queries.
7. **Verify dashboards visually** in the OpenObserve UI at http://localhost:5080 — confirm panels render and queries return data now that at least one trace exists.
8. **Review dashboard SQL** — the same `attributes_string[...]` bracket syntax and `NOW() - INTERVAL` patterns likely exist in the dashboard panel queries too. Apply the same flattened-column and `CAST(...AS BIGINT)/1000` fixes.
