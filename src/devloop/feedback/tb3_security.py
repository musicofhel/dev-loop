"""TB-3: Security gate self-remediation — seed vulnerability, detect, agent fix.

Extracted from pipeline.py to keep the main module manageable.

Usage::

    from devloop.feedback.tb3_security import run_tb3
    result = run_tb3(
        issue_id="dl-abc",
        repo_path="/home/user/some-repo",
        force_vuln_seed=True,
    )
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path

from opentelemetry import trace
from opentelemetry.trace import Link

from devloop.feedback.pipeline import (
    _FIXTURES_DIR,
    _clear_pipeline_timeout,
    _latest_failure_gate,
    _set_pipeline_timeout,
    _span_id_hex,
    _trace_id_hex,
    _unclaim_issue,
    tracer_tb3,
)
from devloop.feedback.server import escalate_to_human, retry_agent
from devloop.feedback.types import RetryAttempt, SecurityFinding, TB3Result
from devloop.gates.server import run_gate_3_security_standalone
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

logger = logging.getLogger(__name__)


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
    _set_pipeline_timeout()

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
                items = poll_ready(repo_path=repo_path)
                issue = None
                for item in items:
                    if item.id == issue_id:
                        issue = item
                        break

                if issue is None:
                    poll_span.set_attribute("tb3.issue_found_in_poll", False)
                    issue = get_issue(issue_id, repo_path=repo_path)
                if issue is None:
                    issue_title = issue_id
                    issue_description = ""
                    issue_labels: list[str] = ["security"]
                else:
                    poll_span.set_attribute("tb3.issue_found_in_poll", True)
                    issue_title = issue.title
                    issue_description = issue.description or ""
                    issue_labels = issue.labels
                    if not issue_labels:
                        full_issue = get_issue(issue_id, repo_path=repo_path)
                        if full_issue and full_issue.labels:
                            issue_labels = full_issue.labels
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
                claimed = claim_issue(issue_id, repo_path=repo_path)
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
                # Use standalone Gate 3 for pre-flight so we always get
                # security findings, even if earlier gates would fail in
                # fail-fast mode (Bug: gate_3_security not found).
                gate_raw = run_gate_3_security_standalone(
                    worktree_path=worktree_path,
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
                root_span.set_status(trace.StatusCode.OK)
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

                    last_failure_name = _latest_failure_gate(all_gate_failures) or gate_suite.first_failure
                    logger.info(
                        "TB-3 RETRY %d/%d for issue %s (failed at %s, CWEs: %s)",
                        attempt,
                        max_retries,
                        issue_id,
                        last_failure_name,
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
                        # Reconcile security_findings: mark findings as fixed
                        # if they no longer appear in the passing gate's scan.
                        if vuln_fixed:
                            still_present = {
                                (f.file, f.rule, f.cwe) for f in retry_sec_findings
                            }
                            for finding in security_findings:
                                key = (finding.file, finding.rule, finding.cwe)
                                if key not in still_present:
                                    finding.fixed = True

                        # Create PR after retry success
                        retry_pr_url: str | None = None
                        with tracer_tb3.start_as_current_span(
                            "tb3.phase.create_pr",
                            attributes={"tb3.phase": "create_pr"},
                        ) as pr_span:
                            gate_desc = "vulnerability FIXED" if vuln_fixed else "all gates passed"
                            pr_result = create_pull_request(
                                issue_id=issue_id,
                                repo_path=repo_path,
                                worktree_path=worktree_path,
                                branch_name=f"dl/{issue_id}",
                                issue_title=issue_title,
                                issue_description=issue_description,
                                gate_summary=f"Security gate: {gate_desc} after {attempt} retry(ies)",
                            )
                            pr_span.set_attribute("tb3.pr_created", pr_result.get("success", False))
                            if pr_result.get("pr_url"):
                                pr_span.set_attribute("tb3.pr_url", pr_result["pr_url"])
                                retry_pr_url = pr_result["pr_url"]
                            if not pr_result.get("success"):
                                logger.warning(
                                    "TB-3 PR creation failed for %s: %s (pipeline still succeeds)",
                                    issue_id,
                                    pr_result.get("message", "unknown error"),
                                )

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
                        root_span.set_status(trace.StatusCode.OK)
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
                            pr_url=retry_pr_url,
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
                    repo_path=repo_path,
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
            escalated = retries_used >= max_retries
            if worktree_path and not escalated:
                cleanup_worktree(issue_id)
            elif worktree_path:
                logger.info(
                    "TB-3: Preserving worktree at %s for security post-mortem",
                    worktree_path,
                )

            # Unclaim issue if pipeline didn't succeed (M8 fix)
            if not pipeline_success:
                _unclaim_issue(issue_id, repo_path=repo_path)

            # Force flush OTel spans
            if provider is not None:
                try:
                    provider.force_flush(timeout_millis=5000)
                except Exception:
                    logger.warning("Failed to flush OTel spans")

            _clear_pipeline_timeout()
