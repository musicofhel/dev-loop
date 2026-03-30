"""Cost/usage monitoring — standalone analysis tool (Channel 3).

Not wired into real-time pipeline feedback. Invoked via ``just usage``.

Aggregates turn and token usage across runs and checks against budgets.
On Claude Code Max (flat subscription), dollar cost is always $0, so this
monitors resource consumption (turns, tokens) rather than spend.

Usage::

    from devloop.feedback.cost_monitor import get_usage_summary, check_budget

    summary = get_usage_summary(hours=24)
    alert = check_budget(summary)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from opentelemetry import trace

from devloop.paths import SESSIONS_DIR as _SESSIONS_DIR

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("feedback.cost_monitor", "0.1.0")

# ---------------------------------------------------------------------------
# Session-based usage tracking (reads from TB-6 session metadata)
# ---------------------------------------------------------------------------


def get_usage_summary(hours: int = 24) -> dict:
    """Aggregate usage stats from recent session metadata files.

    Scans ``SESSIONS_DIR/*.meta.json`` for sessions within the
    time window and sums up turn/token counts.

    Args:
        hours: Look back this many hours.

    Returns:
        Dict with total_runs, total_turns, total_input_tokens,
        total_output_tokens, and per-run breakdown.
    """
    import time

    with tracer.start_as_current_span(
        "feedback.cost_monitor.get_usage_summary",
        attributes={"cost.hours_lookback": hours},
    ) as span:
        cutoff = time.time() - (hours * 3600)
        runs: list[dict] = []
        total_turns = 0
        total_input = 0
        total_output = 0

        if not _SESSIONS_DIR.is_dir():
            span.set_attribute("cost.sessions_dir_exists", False)
            return {
                "total_runs": 0,
                "total_turns": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "runs": [],
                "hours": hours,
            }

        for meta_path in _SESSIONS_DIR.glob("*.meta.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue

            # Check timestamp
            ts = meta.get("timestamp", 0)
            if ts < cutoff:
                continue

            turns = meta.get("num_turns", 0)
            input_tok = meta.get("input_tokens", 0)
            output_tok = meta.get("output_tokens", 0)

            total_turns += turns
            total_input += input_tok
            total_output += output_tok

            runs.append({
                "session_id": meta_path.stem.replace(".meta", ""),
                "issue_id": meta.get("issue_id", "unknown"),
                "turns": turns,
                "input_tokens": input_tok,
                "output_tokens": output_tok,
                "timestamp": ts,
            })

        span.set_attribute("cost.total_runs", len(runs))
        span.set_attribute("cost.total_turns", total_turns)

        return {
            "total_runs": len(runs),
            "total_turns": total_turns,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "runs": runs,
            "hours": hours,
        }


def check_budget(
    summary: dict,
    max_daily_turns: int = 200,
    max_daily_input_tokens: int = 5_000_000,
    max_daily_output_tokens: int = 1_000_000,
) -> dict:
    """Check if usage exceeds daily budgets.

    Args:
        summary: Output from get_usage_summary().
        max_daily_turns: Turn budget per day.
        max_daily_input_tokens: Input token budget per day.
        max_daily_output_tokens: Output token budget per day.

    Returns:
        Dict with budget status, warnings, and whether to pause.
    """
    with tracer.start_as_current_span(
        "feedback.cost_monitor.check_budget",
    ) as span:
        warnings: list[str] = []
        pause = False

        turns = summary.get("total_turns", 0)
        input_tok = summary.get("total_input_tokens", 0)
        output_tok = summary.get("total_output_tokens", 0)

        # Check turn budget
        turn_pct = (turns / max_daily_turns * 100) if max_daily_turns > 0 else 0
        if turn_pct >= 100:
            warnings.append(f"Turn budget EXCEEDED: {turns}/{max_daily_turns} ({turn_pct:.0f}%)")
            pause = True
        elif turn_pct >= 80:
            warnings.append(f"Turn budget WARNING: {turns}/{max_daily_turns} ({turn_pct:.0f}%)")

        # Check input token budget
        input_pct = (input_tok / max_daily_input_tokens * 100) if max_daily_input_tokens > 0 else 0
        if input_pct >= 100:
            warnings.append(f"Input token budget EXCEEDED: {input_tok:,}/{max_daily_input_tokens:,}")
            pause = True
        elif input_pct >= 80:
            warnings.append(f"Input token budget WARNING: {input_tok:,}/{max_daily_input_tokens:,}")

        # Check output token budget
        output_pct = (output_tok / max_daily_output_tokens * 100) if max_daily_output_tokens > 0 else 0
        if output_pct >= 100:
            warnings.append(f"Output token budget EXCEEDED: {output_tok:,}/{max_daily_output_tokens:,}")
            pause = True
        elif output_pct >= 80:
            warnings.append(f"Output token budget WARNING: {output_tok:,}/{max_daily_output_tokens:,}")

        span.set_attribute("cost.pause_recommended", pause)
        span.set_attribute("cost.warnings_count", len(warnings))

        return {
            "within_budget": not pause,
            "pause_recommended": pause,
            "warnings": warnings,
            "turn_usage_pct": round(turn_pct, 1),
            "input_usage_pct": round(input_pct, 1),
            "output_usage_pct": round(output_pct, 1),
        }
