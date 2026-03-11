"""TB-1 end-to-end pipeline orchestrator.

This is the tracer bullet — the thinnest possible vertical slice that cuts
through all six layers:

    intake -> orchestration -> runtime -> gates -> observability -> feedback

Usage::

    from devloop.feedback.pipeline import run_tb1
    result = run_tb1(issue_id="dl-abc", repo_path="/home/user/some-repo")

The function is synchronous (TB-1 is single-threaded, blocking). It returns
a TB1Result with full details of what happened at each phase.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from opentelemetry import trace

from devloop.feedback.server import escalate_to_human, retry_agent
from devloop.feedback.types import TB1Result
from devloop.gates.server import run_all_gates
from devloop.gates.types import GateSuiteResult
from devloop.intake.beads_poller import claim_issue, poll_ready
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
# OTel tracer — uses the global provider set up by init_tracing()
# ---------------------------------------------------------------------------

tracer = trace.get_tracer("tb1", "0.1.0")

# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def run_tb1(issue_id: str, repo_path: str) -> dict:
    """Run the full TB-1 golden path for a single issue.

    Phases:
        1.  Poll beads for the issue (intake)
        2.  Claim the issue (optimistic locking)
        3.  Setup worktree (orchestration)
        4.  Select persona + build CLAUDE.md overlay (orchestration)
        5.  Init tracing (observability)
        6.  Start heartbeat (observability)
        7.  Spawn agent in worktree (runtime)
        8.  Run quality gates (gates)
        9.  If gates pass -> log success (PR creation is TB-2)
        10. If gates fail -> retry via feedback loop (max retries from persona)
        11. If retries exhausted -> escalate to human
        12. Stop heartbeat, cleanup worktree

    Args:
        issue_id: The beads issue ID to process.
        repo_path: Absolute path to the git repository.

    Returns:
        A dict (TB1Result) with the outcome of the run.
    """
    pipeline_start = time.monotonic()

    # Phase 5 — init tracing early so all subsequent spans are captured
    init_tracing()

    with tracer.start_as_current_span(
        "tb1.run",
        attributes={
            "tb1.issue_id": issue_id,
            "tb1.repo_path": repo_path,
        },
    ) as root_span:
        # Track state for cleanup
        heartbeat_event = None
        worktree_path: str | None = None
        persona_name: str | None = None
        retries_used = 0
        max_retries = 2

        try:
            # ----------------------------------------------------------
            # Phase 1: Poll beads for the issue
            # ----------------------------------------------------------
            with tracer.start_as_current_span(
                "tb1.phase.poll",
                attributes={"tb1.phase": "poll"},
            ) as poll_span:
                items = poll_ready()
                issue = None
                for item in items:
                    if item.id == issue_id:
                        issue = item
                        break

                if issue is None:
                    # Issue might not be in the "ready" list — that's OK,
                    # we'll proceed with the ID and let claim handle it.
                    poll_span.set_attribute("tb1.issue_found_in_poll", False)
                    logger.info(
                        "Issue %s not found in ready poll (may already be claimed or not ready); "
                        "proceeding with claim attempt",
                        issue_id,
                    )
                    issue_title = issue_id
                    issue_description = ""
                    issue_labels: list[str] = []
                else:
                    poll_span.set_attribute("tb1.issue_found_in_poll", True)
                    issue_title = issue.title
                    issue_description = issue.description or ""
                    issue_labels = issue.labels

                poll_span.set_attribute("tb1.ready_count", len(items))

            # ----------------------------------------------------------
            # Phase 2: Claim the issue (optimistic locking)
            # ----------------------------------------------------------
            with tracer.start_as_current_span(
                "tb1.phase.claim",
                attributes={"tb1.phase": "claim", "issue.id": issue_id},
            ) as claim_span:
                claimed = claim_issue(issue_id)
                claim_span.set_attribute("tb1.claimed", claimed)

                if not claimed:
                    elapsed = time.monotonic() - pipeline_start
                    claim_span.set_status(
                        trace.StatusCode.ERROR,
                        f"Failed to claim issue {issue_id} — already claimed or not found",
                    )
                    root_span.set_status(trace.StatusCode.ERROR, "Claim failed")
                    return TB1Result(
                        issue_id=issue_id,
                        repo_path=repo_path,
                        success=False,
                        phase="claim",
                        error=(
                            f"Could not claim issue {issue_id} "
                            "(already in_progress or not found)"
                        ),
                        duration_seconds=round(elapsed, 2),
                    ).model_dump()

            # ----------------------------------------------------------
            # Phase 3: Setup worktree
            # ----------------------------------------------------------
            with tracer.start_as_current_span(
                "tb1.phase.setup_worktree",
                attributes={"tb1.phase": "setup_worktree"},
            ):
                wt_result = setup_worktree(issue_id, repo_path)

                if not wt_result.get("success"):
                    elapsed = time.monotonic() - pipeline_start
                    root_span.set_status(trace.StatusCode.ERROR, "Worktree setup failed")
                    return TB1Result(
                        issue_id=issue_id,
                        repo_path=repo_path,
                        success=False,
                        phase="setup_worktree",
                        error=wt_result.get("message", "Worktree setup failed"),
                        duration_seconds=round(elapsed, 2),
                    ).model_dump()

                worktree_path = wt_result["worktree_path"]

            # ----------------------------------------------------------
            # Phase 4: Select persona + build CLAUDE.md overlay
            # ----------------------------------------------------------
            with tracer.start_as_current_span(
                "tb1.phase.persona",
                attributes={"tb1.phase": "persona"},
            ) as persona_span:
                persona_result = select_persona(issue_labels)
                persona_name = persona_result.get("name", "feature")
                max_retries = persona_result.get("retry_max", 2)

                persona_span.set_attribute("tb1.persona", persona_name)
                persona_span.set_attribute("tb1.max_retries", max_retries)

                overlay_result = build_claude_md_overlay(
                    persona=persona_name,
                    issue_title=issue_title,
                    issue_description=issue_description,
                )
                overlay_text = overlay_result.get("overlay_text", "")

                # Write the overlay to the worktree's CLAUDE.md
                if worktree_path and overlay_text:
                    claude_md_path = Path(worktree_path) / "CLAUDE.md"
                    existing = ""
                    if claude_md_path.exists():
                        existing = claude_md_path.read_text(encoding="utf-8")

                    # Append the overlay after existing content
                    combined = existing
                    if combined and not combined.endswith("\n"):
                        combined += "\n"
                    combined += "\n" + overlay_text
                    claude_md_path.write_text(combined, encoding="utf-8")

            # ----------------------------------------------------------
            # Phase 6: Start heartbeat
            # ----------------------------------------------------------
            with tracer.start_as_current_span(
                "tb1.phase.heartbeat_start",
                attributes={"tb1.phase": "heartbeat_start"},
            ):
                heartbeat_event = start_heartbeat(issue_id, interval_seconds=30)

            # ----------------------------------------------------------
            # Phase 7: Spawn agent in worktree
            # ----------------------------------------------------------
            with tracer.start_as_current_span(
                "tb1.phase.spawn_agent",
                attributes={"tb1.phase": "spawn_agent"},
            ) as agent_span:
                # Build the task prompt from the overlay
                task_prompt = overlay_text or f"Fix issue: {issue_title}\n\n{issue_description}"

                agent_result = spawn_agent(
                    worktree_path=worktree_path,
                    task_prompt=task_prompt,
                    model=persona_result.get("model", "sonnet"),
                )

                agent_exit = agent_result.get("exit_code", -1)
                agent_span.set_attribute("tb1.agent_exit_code", agent_exit)

                if agent_exit != 0:
                    elapsed = time.monotonic() - pipeline_start
                    agent_span.set_status(
                        trace.StatusCode.ERROR,
                        f"Agent exited with code {agent_exit}",
                    )
                    root_span.set_status(trace.StatusCode.ERROR, "Agent spawn failed")
                    return TB1Result(
                        issue_id=issue_id,
                        repo_path=repo_path,
                        success=False,
                        phase="spawn_agent",
                        worktree_path=worktree_path,
                        persona=persona_name,
                        agent_exit_code=agent_exit,
                        error=agent_result.get("stderr", "Agent failed"),
                        max_retries=max_retries,
                        duration_seconds=round(elapsed, 2),
                    ).model_dump()

            # ----------------------------------------------------------
            # Phase 8: Run quality gates
            # ----------------------------------------------------------
            with tracer.start_as_current_span(
                "tb1.phase.gates",
                attributes={"tb1.phase": "gates"},
            ) as gates_span:
                gate_raw = run_all_gates(
                    worktree_path=worktree_path,
                    issue_title=issue_title,
                    issue_description=issue_description,
                )
                gate_suite = GateSuiteResult(**gate_raw)

                gates_span.set_attribute("tb1.gates_passed", gate_suite.overall_passed)
                if gate_suite.first_failure:
                    gates_span.set_attribute("tb1.first_failure", gate_suite.first_failure)

            # ----------------------------------------------------------
            # Phase 9: Gates passed -> success
            # ----------------------------------------------------------
            if gate_suite.overall_passed:
                elapsed = time.monotonic() - pipeline_start
                logger.info(
                    "TB-1 SUCCESS: Issue %s — all gates passed in %.1fs",
                    issue_id,
                    elapsed,
                )
                root_span.set_attribute("tb1.outcome", "success")
                root_span.set_status(trace.StatusCode.OK, "All gates passed")
                return TB1Result(
                    issue_id=issue_id,
                    repo_path=repo_path,
                    success=True,
                    phase="gates_passed",
                    worktree_path=worktree_path,
                    persona=persona_name,
                    agent_exit_code=agent_exit,
                    gate_results=gate_suite,
                    max_retries=max_retries,
                    duration_seconds=round(elapsed, 2),
                ).model_dump()

            # ----------------------------------------------------------
            # Phase 10: Gates failed -> retry loop
            # ----------------------------------------------------------
            # Accumulate ALL gate failures across retries so the prompt
            # includes the full history, not just the last failure.
            all_gate_failures: list[dict] = [gate_raw]

            for attempt in range(1, max_retries + 1):
                retries_used = attempt

                with tracer.start_as_current_span(
                    "tb1.phase.retry",
                    attributes={
                        "tb1.phase": "retry",
                        "retry.attempt": attempt,
                        "retry.max_retries": max_retries,
                    },
                ) as retry_span:
                    logger.info(
                        "TB-1 RETRY %d/%d for issue %s (failed at %s)",
                        attempt,
                        max_retries,
                        issue_id,
                        gate_suite.first_failure,
                    )

                    retry_raw = retry_agent(
                        worktree_path=worktree_path,
                        issue_id=issue_id,
                        issue_title=issue_title,
                        issue_description=issue_description,
                        gate_failures=all_gate_failures,
                        attempt=attempt,
                        max_retries=max_retries,
                    )

                    retry_success = retry_raw.get("success", False)
                    retry_span.set_attribute("tb1.retry_success", retry_success)

                    if retry_success:
                        elapsed = time.monotonic() - pipeline_start
                        logger.info(
                            "TB-1 SUCCESS after retry %d: Issue %s in %.1fs",
                            attempt,
                            issue_id,
                            elapsed,
                        )
                        # Reconstruct gate results from the retry
                        retry_gate_results = retry_raw.get("gate_results")
                        if retry_gate_results:
                            gate_suite = GateSuiteResult(**retry_gate_results)

                        root_span.set_attribute("tb1.outcome", "success_after_retry")
                        root_span.set_attribute("tb1.retries_used", attempt)
                        root_span.set_status(
                            trace.StatusCode.OK,
                            f"Gates passed after {attempt} retry(ies)",
                        )
                        return TB1Result(
                            issue_id=issue_id,
                            repo_path=repo_path,
                            success=True,
                            phase="retry_passed",
                            worktree_path=worktree_path,
                            persona=persona_name,
                            agent_exit_code=retry_raw.get("agent_exit_code", 0),
                            gate_results=gate_suite,
                            retries_used=attempt,
                            max_retries=max_retries,
                            duration_seconds=round(elapsed, 2),
                        ).model_dump()

                    # Accumulate this retry's gate failures for the next prompt
                    retry_gate_raw = retry_raw.get("gate_results")
                    if retry_gate_raw:
                        all_gate_failures.append(retry_gate_raw)

            # ----------------------------------------------------------
            # Phase 11: Retries exhausted -> escalate to human
            # ----------------------------------------------------------
            with tracer.start_as_current_span(
                "tb1.phase.escalate",
                attributes={
                    "tb1.phase": "escalate",
                    "escalate.attempts": retries_used + 1,
                },
            ) as esc_span:
                logger.warning(
                    "TB-1 ESCALATE: Issue %s — %d retries exhausted, escalating to human",
                    issue_id,
                    max_retries,
                )

                esc_result = escalate_to_human(
                    issue_id=issue_id,
                    gate_failures=all_gate_failures,
                    attempts=retries_used + 1,  # +1 for the initial attempt
                )

                esc_span.set_attribute(
                    "tb1.escalation_success",
                    esc_result.get("success", False),
                )

            elapsed = time.monotonic() - pipeline_start
            root_span.set_attribute("tb1.outcome", "escalated")
            root_span.set_attribute("tb1.retries_used", retries_used)
            root_span.set_status(
                trace.StatusCode.ERROR,
                f"Escalated after {retries_used} retries",
            )
            return TB1Result(
                issue_id=issue_id,
                repo_path=repo_path,
                success=False,
                phase="escalated",
                worktree_path=worktree_path,
                persona=persona_name,
                gate_results=gate_suite,
                retries_used=retries_used,
                max_retries=max_retries,
                escalated=True,
                error=f"All {retries_used} retries failed; issue escalated to human",
                duration_seconds=round(elapsed, 2),
            ).model_dump()

        except Exception as exc:
            elapsed = time.monotonic() - pipeline_start
            error_msg = f"Pipeline error: {type(exc).__name__}: {exc}"
            logger.exception("TB-1 pipeline error for issue %s", issue_id)
            root_span.set_status(trace.StatusCode.ERROR, error_msg)
            root_span.record_exception(exc)
            return TB1Result(
                issue_id=issue_id,
                repo_path=repo_path,
                success=False,
                phase="error",
                worktree_path=worktree_path,
                persona=persona_name,
                retries_used=retries_used,
                max_retries=max_retries,
                error=error_msg,
                duration_seconds=round(elapsed, 2),
            ).model_dump()

        finally:
            # ----------------------------------------------------------
            # Phase 12: Cleanup — always runs
            # ----------------------------------------------------------
            with tracer.start_as_current_span(
                "tb1.phase.cleanup",
                attributes={"tb1.phase": "cleanup"},
            ):
                # Stop heartbeat
                if heartbeat_event is not None:
                    stop_heartbeat(heartbeat_event)

                # Cleanup worktree (only on success — keep on failure for debugging)
                # For TB-1, always clean up. TB-2 will add a "keep on failure" flag.
                if worktree_path:
                    cleanup_worktree(issue_id)
