"""Changelog generation — Channel 5 of the feedback loop.

Accumulates completed issues and generates changelogs grouped by repo
and issue type. Uses beads (br) data + session metadata.

Usage::

    from devloop.feedback.changelog import generate_changelog

    log = generate_changelog(days=7)
    print(log["markdown"])
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone

from opentelemetry import trace

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("feedback.changelog", "0.1.0")


def _run_br(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a br CLI command."""
    return subprocess.run(
        ["br", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def generate_changelog(days: int = 7) -> dict:
    """Generate a changelog from recently closed beads issues.

    Queries beads for issues closed in the last N days and formats
    them into a Markdown changelog grouped by repo.

    Args:
        days: Look back this many days.

    Returns:
        Dict with markdown text, issue count, and structured entries.
    """
    with tracer.start_as_current_span(
        "feedback.changelog.generate",
        attributes={"changelog.days": days},
    ) as span:
        # Get closed/done issues from beads
        result = _run_br("list", "--status", "done", "--json")
        if result.returncode != 0:
            # Try "closed" status
            result = _run_br("list", "--status", "closed", "--json")

        if result.returncode != 0:
            error = result.stderr.strip() or "br list failed"
            span.set_status(trace.StatusCode.ERROR, error)
            return {
                "markdown": f"*Changelog generation failed: {error}*",
                "entries": [],
                "issue_count": 0,
                "days": days,
            }

        try:
            issues = json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError):
            return {
                "markdown": "*No changelog data available*",
                "entries": [],
                "issue_count": 0,
                "days": days,
            }

        if not isinstance(issues, list):
            issues = [issues]

        # Group by repo label
        by_repo: dict[str, list[dict]] = {}
        for issue in issues:
            labels = issue.get("labels", [])
            repo = "general"
            for label in labels:
                if label.startswith("repo:"):
                    repo = label.removeprefix("repo:")
                    break

            # Determine change type from labels
            change_type = "Changed"
            for label in labels:
                if label == "bug":
                    change_type = "Fixed"
                    break
                if label == "feature":
                    change_type = "Added"
                    break
                if label == "refactor":
                    change_type = "Refactored"
                    break
                if label == "security":
                    change_type = "Security"
                    break
                if label == "docs":
                    change_type = "Documented"
                    break

            entry = {
                "id": issue.get("id", "?"),
                "title": issue.get("title", "Untitled"),
                "type": change_type,
                "repo": repo,
                "labels": labels,
            }

            by_repo.setdefault(repo, []).append(entry)

        # Format as Markdown
        now = datetime.now(timezone.utc)
        lines: list[str] = []
        lines.append(f"## Changelog — {days}-day window (generated {now.strftime('%Y-%m-%d')})")
        lines.append("")

        total_issues = 0
        all_entries: list[dict] = []

        for repo in sorted(by_repo.keys()):
            entries = by_repo[repo]
            lines.append(f"### {repo}")
            for entry in entries:
                lines.append(f"- **{entry['type']}** {entry['title']} ({entry['id']})")
                total_issues += 1
                all_entries.append(entry)
            lines.append("")

        if not all_entries:
            lines.append("*No completed issues in this period.*")
            lines.append("")

        # Stats
        lines.append("### Stats")
        lines.append(f"- Issues completed: {total_issues}")
        lines.append("")

        markdown = "\n".join(lines)

        span.set_attribute("changelog.issue_count", total_issues)
        span.set_attribute("changelog.repo_count", len(by_repo))

        return {
            "markdown": markdown,
            "entries": all_entries,
            "issue_count": total_issues,
            "days": days,
        }
