"""Poll beads for ready issues — replaces Linear polling.

br ready --json returns all unblocked, non-deferred issues.
This is the intake layer's primary data source.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from opentelemetry import trace

logger = logging.getLogger(__name__)

tracer = trace.get_tracer("intake", "0.1.0")

# dev-loop project root — beads workspace lives here (.beads/)
_DEVLOOP_ROOT = str(Path(__file__).resolve().parents[3])


class BeadsUnavailable(Exception):
    """Raised when the br CLI is not installed or not functional."""
    pass


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
        """Extract target repo from labels (e.g., 'repo:OOTestProject1')."""
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


def claim_issue(issue_id: str, repo_path: str | None = None) -> bool:
    """Atomically claim an issue via `br update --claim`.

    Uses br's --claim flag which sets assignee + status=in_progress
    in a single operation. Detects whether the status actually
    transitioned by checking for "status:" in the output — if the
    issue was already in_progress, br succeeds but reports no
    transition.

    Returns True if this call claimed the issue (status transitioned),
    False if it was already claimed or the command failed.
    """
    with tracer.start_as_current_span(
        "intake.claim_issue",
        attributes={"intake.issue_id": issue_id, "intake.repo_path": repo_path or _DEVLOOP_ROOT},
    ) as span:
        cwd = repo_path or _DEVLOOP_ROOT
        try:
            result = subprocess.run(
                ["br", "update", issue_id, "--claim"],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
                cwd=cwd,
            )
        except subprocess.TimeoutExpired:
            logger.error("Timed out claiming issue %s", issue_id)
            span.set_attribute("intake.claimed", False)
            return False
        if result.returncode != 0:
            logger.warning(
                "Failed to claim issue %s: %s",
                issue_id,
                result.stderr.strip(),
            )
            span.set_attribute("intake.claimed", False)
            return False

        # br prints "status: open → in_progress" only when the status
        # actually changed. If already in_progress, no transition line.
        output = result.stdout + result.stderr
        if "status:" in output and "in_progress" in output:
            logger.info("Claimed issue %s", issue_id)
            span.set_attribute("intake.claimed", True)
            return True

        logger.debug(
            "Issue %s was already claimed (no status transition)",
            issue_id,
        )
        span.set_attribute("intake.claimed", False)
        return False


def get_issue(issue_id: str, repo_path: str | None = None) -> WorkItem | None:
    """Fetch a single issue by ID via ``br show <id> --json``.

    Returns a WorkItem or None on failure.  This is used as a fallback
    when an issue isn't found in the ``poll_ready()`` results (e.g. because
    it was already claimed or is in a non-ready state).
    """
    with tracer.start_as_current_span(
        "intake.get_issue",
        attributes={"intake.issue_id": issue_id, "intake.repo_path": repo_path or _DEVLOOP_ROOT},
    ) as span:
        cwd = repo_path or _DEVLOOP_ROOT
        try:
            result = subprocess.run(
                ["br", "show", issue_id, "--json"],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
                cwd=cwd,
            )
        except subprocess.TimeoutExpired:
            logger.error("Timed out fetching issue %s", issue_id)
            span.set_attribute("intake.found", False)
            return None
        if result.returncode != 0:
            logger.warning(
                "br show %s failed (exit %d): %s",
                issue_id,
                result.returncode,
                result.stderr.strip(),
            )
            span.set_attribute("intake.found", False)
            return None

        try:
            data = json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError):
            logger.error("Failed to parse br show JSON for %s", issue_id)
            span.set_attribute("intake.found", False)
            return None

        # br show --json returns a list with one element
        issue = data[0] if isinstance(data, list) and data else data
        if not isinstance(issue, dict):
            span.set_attribute("intake.found", False)
            return None

        span.set_attribute("intake.found", True)
        return WorkItem(
            id=issue.get("id", issue_id),
            title=issue.get("title", issue_id),
            type=issue.get("issue_type", issue.get("type", "task")),
            priority=issue.get("priority", 2),
            labels=issue.get("labels", []),
            description=issue.get("description"),
            parent=issue.get("parent"),
        )


def poll_ready(*, repo_path: str | None = None, fail_on_missing: bool = False) -> list[WorkItem]:
    """Poll beads for ready issues. Returns WorkItems sorted by priority.

    Args:
        repo_path: Directory containing .beads/ (auto-discovers if None).
        fail_on_missing: If True, raise BeadsUnavailable when br CLI is not
            found on PATH. If False (default), return an empty list silently.
    """
    with tracer.start_as_current_span(
        "intake.poll_ready",
        attributes={"intake.repo_path": repo_path or _DEVLOOP_ROOT},
    ) as span:
        br_path = shutil.which("br")
        if br_path is None:
            msg = "br CLI not found on PATH. Install: br upgrade"
            logger.error(msg)
            span.set_attribute("intake.ready_count", 0)
            if fail_on_missing:
                raise BeadsUnavailable(msg)
            return []
        cwd = repo_path or _DEVLOOP_ROOT
        try:
            result = subprocess.run(
                ["br", "ready", "--json"],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
                cwd=cwd,
            )
        except subprocess.TimeoutExpired:
            logger.error("Timed out polling br ready")
            span.set_attribute("intake.ready_count", 0)
            return []
        if result.returncode != 0:
            logger.warning("br ready failed (exit %d): %s", result.returncode, result.stderr.strip())
            span.set_attribute("intake.ready_count", 0)
            return []

        try:
            issues = json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError):
            logger.error(
                "Failed to parse br ready JSON: %s",
                result.stdout[:200] if result.stdout else "(empty)",
            )
            span.set_attribute("intake.ready_count", 0)
            return []

        items = [
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
        span.set_attribute("intake.ready_count", len(items))
        return items
