"""Post-pipeline feedback channels — run after each TB completes."""

import logging

from opentelemetry import trace

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("feedback.post_pipeline", "0.1.0")


def run_post_pipeline(
    issue_id: str,
    session_events: list[dict] | None = None,
    success: bool = False,
) -> dict:
    """Run all post-pipeline feedback channels. Non-blocking, best-effort."""
    results = {}

    with tracer.start_as_current_span(
        "feedback.post_pipeline",
        attributes={"issue.id": issue_id, "pipeline.success": success},
    ) as span:
        # Channel 2: Pattern Detection
        try:
            from devloop.feedback.pattern_detector import detect_patterns

            results["patterns"] = detect_patterns(hours=1)
            span.set_attribute(
                "post_pipeline.patterns_found",
                results["patterns"].get("patterns_found", 0),
            )
        except Exception as exc:
            logger.debug("Pattern detection skipped: %s", exc)

        # Channel 3: Cost Monitor
        try:
            from devloop.feedback.cost_monitor import check_budget, get_usage_summary

            summary = get_usage_summary(hours=24)
            budget = check_budget(summary)
            results["cost"] = {"summary": summary, "budget": budget}
            span.set_attribute(
                "post_pipeline.cost_pause_recommended",
                budget.get("pause_recommended", False),
            )
            span.set_attribute(
                "post_pipeline.cost_warnings_count",
                len(budget.get("warnings", [])),
            )
        except Exception as exc:
            logger.debug("Cost monitor skipped: %s", exc)

        # Channel 7: Efficiency (only if session data available)
        if session_events:
            try:
                from devloop.feedback.efficiency import analyze_efficiency

                results["efficiency"] = analyze_efficiency(session_events)
                span.set_attribute(
                    "post_pipeline.efficiency_score",
                    results["efficiency"].get("score", 0),
                )
                span.set_attribute(
                    "post_pipeline.efficiency_waste_ratio",
                    results["efficiency"].get("waste_ratio", 0.0),
                )
            except Exception as exc:
                logger.debug("Efficiency analysis skipped: %s", exc)

        span.set_attribute("post_pipeline.channels_run", len(results))

    return results
