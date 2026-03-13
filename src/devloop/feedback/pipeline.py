"""Pipeline orchestrators for dev-loop tracer bullets.

TB-1: Golden path — issue to PR, all gates pass.
TB-2: Failure-to-retry — intentional gate failure, retry loop, blocked escalation.

Both are vertical slices through all six layers:

    intake -> orchestration -> runtime -> gates -> observability -> feedback

Usage::

    from devloop.feedback.pipeline import run_tb1, run_tb2
    result = run_tb1(issue_id="dl-abc", repo_path="/home/user/some-repo")
    result = run_tb2(issue_id="dl-xyz", repo_path="/home/user/some-repo")

Functions are synchronous (single-threaded, blocking). They return
result dicts with full details of what happened at each phase.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from pathlib import Path

from opentelemetry import trace
from opentelemetry.trace import Link

from devloop.feedback.server import escalate_to_human, retry_agent
from devloop.feedback.types import RetryAttempt, SecurityFinding, TB1Result, TB2Result, TB3Result
from devloop.gates.server import run_all_gates
from devloop.gates.types import Finding, GateResult, GateSuiteResult
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
# OTel tracers — use the global provider set up by init_tracing()
# ---------------------------------------------------------------------------

tracer = trace.get_tracer("tb1", "0.1.0")
tracer_tb2 = trace.get_tracer("tb2", "0.1.0")
tracer_tb3 = trace.get_tracer("tb3", "0.1.0")

# ---------------------------------------------------------------------------
# TB-2 test fixtures path
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).resolve().parents[3] / "test-fixtures"


def _unclaim_issue(issue_id: str) -> None:
    """Release a claimed issue back to open status (M8 fix).

    Called in finally blocks when a pipeline fails without completing
    successfully, so the issue doesn't stay stuck as in_progress.
    """
    try:
        subprocess.run(
            ["br", "update", issue_id, "--status", "open"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        logger.info("Unclaimed issue %s (set to open)", issue_id)
    except Exception:
        logger.warning("Failed to unclaim issue %s", issue_id)


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
    provider = init_tracing()

    with tracer.start_as_current_span(
        "tb1.run",
        attributes={
            "tb1.issue_id": issue_id,
            "tb1.repo_path": repo_path,
        },
    ) as root_span:
        # Track state for cleanup
        heartbeat_event = None
        heartbeat_thread = None
        worktree_path: str | None = None
        persona_name: str | None = None
        pipeline_success = False
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
                heartbeat_event, heartbeat_thread = start_heartbeat(
                    issue_id, interval_seconds=30, worktree_path=worktree_path,
                )

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
                try:
                    gate_suite = GateSuiteResult(**gate_raw)
                except Exception as exc:
                    elapsed = time.monotonic() - pipeline_start
                    error_msg = f"Malformed gate result: {exc}"
                    gates_span.set_status(trace.StatusCode.ERROR, error_msg)
                    root_span.set_status(trace.StatusCode.ERROR, error_msg)
                    return TB1Result(
                        issue_id=issue_id,
                        repo_path=repo_path,
                        success=False,
                        phase="gates",
                        worktree_path=worktree_path,
                        persona=persona_name,
                        error=error_msg,
                        duration_seconds=round(elapsed, 2),
                    ).model_dump()

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
                pipeline_success = True
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
                        pipeline_success = True
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

                    # Accumulate failures for next retry prompt (M6: include spawn failures)
                    retry_gate_raw = retry_raw.get("gate_results")
                    if retry_gate_raw:
                        all_gate_failures.append(retry_gate_raw)
                    elif retry_raw.get("error"):
                        # Agent spawn itself failed — synthesize a failure record
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
            try:
                with tracer.start_as_current_span(
                    "tb1.phase.cleanup",
                    attributes={"tb1.phase": "cleanup"},
                ):
                    pass  # span for observability only
            except Exception:
                pass  # OTel failure must not block cleanup

            # Stop heartbeat and join thread before cleanup
            if heartbeat_event is not None:
                stop_heartbeat(heartbeat_event, heartbeat_thread)

            # Cleanup worktree
            if worktree_path:
                cleanup_worktree(issue_id)

            # Unclaim issue if pipeline didn't succeed (M8 fix)
            if not pipeline_success:
                _unclaim_issue(issue_id)

            # Flush spans (M1 fix — TB-1 was missing force_flush)
            if provider is not None:
                try:
                    provider.force_flush(timeout_millis=5000)
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# TB-2 helpers
# ---------------------------------------------------------------------------


def _span_id_hex(span: trace.Span) -> str:
    """Extract the hex span ID from an OTel span."""
    ctx = span.get_span_context()
    return format(ctx.span_id, "016x")


def _trace_id_hex(span: trace.Span) -> str:
    """Extract the hex trace ID from an OTel span."""
    ctx = span.get_span_context()
    return format(ctx.trace_id, "032x")


def _seed_test_fixture(worktree_path: str) -> bool:
    """Copy TB-2 trap test into the worktree's tests/ dir.

    Returns True if the file was successfully copied.
    """
    src = _FIXTURES_DIR / "tests" / "test_factorial_trap.py"
    if not src.exists():
        logger.warning("TB-2 test fixture not found: %s", src)
        return False

    dst_dir = Path(worktree_path) / "tests"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / "test_factorial.py"
    shutil.copy2(src, dst)
    logger.info("Seeded TB-2 test fixture → %s", dst)
    return True


def _make_forced_failure() -> dict:
    """Return a synthetic GateSuiteResult dict that always fails."""
    return GateSuiteResult(
        overall_passed=False,
        gate_results=[
            GateResult(
                gate_name="gate_0_sanity",
                passed=False,
                findings=[
                    Finding(
                        severity="critical",
                        message=(
                            "[TB-2 FORCED FAILURE] Simulated gate failure "
                            "for retry path verification"
                        ),
                    )
                ],
                duration_seconds=0.0,
            )
        ],
        first_failure="gate_0_sanity",
        total_duration_seconds=0.0,
    ).model_dump()


def _verify_blocked_status(issue_id: str) -> bool:
    """Check that a beads issue has status 'blocked'."""
    try:
        result = subprocess.run(
            ["br", "show", issue_id, "--format", "json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warning(
                "br show failed for %s: %s", issue_id, result.stderr.strip()[:200]
            )
            return False

        data = json.loads(result.stdout)
        # br show returns a list with one item
        if isinstance(data, list) and len(data) > 0:
            data = data[0]
        status = data.get("status", "").lower()
        return status == "blocked"
    except (json.JSONDecodeError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Failed to verify blocked status for %s: %s", issue_id, exc)
        return False


# ---------------------------------------------------------------------------
# TB-2 Pipeline
# ---------------------------------------------------------------------------


def run_tb2(
    issue_id: str,
    repo_path: str,
    max_retries: int = 2,
    force_gate_fail: bool = False,
) -> dict:
    """Run the full TB-2 failure-to-retry path for a single issue.

    TB-2 proves the feedback loop actually loops. It seeds a tricky test
    fixture, runs the agent, and verifies the retry/escalation path.

    Modes:
        - Organic: Uses a tricky issue + pre-seeded test that is likely
          (but not guaranteed) to fail on the first attempt.
        - Forced (force_gate_fail=True): Artificially fails Gate 0 on the
          first attempt, then runs gates normally on retry.

    Args:
        issue_id: The beads issue ID to process.
        repo_path: Absolute path to the git repository.
        max_retries: Maximum retry attempts (default 2).
        force_gate_fail: If True, force the first gate run to fail.

    Returns:
        A dict (TB2Result) with outcome, trace IDs, and retry history.
    """
    pipeline_start = time.monotonic()

    # Phase 5 — init tracing early so all subsequent spans are captured
    provider = init_tracing()

    with tracer_tb2.start_as_current_span(
        "tb2.run",
        attributes={
            "tb2.issue_id": issue_id,
            "tb2.repo_path": repo_path,
            "tb2.max_retries": max_retries,
            "tb2.force_gate_fail": force_gate_fail,
        },
    ) as root_span:
        root_trace_id = _trace_id_hex(root_span)
        attempt_span_ids: list[str] = []
        retry_history: list[RetryAttempt] = []

        # Track state for cleanup
        heartbeat_event = None
        heartbeat_thread = None
        worktree_path: str | None = None
        persona_name: str | None = None
        pipeline_success = False
        retries_used = 0

        try:
            # ----------------------------------------------------------
            # Phase 1: Poll beads for the issue
            # ----------------------------------------------------------
            with tracer_tb2.start_as_current_span(
                "tb2.phase.poll",
                attributes={"tb2.phase": "poll"},
            ) as poll_span:
                items = poll_ready()
                issue = None
                for item in items:
                    if item.id == issue_id:
                        issue = item
                        break

                if issue is None:
                    poll_span.set_attribute("tb2.issue_found_in_poll", False)
                    logger.info(
                        "Issue %s not found in ready poll; proceeding with claim",
                        issue_id,
                    )
                    issue_title = issue_id
                    issue_description = ""
                    issue_labels: list[str] = []
                else:
                    poll_span.set_attribute("tb2.issue_found_in_poll", True)
                    issue_title = issue.title
                    issue_description = issue.description or ""
                    issue_labels = issue.labels

                poll_span.set_attribute("tb2.ready_count", len(items))

            # ----------------------------------------------------------
            # Phase 2: Claim the issue
            # ----------------------------------------------------------
            with tracer_tb2.start_as_current_span(
                "tb2.phase.claim",
                attributes={"tb2.phase": "claim", "issue.id": issue_id},
            ) as claim_span:
                claimed = claim_issue(issue_id)
                claim_span.set_attribute("tb2.claimed", claimed)

                if not claimed:
                    elapsed = time.monotonic() - pipeline_start
                    claim_span.set_status(
                        trace.StatusCode.ERROR,
                        f"Failed to claim issue {issue_id}",
                    )
                    return TB2Result(
                        issue_id=issue_id,
                        repo_path=repo_path,
                        success=False,
                        phase="claim",
                        error=f"Could not claim issue {issue_id}",
                        duration_seconds=round(elapsed, 2),
                        trace_id=root_trace_id,
                        force_gate_fail_used=force_gate_fail,
                    ).model_dump()

            # ----------------------------------------------------------
            # Phase 3: Setup worktree
            # ----------------------------------------------------------
            with tracer_tb2.start_as_current_span(
                "tb2.phase.setup_worktree",
                attributes={"tb2.phase": "setup_worktree"},
            ):
                wt_result = setup_worktree(issue_id, repo_path)

                if not wt_result.get("success"):
                    elapsed = time.monotonic() - pipeline_start
                    return TB2Result(
                        issue_id=issue_id,
                        repo_path=repo_path,
                        success=False,
                        phase="setup_worktree",
                        error=wt_result.get("message", "Worktree setup failed"),
                        duration_seconds=round(elapsed, 2),
                        trace_id=root_trace_id,
                        force_gate_fail_used=force_gate_fail,
                    ).model_dump()

                worktree_path = wt_result["worktree_path"]

            # ----------------------------------------------------------
            # Phase 3.5: Seed test fixture into worktree
            # ----------------------------------------------------------
            with tracer_tb2.start_as_current_span(
                "tb2.phase.seed_fixture",
                attributes={"tb2.phase": "seed_fixture"},
            ) as seed_span:
                seeded = _seed_test_fixture(worktree_path)
                seed_span.set_attribute("tb2.fixture_seeded", seeded)

            # ----------------------------------------------------------
            # Phase 4: Select persona + build CLAUDE.md overlay
            # ----------------------------------------------------------
            with tracer_tb2.start_as_current_span(
                "tb2.phase.persona",
                attributes={"tb2.phase": "persona"},
            ) as persona_span:
                persona_result = select_persona(issue_labels)
                persona_name = persona_result.get("name", "feature")
                # TB-2 uses its own max_retries, not the persona's
                persona_span.set_attribute("tb2.persona", persona_name)
                persona_span.set_attribute("tb2.max_retries", max_retries)

                overlay_result = build_claude_md_overlay(
                    persona=persona_name,
                    issue_title=issue_title,
                    issue_description=issue_description,
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

            # ----------------------------------------------------------
            # Phase 6: Start heartbeat
            # ----------------------------------------------------------
            with tracer_tb2.start_as_current_span(
                "tb2.phase.heartbeat_start",
                attributes={"tb2.phase": "heartbeat_start"},
            ):
                heartbeat_event, heartbeat_thread = start_heartbeat(
                    issue_id, interval_seconds=30, worktree_path=worktree_path,
                )

            # ----------------------------------------------------------
            # Phase 7: Initial agent spawn
            # ----------------------------------------------------------
            with tracer_tb2.start_as_current_span(
                "tb2.phase.spawn_agent",
                attributes={"tb2.phase": "spawn_agent", "tb2.attempt": 0},
            ) as agent_span:
                task_prompt = overlay_text or f"Fix issue: {issue_title}\n\n{issue_description}"

                agent_result = spawn_agent(
                    worktree_path=worktree_path,
                    task_prompt=task_prompt,
                    model=persona_result.get("model", "sonnet"),
                )

                agent_exit = agent_result.get("exit_code", -1)
                agent_span.set_attribute("tb2.agent_exit_code", agent_exit)
                attempt_span_ids.append(_span_id_hex(agent_span))

                if agent_exit != 0:
                    elapsed = time.monotonic() - pipeline_start
                    return TB2Result(
                        issue_id=issue_id,
                        repo_path=repo_path,
                        success=False,
                        phase="spawn_agent",
                        worktree_path=worktree_path,
                        persona=persona_name,
                        error=agent_result.get("stderr", "Agent failed"),
                        max_retries=max_retries,
                        duration_seconds=round(elapsed, 2),
                        trace_id=root_trace_id,
                        attempt_span_ids=attempt_span_ids,
                        force_gate_fail_used=force_gate_fail,
                    ).model_dump()

            # ----------------------------------------------------------
            # Phase 8: Run quality gates (or force-fail on first attempt)
            # ----------------------------------------------------------
            with tracer_tb2.start_as_current_span(
                "tb2.phase.gates",
                attributes={
                    "tb2.phase": "gates",
                    "tb2.attempt": 0,
                    "tb2.force_fail": force_gate_fail,
                },
            ) as gates_span:
                if force_gate_fail:
                    logger.info("TB-2: FORCED FAILURE on initial gate run")
                    gate_raw = _make_forced_failure()
                else:
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
                    return TB2Result(
                        issue_id=issue_id,
                        repo_path=repo_path,
                        success=False,
                        phase="gates",
                        worktree_path=worktree_path,
                        persona=persona_name,
                        error=error_msg,
                        duration_seconds=round(elapsed, 2),
                        trace_id=root_trace_id,
                        attempt_span_ids=attempt_span_ids,
                        force_gate_fail_used=force_gate_fail,
                    ).model_dump()

                gates_span.set_attribute("tb2.gates_passed", gate_suite.overall_passed)
                if gate_suite.first_failure:
                    gates_span.set_attribute("tb2.first_failure", gate_suite.first_failure)

                # Record initial attempt
                retry_history.append(
                    RetryAttempt(
                        attempt=0,
                        agent_exit_code=agent_exit,
                        gates_passed=gate_suite.overall_passed,
                        first_failure=gate_suite.first_failure,
                        span_id=attempt_span_ids[0] if attempt_span_ids else None,
                    )
                )

            # ----------------------------------------------------------
            # Phase 9: Gates passed on first try -> success
            # ----------------------------------------------------------
            if gate_suite.overall_passed:
                elapsed = time.monotonic() - pipeline_start
                logger.info(
                    "TB-2: Gates passed on first attempt (%.1fs). "
                    "Retry path was NOT exercised.",
                    elapsed,
                )
                root_span.set_attribute("tb2.outcome", "success_first_attempt")
                pipeline_success = True
                root_span.set_status(
                    trace.StatusCode.OK,
                    "Gates passed on first attempt — retry path not exercised",
                )
                return TB2Result(
                    issue_id=issue_id,
                    repo_path=repo_path,
                    success=True,
                    phase="gates_passed_first",
                    worktree_path=worktree_path,
                    persona=persona_name,
                    max_retries=max_retries,
                    duration_seconds=round(elapsed, 2),
                    trace_id=root_trace_id,
                    attempt_span_ids=attempt_span_ids,
                    force_gate_fail_used=force_gate_fail,
                    retry_history=retry_history,
                ).model_dump()

            # ----------------------------------------------------------
            # Phase 10: Gates failed -> retry loop with span linking
            # ----------------------------------------------------------
            all_gate_failures: list[dict] = [gate_raw]
            previous_span_context = None

            for attempt in range(1, max_retries + 1):
                retries_used = attempt

                # Build span links to previous attempt
                links: list[Link] = []
                if previous_span_context is not None:
                    links = [Link(previous_span_context)]

                with tracer_tb2.start_as_current_span(
                    "tb2.phase.retry",
                    links=links,
                    attributes={
                        "tb2.phase": "retry",
                        "retry.attempt": attempt,
                        "retry.max_retries": max_retries,
                        "retry.linked_to_previous": len(links) > 0,
                    },
                ) as retry_span:
                    # Capture this span's context for next iteration's link
                    previous_span_context = retry_span.get_span_context()
                    attempt_span_ids.append(_span_id_hex(retry_span))

                    logger.info(
                        "TB-2 RETRY %d/%d for issue %s (failed at %s)",
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
                    retry_span.set_attribute("tb2.retry_success", retry_success)

                    # Record this attempt
                    retry_history.append(
                        RetryAttempt(
                            attempt=attempt,
                            agent_exit_code=retry_raw.get("agent_exit_code", -1),
                            gates_passed=retry_success,
                            first_failure=retry_raw.get("gate_results", {}).get(
                                "first_failure"
                            )
                            if retry_raw.get("gate_results")
                            else None,
                            span_id=attempt_span_ids[-1],
                        )
                    )

                    if retry_success:
                        elapsed = time.monotonic() - pipeline_start
                        logger.info(
                            "TB-2 SUCCESS after retry %d: Issue %s in %.1fs",
                            attempt,
                            issue_id,
                            elapsed,
                        )

                        root_span.set_attribute("tb2.outcome", "success_after_retry")
                        pipeline_success = True
                        root_span.set_attribute("tb2.retries_used", attempt)
                        root_span.set_status(
                            trace.StatusCode.OK,
                            f"Gates passed after {attempt} retry(ies)",
                        )
                        return TB2Result(
                            issue_id=issue_id,
                            repo_path=repo_path,
                            success=True,
                            phase="retry_passed",
                            worktree_path=worktree_path,
                            persona=persona_name,
                            retries_used=attempt,
                            max_retries=max_retries,
                            duration_seconds=round(elapsed, 2),
                            trace_id=root_trace_id,
                            attempt_span_ids=attempt_span_ids,
                            force_gate_fail_used=force_gate_fail,
                            retry_history=retry_history,
                        ).model_dump()

                    # Accumulate failures for next retry prompt (M6: include spawn failures)
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
            # Phase 11: Retries exhausted -> escalate + verify blocked
            # ----------------------------------------------------------
            with tracer_tb2.start_as_current_span(
                "tb2.phase.escalate",
                attributes={
                    "tb2.phase": "escalate",
                    "escalate.attempts": retries_used + 1,
                },
            ) as esc_span:
                logger.warning(
                    "TB-2 ESCALATE: Issue %s — %d retries exhausted",
                    issue_id,
                    max_retries,
                )

                esc_result = escalate_to_human(
                    issue_id=issue_id,
                    gate_failures=all_gate_failures,
                    attempts=retries_used + 1,
                )

                esc_span.set_attribute(
                    "tb2.escalation_success",
                    esc_result.get("success", False),
                )

                # TB-2 specific: verify the issue is actually blocked
                blocked_verified = _verify_blocked_status(issue_id)
                esc_span.set_attribute("tb2.blocked_verified", blocked_verified)

                if blocked_verified:
                    logger.info("TB-2: Verified issue %s status is 'blocked'", issue_id)
                else:
                    logger.warning(
                        "TB-2: Could not verify 'blocked' status for %s", issue_id
                    )

            elapsed = time.monotonic() - pipeline_start
            root_span.set_attribute("tb2.outcome", "escalated")
            root_span.set_attribute("tb2.retries_used", retries_used)
            root_span.set_attribute("tb2.blocked_verified", blocked_verified)
            root_span.set_status(
                trace.StatusCode.ERROR,
                f"Escalated after {retries_used} retries",
            )
            return TB2Result(
                issue_id=issue_id,
                repo_path=repo_path,
                success=False,
                phase="escalated",
                worktree_path=worktree_path,
                persona=persona_name,
                retries_used=retries_used,
                max_retries=max_retries,
                escalated=True,
                error=f"All {retries_used} retries failed; issue escalated",
                duration_seconds=round(elapsed, 2),
                trace_id=root_trace_id,
                attempt_span_ids=attempt_span_ids,
                blocked_verified=blocked_verified,
                force_gate_fail_used=force_gate_fail,
                retry_history=retry_history,
            ).model_dump()

        except Exception as exc:
            elapsed = time.monotonic() - pipeline_start
            error_msg = f"Pipeline error: {type(exc).__name__}: {exc}"
            logger.exception("TB-2 pipeline error for issue %s", issue_id)
            root_span.set_status(trace.StatusCode.ERROR, error_msg)
            root_span.record_exception(exc)
            return TB2Result(
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
                trace_id=root_trace_id,
                attempt_span_ids=attempt_span_ids,
                force_gate_fail_used=force_gate_fail,
                retry_history=retry_history,
            ).model_dump()

        finally:
            # ----------------------------------------------------------
            # Phase 12: Cleanup
            # ----------------------------------------------------------
            try:
                with tracer_tb2.start_as_current_span(
                    "tb2.phase.cleanup",
                    attributes={"tb2.phase": "cleanup"},
                ):
                    pass
            except Exception:
                pass

            if heartbeat_event is not None:
                stop_heartbeat(heartbeat_event, heartbeat_thread)

            # TB-2: keep worktree on escalation for debugging
            if worktree_path and not retries_used >= max_retries:
                cleanup_worktree(issue_id)
            elif worktree_path:
                logger.info(
                    "TB-2: Preserving worktree at %s for post-mortem",
                    worktree_path,
                )

            # Unclaim issue if pipeline didn't succeed (M8 fix)
            if not pipeline_success:
                _unclaim_issue(issue_id)

            # Force flush OTel spans so they're available for verification
            if provider is not None:
                try:
                    provider.force_flush(timeout_millis=5000)
                except Exception:
                    logger.warning("Failed to flush OTel spans")


# ---------------------------------------------------------------------------
# TB-3 helpers
# ---------------------------------------------------------------------------


def _seed_vulnerable_code(worktree_path: str) -> bool:
    """Copy TB-3 vulnerable code fixture into the worktree.

    Copies the intentionally vulnerable search module into the worktree's
    src directory. The file contains SQL injection vulnerabilities that
    bandit will flag.

    Returns True if the file was successfully copied.
    """
    src = _FIXTURES_DIR / "code" / "vulnerable_search.py"
    if not src.exists():
        logger.warning("TB-3 vulnerable code fixture not found: %s", src)
        return False

    # Determine the package directory (e.g. src/prompt_bench/)
    src_dir = Path(worktree_path) / "src"
    if not src_dir.is_dir():
        src_dir.mkdir(parents=True, exist_ok=True)

    # Find existing package dir under src/, or create one
    pkg_dirs = [d for d in src_dir.iterdir() if d.is_dir() and (d / "__init__.py").exists()]
    if pkg_dirs:
        dst_dir = pkg_dirs[0]
    else:
        dst_dir = src_dir
        dst_dir.mkdir(parents=True, exist_ok=True)

    dst = dst_dir / "search.py"
    shutil.copy2(src, dst)
    logger.info("Seeded TB-3 vulnerable code → %s", dst)

    # Commit the seeded file so Gate 0 sees committed changes and
    # Gate 3 scans committed code (agent's fix will be a separate commit).
    # Check return codes — silent git failure = false positive (C3 fix).
    try:
        add_r = subprocess.run(
            ["git", "add", str(dst)],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if add_r.returncode != 0:
            logger.error("git add failed for seeded file: %s", add_r.stderr.strip())
            return False

        commit_r = subprocess.run(
            ["git", "commit", "-m", "Add user search endpoint (seeded for TB-3)"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if commit_r.returncode != 0:
            logger.error("git commit failed for seeded file: %s", commit_r.stderr.strip())
            return False
    except subprocess.TimeoutExpired:
        logger.error("git command timed out during seed")
        return False

    return True


def _make_forced_security_failure() -> dict:
    """Return a synthetic GateSuiteResult that fails at Gate 3 with a security finding."""
    return GateSuiteResult(
        overall_passed=False,
        gate_results=[
            GateResult(
                gate_name="gate_0_sanity",
                passed=True,
                duration_seconds=0.0,
            ),
            GateResult(
                gate_name="gate_2_secrets",
                passed=True,
                duration_seconds=0.0,
            ),
            GateResult(
                gate_name="gate_3_security",
                passed=False,
                findings=[
                    Finding(
                        severity="critical",
                        message=(
                            "Possible SQL injection vector through string-based "
                            "query construction. [CWE-89]"
                        ),
                        file="src/prompt_bench/search.py",
                        line=25,
                        rule="B608",
                        cwe="CWE-89",
                    ),
                    Finding(
                        severity="critical",
                        message=(
                            "Possible SQL injection vector through string-based "
                            "query construction. [CWE-89]"
                        ),
                        file="src/prompt_bench/search.py",
                        line=42,
                        rule="B608",
                        cwe="CWE-89",
                    ),
                ],
                duration_seconds=0.0,
            ),
        ],
        first_failure="gate_3_security",
        total_duration_seconds=0.0,
    ).model_dump()


def _extract_security_findings(gate_suite_dict: dict) -> tuple[list[SecurityFinding], bool]:
    """Extract security findings from a gate suite result.

    Returns:
        A tuple of (findings, gate_3_ran). gate_3_ran is False if Gate 3
        was not found or was skipped — callers must not treat empty findings
        as "vulnerability fixed" in that case (M9/M10 fix).
    """
    findings: list[SecurityFinding] = []
    gate_3_found = False
    gate_3_skipped = False

    for gate_result in gate_suite_dict.get("gate_results", []):
        if gate_result.get("gate_name") == "gate_3_security":
            gate_3_found = True
            gate_3_skipped = gate_result.get("skipped", False)
            for f in gate_result.get("findings", []):
                if f.get("severity") in ("critical", "warning"):
                    findings.append(
                        SecurityFinding(
                            cwe=f.get("cwe"),
                            severity=f.get("severity", "critical"),
                            message=f.get("message", ""),
                            file=f.get("file"),
                            line=f.get("line"),
                            rule=f.get("rule"),
                        )
                    )

    if not gate_3_found:
        logger.warning(
            "_extract_security_findings: gate_3_security not found in gate results"
        )

    gate_3_ran = gate_3_found and not gate_3_skipped
    return findings, gate_3_ran


# ---------------------------------------------------------------------------
# TB-3 Pipeline
# ---------------------------------------------------------------------------


def run_tb3(
    issue_id: str,
    repo_path: str,
    max_retries: int = 3,
    force_vuln_seed: bool = True,
) -> dict:
    """Run the full TB-3 security-gate-to-fix path for a single issue.

    TB-3 proves the security scanning loop works. It seeds vulnerable code,
    the security gate catches it, and the agent self-remediates.

    Modes:
        - Forced (force_vuln_seed=True, default): Pre-seeds a known vulnerable
          file into the worktree. Gate 3 catches it deterministically.
        - Organic (force_vuln_seed=False): Relies on the agent writing
          vulnerable code based on the ticket instructions.

    Args:
        issue_id: The beads issue ID to process.
        repo_path: Absolute path to the git repository.
        max_retries: Maximum retry attempts (default 3, security persona default).
        force_vuln_seed: If True, seed known vulnerable code into worktree.

    Returns:
        A dict (TB3Result) with outcome, security findings, and CWE IDs.
    """
    pipeline_start = time.monotonic()

    # Phase 5 — init tracing early
    provider = init_tracing()

    with tracer_tb3.start_as_current_span(
        "tb3.run",
        attributes={
            "tb3.issue_id": issue_id,
            "tb3.repo_path": repo_path,
            "tb3.max_retries": max_retries,
            "tb3.force_vuln_seed": force_vuln_seed,
        },
    ) as root_span:
        root_trace_id = _trace_id_hex(root_span)
        attempt_span_ids: list[str] = []
        retry_history: list[RetryAttempt] = []
        security_findings: list[SecurityFinding] = []
        cwe_ids: list[str] = []

        # Track state for cleanup
        heartbeat_event = None
        heartbeat_thread = None
        worktree_path: str | None = None
        persona_name: str | None = None
        pipeline_success = False
        retries_used = 0

        try:
            # ----------------------------------------------------------
            # Phase 1: Poll beads for the issue
            # ----------------------------------------------------------
            with tracer_tb3.start_as_current_span(
                "tb3.phase.poll",
                attributes={"tb3.phase": "poll"},
            ) as poll_span:
                items = poll_ready()
                issue = None
                for item in items:
                    if item.id == issue_id:
                        issue = item
                        break

                if issue is None:
                    poll_span.set_attribute("tb3.issue_found_in_poll", False)
                    logger.info(
                        "Issue %s not found in ready poll; proceeding with claim",
                        issue_id,
                    )
                    issue_title = issue_id
                    issue_description = ""
                    issue_labels: list[str] = ["security"]
                else:
                    poll_span.set_attribute("tb3.issue_found_in_poll", True)
                    issue_title = issue.title
                    issue_description = issue.description or ""
                    issue_labels = issue.labels
                    # Ensure security label for persona selection
                    if "security" not in issue_labels:
                        issue_labels.append("security")

                poll_span.set_attribute("tb3.ready_count", len(items))

            # ----------------------------------------------------------
            # Phase 2: Claim the issue
            # ----------------------------------------------------------
            with tracer_tb3.start_as_current_span(
                "tb3.phase.claim",
                attributes={"tb3.phase": "claim", "issue.id": issue_id},
            ) as claim_span:
                claimed = claim_issue(issue_id)
                claim_span.set_attribute("tb3.claimed", claimed)

                if not claimed:
                    elapsed = time.monotonic() - pipeline_start
                    claim_span.set_status(
                        trace.StatusCode.ERROR,
                        f"Failed to claim issue {issue_id}",
                    )
                    return TB3Result(
                        issue_id=issue_id,
                        repo_path=repo_path,
                        success=False,
                        phase="claim",
                        error=f"Could not claim issue {issue_id}",
                        duration_seconds=round(elapsed, 2),
                        trace_id=root_trace_id,
                        vuln_seeded=force_vuln_seed,
                    ).model_dump()

            # ----------------------------------------------------------
            # Phase 3: Setup worktree
            # ----------------------------------------------------------
            with tracer_tb3.start_as_current_span(
                "tb3.phase.setup_worktree",
                attributes={"tb3.phase": "setup_worktree"},
            ):
                wt_result = setup_worktree(issue_id, repo_path)

                if not wt_result.get("success"):
                    elapsed = time.monotonic() - pipeline_start
                    return TB3Result(
                        issue_id=issue_id,
                        repo_path=repo_path,
                        success=False,
                        phase="setup_worktree",
                        error=wt_result.get("message", "Worktree setup failed"),
                        duration_seconds=round(elapsed, 2),
                        trace_id=root_trace_id,
                        vuln_seeded=force_vuln_seed,
                    ).model_dump()

                worktree_path = wt_result["worktree_path"]

            # ----------------------------------------------------------
            # Phase 3.5: Seed vulnerable code (forced mode)
            # ----------------------------------------------------------
            with tracer_tb3.start_as_current_span(
                "tb3.phase.seed_vulnerability",
                attributes={
                    "tb3.phase": "seed_vulnerability",
                    "tb3.force_vuln_seed": force_vuln_seed,
                },
            ) as seed_span:
                if force_vuln_seed:
                    seeded = _seed_vulnerable_code(worktree_path)
                    seed_span.set_attribute("tb3.vuln_seeded", seeded)
                    if not seeded:
                        raise RuntimeError(
                            "TB-3 forced mode: _seed_vulnerable_code failed — "
                            "cannot proceed without seeded vulnerability"
                        )
                else:
                    seed_span.set_attribute("tb3.vuln_seeded", False)

            # ----------------------------------------------------------
            # Phase 4: Select persona + build CLAUDE.md overlay
            # ----------------------------------------------------------
            with tracer_tb3.start_as_current_span(
                "tb3.phase.persona",
                attributes={"tb3.phase": "persona"},
            ) as persona_span:
                persona_result = select_persona(issue_labels)
                persona_name = persona_result.get("name", "security-fix")
                persona_span.set_attribute("tb3.persona", persona_name)
                persona_span.set_attribute("tb3.max_retries", max_retries)

                overlay_result = build_claude_md_overlay(
                    persona=persona_name,
                    issue_title=issue_title,
                    issue_description=issue_description,
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

            # ----------------------------------------------------------
            # Phase 6: Start heartbeat
            # ----------------------------------------------------------
            with tracer_tb3.start_as_current_span(
                "tb3.phase.heartbeat_start",
                attributes={"tb3.phase": "heartbeat_start"},
            ):
                heartbeat_event, heartbeat_thread = start_heartbeat(
                    issue_id, interval_seconds=30, worktree_path=worktree_path,
                )

            # ----------------------------------------------------------
            # Phase 7: Pre-flight gate scan (catches seeded vulnerability)
            # ----------------------------------------------------------
            # In TB-3, we run gates BEFORE the agent so Gate 3 catches
            # the seeded vulnerability. The agent then gets the findings
            # as context and must fix them.
            with tracer_tb3.start_as_current_span(
                "tb3.phase.preflight_gates",
                attributes={
                    "tb3.phase": "preflight_gates",
                    "tb3.attempt": 0,
                },
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
                    return TB3Result(
                        issue_id=issue_id,
                        repo_path=repo_path,
                        success=False,
                        phase="preflight_gates",
                        worktree_path=worktree_path,
                        persona=persona_name,
                        error=error_msg,
                        duration_seconds=round(elapsed, 2),
                        trace_id=root_trace_id,
                        attempt_span_ids=attempt_span_ids,
                        vuln_seeded=force_vuln_seed,
                    ).model_dump()

                gates_span.set_attribute("tb3.gates_passed", gate_suite.overall_passed)
                if gate_suite.first_failure:
                    gates_span.set_attribute("tb3.first_failure", gate_suite.first_failure)

                # Extract security findings for result tracking
                security_findings, _gate_3_ran = _extract_security_findings(gate_raw)
                cwe_ids = list({f.cwe for f in security_findings if f.cwe})

                if cwe_ids:
                    gates_span.set_attribute("tb3.cwe_ids", ",".join(cwe_ids))
                    root_span.set_attribute("tb3.initial_cwe_ids", ",".join(cwe_ids))

                attempt_span_ids.append(_span_id_hex(gates_span))

                # Record pre-flight scan result
                retry_history.append(
                    RetryAttempt(
                        attempt=0,
                        agent_exit_code=-1,  # no agent ran yet
                        gates_passed=gate_suite.overall_passed,
                        first_failure=gate_suite.first_failure,
                        span_id=attempt_span_ids[0] if attempt_span_ids else None,
                    )
                )

            # ----------------------------------------------------------
            # Phase 8: Check if gates already pass (no vulnerability found)
            # ----------------------------------------------------------
            if gate_suite.overall_passed:
                elapsed = time.monotonic() - pipeline_start
                logger.info(
                    "TB-3: Pre-flight gates passed (%.1fs). "
                    "Security gate did NOT catch a vulnerability.",
                    elapsed,
                )
                root_span.set_attribute("tb3.outcome", "no_vulnerability_detected")
                pipeline_success = True
                root_span.set_status(
                    trace.StatusCode.OK,
                    "Pre-flight scan found no vulnerability",
                )
                return TB3Result(
                    issue_id=issue_id,
                    repo_path=repo_path,
                    success=True,
                    phase="preflight_clean",
                    worktree_path=worktree_path,
                    persona=persona_name,
                    max_retries=max_retries,
                    duration_seconds=round(elapsed, 2),
                    trace_id=root_trace_id,
                    attempt_span_ids=attempt_span_ids,
                    security_findings=security_findings,
                    cwe_ids=cwe_ids,
                    vuln_seeded=force_vuln_seed,
                    retry_history=retry_history,
                ).model_dump()

            logger.info(
                "TB-3 PRE-FLIGHT: Gate 3 caught vulnerability (CWEs: %s) — "
                "feeding findings to agent for remediation",
                cwe_ids,
            )

            # ----------------------------------------------------------
            # Phase 10: Gates failed -> retry loop with span linking
            # ----------------------------------------------------------
            all_gate_failures: list[dict] = [gate_raw]
            previous_span_context = None

            for attempt in range(1, max_retries + 1):
                retries_used = attempt

                # Build span links to previous attempt
                links: list[Link] = []
                if previous_span_context is not None:
                    links = [Link(previous_span_context)]

                with tracer_tb3.start_as_current_span(
                    "tb3.phase.retry",
                    links=links,
                    attributes={
                        "tb3.phase": "retry",
                        "retry.attempt": attempt,
                        "retry.max_retries": max_retries,
                        "retry.linked_to_previous": len(links) > 0,
                        "retry.security_findings": len(security_findings),
                    },
                ) as retry_span:
                    previous_span_context = retry_span.get_span_context()
                    attempt_span_ids.append(_span_id_hex(retry_span))

                    logger.info(
                        "TB-3 RETRY %d/%d for issue %s (failed at %s, CWEs: %s)",
                        attempt,
                        max_retries,
                        issue_id,
                        gate_suite.first_failure,
                        cwe_ids,
                    )

                    retry_raw = retry_agent(
                        worktree_path=worktree_path,
                        issue_id=issue_id,
                        issue_title=issue_title,
                        issue_description=issue_description,
                        gate_failures=all_gate_failures,
                        attempt=attempt,
                        max_retries=max_retries,
                        model=persona_result.get("model", "opus"),
                    )

                    retry_success = retry_raw.get("success", False)
                    retry_span.set_attribute("tb3.retry_success", retry_success)

                    # Check if security findings are now fixed.
                    # vuln_fixed is only true if Gate 3 actually ran AND
                    # found zero issues (M9 fix: skipped Gate 3 ≠ fixed).
                    retry_gate_results = retry_raw.get("gate_results")
                    retry_sec_findings: list[SecurityFinding] = []
                    retry_gate_3_ran = False
                    if retry_gate_results:
                        retry_sec_findings, retry_gate_3_ran = _extract_security_findings(
                            retry_gate_results
                        )

                    vuln_fixed = (
                        retry_gate_3_ran
                        and len(retry_sec_findings) == 0
                        and len(security_findings) > 0
                    )
                    retry_span.set_attribute("tb3.vulnerability_fixed", vuln_fixed)

                    # Record this attempt
                    retry_history.append(
                        RetryAttempt(
                            attempt=attempt,
                            agent_exit_code=retry_raw.get("agent_exit_code", -1),
                            gates_passed=retry_success,
                            first_failure=retry_raw.get("gate_results", {}).get(
                                "first_failure"
                            )
                            if retry_raw.get("gate_results")
                            else None,
                            span_id=attempt_span_ids[-1],
                        )
                    )

                    if retry_success:
                        elapsed = time.monotonic() - pipeline_start
                        logger.info(
                            "TB-3 SUCCESS after retry %d: Issue %s in %.1fs "
                            "(vulnerability %s)",
                            attempt,
                            issue_id,
                            elapsed,
                            "FIXED" if vuln_fixed else "not applicable",
                        )

                        root_span.set_attribute("tb3.outcome", "success_after_retry")
                        pipeline_success = True
                        root_span.set_attribute("tb3.retries_used", attempt)
                        root_span.set_attribute("tb3.vulnerability_fixed", vuln_fixed)
                        root_span.set_status(
                            trace.StatusCode.OK,
                            f"Security fix verified after {attempt} retry(ies)",
                        )
                        return TB3Result(
                            issue_id=issue_id,
                            repo_path=repo_path,
                            success=True,
                            phase="retry_passed",
                            worktree_path=worktree_path,
                            persona=persona_name,
                            retries_used=attempt,
                            max_retries=max_retries,
                            duration_seconds=round(elapsed, 2),
                            trace_id=root_trace_id,
                            attempt_span_ids=attempt_span_ids,
                            security_findings=security_findings,
                            vulnerability_fixed=vuln_fixed,
                            cwe_ids=cwe_ids,
                            vuln_seeded=force_vuln_seed,
                            retry_history=retry_history,
                        ).model_dump()

                    # Accumulate failures for next retry prompt (M6: include spawn failures)
                    if retry_gate_results:
                        all_gate_failures.append(retry_gate_results)
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
            # Phase 11: Retries exhausted -> escalate
            # ----------------------------------------------------------
            with tracer_tb3.start_as_current_span(
                "tb3.phase.escalate",
                attributes={
                    "tb3.phase": "escalate",
                    "escalate.attempts": retries_used + 1,
                },
            ) as esc_span:
                logger.warning(
                    "TB-3 ESCALATE: Issue %s — %d retries exhausted, "
                    "security vulnerability not fixed",
                    issue_id,
                    max_retries,
                )

                esc_result = escalate_to_human(
                    issue_id=issue_id,
                    gate_failures=all_gate_failures,
                    attempts=retries_used + 1,
                )

                esc_span.set_attribute(
                    "tb3.escalation_success",
                    esc_result.get("success", False),
                )

            elapsed = time.monotonic() - pipeline_start
            root_span.set_attribute("tb3.outcome", "escalated")
            root_span.set_attribute("tb3.retries_used", retries_used)
            root_span.set_status(
                trace.StatusCode.ERROR,
                f"Escalated after {retries_used} retries — vulnerability not fixed",
            )
            return TB3Result(
                issue_id=issue_id,
                repo_path=repo_path,
                success=False,
                phase="escalated",
                worktree_path=worktree_path,
                persona=persona_name,
                retries_used=retries_used,
                max_retries=max_retries,
                escalated=True,
                error=f"All {retries_used} retries failed; vulnerability not fixed",
                duration_seconds=round(elapsed, 2),
                trace_id=root_trace_id,
                attempt_span_ids=attempt_span_ids,
                security_findings=security_findings,
                cwe_ids=cwe_ids,
                vuln_seeded=force_vuln_seed,
                retry_history=retry_history,
            ).model_dump()

        except Exception as exc:
            elapsed = time.monotonic() - pipeline_start
            error_msg = f"Pipeline error: {type(exc).__name__}: {exc}"
            logger.exception("TB-3 pipeline error for issue %s", issue_id)
            root_span.set_status(trace.StatusCode.ERROR, error_msg)
            root_span.record_exception(exc)
            return TB3Result(
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
                trace_id=root_trace_id,
                attempt_span_ids=attempt_span_ids,
                security_findings=security_findings,
                cwe_ids=cwe_ids,
                vuln_seeded=force_vuln_seed,
                retry_history=retry_history,
            ).model_dump()

        finally:
            # ----------------------------------------------------------
            # Phase 12: Cleanup
            # ----------------------------------------------------------
            try:
                with tracer_tb3.start_as_current_span(
                    "tb3.phase.cleanup",
                    attributes={"tb3.phase": "cleanup"},
                ):
                    pass
            except Exception:
                pass

            if heartbeat_event is not None:
                stop_heartbeat(heartbeat_event, heartbeat_thread)

            # Keep worktree on escalation for debugging
            if worktree_path and not retries_used >= max_retries:
                cleanup_worktree(issue_id)
            elif worktree_path:
                logger.info(
                    "TB-3: Preserving worktree at %s for security post-mortem",
                    worktree_path,
                )

            # Unclaim issue if pipeline didn't succeed (M8 fix)
            if not pipeline_success:
                _unclaim_issue(issue_id)

            # Force flush OTel spans
            if provider is not None:
                try:
                    provider.force_flush(timeout_millis=5000)
                except Exception:
                    logger.warning("Failed to flush OTel spans")
