"""TB-1: Golden path — issue to PR, all gates pass.

Extracted from pipeline.py to keep the main module manageable.

Usage::

    from devloop.feedback.tb1_golden_path import run_tb1
    result = run_tb1(issue_id="dl-abc", repo_path="/home/user/some-repo")
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

from opentelemetry import trace

from devloop.feedback.pipeline import (
    _clear_pipeline_timeout,
    _latest_failure_gate,
    _load_allowed_tools,
    _set_pipeline_timeout,
    _unclaim_issue,
)
from devloop.feedback.server import escalate_to_human, retry_agent
from devloop.feedback.tb4_runaway import (
    HANDOFF_DIR,
    MAX_CONTEXT_RESTARTS,
    _build_context_restart_prompt,
    _clear_handoff,
    _read_handoff,
)
from devloop.feedback.tb5_cascade import find_cascade_targets, run_tb5
from devloop.feedback.tb6_replay import (
    _generate_session_id,
    _save_session,
    _suggest_claude_md_fix,
)
from devloop.feedback.types import TB1Result
from devloop.gates.server import run_all_gates
from devloop.gates.types import GateSuiteResult
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
# OTel tracer — use the global provider set up by init_tracing()
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
    _set_pipeline_timeout()

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
        max_context_pct = 75
        context_restarts = 0

        try:
            # ----------------------------------------------------------
            # Phase 1: Poll beads for the issue
            # ----------------------------------------------------------
            with tracer.start_as_current_span(
                "tb1.phase.poll",
                attributes={"tb1.phase": "poll"},
            ) as poll_span:
                items = poll_ready(repo_path=repo_path)
                issue = None
                for item in items:
                    if item.id == issue_id:
                        issue = item
                        break

                if issue is None:
                    # Issue might not be in the "ready" list — fetch directly.
                    poll_span.set_attribute("tb1.issue_found_in_poll", False)
                    issue = get_issue(issue_id, repo_path=repo_path)
                if issue is None:
                    elapsed = time.monotonic() - pipeline_start
                    poll_span.set_status(
                        trace.StatusCode.ERROR,
                        f"Issue {issue_id} not found",
                    )
                    root_span.set_status(trace.StatusCode.ERROR, "Issue not found")
                    return TB1Result(
                        issue_id=issue_id,
                        repo_path=repo_path,
                        success=False,
                        phase="poll",
                        error=f"Issue {issue_id} not found in beads",
                        duration_seconds=round(elapsed, 2),
                    ).model_dump()
                else:
                    poll_span.set_attribute("tb1.issue_found_in_poll", True)
                    issue_title = issue.title
                    issue_description = issue.description or ""
                    issue_labels = issue.labels
                    # br ready --json omits labels; enrich via get_issue
                    if not issue_labels:
                        full_issue = get_issue(issue_id, repo_path=repo_path)
                        if full_issue and full_issue.labels:
                            issue_labels = full_issue.labels

                poll_span.set_attribute("tb1.ready_count", len(items))

            # ----------------------------------------------------------
            # Phase 1b: Ambiguity check (safety net for direct invocations)
            # ----------------------------------------------------------
            with tracer.start_as_current_span(
                "tb1.phase.ambiguity_check",
                attributes={"tb1.phase": "ambiguity_check"},
            ) as ambiguity_span:
                from devloop.intake.ambiguity import detect_ambiguity

                # Cascade issues (auto-generated by TB-5) are inherently vague —
                # exempt them from ambiguity gating.
                is_cascade = "cascade" in (issue_labels or [])
                ambiguity_span.set_attribute("tb1.is_cascade", is_cascade)

                if is_cascade:
                    ambiguity_span.set_attribute("tb1.ambiguity_exempted", True)
                    ambiguity_span.set_attribute("tb1.ambiguity_score", 0.0)
                    ambiguity_span.set_attribute("tb1.is_ambiguous", False)
                    logger.info("TB-1: Cascade issue %s — skipping ambiguity check", issue_id)
                else:
                    ambiguity_result = detect_ambiguity(issue_title, issue_description)
                    ambiguity_span.set_attribute("tb1.ambiguity_score", ambiguity_result.score)
                    ambiguity_span.set_attribute("tb1.is_ambiguous", ambiguity_result.is_ambiguous)

                if not is_cascade and ambiguity_result.is_ambiguous:
                    elapsed = time.monotonic() - pipeline_start
                    ambiguity_span.set_status(
                        trace.StatusCode.ERROR,
                        f"Issue deferred as ambiguous: {ambiguity_result.summary}",
                    )
                    root_span.set_status(trace.StatusCode.ERROR, "Issue ambiguous")
                    return TB1Result(
                        issue_id=issue_id,
                        repo_path=repo_path,
                        success=False,
                        phase="ambiguity_check",
                        error=f"Issue deferred as ambiguous: {ambiguity_result.summary}",
                        duration_seconds=round(elapsed, 2),
                    ).model_dump()

            # ----------------------------------------------------------
            # Phase 2: Claim the issue (optimistic locking)
            # ----------------------------------------------------------
            with tracer.start_as_current_span(
                "tb1.phase.claim",
                attributes={"tb1.phase": "claim", "issue.id": issue_id},
            ) as claim_span:
                claimed = claim_issue(issue_id, repo_path=repo_path)
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
                persona_result = select_persona(issue_labels, issue_description=issue_description)
                persona_name = persona_result.get("name", "feature")
                max_retries = persona_result.get("retry_max", 2)
                max_context_pct = persona_result.get("max_context_pct", 75)

                # Budget-aware model downgrade (#37)
                from devloop.orchestration.server import budget_aware_model
                from devloop.orchestration.scheduler import (
                    get_budget_usage_pct,
                    load_scheduler_config,
                )

                _sched_cfg = load_scheduler_config()
                _budget_pct = get_budget_usage_pct(_sched_cfg)
                original_model = persona_result.get("model", "sonnet")
                downgraded_model = budget_aware_model(original_model, _budget_pct)
                if downgraded_model != original_model:
                    persona_result["model"] = downgraded_model
                    persona_span.set_attribute("tb1.model_downgraded_from", original_model)
                    persona_span.set_attribute("tb1.budget_pct", _budget_pct)

                persona_span.set_attribute("tb1.persona", persona_name)
                persona_span.set_attribute("tb1.max_retries", max_retries)
                persona_span.set_attribute("tb1.max_context_pct", max_context_pct)

                overlay_result = build_claude_md_overlay(
                    persona=persona_name,
                    issue_title=issue_title,
                    issue_description=issue_description,
                    issue_id=issue_id,
                    max_context_pct=max_context_pct,
                    repo_path=repo_path,
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

                # Ensure handoff directory exists
                HANDOFF_DIR.mkdir(parents=True, exist_ok=True)

                allowed_tools = _load_allowed_tools(repo_path)
                agent_result = spawn_agent(
                    worktree_path=worktree_path,
                    task_prompt=task_prompt,
                    model=persona_result.get("model", "sonnet"),
                    allowed_tools=allowed_tools,
                    max_context_pct=max_context_pct,
                    timeout_seconds=persona_result.get("timeout_seconds", 300),
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
            # Phase 7c: Save session (TB-6 — fail-safe)
            # ----------------------------------------------------------
            session_id: str | None = None
            session_path: str | None = None
            try:
                agent_stdout = agent_result.get("stdout", "")
                if agent_stdout:
                    session_id = _generate_session_id(issue_id)
                    session_path = _save_session(session_id, agent_stdout, {
                        "issue_id": issue_id,
                        "repo_path": repo_path,
                        "persona": persona_name,
                        "worktree_path": worktree_path,
                    })
                    logger.info("TB-1: Session saved as %s", session_id)
            except Exception:
                logger.warning("TB-1: Session capture failed for %s", issue_id)

            # ----------------------------------------------------------
            # Phase 7b: Context restart loop (if agent hit context limit)
            # ----------------------------------------------------------
            agent_context_limited = agent_result.get("context_limited", False)
            agent_context_pct = agent_result.get("context_pct", 0.0)
            while agent_context_limited and context_restarts < MAX_CONTEXT_RESTARTS:
                with tracer.start_as_current_span(
                    "runtime.context_restart",
                    attributes={
                        "tb1.phase": "context_restart",
                        "context_pct_at_exit": agent_context_pct,
                        "restart_count": context_restarts + 1,
                    },
                ) as restart_span:
                    context_restarts += 1

                    handoff_note = _read_handoff(issue_id)
                    if handoff_note is None:
                        logger.info(
                            "TB-1: Context-limited at %.1f%% but no handoff — skipping restart",
                            agent_context_pct,
                        )
                        restart_span.set_attribute("context_restart.handoff_found", False)
                        break

                    restart_span.set_attribute("context_restart.handoff_found", True)
                    _clear_handoff(issue_id)

                    logger.info(
                        "TB-1 CONTEXT RESTART %d: Issue %s at %.1f%%",
                        context_restarts, issue_id, agent_context_pct,
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
                        allowed_tools=allowed_tools,
                        max_context_pct=max_context_pct,
                        timeout_seconds=persona_result.get("timeout_seconds", 300),
                    )

                    agent_exit = agent_result.get("exit_code", -1)
                    agent_context_pct = agent_result.get("context_pct", 0.0)
                    agent_context_limited = agent_result.get("context_limited", False)
                    restart_span.set_attribute("runtime.context_pct", agent_context_pct)

                    if agent_exit != 0:
                        restart_span.set_status(
                            trace.StatusCode.ERROR,
                            f"Restarted agent exited with code {agent_exit}",
                        )
                        break
                    restart_span.set_status(trace.StatusCode.OK)

            root_span.set_attribute("runtime_context_restarts", context_restarts)

            # ----------------------------------------------------------
            # Phase 7d: Zero-diff detection (#31 — "already fixed")
            # ----------------------------------------------------------
            with tracer.start_as_current_span(
                "tb1.phase.zero_diff_check",
                attributes={"tb1.phase": "zero_diff_check"},
            ) as zd_span:
                try:
                    diff_stat = subprocess.run(
                        ["git", "diff", "HEAD~1", "--stat"],
                        cwd=worktree_path,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    has_changes = bool(diff_stat.stdout.strip())
                    # Also check uncommitted changes
                    if not has_changes:
                        diff_wt = subprocess.run(
                            ["git", "diff", "--stat"],
                            cwd=worktree_path,
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
                        has_changes = bool(diff_wt.stdout.strip())
                    if not has_changes:
                        diff_cached = subprocess.run(
                            ["git", "diff", "--cached", "--stat"],
                            cwd=worktree_path,
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
                        has_changes = bool(diff_cached.stdout.strip())
                except Exception:
                    has_changes = True  # Assume changes on error — don't block

                zd_span.set_attribute("tb1.has_changes", has_changes)

                if not has_changes:
                    elapsed = time.monotonic() - pipeline_start
                    zd_span.set_status(
                        trace.StatusCode.OK, "Zero-diff: agent reported done with no changes"
                    )
                    root_span.set_status(trace.StatusCode.OK, "Zero-diff — needs verification")
                    logger.warning(
                        "TB-1: Zero-diff for %s — agent produced no changes, routing to verification",
                        issue_id,
                    )
                    return TB1Result(
                        issue_id=issue_id,
                        repo_path=repo_path,
                        success=False,
                        phase="zero_diff",
                        worktree_path=worktree_path,
                        persona=persona_name,
                        session_id=session_id,
                        session_path=session_path,
                        error="Agent completed with zero changes — needs human verification",
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
                # ----------------------------------------------------------
                # Phase 9a: Create PR (if gates passed)
                # ----------------------------------------------------------
                pr_url: str | None = None
                with tracer.start_as_current_span(
                    "tb1.phase.create_pr",
                    attributes={"tb1.phase": "create_pr"},
                ) as pr_span:
                    pr_result = create_pull_request(
                        issue_id=issue_id,
                        repo_path=repo_path,
                        worktree_path=worktree_path,
                        branch_name=f"dl/{issue_id}",
                        issue_title=issue_title,
                        issue_description=issue_description,
                        gate_summary=f"All gates passed ({len(gate_suite.gate_results)} gates)",
                    )
                    pr_span.set_attribute("tb1.pr_created", pr_result.get("success", False))
                    if pr_result.get("pr_url"):
                        pr_span.set_attribute("tb1.pr_url", pr_result["pr_url"])
                        pr_url = pr_result["pr_url"]
                    if not pr_result.get("success"):
                        logger.warning(
                            "TB-1 PR creation failed for %s: %s (pipeline still succeeds)",
                            issue_id,
                            pr_result.get("message", "unknown error"),
                        )

                # ----------------------------------------------------------
                # Phase 9b: Check cascade targets (TB-5 — fail-safe)
                # ----------------------------------------------------------
                cascade_results: list[dict] = []
                try:
                    targets = find_cascade_targets(repo_path, issue_id)
                    for target in targets:
                        try:
                            logger.info(
                                "TB-1 CASCADE: %s -> %s (%s)",
                                issue_id,
                                target["target_repo_name"],
                                ", ".join(target["matched_watches"]),
                            )
                            cascade_results.append(run_tb5(
                                source_issue_id=issue_id,
                                source_repo_path=repo_path,
                                target_repo_path=target["target_repo_path"],
                            ))
                        except Exception as cascade_exc:
                            logger.warning(
                                "TB-1 CASCADE %s -> %s failed: %s",
                                issue_id, target["target_repo_name"], cascade_exc,
                            )
                except Exception:
                    logger.warning("TB-1 CASCADE CHECK failed for %s", issue_id)

                elapsed = time.monotonic() - pipeline_start
                logger.info(
                    "TB-1 SUCCESS: Issue %s — all gates passed in %.1fs",
                    issue_id,
                    elapsed,
                )
                root_span.set_attribute("tb1.outcome", "success")
                root_span.set_attribute("status.detail", "All gates passed")
                root_span.set_status(trace.StatusCode.OK)
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
                    pr_url=pr_url,
                    session_id=session_id,
                    session_path=session_path,
                    cascade_results=cascade_results,
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
                    # Use latest failure info, not stale initial gate_suite (L8 fix)
                    last_failure_name = _latest_failure_gate(all_gate_failures) or gate_suite.first_failure
                    logger.info(
                        "TB-1 RETRY %d/%d for issue %s (failed at %s)",
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
                    )

                    retry_success = retry_raw.get("success", False)
                    retry_span.set_attribute("tb1.retry_success", retry_success)

                    if retry_success:
                        # Reconstruct gate results from the retry
                        retry_gate_results = retry_raw.get("gate_results")
                        if retry_gate_results:
                            gate_suite = GateSuiteResult(**retry_gate_results)

                        # Create PR after retry success
                        retry_pr_url: str | None = None
                        with tracer.start_as_current_span(
                            "tb1.phase.create_pr_after_retry",
                            attributes={"tb1.phase": "create_pr"},
                        ) as retry_pr_span:
                            retry_pr_result = create_pull_request(
                                issue_id=issue_id,
                                repo_path=repo_path,
                                worktree_path=worktree_path,
                                branch_name=f"dl/{issue_id}",
                                issue_title=issue_title,
                                issue_description=issue_description,
                                gate_summary=f"All gates passed after {attempt} retry(ies) ({len(gate_suite.gate_results)} gates)",
                            )
                            retry_pr_span.set_attribute("tb1.pr_created", retry_pr_result.get("success", False))
                            if retry_pr_result.get("pr_url"):
                                retry_pr_span.set_attribute("tb1.pr_url", retry_pr_result["pr_url"])
                                retry_pr_url = retry_pr_result["pr_url"]
                            if not retry_pr_result.get("success"):
                                logger.warning(
                                    "TB-1 PR creation failed for %s after retry: %s (pipeline still succeeds)",
                                    issue_id,
                                    retry_pr_result.get("message", "unknown error"),
                                )

                        # Check cascade targets after retry-PR (TB-5 — fail-safe)
                        retry_cascade_results: list[dict] = []
                        try:
                            retry_targets = find_cascade_targets(repo_path, issue_id)
                            for target in retry_targets:
                                try:
                                    retry_cascade_results.append(run_tb5(
                                        source_issue_id=issue_id,
                                        source_repo_path=repo_path,
                                        target_repo_path=target["target_repo_path"],
                                    ))
                                except Exception as cascade_exc:
                                    logger.warning(
                                        "TB-1 CASCADE %s -> %s failed: %s",
                                        issue_id, target["target_repo_name"], cascade_exc,
                                    )
                        except Exception:
                            logger.warning("TB-1 CASCADE CHECK failed for %s", issue_id)

                        elapsed = time.monotonic() - pipeline_start
                        logger.info(
                            "TB-1 SUCCESS after retry %d: Issue %s in %.1fs",
                            attempt,
                            issue_id,
                            elapsed,
                        )

                        root_span.set_attribute("tb1.outcome", "success_after_retry")
                        root_span.set_attribute("tb1.retries_used", attempt)
                        pipeline_success = True
                        root_span.set_status(trace.StatusCode.OK)
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
                            pr_url=retry_pr_url,
                            session_id=session_id,
                            session_path=session_path,
                            cascade_results=retry_cascade_results,
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
                    repo_path=repo_path,
                )

                esc_span.set_attribute(
                    "tb1.escalation_success",
                    esc_result.get("success", False),
                )

                # ----------------------------------------------------------
                # Phase 11b: Suggest CLAUDE.md fix (TB-6 — fail-safe)
                # ----------------------------------------------------------
                suggested_fix: str | None = None
                try:
                    suggested_fix = _suggest_claude_md_fix(all_gate_failures)
                    if suggested_fix:
                        from devloop.feedback.pipeline import _DEVLOOP_ROOT
                        subprocess.run(
                            ["br", "comments", "add", issue_id, "--message",
                             f"[dev-loop] Suggested CLAUDE.md fix:\n{suggested_fix}"],
                            capture_output=True, text=True, check=False,
                            timeout=30, cwd=_DEVLOOP_ROOT,
                        )
                except Exception:
                    pass  # fail-safe

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
                session_id=session_id,
                session_path=session_path,
                suggested_fix=suggested_fix,
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

            # Clean up any leftover handoff file
            _clear_handoff(issue_id)

            # Unclaim issue if pipeline didn't succeed (M8 fix)
            if not pipeline_success:
                _unclaim_issue(issue_id, repo_path=repo_path)

            # Flush spans (M1 fix — TB-1 was missing force_flush)
            if provider is not None:
                try:
                    provider.force_flush(timeout_millis=5000)
                except Exception:
                    pass

            _clear_pipeline_timeout()
