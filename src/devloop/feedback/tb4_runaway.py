"""TB-4: Runaway-to-Stop — turn limits, usage parsing, escalation with usage table.

Extracted from pipeline.py to keep the main module manageable.

Usage::

    from devloop.feedback.tb4_runaway import run_tb4
    result = run_tb4(
        issue_id="dl-abc",
        repo_path="/home/user/some-repo",
        turns_override=10,
    )
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from opentelemetry import trace
from opentelemetry.trace import Link

from devloop.feedback.pipeline import (
    _clear_pipeline_timeout,
    _latest_failure_gate,
    _load_allowed_tools,
    _set_pipeline_timeout,
    _span_id_hex,
    _trace_id_hex,
    _unclaim_issue,
)
from devloop.feedback.server import escalate_to_human, retry_agent
from devloop.feedback.types import TB4Result, UsageBreakdown
from devloop.gates.server import run_all_gates
from devloop.gates.types import GateSuiteResult
from devloop.intake.beads_poller import claim_issue, get_issue, poll_ready
from devloop.observability.heartbeat import start_heartbeat, stop_heartbeat
from devloop.observability.tracing import init_tracing
from devloop.orchestration.server import (
    build_claude_md_overlay,
    cleanup_worktree,
    select_persona,
    setup_worktree,
)
from devloop.runtime.server import spawn_agent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HANDOFF_DIR = Path("/tmp/dev-loop/handoffs")
MAX_CONTEXT_RESTARTS = 3  # hard cap on context restarts per pipeline run

# ---------------------------------------------------------------------------
# OTel tracer
# ---------------------------------------------------------------------------

tracer_tb4 = trace.get_tracer("tb4", "0.1.0")


# ---------------------------------------------------------------------------
# Handoff helpers
# ---------------------------------------------------------------------------


def _read_handoff(issue_id: str) -> str | None:
    """Read the handoff note for an issue, if it exists."""
    handoff_path = HANDOFF_DIR / f"{issue_id}.md"
    if handoff_path.is_file():
        try:
            content = handoff_path.read_text(encoding="utf-8").strip()
            return content if content else None
        except OSError:
            return None
    return None


def _clear_handoff(issue_id: str) -> None:
    """Remove a consumed handoff file."""
    handoff_path = HANDOFF_DIR / f"{issue_id}.md"
    try:
        handoff_path.unlink(missing_ok=True)
    except OSError:
        pass


def _build_context_restart_prompt(
    issue_title: str,
    issue_description: str,
    handoff_note: str,
    overlay_text: str,
) -> str:
    """Build a task prompt for a context-restarted session.

    Prepends the handoff note from the previous session so the fresh
    agent knows what was already done and what remains.
    """
    sections: list[str] = []
    sections.append("# Context Restart — Continuing from Previous Session")
    sections.append("")
    sections.append("A previous agent session ran out of context window space.")
    sections.append("Below is its handoff note describing progress so far.")
    sections.append("")
    sections.append("## Handoff Note from Previous Session")
    sections.append("")
    sections.append(handoff_note)
    sections.append("")
    sections.append("## Original Task")
    sections.append("")
    if overlay_text:
        sections.append(overlay_text)
    else:
        sections.append(f"Fix issue: {issue_title}")
        sections.append("")
        if issue_description:
            sections.append(issue_description)
    sections.append("")
    sections.append("## Instructions")
    sections.append("")
    sections.append(
        "Continue from where the previous session left off. "
        "Review the handoff note above, verify the current state of the "
        "worktree, and complete the remaining work."
    )
    return "\n".join(sections)


# ---------------------------------------------------------------------------
# TB-4: Runaway-to-Stop
# ---------------------------------------------------------------------------


def run_tb4(
    issue_id: str,
    repo_path: str,
    turns_override: int | None = None,
) -> dict:
    """Run the full TB-4 runaway-to-stop pipeline.

    Proves: runaway agents get stopped, not just logged. Turn usage is
    visible and controllable.  On Max subscription, turns are the control.

    Phases:
        1.  Poll beads for the issue (intake)
        2.  Claim the issue (optimistic locking)
        3.  Setup worktree (orchestration)
        4.  Select persona → get max_turns_default (orchestration)
        5.  Init tracing (observability)
        6.  Start heartbeat (observability)
        7.  Spawn agent with --max-turns (runtime)
        8.  Run quality gates if turns remain (gates)
        9.  Gates pass → success
        10. Gates fail → retry with remaining turn budget
        11. Turns exhausted or retries exhausted → escalate with usage table
        12. Cleanup

    Args:
        issue_id: The beads issue ID to process.
        repo_path: Absolute path to the git repository.
        turns_override: Override the persona's max_turns_default.

    Returns:
        A dict (TB4Result) with the outcome of the run.
    """
    pipeline_start = time.monotonic()
    _set_pipeline_timeout()

    # Phase 5 — init tracing early
    provider = init_tracing()

    with tracer_tb4.start_as_current_span(
        "tb4.run",
        attributes={
            "tb4.issue_id": issue_id,
            "tb4.repo_path": repo_path,
        },
    ) as root_span:
        heartbeat_event = None
        heartbeat_thread = None
        worktree_path: str | None = None
        persona_name: str | None = None
        pipeline_success = False
        retries_used = 0
        max_retries = 2
        max_turns_total = 0
        max_context_pct = 75
        turns_used_total = 0
        context_restarts = 0
        usage_breakdown: list[UsageBreakdown] = []
        attempt_span_ids: list[str] = []
        trace_id: str | None = None

        try:
            # ----------------------------------------------------------
            # Phase 1: Poll beads for the issue
            # ----------------------------------------------------------
            with tracer_tb4.start_as_current_span(
                "tb4.phase.poll",
                attributes={"tb4.phase": "poll"},
            ) as poll_span:
                items = poll_ready(repo_path=repo_path)
                issue = None
                for item in items:
                    if item.id == issue_id:
                        issue = item
                        break

                if issue is None:
                    poll_span.set_attribute("tb4.issue_found_in_poll", False)
                    issue = get_issue(issue_id, repo_path=repo_path)
                if issue is None:
                    issue_title = issue_id
                    issue_description = ""
                    issue_labels: list[str] = []
                else:
                    poll_span.set_attribute("tb4.issue_found_in_poll", True)
                    issue_title = issue.title
                    issue_description = issue.description or ""
                    issue_labels = issue.labels
                    if not issue_labels:
                        full_issue = get_issue(issue_id, repo_path=repo_path)
                        if full_issue and full_issue.labels:
                            issue_labels = full_issue.labels

                poll_span.set_attribute("tb4.ready_count", len(items))

            # ----------------------------------------------------------
            # Phase 2: Claim the issue
            # ----------------------------------------------------------
            with tracer_tb4.start_as_current_span(
                "tb4.phase.claim",
                attributes={"tb4.phase": "claim", "issue.id": issue_id},
            ) as claim_span:
                claimed = claim_issue(issue_id, repo_path=repo_path)
                claim_span.set_attribute("tb4.claimed", claimed)

                if not claimed:
                    elapsed = time.monotonic() - pipeline_start
                    claim_span.set_status(
                        trace.StatusCode.ERROR,
                        f"Failed to claim issue {issue_id}",
                    )
                    return TB4Result(
                        issue_id=issue_id,
                        repo_path=repo_path,
                        success=False,
                        phase="claim",
                        error=f"Could not claim issue {issue_id}",
                        duration_seconds=round(elapsed, 2),
                    ).model_dump()

            # ----------------------------------------------------------
            # Phase 3: Setup worktree
            # ----------------------------------------------------------
            with tracer_tb4.start_as_current_span(
                "tb4.phase.setup_worktree",
                attributes={"tb4.phase": "setup_worktree"},
            ):
                wt_result = setup_worktree(issue_id, repo_path)

                if not wt_result.get("success"):
                    elapsed = time.monotonic() - pipeline_start
                    return TB4Result(
                        issue_id=issue_id,
                        repo_path=repo_path,
                        success=False,
                        phase="setup_worktree",
                        error=wt_result.get("message", "Worktree setup failed"),
                        duration_seconds=round(elapsed, 2),
                    ).model_dump()

                worktree_path = wt_result["worktree_path"]

            # ----------------------------------------------------------
            # Phase 4: Select persona + get max_turns_default
            # ----------------------------------------------------------
            with tracer_tb4.start_as_current_span(
                "tb4.phase.persona",
                attributes={"tb4.phase": "persona"},
            ) as persona_span:
                persona_result = select_persona(issue_labels, issue_description=issue_description)
                persona_name = persona_result.get("name", "feature")
                max_retries = persona_result.get("retry_max", 2)
                max_turns_total = turns_override or persona_result.get(
                    "max_turns_default", 15
                )
                max_context_pct = persona_result.get("max_context_pct", 75)

                persona_span.set_attribute("tb4.persona", persona_name)
                persona_span.set_attribute("tb4.max_retries", max_retries)
                persona_span.set_attribute("tb4.max_turns_total", max_turns_total)
                persona_span.set_attribute("tb4.max_context_pct", max_context_pct)

                overlay_result = build_claude_md_overlay(
                    persona=persona_name,
                    issue_title=issue_title,
                    issue_description=issue_description,
                    issue_id=issue_id,
                    max_context_pct=max_context_pct,
                )
                overlay_text = overlay_result.get("overlay_text", "")

                if worktree_path and overlay_text:
                    claude_md_path = Path(worktree_path) / "CLAUDE.md"
                    existing = ""
                    if claude_md_path.exists():
                        existing = claude_md_path.read_text(encoding="utf-8")
                    combined = existing
                    if combined and not combined.endswith("\n"):
                        combined += "\n"
                    combined += "\n" + overlay_text
                    claude_md_path.write_text(combined, encoding="utf-8")

            root_span.set_attribute("tb4.max_turns_total", max_turns_total)
            root_span.set_attribute("tb4.max_context_pct", max_context_pct)
            trace_id = _trace_id_hex(root_span)

            # Ensure handoff directory exists
            HANDOFF_DIR.mkdir(parents=True, exist_ok=True)

            # ----------------------------------------------------------
            # Phase 6: Start heartbeat
            # ----------------------------------------------------------
            with tracer_tb4.start_as_current_span(
                "tb4.phase.heartbeat_start",
                attributes={"tb4.phase": "heartbeat_start"},
            ):
                heartbeat_event, heartbeat_thread = start_heartbeat(
                    issue_id, interval_seconds=30, worktree_path=worktree_path,
                )

            # ----------------------------------------------------------
            # Phase 7: Spawn agent with turn budget
            # ----------------------------------------------------------
            remaining_turns = max_turns_total

            with tracer_tb4.start_as_current_span(
                "tb4.phase.spawn_agent",
                attributes={
                    "tb4.phase": "spawn_agent",
                    "tb4.max_turns": remaining_turns,
                },
            ) as agent_span:
                attempt_span_ids.append(_span_id_hex(agent_span))
                task_prompt = overlay_text or f"Fix issue: {issue_title}\n\n{issue_description}"

                allowed_tools = _load_allowed_tools(repo_path)
                agent_result = spawn_agent(
                    worktree_path=worktree_path,
                    task_prompt=task_prompt,
                    model=persona_result.get("model", "sonnet"),
                    max_turns=remaining_turns,
                    allowed_tools=allowed_tools,
                    max_context_pct=max_context_pct,
                )

                agent_exit = agent_result.get("exit_code", -1)
                agent_turns = agent_result.get("num_turns", 0)
                agent_context_pct = agent_result.get("context_pct", 0.0)
                agent_context_limited = agent_result.get("context_limited", False)
                turns_used_total += agent_turns
                remaining_turns = max(0, max_turns_total - turns_used_total)

                usage_breakdown.append(UsageBreakdown(
                    attempt=0,
                    num_turns=agent_turns,
                    input_tokens=agent_result.get("input_tokens", 0),
                    output_tokens=agent_result.get("output_tokens", 0),
                    cumulative_turns=turns_used_total,
                    context_pct_at_exit=agent_context_pct,
                ))

                agent_span.set_attribute("tb4.agent_exit_code", agent_exit)
                agent_span.set_attribute("runtime.num_turns", agent_turns)
                agent_span.set_attribute("runtime.input_tokens", agent_result.get("input_tokens", 0))
                agent_span.set_attribute("runtime.output_tokens", agent_result.get("output_tokens", 0))
                agent_span.set_attribute("runtime.context_pct", agent_context_pct)
                agent_span.set_attribute("runtime.context_limited", agent_context_limited)
                agent_span.set_attribute("tb4.turns_remaining", remaining_turns)

                if agent_exit != 0:
                    elapsed = time.monotonic() - pipeline_start
                    agent_span.set_status(
                        trace.StatusCode.ERROR,
                        f"Agent exited with code {agent_exit}",
                    )
                    return TB4Result(
                        issue_id=issue_id,
                        repo_path=repo_path,
                        success=False,
                        phase="spawn_agent",
                        worktree_path=worktree_path,
                        persona=persona_name,
                        max_retries=max_retries,
                        turns_used_total=turns_used_total,
                        max_turns_total=max_turns_total,
                        usage_breakdown=usage_breakdown,
                        context_restarts=context_restarts,
                        trace_id=trace_id,
                        attempt_span_ids=attempt_span_ids,
                        error=f"Agent exited with code {agent_exit}",
                        duration_seconds=round(elapsed, 2),
                    ).model_dump()

            # ----------------------------------------------------------
            # Phase 7b: Context restart loop
            # ----------------------------------------------------------
            # If the agent hit the context limit, spawn a fresh session
            # with the handoff note. This is NOT a gate retry — it's
            # resource management.
            while agent_context_limited and context_restarts < MAX_CONTEXT_RESTARTS:
                if remaining_turns <= 0:
                    logger.warning(
                        "TB-4: Context-limited but no turns remaining — skipping restart"
                    )
                    break

                with tracer_tb4.start_as_current_span(
                    "runtime.context_restart",
                    attributes={
                        "tb4.phase": "context_restart",
                        "context_pct_at_exit": agent_context_pct,
                        "restart_count": context_restarts + 1,
                        "handoff_file_path": str(HANDOFF_DIR / f"{issue_id}.md"),
                    },
                ) as restart_span:
                    context_restarts += 1
                    attempt_span_ids.append(_span_id_hex(restart_span))

                    handoff_note = _read_handoff(issue_id)
                    if handoff_note is None:
                        logger.info(
                            "TB-4: Context-limited at %.1f%% but no handoff file — "
                            "continuing without restart",
                            agent_context_pct,
                        )
                        restart_span.set_attribute("context_restart.handoff_found", False)
                        break

                    restart_span.set_attribute("context_restart.handoff_found", True)
                    restart_span.set_attribute(
                        "context_restart.handoff_length", len(handoff_note)
                    )
                    _clear_handoff(issue_id)

                    logger.info(
                        "TB-4 CONTEXT RESTART %d: Issue %s — context at %.1f%%, "
                        "spawning fresh session with handoff (%d chars)",
                        context_restarts,
                        issue_id,
                        agent_context_pct,
                        len(handoff_note),
                    )

                    restart_prompt = _build_context_restart_prompt(
                        issue_title=issue_title,
                        issue_description=issue_description,
                        handoff_note=handoff_note,
                        overlay_text=overlay_text,
                    )

                    agent_result = spawn_agent(
                        worktree_path=worktree_path,
                        task_prompt=restart_prompt,
                        model=persona_result.get("model", "sonnet"),
                        max_turns=remaining_turns,
                        allowed_tools=allowed_tools,
                        max_context_pct=max_context_pct,
                    )

                    agent_exit = agent_result.get("exit_code", -1)
                    agent_turns = agent_result.get("num_turns", 0)
                    agent_context_pct = agent_result.get("context_pct", 0.0)
                    agent_context_limited = agent_result.get("context_limited", False)
                    turns_used_total += agent_turns
                    remaining_turns = max(0, max_turns_total - turns_used_total)

                    usage_breakdown.append(UsageBreakdown(
                        attempt=0,
                        num_turns=agent_turns,
                        input_tokens=agent_result.get("input_tokens", 0),
                        output_tokens=agent_result.get("output_tokens", 0),
                        cumulative_turns=turns_used_total,
                        context_pct_at_exit=agent_context_pct,
                        context_restart=True,
                    ))

                    restart_span.set_attribute("runtime.num_turns", agent_turns)
                    restart_span.set_attribute("runtime.context_pct", agent_context_pct)
                    restart_span.set_attribute("tb4.turns_remaining", remaining_turns)

                    if agent_exit != 0:
                        restart_span.set_status(
                            trace.StatusCode.ERROR,
                            f"Restarted agent exited with code {agent_exit}",
                        )
                        # Don't fail the pipeline — fall through to gates
                        break

                    restart_span.set_status(trace.StatusCode.OK)

            root_span.set_attribute("runtime_context_restarts", context_restarts)

            # ----------------------------------------------------------
            # Phase 8: Run quality gates (if turns remain)
            # ----------------------------------------------------------
            if remaining_turns <= 0:
                logger.warning(
                    "TB-4: Turn budget exhausted after initial spawn (%d/%d turns)",
                    turns_used_total,
                    max_turns_total,
                )
                # Skip gates, go straight to escalation
                gate_suite = None
                all_gate_failures: list[dict] = [{
                    "gate_results": [{
                        "gate_name": "turn_budget",
                        "passed": False,
                        "findings": [{
                            "severity": "critical",
                            "message": (
                                f"Turn budget exhausted: {turns_used_total}/{max_turns_total} "
                                "turns used on initial attempt"
                            ),
                        }],
                    }],
                }]
            else:
                with tracer_tb4.start_as_current_span(
                    "tb4.phase.gates",
                    attributes={"tb4.phase": "gates"},
                ) as gates_span:
                    gate_raw = run_all_gates(
                        worktree_path=worktree_path,
                        issue_title=issue_title,
                        issue_description=issue_description,
                    )
                    try:
                        gate_suite = GateSuiteResult(**gate_raw)
                    except Exception as exc:
                        elapsed = time.monotonic() - pipeline_start
                        error_msg = f"Malformed gate result: {exc}"
                        gates_span.set_status(trace.StatusCode.ERROR, error_msg)
                        return TB4Result(
                            issue_id=issue_id,
                            repo_path=repo_path,
                            success=False,
                            phase="gates",
                            worktree_path=worktree_path,
                            persona=persona_name,
                            turns_used_total=turns_used_total,
                            max_turns_total=max_turns_total,
                            usage_breakdown=usage_breakdown,
                            context_restarts=context_restarts,
                            trace_id=trace_id,
                            attempt_span_ids=attempt_span_ids,
                            error=error_msg,
                            duration_seconds=round(elapsed, 2),
                        ).model_dump()

                    gates_span.set_attribute("tb4.gates_passed", gate_suite.overall_passed)

                    if gate_suite.overall_passed:
                        elapsed = time.monotonic() - pipeline_start
                        logger.info(
                            "TB-4 SUCCESS: Issue %s — all gates passed in %.1fs "
                            "(%d/%d turns used, %d context restarts)",
                            issue_id,
                            elapsed,
                            turns_used_total,
                            max_turns_total,
                            context_restarts,
                        )
                        root_span.set_attribute("tb4.outcome", "success")
                        root_span.set_attribute("tb4.turns_used_total", turns_used_total)
                        root_span.set_attribute("status.detail", "All gates passed")
                        root_span.set_status(trace.StatusCode.OK)
                        pipeline_success = True
                        return TB4Result(
                            issue_id=issue_id,
                            repo_path=repo_path,
                            success=True,
                            phase="gates_passed",
                            worktree_path=worktree_path,
                            persona=persona_name,
                            turns_used_total=turns_used_total,
                            max_turns_total=max_turns_total,
                            usage_breakdown=usage_breakdown,
                            context_restarts=context_restarts,
                            trace_id=trace_id,
                            attempt_span_ids=attempt_span_ids,
                            duration_seconds=round(elapsed, 2),
                        ).model_dump()

                    all_gate_failures = [gate_raw]

            # ----------------------------------------------------------
            # Phase 10: Gates failed → retry with remaining turn budget
            # ----------------------------------------------------------
            for attempt in range(1, max_retries + 1):
                retries_used = attempt

                # Check turn budget before retrying
                if remaining_turns <= 0:
                    logger.warning(
                        "TB-4: No turns remaining for retry %d/%d (%d/%d used)",
                        attempt,
                        max_retries,
                        turns_used_total,
                        max_turns_total,
                    )
                    break

                with tracer_tb4.start_as_current_span(
                    "tb4.phase.retry",
                    attributes={
                        "tb4.phase": "retry",
                        "retry.attempt": attempt,
                        "retry.max_retries": max_retries,
                        "tb4.turns_remaining": remaining_turns,
                    },
                    links=[Link(root_span.get_span_context())],
                ) as retry_span:
                    attempt_span_ids.append(_span_id_hex(retry_span))

                    last_failure_name = _latest_failure_gate(all_gate_failures)
                    logger.info(
                        "TB-4 RETRY %d/%d for issue %s (failed at %s, %d turns remaining)",
                        attempt,
                        max_retries,
                        issue_id,
                        last_failure_name,
                        remaining_turns,
                    )

                    # Spawn agent with remaining turns as budget
                    retry_raw = retry_agent(
                        worktree_path=worktree_path,
                        issue_id=issue_id,
                        issue_title=issue_title,
                        issue_description=issue_description,
                        gate_failures=all_gate_failures,
                        attempt=attempt,
                        max_retries=max_retries,
                        model=persona_result.get("model", "sonnet"),
                        max_turns=remaining_turns,
                    )

                    # Parse usage from retry (retry_agent calls spawn_agent internally)
                    retry_turns = retry_raw.get("num_turns", 0)
                    turns_used_total += retry_turns
                    remaining_turns = max(0, max_turns_total - turns_used_total)

                    usage_breakdown.append(UsageBreakdown(
                        attempt=attempt,
                        num_turns=retry_turns,
                        input_tokens=retry_raw.get("input_tokens", 0),
                        output_tokens=retry_raw.get("output_tokens", 0),
                        cumulative_turns=turns_used_total,
                    ))

                    retry_span.set_attribute("runtime.num_turns", retry_turns)
                    retry_span.set_attribute("tb4.turns_remaining", remaining_turns)

                    retry_success = retry_raw.get("success", False)
                    retry_span.set_attribute("tb4.retry_success", retry_success)

                    if retry_success:
                        elapsed = time.monotonic() - pipeline_start
                        logger.info(
                            "TB-4 SUCCESS after retry %d: Issue %s in %.1fs "
                            "(%d/%d turns used, %d context restarts)",
                            attempt,
                            issue_id,
                            elapsed,
                            turns_used_total,
                            max_turns_total,
                            context_restarts,
                        )
                        root_span.set_attribute("tb4.outcome", "success_after_retry")
                        root_span.set_attribute("tb4.retries_used", attempt)
                        root_span.set_attribute("tb4.turns_used_total", turns_used_total)
                        pipeline_success = True
                        root_span.set_status(trace.StatusCode.OK)
                        return TB4Result(
                            issue_id=issue_id,
                            repo_path=repo_path,
                            success=True,
                            phase="retry_passed",
                            worktree_path=worktree_path,
                            persona=persona_name,
                            retries_used=attempt,
                            max_retries=max_retries,
                            turns_used_total=turns_used_total,
                            max_turns_total=max_turns_total,
                            usage_breakdown=usage_breakdown,
                            context_restarts=context_restarts,
                            trace_id=trace_id,
                            attempt_span_ids=attempt_span_ids,
                            duration_seconds=round(elapsed, 2),
                        ).model_dump()

                    # Accumulate failures
                    retry_gate_raw = retry_raw.get("gate_results")
                    if retry_gate_raw:
                        all_gate_failures.append(retry_gate_raw)
                    elif retry_raw.get("error"):
                        all_gate_failures.append({
                            "gate_results": [{
                                "gate_name": "agent_spawn",
                                "passed": False,
                                "findings": [{
                                    "severity": "critical",
                                    "message": f"Agent spawn failed: {retry_raw['error']}",
                                }],
                            }],
                        })

            # ----------------------------------------------------------
            # Phase 11: Escalate with usage table
            # ----------------------------------------------------------
            with tracer_tb4.start_as_current_span(
                "tb4.phase.escalate",
                attributes={
                    "tb4.phase": "escalate",
                    "escalate.attempts": retries_used + 1,
                    "tb4.turns_used_total": turns_used_total,
                    "tb4.context_restarts": context_restarts,
                },
            ) as esc_span:
                reason = (
                    f"Turn limit reached: {turns_used_total}/{max_turns_total} turns "
                    f"across {retries_used + 1} attempt(s)"
                )
                if context_restarts > 0:
                    reason += f" ({context_restarts} context restart(s))"
                logger.warning("TB-4 ESCALATE: Issue %s — %s", issue_id, reason)

                esc_result = escalate_to_human(
                    issue_id=issue_id,
                    gate_failures=all_gate_failures,
                    attempts=retries_used + 1,
                    usage_breakdown=[u.model_dump() for u in usage_breakdown],
                    repo_path=repo_path,
                )

                esc_span.set_attribute(
                    "tb4.escalation_success",
                    esc_result.get("success", False),
                )

            elapsed = time.monotonic() - pipeline_start
            root_span.set_attribute("tb4.outcome", "escalated")
            root_span.set_attribute("tb4.retries_used", retries_used)
            root_span.set_attribute("tb4.turns_used_total", turns_used_total)
            root_span.set_attribute("runtime_context_restarts", context_restarts)
            root_span.set_status(
                trace.StatusCode.ERROR,
                f"Escalated: {turns_used_total}/{max_turns_total} turns used",
            )
            return TB4Result(
                issue_id=issue_id,
                repo_path=repo_path,
                success=False,
                phase="escalated",
                worktree_path=worktree_path,
                persona=persona_name,
                retries_used=retries_used,
                max_retries=max_retries,
                escalated=True,
                turns_used_total=turns_used_total,
                max_turns_total=max_turns_total,
                usage_breakdown=usage_breakdown,
                context_restarts=context_restarts,
                trace_id=trace_id,
                attempt_span_ids=attempt_span_ids,
                error=reason,
                duration_seconds=round(elapsed, 2),
            ).model_dump()

        except Exception as exc:
            elapsed = time.monotonic() - pipeline_start
            error_msg = f"Pipeline error: {type(exc).__name__}: {exc}"
            logger.exception("TB-4 pipeline error for issue %s", issue_id)
            root_span.set_status(trace.StatusCode.ERROR, error_msg)
            root_span.record_exception(exc)
            return TB4Result(
                issue_id=issue_id,
                repo_path=repo_path,
                success=False,
                phase="error",
                worktree_path=worktree_path,
                persona=persona_name,
                retries_used=retries_used,
                max_retries=max_retries,
                turns_used_total=turns_used_total,
                max_turns_total=max_turns_total,
                usage_breakdown=usage_breakdown,
                context_restarts=context_restarts,
                error=error_msg,
                duration_seconds=round(elapsed, 2),
            ).model_dump()

        finally:
            # ----------------------------------------------------------
            # Phase 12: Cleanup
            # ----------------------------------------------------------
            try:
                with tracer_tb4.start_as_current_span(
                    "tb4.phase.cleanup",
                    attributes={"tb4.phase": "cleanup"},
                ):
                    pass
            except Exception:
                pass

            if heartbeat_event is not None:
                stop_heartbeat(heartbeat_event, heartbeat_thread)

            if worktree_path:
                cleanup_worktree(issue_id)

            # Clean up any leftover handoff file
            _clear_handoff(issue_id)

            if not pipeline_success:
                _unclaim_issue(issue_id, repo_path=repo_path)

            if provider is not None:
                try:
                    provider.force_flush(timeout_millis=5000)
                except Exception:
                    pass

            _clear_pipeline_timeout()
