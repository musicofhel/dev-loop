"""Pydantic models for the runtime layer MCP server."""

from __future__ import annotations

from pydantic import BaseModel, Field


class AgentConfig(BaseModel):
    """Configuration for spawning a Claude Code agent."""

    worktree_path: str = Field(
        description="Absolute path to the git worktree where the agent runs.",
    )
    task_prompt: str = Field(
        description="The task prompt to send to the agent.",
    )
    model: str = Field(
        default="sonnet",
        description="Model name passed to claude --model (e.g. sonnet, opus, haiku).",
    )
    allowed_tools: list[str] | None = Field(
        default=None,
        description=(
            "List of tools the agent is allowed to use. "
            "Defaults to None (all tools allowed). "
            "Example: ['Read', 'Write', 'Edit', 'Glob', 'Grep', 'Bash']."
        ),
    )
    max_turns: int | None = Field(
        default=None,
        description=(
            "Maximum agentic turns before the CLI stops. "
            "None means no limit (use persona default or CLI default)."
        ),
    )
    cost_ceiling: float = Field(
        default=2.0,
        ge=0.01,
        description="Maximum cost in USD before the agent is killed. TB-1 uses timeout only.",
    )
    timeout_seconds: float = Field(
        default=300.0,
        gt=0,
        description="Maximum wall-clock time in seconds before the agent is killed.",
    )


class AgentResult(BaseModel):
    """Result of a completed (or failed) agent run."""

    exit_code: int = Field(
        description="Exit code from the claude CLI process.",
    )
    stdout: str = Field(
        default="",
        description="Captured stdout from the agent (the agent's output in --print mode).",
    )
    stderr: str = Field(
        default="",
        description="Captured stderr from the agent.",
    )
    pid: int | None = Field(
        default=None,
        description="PID of the agent process (if available).",
    )
    duration_seconds: float = Field(
        default=0.0,
        ge=0,
        description="Wall-clock duration of the agent run in seconds.",
    )
    timed_out: bool = Field(
        default=False,
        description="True if the agent was killed due to timeout.",
    )
    worktree_path: str = Field(
        default="",
        description="Worktree path the agent ran in.",
    )
    model: str = Field(
        default="sonnet",
        description="Model used for the run.",
    )
    num_turns: int = Field(
        default=0,
        ge=0,
        description="Number of agentic turns used (parsed from --output-format json).",
    )
    input_tokens: int = Field(
        default=0,
        ge=0,
        description="Total input tokens consumed (parsed from --output-format json).",
    )
    output_tokens: int = Field(
        default=0,
        ge=0,
        description="Total output tokens consumed (parsed from --output-format json).",
    )
    context_pct: float = Field(
        default=0.0,
        ge=0,
        description="Estimated context window usage as a percentage (0-100+).",
    )
    context_limited: bool = Field(
        default=False,
        description="True if the agent exited at or above the context percentage threshold.",
    )
