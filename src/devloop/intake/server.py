"""Beads-intake MCP server — exposes beads (br) issue tracking via MCP tools.

This is Layer 1 of the dev-loop harness. It bridges the beads CLI into the
MCP protocol so orchestration agents can poll, inspect, and mutate issues
without shelling out directly.

Run standalone:  uv run python -m devloop.intake.server
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from fastmcp import FastMCP
from opentelemetry import trace

from devloop.intake.beads_poller import WorkItem, poll_ready
from devloop.intake.types import (
    CommentAddResult,
    IssueDetail,
    StatusUpdateResult,
    WorkItemModel,
)

# ---------------------------------------------------------------------------
# OTel tracer for intake layer
# ---------------------------------------------------------------------------

tracer = trace.get_tracer("intake", "0.1.0")

# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="beads-intake",
    instructions=(
        "Beads issue-tracking intake layer for dev-loop. "
        "Use these tools to poll ready issues, inspect details, "
        "update statuses, and add comments via the beads (br) CLI."
    ),
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_STATUSES = frozenset({
    "open",
    "in_progress",
    "review",
    "done",
    "closed",
    "blocked",
    "deferred",
})


def _workitem_to_model(item: WorkItem) -> WorkItemModel:
    """Convert a dataclass WorkItem to its Pydantic equivalent."""
    return WorkItemModel(
        id=item.id,
        title=item.title,
        type=item.type,
        priority=item.priority,
        labels=item.labels,
        description=item.description,
        parent=item.parent,
        target_repo=item.target_repo,
        persona=item.persona,
    )


_DEVLOOP_ROOT = str(Path(__file__).resolve().parents[3])


def _run_br(*args: str, check: bool = False, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run a br CLI command and return the result."""
    return subprocess.run(
        ["br", *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=30,
        cwd=cwd or _DEVLOOP_ROOT,
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool(
    description=(
        "Poll beads for all ready (unblocked, non-deferred) issues. "
        "Returns a JSON list of WorkItems sorted by priority."
    ),
    tags={"intake", "poll"},
)
def poll_ready_issues() -> list[dict]:
    """Poll beads for ready issues and return them as JSON-serializable dicts."""
    with tracer.start_as_current_span(
        "intake.poll_ready_issues",
        attributes={"intake.operation": "poll"},
    ) as span:
        items = poll_ready()
        models = [_workitem_to_model(item) for item in items]

        span.set_attribute("intake.issue_count", len(models))
        if models:
            span.set_attribute(
                "intake.issue_ids",
                [m.id for m in models],
            )
            span.set_attribute(
                "intake.repos",
                list({m.target_repo for m in models if m.target_repo}),
            )

        return [m.model_dump() for m in models]


@mcp.tool(
    description=(
        "Get full details for a specific beads issue by ID. "
        "Returns the complete issue object including description, "
        "comments, labels, and metadata."
    ),
    tags={"intake", "read"},
)
def get_issue_detail(issue_id: str) -> dict:
    """Run `br show <id> --format json` and return the parsed issue."""
    with tracer.start_as_current_span(
        "intake.get_issue_detail",
        attributes={
            "intake.operation": "show",
            "issue.id": issue_id,
        },
    ) as span:
        result = _run_br("show", issue_id, "--format", "json")

        if result.returncode != 0:
            error_msg = (
                result.stderr.strip()
                or f"br show failed with exit code {result.returncode}"
            )
            span.set_status(trace.StatusCode.ERROR, error_msg)
            span.set_attribute("error.message", error_msg)
            return {"error": error_msg, "issue_id": issue_id}

        raw = json.loads(result.stdout)

        # br show --format json may return a list (even for single ID) or a dict
        if isinstance(raw, list):
            if not raw:
                span.set_status(trace.StatusCode.ERROR, "Issue not found")
                return {"error": "Issue not found", "issue_id": issue_id}
            raw = raw[0]

        detail = IssueDetail(
            id=raw.get("id", issue_id),
            title=raw.get("title", ""),
            status=raw.get("status", "unknown"),
            type=raw.get("type", "task"),
            priority=raw.get("priority", 2),
            labels=raw.get("labels", []),
            description=raw.get("description"),
            parent=raw.get("parent"),
            assignee=raw.get("assignee"),
            owner=raw.get("owner"),
            created_at=raw.get("created_at"),
            updated_at=raw.get("updated_at"),
            comments=raw.get("comments"),
            raw=raw,
        )

        span.set_attribute("issue.status", detail.status)
        span.set_attribute("issue.priority", detail.priority)
        span.set_attribute("issue.labels", detail.labels)
        if detail.assignee:
            span.set_attribute("issue.assignee", detail.assignee)

        return detail.model_dump()


@mcp.tool(
    description=(
        "Update the status of a beads issue. "
        "Valid statuses: open, in_progress, review, done, closed, blocked, deferred."
    ),
    tags={"intake", "write"},
)
def update_issue_status(issue_id: str, status: str) -> dict:
    """Run `br update <id> --status <status>` to change an issue's status."""
    with tracer.start_as_current_span(
        "intake.update_issue_status",
        attributes={
            "intake.operation": "update_status",
            "issue.id": issue_id,
            "issue.new_status": status,
        },
    ) as span:
        status_lower = status.lower().strip()

        if status_lower not in VALID_STATUSES:
            error_msg = (
                f"Invalid status '{status}'. "
                f"Valid statuses: {', '.join(sorted(VALID_STATUSES))}"
            )
            span.set_status(trace.StatusCode.ERROR, error_msg)
            return StatusUpdateResult(
                issue_id=issue_id,
                new_status=status,
                success=False,
                message=error_msg,
            ).model_dump()

        result = _run_br("update", issue_id, "--status", status_lower)

        if result.returncode != 0:
            error_msg = (
                result.stderr.strip()
                or f"br update failed with exit code {result.returncode}"
            )
            span.set_status(trace.StatusCode.ERROR, error_msg)
            return StatusUpdateResult(
                issue_id=issue_id,
                new_status=status_lower,
                success=False,
                message=error_msg,
            ).model_dump()

        span.set_status(trace.StatusCode.OK)
        return StatusUpdateResult(
            issue_id=issue_id,
            new_status=status_lower,
            success=True,
            message=f"Issue {issue_id} status updated to '{status_lower}'",
        ).model_dump()


@mcp.tool(
    description=(
        "Add a comment to a beads issue. The comment is appended to the "
        "issue's comment history with the current actor as author."
    ),
    tags={"intake", "write"},
)
def add_issue_comment(issue_id: str, comment: str) -> dict:
    """Run `br comments add <id> --message <comment>` to append a comment."""
    with tracer.start_as_current_span(
        "intake.add_issue_comment",
        attributes={
            "intake.operation": "add_comment",
            "issue.id": issue_id,
            "comment.length": len(comment),
        },
    ) as span:
        if not comment.strip():
            error_msg = "Comment text cannot be empty"
            span.set_status(trace.StatusCode.ERROR, error_msg)
            return CommentAddResult(
                issue_id=issue_id,
                success=False,
                message=error_msg,
            ).model_dump()

        result = _run_br("comments", "add", issue_id, "--message", comment)

        if result.returncode != 0:
            error_msg = (
                result.stderr.strip()
                or f"br comments add failed with exit code {result.returncode}"
            )
            span.set_status(trace.StatusCode.ERROR, error_msg)
            return CommentAddResult(
                issue_id=issue_id,
                success=False,
                message=error_msg,
            ).model_dump()

        span.set_status(trace.StatusCode.OK)
        return CommentAddResult(
            issue_id=issue_id,
            success=True,
            message=f"Comment added to issue {issue_id}",
        ).model_dump()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
