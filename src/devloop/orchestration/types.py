"""Pydantic models for the orchestration layer MCP server."""

from __future__ import annotations

from pydantic import BaseModel, Field


class WorktreeInfo(BaseModel):
    """Metadata about a created git worktree for an issue."""

    issue_id: str
    repo_path: str
    worktree_path: str
    branch_name: str
    created_at: str
    success: bool
    message: str


class PersonaConfig(BaseModel):
    """Agent persona configuration matched from labels."""

    name: str
    labels: list[str] = Field(default_factory=list)
    claude_md_overlay: str = ""
    cost_ceiling_default: float = 1.00
    retry_max: int = 1
    model: str = "sonnet"


class ClaudeOverlay(BaseModel):
    """Generated CLAUDE.md overlay combining persona + issue context."""

    persona: str
    issue_title: str
    overlay_text: str


class CleanupResult(BaseModel):
    """Result of cleaning up a worktree."""

    issue_id: str
    worktree_removed: bool
    branch_removed: bool
    success: bool
    message: str
