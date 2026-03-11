"""Agent Runtime MCP server — spawns and manages Claude Code agents in worktrees.

This is Layer 3 of the dev-loop harness. It launches Claude Code in headless
mode (--print) inside a git worktree, captures the output, and returns the
result.

TB-1: synchronous subprocess.run — one agent at a time, blocking.
TB-2+: async spawning, streaming NDJSON, kill switch on cost ceiling.

Run standalone:  uv run python -m devloop.runtime.server
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

from fastmcp import FastMCP
from opentelemetry import trace

from devloop.runtime.types import AgentConfig, AgentResult

# ---------------------------------------------------------------------------
# OTel tracer for runtime layer
# ---------------------------------------------------------------------------

tracer = trace.get_tracer("runtime", "0.1.0")

# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="agent-runtime",
    instructions=(
        "Agent runtime layer for dev-loop. "
        "Use these tools to spawn Claude Code agents in git worktrees, "
        "kill running agents, and retrieve agent output."
    ),
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_ALLOWED_TOOLS = [
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "Bash",
]

DEFAULT_TIMEOUT_SECONDS = 300.0  # 5 minutes for TB-1
DEFAULT_COST_CEILING = 2.0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_claude_cli() -> str:
    """Locate the claude CLI on PATH, raising if not found."""
    path = shutil.which("claude")
    if path is None:
        raise FileNotFoundError(
            "claude CLI not found on PATH. "
            "Install it: https://docs.anthropic.com/en/docs/claude-code"
        )
    return path


def _build_command(
    claude_path: str,
    config: AgentConfig,
) -> list[str]:
    """Build the subprocess command list for claude --print."""
    cmd = [
        claude_path,
        "--print",
        "--cwd",
        config.worktree_path,
        "--model",
        config.model,
    ]

    tools = config.allowed_tools if config.allowed_tools else DEFAULT_ALLOWED_TOOLS
    cmd.extend(["--allowedTools", ",".join(tools)])

    cmd.extend(["--message", config.task_prompt])

    return cmd


def _run_agent(config: AgentConfig) -> AgentResult:
    """Execute a Claude Code agent synchronously and return the result.

    This is the TB-1 implementation: blocking subprocess.run with timeout.
    """
    claude_path = _find_claude_cli()
    cmd = _build_command(claude_path, config)

    # Ensure ANTHROPIC_API_KEY is in the environment
    env = os.environ.copy()
    if "ANTHROPIC_API_KEY" not in env:
        raise OSError(
            "ANTHROPIC_API_KEY not set in environment. "
            "The claude CLI needs this to authenticate with the Anthropic API."
        )

    start = time.monotonic()

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=config.timeout_seconds,
            cwd=config.worktree_path,
            env=env,
        )
        elapsed = time.monotonic() - start

        return AgentResult(
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            pid=None,  # subprocess.run doesn't expose PID after completion
            duration_seconds=round(elapsed, 2),
            timed_out=False,
            worktree_path=config.worktree_path,
            model=config.model,
        )

    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - start
        return AgentResult(
            exit_code=-1,
            stdout=(
                exc.stdout or ""
                if isinstance(exc.stdout, str)
                else (exc.stdout or b"").decode(errors="replace")
            ),
            stderr=(
                exc.stderr or ""
                if isinstance(exc.stderr, str)
                else (exc.stderr or b"").decode(errors="replace")
            ),
            pid=None,
            duration_seconds=round(elapsed, 2),
            timed_out=True,
            worktree_path=config.worktree_path,
            model=config.model,
        )


def _find_session_output(worktree_path: str) -> str | None:
    """Look for the latest agent output in a worktree's .claude/ directory.

    Claude Code writes session data under .claude/ in the working directory.
    This function attempts to find and return the most recent session output.
    """
    claude_dir = Path(worktree_path) / ".claude"
    if not claude_dir.is_dir():
        return None

    # Look for session files — Claude Code stores them as JSON
    session_files = sorted(
        claude_dir.glob("**/*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    if not session_files:
        return None

    # Return the content of the most recent session file
    latest = session_files[0]
    try:
        return latest.read_text(encoding="utf-8")
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool(
    description=(
        "Spawn a Claude Code agent in a git worktree. "
        "Runs synchronously (blocking) and returns the agent's output and exit code. "
        "The agent runs in --print mode with the given task prompt. "
        "TB-1: subprocess.run with timeout. No streaming, no cost kill-switch."
    ),
    tags={"runtime", "spawn"},
)
def spawn_agent(
    worktree_path: str,
    task_prompt: str,
    model: str = "sonnet",
    allowed_tools: list[str] | None = None,
    cost_ceiling: float = DEFAULT_COST_CEILING,
) -> dict:
    """Spawn a Claude Code agent in a worktree and return its output."""
    config = AgentConfig(
        worktree_path=worktree_path,
        task_prompt=task_prompt,
        model=model,
        allowed_tools=allowed_tools,
        cost_ceiling=cost_ceiling,
    )

    with tracer.start_as_current_span(
        "runtime.spawn_agent",
        attributes={
            "runtime.model": config.model,
            "runtime.worktree_path": config.worktree_path,
            "runtime.cost_ceiling": config.cost_ceiling,
            "runtime.timeout_seconds": config.timeout_seconds,
            "runtime.allowed_tools": ",".join(
                config.allowed_tools or DEFAULT_ALLOWED_TOOLS
            ),
        },
    ) as span:
        # Validate worktree path exists
        wt = Path(config.worktree_path)
        if not wt.is_dir():
            error_msg = f"Worktree path does not exist: {config.worktree_path}"
            span.set_status(trace.StatusCode.ERROR, error_msg)
            return AgentResult(
                exit_code=-1,
                stderr=error_msg,
                worktree_path=config.worktree_path,
                model=config.model,
            ).model_dump()

        try:
            result = _run_agent(config)
        except FileNotFoundError as exc:
            error_msg = str(exc)
            span.set_status(trace.StatusCode.ERROR, error_msg)
            return AgentResult(
                exit_code=-1,
                stderr=error_msg,
                worktree_path=config.worktree_path,
                model=config.model,
            ).model_dump()
        except OSError as exc:
            error_msg = str(exc)
            span.set_status(trace.StatusCode.ERROR, error_msg)
            return AgentResult(
                exit_code=-1,
                stderr=error_msg,
                worktree_path=config.worktree_path,
                model=config.model,
            ).model_dump()

        # Record result attributes on the span
        span.set_attribute("runtime.exit_code", result.exit_code)
        span.set_attribute("runtime.duration_seconds", result.duration_seconds)
        span.set_attribute("runtime.output_length", len(result.stdout))
        span.set_attribute("runtime.timed_out", result.timed_out)

        if result.exit_code == 0:
            span.set_status(trace.StatusCode.OK)
        else:
            span.set_status(
                trace.StatusCode.ERROR,
                f"Agent exited with code {result.exit_code}"
                + (" (timed out)" if result.timed_out else ""),
            )

        return result.model_dump()


@mcp.tool(
    description=(
        "Send SIGTERM to a running agent process by PID. "
        "Use this to kill a hung or over-budget agent. "
        "Returns success/failure and any error message."
    ),
    tags={"runtime", "kill"},
)
def kill_agent(pid: int) -> dict:
    """Send SIGTERM to a running agent process."""
    with tracer.start_as_current_span(
        "runtime.kill_agent",
        attributes={
            "runtime.target_pid": pid,
        },
    ) as span:
        try:
            os.kill(pid, signal.SIGTERM)
            span.set_status(trace.StatusCode.OK)
            return {
                "success": True,
                "pid": pid,
                "signal": "SIGTERM",
                "message": f"SIGTERM sent to process {pid}",
            }
        except ProcessLookupError:
            error_msg = f"No process found with PID {pid}"
            span.set_status(trace.StatusCode.ERROR, error_msg)
            return {
                "success": False,
                "pid": pid,
                "signal": "SIGTERM",
                "message": error_msg,
            }
        except PermissionError:
            error_msg = f"Permission denied sending SIGTERM to PID {pid}"
            span.set_status(trace.StatusCode.ERROR, error_msg)
            return {
                "success": False,
                "pid": pid,
                "signal": "SIGTERM",
                "message": error_msg,
            }


@mcp.tool(
    description=(
        "Read the latest agent output from a worktree. "
        "Checks for .claude/ session files or other output artifacts. "
        "Returns the content if found, or an error if no output exists."
    ),
    tags={"runtime", "read"},
)
def get_agent_output(worktree_path: str) -> dict:
    """Read the latest agent output from a worktree's .claude/ directory."""
    with tracer.start_as_current_span(
        "runtime.get_agent_output",
        attributes={
            "runtime.worktree_path": worktree_path,
        },
    ) as span:
        wt = Path(worktree_path)
        if not wt.is_dir():
            error_msg = f"Worktree path does not exist: {worktree_path}"
            span.set_status(trace.StatusCode.ERROR, error_msg)
            return {
                "success": False,
                "worktree_path": worktree_path,
                "output": None,
                "message": error_msg,
            }

        output = _find_session_output(worktree_path)

        if output is None:
            span.set_attribute("runtime.output_found", False)
            span.set_status(trace.StatusCode.OK)
            return {
                "success": True,
                "worktree_path": worktree_path,
                "output": None,
                "message": "No agent output found in .claude/ directory",
            }

        span.set_attribute("runtime.output_found", True)
        span.set_attribute("runtime.output_length", len(output))
        span.set_status(trace.StatusCode.OK)
        return {
            "success": True,
            "worktree_path": worktree_path,
            "output": output,
            "message": "Latest agent output retrieved",
        }


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
