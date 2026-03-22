# Handoff: Post-Mirror Run #3 Fixes (2026-03-20)

## What Was Done

### Phase 1: Config JSON Fixes (`~/dev-loop/config/dashboards/`)

| File | Panel | Fix |
|------|-------|-----|
| agent-performance.json | P2 | Switched from `tb%.phase.persona` (7-17µs) to `tb%.run` spans; `/1000` → `/1000000` for seconds |
| quality-gates.json | P1 | Added color overrides: passed=#4caf50 (green), failed=#d62728 (red) |
| quality-gates.json | P2 | Added `/1000` division (gate_duration_ms stores µs despite name) |
| cost-tracking.json | P2 | Renamed `devloop_agent_persona` → `persona_name` (matches OO schema) |
| calibration.json | P1-5,7 | Standardized `INTERVAL '7 DAYS'` → `INTERVAL '30 DAYS'` |
| calibration.json | P2 | Chart type `area` → `line` (single series) |
| calibration.json | P6 | `ROUND(..., 2)` → `ROUND(..., 1)` |

### Phase 2: Import Script (`~/dev-loop/scripts/import-dashboards.py`)

- **LIMIT skip**: `_fix_aggregate_timestamp()` no longer injects `MIN(_timestamp)` into queries with `LIMIT` (fixes Cost Tracking P5 flat listing)
- **Label overrides**: `_LABEL_OVERRIDES` dict maps 10 common aliases to human-readable labels (e.g., `failure_pct` → "Failure Rate (%)")
- **Color overrides**: `_translate_panel()` reads optional `"colors"` dict from panel config, overlays onto y-axis fields
- **Categorical axis protection**: Non-time x-axis fields get `aggregationFunction="count"` to signal OO they're categorical dimensions (prevents OO restructuring)
- **Tests**: 38/38 pass (8 new: 2 LIMIT skip, 3 label override, 3 translate panel)
- **Full suite**: 431/431 pass

### Phase 3: Mirror Pipeline (`~/dashboard-mirror/src/dashboard_mirror/collect.py`)

- **Canvas paint wait**: `wait_for_selector('canvas', timeout=5000)` + 2s timeout before DOM extraction
- **Pre-screenshot lazy-load scroll**: Full scroll-through before `full_page=True` capture
- **Panel body targeting**: Per-panel screenshots target `.panelBody, canvas, svg` instead of full grid item

### Calibration Dashboard Rewrite (Critical Discovery)

The calibration dashboard queries used **fictional column names** that didn't match the daemon's actual ATSC-conformant OTel attributes:

| Dashboard assumed | Actual OTel → OO column |
|---|---|
| `operation_name = 'shadow_verdict'` | `operation_name = 'ambient.check'` |
| `verdict` | `guardrail_action` |
| `reason` | `guardrail_policy` |
| `operation_name = 'session'` | `operation_name = 'ambient.session'` |
| `operation_name = 'replay_verdict'` | No OTel span — replaced with Block Rate by Check Type |
| `operation_name = 'feedback'` | No OTel span — replaced with Sessions Per Day |

All 8 panels rewritten with correct column names.

### Ambient Layer Activation

- **Root cause of empty dashboard**: `~/.config/dev-loop/ambient.yaml` had blank OO credentials (`openobserve_user: ""`, `openobserve_password: ""`). Spans never exported.
- **Fixed**: Set credentials to `admin@dev-loop.local` / `devloop123`
- **Daemon started**: `dl start` → pid 66287, receiving hook events
- **Schema bootstrapped**: Manually POSTed seed ambient spans to create all required OO columns (`guardrail_action`, `guardrail_policy`, `check_type`, `session_outcome`, etc.)

## State After

- 6 dashboards imported, 32 panels, zero OO config drift
- Calibration dashboard: schema errors eliminated, will populate as sessions accumulate
- Daemon running with correct OTel export credentials
- All tests pass (431 Python + 467 Rust)

## What's Left

- **Cost Tracking** (5 panels): Still blocked on missing `devloop_cost_*` telemetry columns
- **Phase 0 investigation**: Timestamp filter validation (µs vs ms) — currently benign with ~6 days of data
- **Phase 0 investigation**: String literal validation (`gate_status = 'fail'` vs `'failed'`) — low risk, panels render
- **Mirror run #4**: Re-run pipeline to validate visual improvements
- **ATSC conformance commit**: daemon/src changes from 2026-03-17 still uncommitted in dev-loop
