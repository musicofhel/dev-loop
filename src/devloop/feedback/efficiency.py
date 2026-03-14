"""Step efficiency analysis — Channel 7 of the feedback loop.

Analyzes agent session NDJSON for wasteful patterns: re-reading the same
files, edit-undo cycles, excessive tool calls without progress.

Rule-based analysis (not DeepEval). Fast and deterministic.

Usage::

    from devloop.feedback.efficiency import analyze_efficiency

    result = analyze_efficiency(session_events)
"""

from __future__ import annotations

import logging
from collections import Counter

from opentelemetry import trace

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("feedback.efficiency", "0.1.0")


def analyze_efficiency(events: list[dict]) -> dict:
    """Analyze a session's NDJSON events for wasteful patterns.

    Args:
        events: List of parsed NDJSON event dicts (from _parse_session_events).

    Returns:
        Dict with efficiency score, waste patterns, and suggestions.
    """
    with tracer.start_as_current_span(
        "feedback.efficiency.analyze",
        attributes={"efficiency.event_count": len(events)},
    ) as span:
        if not events:
            return {
                "score": 1.0,
                "total_tool_calls": 0,
                "meaningful_steps": 0,
                "waste_ratio": 0.0,
                "patterns": [],
                "suggestions": [],
            }

        # Count tool usage
        tool_calls: list[dict] = []
        tool_names: Counter[str] = Counter()
        read_targets: Counter[str] = Counter()
        edit_count = 0
        total_tool_calls = 0

        for event in events:
            data = event.get("data", {})
            event_type = event.get("type", data.get("type", ""))

            if event_type == "tool_use":
                total_tool_calls += 1
                tool_name = data.get("tool", data.get("name", "unknown"))
                tool_names[tool_name] += 1
                tool_calls.append(data)

                # Track file reads
                if tool_name in ("Read", "read"):
                    path = (data.get("args", {}) or {}).get("path", "") or (
                        (data.get("input", {}) or {}).get("file_path", "")
                    )
                    if path:
                        read_targets[path] += 1

                # Track edits
                if tool_name in ("Edit", "Write", "edit", "write"):
                    edit_count += 1

        # Detect waste patterns
        patterns: list[dict] = []
        suggestions: list[str] = []

        # Pattern 1: Re-reading the same file 3+ times
        for path, count in read_targets.items():
            if count >= 3:
                patterns.append({
                    "type": "repeated_read",
                    "detail": f"Read '{path}' {count} times",
                    "waste_count": count - 1,
                })
        if any(p["type"] == "repeated_read" for p in patterns):
            suggestions.append(
                "Agent is re-reading files it already read. Add to CLAUDE.md: "
                "'Do not re-read files you have already read in this session.'"
            )

        # Pattern 2: High tool-call count with few edits (spinning)
        if total_tool_calls > 10 and edit_count == 0:
            patterns.append({
                "type": "no_edits",
                "detail": f"{total_tool_calls} tool calls but 0 edits",
                "waste_count": total_tool_calls,
            })
            suggestions.append(
                "Agent made many tool calls without editing any files. "
                "It may be stuck in a read-analyze loop."
            )

        # Pattern 3: Very high tool count relative to edits
        if edit_count > 0 and total_tool_calls > edit_count * 10:
            ratio = total_tool_calls / edit_count
            patterns.append({
                "type": "high_ratio",
                "detail": f"{total_tool_calls} tool calls for {edit_count} edits (ratio: {ratio:.0f}:1)",
                "waste_count": total_tool_calls - (edit_count * 5),
            })
            suggestions.append(
                "Agent is making too many tool calls per edit. "
                "Consider narrowing the scope or providing more context in the issue."
            )

        # Pattern 4: Excessive Glob/Grep without targeted reads
        search_count = tool_names.get("Glob", 0) + tool_names.get("Grep", 0)
        if search_count > 15:
            patterns.append({
                "type": "excessive_search",
                "detail": f"{search_count} search operations (Glob+Grep)",
                "waste_count": search_count - 10,
            })
            suggestions.append(
                "Agent is doing excessive searching. Add scope hints to the issue: "
                "mention specific files or directories to look at."
            )

        # Calculate efficiency score (0.0 = wasteful, 1.0 = efficient)
        total_waste = sum(p.get("waste_count", 0) for p in patterns)
        meaningful = max(total_tool_calls - total_waste, 0)

        if total_tool_calls > 0:
            score = round(meaningful / total_tool_calls, 2)
            waste_ratio = round(total_waste / total_tool_calls, 2)
        else:
            score = 1.0
            waste_ratio = 0.0

        span.set_attribute("efficiency.score", score)
        span.set_attribute("efficiency.waste_ratio", waste_ratio)
        span.set_attribute("efficiency.patterns_found", len(patterns))

        return {
            "score": score,
            "total_tool_calls": total_tool_calls,
            "meaningful_steps": meaningful,
            "waste_ratio": waste_ratio,
            "patterns": patterns,
            "suggestions": suggestions,
            "tool_breakdown": dict(tool_names),
        }
