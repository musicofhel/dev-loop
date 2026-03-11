"""Pydantic models for the feedback loop layer."""

from __future__ import annotations

from pydantic import BaseModel, Field

from devloop.gates.types import GateSuiteResult


class RetryPrompt(BaseModel):
    """A constructed retry prompt with failure context for the agent."""

    prompt_text: str = Field(
        description="The full retry prompt including all failure details.",
    )
    failure_count: int = Field(
        default=0,
        ge=0,
        description="Number of gate failures encoded in this prompt.",
    )


class RetryResult(BaseModel):
    """Result of a retry attempt (agent re-spawn + gate re-run)."""

    attempt: int = Field(
        description="Which retry attempt this was (1-indexed).",
    )
    max_retries: int = Field(
        description="Maximum retries allowed for this run.",
    )
    success: bool = Field(
        description="True if all gates passed after this retry.",
    )
    gate_results: GateSuiteResult | None = Field(
        default=None,
        description="Gate suite results from the retry run (None if agent failed to spawn).",
    )
    escalated: bool = Field(
        default=False,
        description="True if retries were exhausted and the issue was escalated to a human.",
    )
    agent_exit_code: int = Field(
        default=-1,
        description="Exit code from the agent process during this retry.",
    )
    error: str | None = Field(
        default=None,
        description="Error message if the retry failed at infrastructure level.",
    )


class EscalationResult(BaseModel):
    """Result of escalating a failed issue to a human."""

    issue_id: str
    success: bool
    status_updated: bool = False
    comment_added: bool = False
    attempts: int = 0
    message: str = ""


class TB1Result(BaseModel):
    """Result of a full TB-1 pipeline run."""

    issue_id: str
    repo_path: str
    success: bool
    phase: str = Field(
        description="Last phase completed (or failed at).",
    )
    worktree_path: str | None = None
    persona: str | None = None
    agent_exit_code: int | None = None
    gate_results: GateSuiteResult | None = None
    retries_used: int = 0
    max_retries: int = 0
    escalated: bool = False
    error: str | None = None
    duration_seconds: float = 0.0
