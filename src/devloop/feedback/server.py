"""Feedback-loop MCP server — retry agent on gate failure, escalate to human.

This is Layer 6 of the dev-loop harness. When quality gates fail, this layer
builds a retry prompt with structured failure context, re-spawns the agent
in the same worktree, and re-runs gates. If retries are exhausted, it
escalates the issue to a human by marking it "blocked" in beads.

TB-1 wires only Channel 1: Agent Retry. Channels 2-7 are TB-2+.

Run standalone:  uv run python -m devloop.feedback.server
"""

from __future__ import annotations

import subprocess

from fastmcp import FastMCP
from opentelemetry import trace

from devloop.feedback.types import EscalationResult, RetryPrompt, RetryResult
from devloop.gates.server import run_all_gates
from devloop.gates.types import GateSuiteResult
from devloop.runtime.server import spawn_agent

# ---------------------------------------------------------------------------
# OTel tracer for feedback layer
# ---------------------------------------------------------------------------

tracer = trace.get_tracer("feedback", "0.1.0")

# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="feedback-loop",
    instructions=(
        "Feedback loop layer for dev-loop. "
        "Use these tools to retry agents after gate failures, build retry "
        "prompts with structured failure context, and escalate blocked issues "
        "to humans."
    ),
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_br(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a br CLI command and return the result."""
    return subprocess.run(
        ["br", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _format_findings(gate_result: dict) -> str:
    """Format a single gate's findings into a readable block."""
    lines: list[str] = []
    findings = gate_result.get("findings", [])

    for finding in findings:
        severity = finding.get("severity", "info")
        message = finding.get("message", "")
        file = finding.get("file")
        line = finding.get("line")
        rule = finding.get("rule")

        location = ""
        if file:
            location = f" in {file}"
            if line:
                location += f":{line}"

        rule_text = f" [{rule}]" if rule else ""
        lines.append(f"  - [{severity.upper()}]{location}{rule_text}: {message}")

    return "\n".join(lines)


def _collect_all_failures(gate_failures: list[dict]) -> list[dict]:
    """Extract only the failed gates from a list of gate failure records.

    Each record in gate_failures is a dict with keys from GateSuiteResult
    or individual GateResult dicts. This handles both formats.
    """
    failed = []
    for record in gate_failures:
        # If it's a GateSuiteResult-shaped dict with gate_results
        if "gate_results" in record:
            for gr in record["gate_results"]:
                if not gr.get("passed", True):
                    failed.append(gr)
        # If it's an individual GateResult-shaped dict
        elif "gate_name" in record:
            if not record.get("passed", True):
                failed.append(record)
        # Otherwise treat the whole dict as a failure record
        else:
            failed.append(record)
    return failed


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool(
    description=(
        "Build a retry prompt that includes ALL previous gate failures. "
        "The prompt tells the agent what went wrong and instructs it to make "
        "the minimal fix needed to pass the gates."
    ),
    tags={"feedback", "retry"},
)
def build_retry_prompt(
    issue_title: str,
    issue_description: str,
    gate_failures: list[dict],
) -> dict:
    """Generate a retry context prompt with structured failure details."""
    with tracer.start_as_current_span(
        "feedback.build_retry_prompt",
        attributes={
            "feedback.operation": "build_retry_prompt",
            "issue.title": issue_title,
            "feedback.failure_records": len(gate_failures),
        },
    ) as span:
        all_failed = _collect_all_failures(gate_failures)
        span.set_attribute("feedback.unique_failures", len(all_failed))

        # Build the prompt from the template in the layer doc
        sections: list[str] = []

        sections.append(f"## Issue: {issue_title}")
        sections.append("")
        if issue_description:
            sections.append("### Description")
            sections.append(issue_description.strip())
            sections.append("")

        # Cap included failures to last 2 attempts to keep prompt bounded (M7 fix).
        # Summarize older failures with a count.
        max_detailed = 2 * 4  # ~2 attempts × ~4 gates per attempt
        if len(all_failed) > max_detailed:
            older = len(all_failed) - max_detailed
            sections.append(
                f"*({older} older failure(s) from earlier attempts omitted — "
                "focus on the most recent issues below.)*"
            )
            sections.append("")
            display_failures = all_failed[-max_detailed:]
        else:
            display_failures = all_failed

        for i, failure in enumerate(display_failures, 1):
            gate_name = failure.get("gate_name", "unknown")
            header = f"### Failure {i}: {gate_name} quality gate"
            sections.append(header)
            sections.append("")

            # Error message if present
            error = failure.get("error")
            if error:
                sections.append(f"Error: {error}")
                sections.append("")

            # Structured findings
            formatted = _format_findings(failure)
            if formatted:
                sections.append("Failure details:")
                sections.append(formatted)
                sections.append("")

        # Instructions
        sections.append(
            "Please fix the issues listed above and try again. "
            "Do not start over — your previous changes are still in the worktree. "
            "Make the minimal change needed to pass the gates."
        )

        prompt_text = "\n".join(sections)

        span.set_attribute("feedback.prompt_length", len(prompt_text))
        span.set_status(trace.StatusCode.OK)

        result = RetryPrompt(
            prompt_text=prompt_text,
            failure_count=len(all_failed),
        )
        return result.model_dump()


@mcp.tool(
    description=(
        "Retry an agent after gate failure. Builds a retry prompt from ALL "
        "previous gate failures, re-spawns the agent in the same worktree, "
        "and re-runs quality gates. Returns the retry result with gate outcomes. "
        "Will not exceed max_retries."
    ),
    tags={"feedback", "retry"},
)
def retry_agent(
    worktree_path: str,
    issue_id: str,
    issue_title: str,
    issue_description: str,
    gate_failures: list[dict],
    attempt: int = 1,
    max_retries: int = 2,
    model: str = "sonnet",
) -> dict:
    """Re-spawn agent with failure context and re-run gates."""
    with tracer.start_as_current_span(
        "feedback.retry",
        attributes={
            "feedback.operation": "retry_agent",
            "issue.id": issue_id,
            "retry.attempt": attempt,
            "retry.max_retries": max_retries,
            "retry.gate_failures_count": len(gate_failures),
        },
    ) as span:
        # Guard: don't exceed max retries
        if attempt > max_retries:
            span.set_attribute("retry.exceeded", True)
            span.set_status(trace.StatusCode.OK, "Max retries exceeded")
            return RetryResult(
                attempt=attempt,
                max_retries=max_retries,
                success=False,
                gate_results=None,
                escalated=False,
                error=f"Attempt {attempt} exceeds max_retries={max_retries}",
            ).model_dump()

        # 1. Build the retry prompt with all failures
        prompt_result = build_retry_prompt(issue_title, issue_description, gate_failures)
        prompt_text = prompt_result["prompt_text"]

        # Identify the gate that failed for span attributes
        all_failed = _collect_all_failures(gate_failures)
        if all_failed:
            last_failure = all_failed[-1]
            span.set_attribute("retry.gate_failed", last_failure.get("gate_name", "unknown"))
            reason_parts = []
            for f in last_failure.get("findings", []):
                if f.get("severity") in ("critical", "warning"):
                    reason_parts.append(f.get("message", ""))
            if reason_parts:
                span.set_attribute("retry.reason", "; ".join(reason_parts[:3]))

        # 2. Re-spawn the agent in the same worktree
        agent_result = spawn_agent(
            worktree_path=worktree_path,
            task_prompt=prompt_text,
            model=model,
        )

        exit_code = agent_result.get("exit_code", -1)
        span.set_attribute("retry.agent_exit_code", exit_code)

        if exit_code != 0:
            span.set_status(
                trace.StatusCode.ERROR,
                f"Agent exited with code {exit_code} on retry attempt {attempt}",
            )
            return RetryResult(
                attempt=attempt,
                max_retries=max_retries,
                success=False,
                gate_results=None,
                escalated=False,
                agent_exit_code=exit_code,
                error=f"Agent exited with code {exit_code}",
            ).model_dump()

        # 3. Re-run quality gates
        gate_raw = run_all_gates(
            worktree_path=worktree_path,
            issue_title=issue_title,
            issue_description=issue_description,
        )
        try:
            gate_suite = GateSuiteResult(**gate_raw)
        except Exception as exc:
            error_msg = f"Malformed gate result on retry {attempt}: {exc}"
            span.set_status(trace.StatusCode.ERROR, error_msg)
            return RetryResult(
                attempt=attempt,
                max_retries=max_retries,
                success=False,
                gate_results=None,
                escalated=False,
                agent_exit_code=exit_code,
                error=error_msg,
            ).model_dump()

        span.set_attribute("retry.gates_passed", gate_suite.overall_passed)
        if gate_suite.first_failure:
            span.set_attribute("retry.first_failure", gate_suite.first_failure)

        if gate_suite.overall_passed:
            span.set_status(trace.StatusCode.OK, f"Retry {attempt} succeeded")
        else:
            span.set_status(
                trace.StatusCode.ERROR,
                f"Retry {attempt} failed at {gate_suite.first_failure}",
            )

        return RetryResult(
            attempt=attempt,
            max_retries=max_retries,
            success=gate_suite.overall_passed,
            gate_results=gate_suite,
            escalated=False,
            agent_exit_code=exit_code,
        ).model_dump()


@mcp.tool(
    description=(
        "Escalate a failed issue to a human. Marks the beads issue as 'blocked' "
        "and adds a comment summarizing all retry attempts and gate failures. "
        "Called when retry_agent has exhausted max_retries."
    ),
    tags={"feedback", "escalate"},
)
def escalate_to_human(
    issue_id: str,
    gate_failures: list[dict],
    attempts: int,
) -> dict:
    """Mark issue as blocked with a failure summary comment."""
    with tracer.start_as_current_span(
        "feedback.escalate",
        attributes={
            "feedback.operation": "escalate_to_human",
            "issue.id": issue_id,
            "escalate.attempts": attempts,
            "escalate.failure_records": len(gate_failures),
        },
    ) as span:
        # Build the escalation comment
        all_failed = _collect_all_failures(gate_failures)
        comment_lines: list[str] = []
        comment_lines.append(
            f"[dev-loop] Automated resolution failed after {attempts} attempt(s)."
        )
        comment_lines.append("")
        comment_lines.append("### Gate Failures")

        if all_failed:
            for i, failure in enumerate(all_failed, 1):
                gate_name = failure.get("gate_name", "unknown")
                comment_lines.append("")
                comment_lines.append(f"**Attempt/Gate {i}: {gate_name}**")
                for finding in failure.get("findings", []):
                    sev = finding.get("severity", "info")
                    msg = finding.get("message", "")
                    file = finding.get("file", "")
                    loc = f" ({file}:{finding.get('line', '?')})" if file else ""
                    comment_lines.append(f"- [{sev}]{loc} {msg}")
        else:
            comment_lines.append("No structured failure details available.")

        comment_lines.append("")
        comment_lines.append("Needs human review. The worktree may still contain partial work.")

        comment_text = "\n".join(comment_lines)

        # Update issue status to blocked
        status_result = _run_br("update", issue_id, "--status", "blocked")
        status_updated = status_result.returncode == 0

        if not status_updated:
            span.set_attribute(
                "escalate.status_error",
                status_result.stderr.strip()[:200],
            )

        # Add the failure summary comment
        comment_result = _run_br("comments", "add", issue_id, "--message", comment_text)
        comment_added = comment_result.returncode == 0

        if not comment_added:
            span.set_attribute(
                "escalate.comment_error",
                comment_result.stderr.strip()[:200],
            )

        success = status_updated and comment_added
        span.set_attribute("escalate.status_updated", status_updated)
        span.set_attribute("escalate.comment_added", comment_added)

        if success:
            span.set_status(trace.StatusCode.OK)
            message = f"Issue {issue_id} escalated: status=blocked, comment added"
        else:
            parts = []
            if not status_updated:
                parts.append("status update failed")
            if not comment_added:
                parts.append("comment add failed")
            message = f"Partial escalation for {issue_id}: {', '.join(parts)}"
            span.set_status(trace.StatusCode.ERROR, message)

        return EscalationResult(
            issue_id=issue_id,
            success=success,
            status_updated=status_updated,
            comment_added=comment_added,
            attempts=attempts,
            message=message,
        ).model_dump()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
