"""Agent Runtime MCP server — spawns and manages Claude Code agents in worktrees.

This is Layer 3 of the dev-loop harness. It launches Claude Code in headless
mode (--print) inside a git worktree, captures the output, and returns the
result.

TB-1: synchronous subprocess.run — one agent at a time, blocking.
TB-2+: async spawning, streaming NDJSON, kill switch on cost ceiling.

Run standalone:  uv run python -m devloop.runtime.server
"""

from __future__ import annotations

import json
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


class ClaudeCLINotFound(RuntimeError):
    """Raised when the claude CLI binary is not on PATH."""
    pass


def _find_claude_cli() -> str:
    """Locate the claude CLI on PATH, raising if not found."""
    path = shutil.which("claude")
    if path is None:
        raise ClaudeCLINotFound(
            "claude CLI not found on PATH.\n"
            "Install: https://docs.anthropic.com/en/docs/claude-code"
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
        "--dangerously-skip-permissions",
        "--output-format",
        "json",
        "--model",
        config.model,
    ]

    if config.max_turns is not None:
        cmd.extend(["--max-turns", str(config.max_turns)])

    tools = config.allowed_tools if config.allowed_tools else DEFAULT_ALLOWED_TOOLS
    cmd.extend(["--allowedTools", ",".join(tools)])

    return cmd


def _is_claude_process(pid: int) -> bool:
    """Check if PID belongs to a claude CLI process via /proc/{pid}/cmdline.

    Returns False if the process doesn't exist, isn't readable, or isn't
    a claude process. This prevents kill_agent from sending SIGTERM to
    unrelated processes that happen to reuse a stale PID (L1 fix).
    """
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        # /proc/PID/cmdline uses null bytes as separators
        return b"claude" in cmdline
    except (OSError, PermissionError):
        return False


def _parse_usage_from_output(stdout: str) -> dict:
    """Parse usage stats from ``--output-format json`` output.

    The Claude CLI emits either:
    - A JSON **array** on a single line: ``[{...}, {...}, ...]``
    - NDJSON with one object per line (older versions)

    Scans for the ``{"type":"result"}`` object and extracts ``num_turns``,
    ``input_tokens``, and ``output_tokens``.  Also tracks
    ``peak_input_tokens`` from the last assistant message, which
    approximates peak context window usage.

    Returns a dict with those keys (all defaulting to 0 on parse failure).
    """
    result: dict = {
        "num_turns": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "peak_input_tokens": 0,
    }

    # Collect all JSON objects — handle both array and NDJSON formats
    objects: list[dict] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            objects.extend(obj for obj in parsed if isinstance(obj, dict))
        elif isinstance(parsed, dict):
            objects.append(parsed)

    # Track peak input tokens from assistant messages (last turn = peak context)
    peak_input = 0
    for obj in objects:
        obj_type = obj.get("type", "")
        if obj_type == "assistant":
            msg = obj.get("message")
            msg_usage = (
                (msg.get("usage") if isinstance(msg, dict) else None)
                or obj.get("usage")
                or {}
            )
            turn_input = msg_usage.get("input_tokens", 0)
            if turn_input > peak_input:
                peak_input = turn_input

    for obj in objects:
        if obj.get("type") != "result":
            continue
        result["num_turns"] = obj.get("num_turns", 0)
        usage = obj.get("usage") or {}
        result["input_tokens"] = usage.get("input_tokens", 0)
        result["output_tokens"] = usage.get("output_tokens", 0)
        break

    result["peak_input_tokens"] = peak_input
    return result


# Model context window sizes (tokens)
_MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "opus": 200_000,
    "sonnet": 200_000,
    "haiku": 200_000,
}
_DEFAULT_CONTEXT_WINDOW = 200_000


def _estimate_context_pct(
    peak_input_tokens: int,
    total_input_tokens: int,
    num_turns: int,
    model: str,
) -> float:
    """Estimate context window usage as a percentage.

    Uses peak_input_tokens (from the last assistant message) if available,
    otherwise falls back to a heuristic based on total tokens and turns.
    """
    context_window = _MODEL_CONTEXT_WINDOWS.get(model, _DEFAULT_CONTEXT_WINDOW)

    if peak_input_tokens > 0:
        return round(peak_input_tokens / context_window * 100, 1)

    # Fallback: estimate peak from total input and turn count.
    # Total input grows roughly as a triangular sum; the last turn
    # sees approximately 2 * total / (N+1) tokens.
    if num_turns > 0 and total_input_tokens > 0:
        estimated_peak = total_input_tokens * 2 / (num_turns + 1)
        return round(estimated_peak / context_window * 100, 1)

    return 0.0


def _read_session_context_pct(worktree_path: str) -> float | None:
    """Try to read context_pct from the ambient daemon's session metadata.

    Scans /tmp/dev-loop/sessions/ for the most recently modified session
    file and extracts token_estimate.context_pct.
    """
    import yaml as _yaml

    sessions_dir = Path("/tmp/dev-loop/sessions")
    if not sessions_dir.is_dir():
        return None

    # Find session files modified in the last 5 minutes
    cutoff = time.monotonic()
    candidates: list[tuple[float, Path]] = []
    try:
        for f in sessions_dir.iterdir():
            if f.suffix in (".yaml", ".yml"):
                try:
                    mtime = f.stat().st_mtime
                    candidates.append((mtime, f))
                except OSError:
                    continue
    except OSError:
        return None

    if not candidates:
        return None

    # Use most recently modified
    candidates.sort(key=lambda x: x[0], reverse=True)
    latest = candidates[0][1]

    try:
        data = _yaml.safe_load(latest.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            token_est = data.get("token_estimate", {})
            context_pct = token_est.get("context_pct")
            if context_pct is not None:
                # Stored as a fraction (0.14 = 14%), convert to percentage
                pct = float(context_pct)
                if pct < 1.0:
                    pct *= 100.0
                return round(pct, 1)
    except Exception:
        pass

    return None


def _run_agent(config: AgentConfig, max_context_pct: int = 75) -> AgentResult:
    """Execute a Claude Code agent synchronously and return the result.

    Uses Popen + communicate(timeout=) so we can kill the child process on
    timeout instead of leaving a zombie (C1 fix from hardening slice 1).

    After the agent exits, estimates context window usage and sets
    ``context_limited=True`` if usage >= ``max_context_pct``.
    """
    claude_path = _find_claude_cli()
    cmd = _build_command(claude_path, config)

    # claude CLI authenticates via its own auth (Max subscription, OAuth, or API key)
    # so we don't require ANTHROPIC_API_KEY here.
    # Unset CLAUDECODE to allow --print mode from within a Claude Code session.
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    start = time.monotonic()

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=config.worktree_path,
        env=env,
    )

    try:
        stdout, stderr = proc.communicate(
            input=config.task_prompt,
            timeout=config.timeout_seconds,
        )
        elapsed = time.monotonic() - start

        usage = _parse_usage_from_output(stdout)

        # Determine context percentage — prefer ambient session data,
        # fall back to estimation from parsed tokens.
        session_pct = _read_session_context_pct(config.worktree_path)
        if session_pct is not None:
            context_pct = session_pct
        else:
            context_pct = _estimate_context_pct(
                peak_input_tokens=usage["peak_input_tokens"],
                total_input_tokens=usage["input_tokens"],
                num_turns=usage["num_turns"],
                model=config.model,
            )

        context_limited = context_pct >= max_context_pct

        return AgentResult(
            exit_code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            pid=proc.pid,
            duration_seconds=round(elapsed, 2),
            timed_out=False,
            worktree_path=config.worktree_path,
            model=config.model,
            num_turns=usage["num_turns"],
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            context_pct=context_pct,
            context_limited=context_limited,
        )

    except subprocess.TimeoutExpired:
        # Kill the process tree — proc.kill() sends SIGKILL
        proc.kill()
        # Reap the zombie so it doesn't linger in the process table
        stdout, stderr = proc.communicate()
        elapsed = time.monotonic() - start

        return AgentResult(
            exit_code=-1,
            stdout=stdout or "",
            stderr=stderr or "",
            pid=proc.pid,
            duration_seconds=round(elapsed, 2),
            timed_out=True,
            worktree_path=config.worktree_path,
            model=config.model,
        )



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
    max_turns: int | None = None,
    max_context_pct: int = 75,
) -> dict:
    """Spawn a Claude Code agent in a worktree and return its output."""
    config = AgentConfig(
        worktree_path=worktree_path,
        task_prompt=task_prompt,
        model=model,
        allowed_tools=allowed_tools,
        cost_ceiling=cost_ceiling,
        max_turns=max_turns,
    )

    with tracer.start_as_current_span(
        "runtime.spawn_agent",
        attributes={
            "runtime.model": config.model,
            "runtime.worktree_path": config.worktree_path,
            "runtime.cost_ceiling": config.cost_ceiling,
            "runtime.timeout_seconds": config.timeout_seconds,
            "runtime.max_context_pct": max_context_pct,
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
            result = _run_agent(config, max_context_pct=max_context_pct)
        except (ClaudeCLINotFound, FileNotFoundError) as exc:
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
        span.set_attribute("runtime.num_turns", result.num_turns)
        span.set_attribute("runtime.input_tokens", result.input_tokens)
        span.set_attribute("runtime.output_tokens", result.output_tokens)
        span.set_attribute("runtime.context_pct", result.context_pct)
        span.set_attribute("runtime.context_limited", result.context_limited)

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
    """Send SIGTERM to a running agent process.

    Validates that the PID belongs to a claude process before sending the
    signal to prevent accidentally killing unrelated processes (L1 fix).
    """
    with tracer.start_as_current_span(
        "runtime.kill_agent",
        attributes={
            "runtime.target_pid": pid,
        },
    ) as span:
        # Validate PID belongs to a claude process before killing (L1 fix)
        if not _is_claude_process(pid):
            error_msg = (
                f"PID {pid} is not a claude process — refusing to send SIGTERM"
            )
            span.set_status(trace.StatusCode.ERROR, error_msg)
            return {
                "success": False,
                "pid": pid,
                "signal": "SIGTERM",
                "message": error_msg,
            }

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

        # Look for .claude/ session files in the worktree
        claude_dir = wt / ".claude"
        output = None
        if claude_dir.is_dir():
            session_files = sorted(
                claude_dir.glob("**/*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if session_files:
                try:
                    output = session_files[0].read_text(encoding="utf-8")
                except OSError:
                    pass

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
