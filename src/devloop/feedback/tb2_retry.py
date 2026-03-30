"""TB-2: Failure-to-retry — intentional gate failure, retry loop, blocked escalation.

Extracted from pipeline.py to keep the main module manageable.

Usage::

    from devloop.feedback.tb2_retry import run_tb2
    result = run_tb2(
        issue_id="dl-xyz",
        repo_path="/home/user/some-repo",
        force_gate_fail=True,
    )
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

from devloop.feedback.pipeline import (
    _DEVLOOP_ROOT,
    _FIXTURES_DIR,
    _clear_pipeline_timeout,
    _latest_failure_gate,
    _load_allowed_tools,
    _set_pipeline_timeout,
    _span_id_hex,
    _trace_id_hex,
    _unclaim_issue,
    tracer_tb2,
)
from devloop.feedback.server import escalate_to_human, retry_agent
from devloop.feedback.types import RetryAttempt, TB2Result
from devloop.gates.server import run_all_gates
from devloop.gates.types import Finding, GateResult, GateSuiteResult
from devloop.intake.beads_poller import claim_issue, get_issue, poll_ready
from devloop.observability.heartbeat import start_heartbeat, stop_heartbeat
from devloop.observability.tracing import init_tracing
from devloop.orchestration.server import (
    build_claude_md_overlay,
    cleanup_worktree,
    create_pull_request,
    select_persona,
    setup_worktree,
)
from devloop.runtime.server import spawn_agent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TB-2 helpers
# ---------------------------------------------------------------------------


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
    """Return a synthetic GateSuiteResult dict that always fails.

    Includes ``is_synthetic: True`` so retry prompt construction can
    filter it out on attempt 2+ (the synthetic failure carries no
    diagnostic signal for the agent).
    """
    result = GateSuiteResult(
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
    result["is_synthetic"] = True
    return result


def _verify_blocked_status(issue_id: str, repo_path: str | None = None) -> bool:
    """Check that a beads issue has status 'blocked'."""
    try:
        result = subprocess.run(
            ["br", "show", issue_id, "--format", "json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            cwd=_DEVLOOP_ROOT,
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
    _set_pipeline_timeout()

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
                items = poll_ready(repo_path=repo_path)
                issue = None
                for item in items:
                    if item.id == issue_id:
                        issue = item
                        break

                if issue is None:
                    poll_span.set_attribute("tb2.issue_found_in_poll", False)
                    issue = get_issue(issue_id, repo_path=repo_path)
                if issue is None:
                    issue_title = issue_id
                    issue_description = ""
                    issue_labels: list[str] = []
                else:
                    poll_span.set_attribute("tb2.issue_found_in_poll", True)
                    issue_title = issue.title
                    issue_description = issue.description or ""
                    issue_labels = issue.labels
                    if not issue_labels:
                        full_issue = get_issue(issue_id, repo_path=repo_path)
                        if full_issue and full_issue.labels:
                            issue_labels = full_issue.labels

                poll_span.set_attribute("tb2.ready_count", len(items))

            # ----------------------------------------------------------
            # Phase 2: Claim the issue
            # ----------------------------------------------------------
            with tracer_tb2.start_as_current_span(
                "tb2.phase.claim",
                attributes={"tb2.phase": "claim", "issue.id": issue_id},
            ) as claim_span:
                claimed = claim_issue(issue_id, repo_path=repo_path)
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
                persona_result = select_persona(issue_labels, issue_description=issue_description)
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

                allowed_tools = _load_allowed_tools(repo_path)
                agent_result = spawn_agent(
                    worktree_path=worktree_path,
                    task_prompt=task_prompt,
                    model=persona_result.get("model", "sonnet"),
                    allowed_tools=allowed_tools,
                    timeout_seconds=persona_result.get("timeout_seconds", 300),
                )

                agent_exit = agent_result.get("exit_code", -1)
                agent_span.set_attribute("tb2.agent_exit_code", agent_exit)
                attempt_span_ids.append(_span_id_hex(agent_span))

                # If the agent timed out, feed the timeout into the retry
                # loop instead of returning early.  This lets TB-2 retry
                # after a slow spawn (the 303s timeout seen in stress tests).
                initial_timed_out = agent_result.get("timed_out", False)
                if initial_timed_out:
                    duration = agent_result.get("duration_seconds", 0)
                    agent_span.set_attribute("tb2.agent_timed_out", True)
                    agent_span.set_attribute("tb2.agent_duration", duration)
                    logger.warning(
                        "TB-2: Initial agent timed out after %.0fs — entering retry loop",
                        duration,
                    )
                elif agent_exit != 0:
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
            # Phase 8: Run quality gates (or force-fail, or skip if timed out)
            # ----------------------------------------------------------
            gate_suite: GateSuiteResult | None = None
            gate_raw: dict | None = None
            gates_span_context = None

            if initial_timed_out:
                # Agent timed out — no output to gate.  Seed a timeout
                # failure record and jump straight to the retry loop.
                duration = agent_result.get("duration_seconds", 0)
                gate_raw = GateSuiteResult(
                    overall_passed=False,
                    first_failure="agent_timeout",
                    gate_results=[
                        GateResult(
                            gate_name="agent_timeout",
                            passed=False,
                            findings=[
                                Finding(
                                    severity="critical",
                                    message=f"Agent timed out after {duration:.0f}s (no output to gate)",
                                ),
                            ],
                        ),
                    ],
                ).model_dump()
                gate_suite = GateSuiteResult(**gate_raw)
                retry_history.append(
                    RetryAttempt(
                        attempt=0,
                        agent_exit_code=agent_exit,
                        gates_passed=False,
                        first_failure="agent_timeout",
                        span_id=attempt_span_ids[0] if attempt_span_ids else None,
                    )
                )
            else:
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
                    gates_span_context = gates_span.get_span_context()

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
                # Create PR
                pr_url: str | None = None
                with tracer_tb2.start_as_current_span(
                    "tb2.phase.create_pr",
                    attributes={"tb2.phase": "create_pr"},
                ) as pr_span:
                    pr_result = create_pull_request(
                        issue_id=issue_id,
                        repo_path=repo_path,
                        worktree_path=worktree_path,
                        branch_name=f"dl/{issue_id}",
                        issue_title=issue_title,
                        issue_description=issue_description,
                        gate_summary=f"All gates passed on first attempt ({len(gate_suite.gate_results)} gates)",
                    )
                    pr_span.set_attribute("tb2.pr_created", pr_result.get("success", False))
                    if pr_result.get("pr_url"):
                        pr_span.set_attribute("tb2.pr_url", pr_result["pr_url"])
                        pr_url = pr_result["pr_url"]
                    if not pr_result.get("success"):
                        logger.warning(
                            "TB-2 PR creation failed for %s: %s (pipeline still succeeds)",
                            issue_id,
                            pr_result.get("message", "unknown error"),
                        )

                elapsed = time.monotonic() - pipeline_start
                logger.info(
                    "TB-2: Gates passed on first attempt (%.1fs). "
                    "Retry path was NOT exercised.",
                    elapsed,
                )
                root_span.set_attribute("tb2.outcome", "success_first_attempt")
                pipeline_success = True
                root_span.set_status(trace.StatusCode.OK)
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
                    pr_url=pr_url,
                ).model_dump()

            # ----------------------------------------------------------
            # Phase 10: Gates failed -> retry loop with span linking
            # ----------------------------------------------------------
            all_gate_failures: list[dict] = [gate_raw]
            previous_span_context = gates_span_context

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

                    last_failure_name = _latest_failure_gate(all_gate_failures) or gate_suite.first_failure
                    logger.info(
                        "TB-2 RETRY %d/%d for issue %s (failed at %s)",
                        attempt,
                        max_retries,
                        issue_id,
                        last_failure_name,
                    )

                    retry_raw = retry_agent(
                        worktree_path=worktree_path,
                        issue_id=issue_id,
                        issue_title=issue_title,
                        issue_description=issue_description,
                        gate_failures=all_gate_failures,
                        attempt=attempt,
                        max_retries=max_retries,
                        timeout_seconds=persona_result.get("timeout_seconds", 300),
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
                        # Create PR after retry success
                        retry_pr_url: str | None = None
                        with tracer_tb2.start_as_current_span(
                            "tb2.phase.create_pr_after_retry",
                            attributes={"tb2.phase": "create_pr"},
                        ) as retry_pr_span:
                            retry_pr_result = create_pull_request(
                                issue_id=issue_id,
                                repo_path=repo_path,
                                worktree_path=worktree_path,
                                branch_name=f"dl/{issue_id}",
                                issue_title=issue_title,
                                issue_description=issue_description,
                                gate_summary=f"All gates passed after {attempt} retry(ies)",
                            )
                            retry_pr_span.set_attribute("tb2.pr_created", retry_pr_result.get("success", False))
                            if retry_pr_result.get("pr_url"):
                                retry_pr_span.set_attribute("tb2.pr_url", retry_pr_result["pr_url"])
                                retry_pr_url = retry_pr_result["pr_url"]
                            if not retry_pr_result.get("success"):
                                logger.warning(
                                    "TB-2 PR creation failed for %s after retry: %s (pipeline still succeeds)",
                                    issue_id,
                                    retry_pr_result.get("message", "unknown error"),
                                )

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
                        root_span.set_status(trace.StatusCode.OK)
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
                            pr_url=retry_pr_url,
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
                    repo_path=repo_path,
                )

                esc_span.set_attribute(
                    "tb2.escalation_success",
                    esc_result.get("success", False),
                )

                # TB-2 specific: verify the issue is actually blocked
                blocked_verified = _verify_blocked_status(issue_id, repo_path=repo_path)
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
            escalated = retries_used >= max_retries
            if worktree_path and not escalated:
                cleanup_worktree(issue_id)
            elif worktree_path:
                logger.info(
                    "TB-2: Preserving worktree at %s for post-mortem",
                    worktree_path,
                )

            # Unclaim issue if pipeline didn't succeed (M8 fix)
            if not pipeline_success:
                _unclaim_issue(issue_id, repo_path=repo_path)

            # Force flush OTel spans so they're available for verification
            if provider is not None:
                try:
                    provider.force_flush(timeout_millis=5000)
                except Exception:
                    logger.warning("Failed to flush OTel spans")

            _clear_pipeline_timeout()
