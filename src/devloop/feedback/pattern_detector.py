"""Failure pattern detection — standalone analysis tool (Channel 2).

Not wired into real-time pipeline feedback. Invoked via ``just patterns``.

Scans recent gate failures for repeated patterns and suggests harness
tuning actions (CLAUDE.md rule changes, gate threshold adjustments).

Rule-based for now. LLM-based analysis is a future enhancement.

Usage::

    from devloop.feedback.pattern_detector import detect_patterns

    patterns = detect_patterns(hours=24)
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from pathlib import Path

from opentelemetry import trace

from devloop.paths import SESSIONS_DIR as _SESSIONS_DIR

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("feedback.pattern_detector", "0.1.0")

# ---------------------------------------------------------------------------
# Known failure patterns and their suggested fixes
# ---------------------------------------------------------------------------

_PATTERN_FIXES: dict[str, str] = {
    "gate_0_sanity": (
        "Agents are failing sanity checks repeatedly. Consider adding to CLAUDE.md: "
        "'Always run the test suite before committing. If tests fail, fix them before proceeding.'"
    ),
    "gate_2_secrets": (
        "Agents are leaking secrets. Add to CLAUDE.md: "
        "'Never include API keys, tokens, passwords, or credentials in source code. "
        "Use environment variables or config files excluded from version control.'"
    ),
    "gate_3_security": (
        "Agents are producing vulnerable code. Add to CLAUDE.md: "
        "'Use parameterized queries for all database operations. Never use string "
        "interpolation in SQL, shell commands, or HTML output.'"
    ),
    "gate_4_review": (
        "Code review is catching quality issues. Check review criteria in "
        "config/review-gate.yaml — the criteria may need to be communicated "
        "to agents via CLAUDE.md overlay."
    ),
    "gate_05_relevance": (
        "Agents are doing work that doesn't match the ticket. Add to CLAUDE.md: "
        "'Only modify code directly related to the issue description. Do not refactor, "
        "reorganize, or improve code outside the scope of the issue.'"
    ),
    "gate_25_dangerous_ops": (
        "Agents are making dangerous changes (migrations, CI, auth). Add to CLAUDE.md: "
        "'Do not modify database migrations, CI/CD configs, Dockerfiles, or "
        "authentication/authorization code unless the issue explicitly requires it.'"
    ),
}


def detect_patterns(hours: int = 24, threshold: int = 3) -> dict:
    """Scan recent session metadata for repeated failure patterns.

    Looks at gate_failure fields in .meta.json files to find gates that
    are failing repeatedly, suggesting a systemic issue.

    Args:
        hours: Look back this many hours.
        threshold: Minimum failures of the same gate to flag as a pattern.

    Returns:
        Dict with detected patterns, suggestions, and raw failure counts.
    """
    import time

    with tracer.start_as_current_span(
        "feedback.pattern_detector.detect_patterns",
        attributes={
            "pattern.hours_lookback": hours,
            "pattern.threshold": threshold,
        },
    ) as span:
        cutoff = time.time() - (hours * 3600)
        gate_failures: Counter[str] = Counter()
        total_runs = 0

        if not _SESSIONS_DIR.is_dir():
            return {
                "patterns_found": 0,
                "patterns": [],
                "gate_failure_counts": {},
                "total_runs": 0,
                "hours": hours,
            }

        for meta_path in _SESSIONS_DIR.glob("*.meta.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue

            ts = meta.get("timestamp", 0)
            if ts < cutoff:
                continue

            total_runs += 1
            gate_failure = meta.get("gate_failure")
            if gate_failure:
                # Normalize gate name: "Gate 0 (sanity)" -> "gate_0_sanity"
                normalized = _normalize_gate_name(gate_failure)
                gate_failures[normalized] += 1

        # Find patterns above threshold
        patterns: list[dict] = []
        for gate, count in gate_failures.most_common():
            if count >= threshold:
                fix = _PATTERN_FIXES.get(gate, f"Gate '{gate}' is failing repeatedly. Investigate.")
                patterns.append({
                    "gate": gate,
                    "failure_count": count,
                    "percentage": round(100.0 * count / total_runs, 1) if total_runs > 0 else 0,
                    "suggested_fix": fix,
                })

        span.set_attribute("pattern.total_runs", total_runs)
        span.set_attribute("pattern.patterns_found", len(patterns))

        return {
            "patterns_found": len(patterns),
            "patterns": patterns,
            "gate_failure_counts": dict(gate_failures),
            "total_runs": total_runs,
            "hours": hours,
        }


def _normalize_gate_name(gate_str: str) -> str:
    """Normalize gate failure strings to consistent keys.

    'Gate 0 (sanity)' -> 'gate_0_sanity'
    'gate_3_security' -> 'gate_3_security' (already normalized)
    """
    s = gate_str.lower().strip()
    # Handle "Gate N (name)" format
    if s.startswith("gate "):
        s = s.replace("gate ", "gate_").replace(" (", "_").rstrip(")")
    # Handle "Gate N.5" -> "gate_05"
    s = s.replace(".", "")
    # Clean up any remaining spaces
    s = s.replace(" ", "_")
    return s
