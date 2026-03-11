"""Pydantic models for the intake layer MCP server."""

from __future__ import annotations

from pydantic import BaseModel, Field


class WorkItemModel(BaseModel):
    """A work item from beads, ready for orchestration."""

    id: str
    title: str
    type: str = "task"
    priority: int = Field(default=2, ge=0, le=4)
    labels: list[str] = Field(default_factory=list)
    description: str | None = None
    parent: str | None = None
    target_repo: str | None = None
    persona: str | None = None


class IssueDetail(BaseModel):
    """Full issue detail from `br show --format json`."""

    id: str
    title: str
    status: str
    type: str = "task"
    priority: int = 2
    labels: list[str] = Field(default_factory=list)
    description: str | None = None
    parent: str | None = None
    assignee: str | None = None
    owner: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    comments: list[dict] | None = None
    # Catch-all for any extra fields br returns
    raw: dict = Field(default_factory=dict)


class StatusUpdateResult(BaseModel):
    """Result of updating an issue's status."""

    issue_id: str
    new_status: str
    success: bool
    message: str


class CommentAddResult(BaseModel):
    """Result of adding a comment to an issue."""

    issue_id: str
    success: bool
    message: str
