"""Poll beads for ready issues — replaces Linear polling.

br ready --json returns all unblocked, non-deferred issues.
This is the intake layer's primary data source.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class WorkItem:
    """A work item from beads, ready for orchestration."""

    id: str
    title: str
    type: str
    priority: int
    labels: list[str]
    description: str | None = None
    parent: str | None = None

    @property
    def target_repo(self) -> str | None:
        """Extract target repo from labels (e.g., 'repo:prompt-bench')."""
        for label in self.labels:
            if label.startswith("repo:"):
                return label.removeprefix("repo:")
        return None

    @property
    def persona(self) -> str | None:
        """Map labels to agent persona."""
        label_to_persona = {
            "bug": "bug-fix",
            "feature": "feature",
            "refactor": "refactor",
            "security": "security-fix",
            "docs": "docs",
        }
        for label in self.labels:
            if label in label_to_persona:
                return label_to_persona[label]
        return None


def claim_issue(issue_id: str) -> bool:
    """Atomically claim an issue via `br update --claim`.

    Uses br's --claim flag which sets assignee + status=in_progress
    in a single operation. Detects whether the status actually
    transitioned by checking for "status:" in the output — if the
    issue was already in_progress, br succeeds but reports no
    transition.

    Returns True if this call claimed the issue (status transitioned),
    False if it was already claimed or the command failed.
    """
    try:
        result = subprocess.run(
            ["br", "update", issue_id, "--claim"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        logger.error("Timed out claiming issue %s", issue_id)
        return False
    if result.returncode != 0:
        logger.warning(
            "Failed to claim issue %s: %s",
            issue_id,
            result.stderr.strip(),
        )
        return False

    # br prints "status: open → in_progress" only when the status
    # actually changed. If already in_progress, no transition line.
    output = result.stdout + result.stderr
    if "status:" in output and "in_progress" in output:
        logger.info("Claimed issue %s", issue_id)
        return True

    logger.debug(
        "Issue %s was already claimed (no status transition)",
        issue_id,
    )
    return False


def get_issue(issue_id: str) -> WorkItem | None:
    """Fetch a single issue by ID via ``br show <id> --json``.

    Returns a WorkItem or None on failure.  This is used as a fallback
    when an issue isn't found in the ``poll_ready()`` results (e.g. because
    it was already claimed or is in a non-ready state).
    """
    try:
        result = subprocess.run(
            ["br", "show", issue_id, "--json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        logger.error("Timed out fetching issue %s", issue_id)
        return None
    if result.returncode != 0:
        logger.warning(
            "br show %s failed (exit %d): %s",
            issue_id,
            result.returncode,
            result.stderr.strip(),
        )
        return None

    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        logger.error("Failed to parse br show JSON for %s", issue_id)
        return None

    # br show --json returns a list with one element
    issue = data[0] if isinstance(data, list) and data else data
    if not isinstance(issue, dict):
        return None

    return WorkItem(
        id=issue.get("id", issue_id),
        title=issue.get("title", issue_id),
        type=issue.get("issue_type", issue.get("type", "task")),
        priority=issue.get("priority", 2),
        labels=issue.get("labels", []),
        description=issue.get("description"),
        parent=issue.get("parent"),
    )


def poll_ready() -> list[WorkItem]:
    """Poll beads for ready issues. Returns WorkItems sorted by priority."""
    try:
        result = subprocess.run(
            ["br", "ready", "--json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        logger.error("Timed out polling br ready")
        return []
    if result.returncode != 0:
        logger.warning("br ready failed (exit %d): %s", result.returncode, result.stderr.strip())
        return []

    try:
        issues = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        logger.error(
            "Failed to parse br ready JSON: %s",
            result.stdout[:200] if result.stdout else "(empty)",
        )
        return []

    return [
        WorkItem(
            id=issue["id"],
            title=issue["title"],
            type=issue.get("type", "task"),
            priority=issue.get("priority", 2),
            labels=issue.get("labels", []),
            description=issue.get("description"),
            parent=issue.get("parent"),
        )
        for issue in issues
    ]
