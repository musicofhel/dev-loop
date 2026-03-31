"""TB-6: Session Replay Debug — session capture, parsing, and replay.

Extracted from pipeline.py to keep the main module manageable.

Usage::

    from devloop.feedback.tb6_replay import run_tb6, replay_session
    result = run_tb6(issue_id="dl-abc", repo_path="/home/user/some-repo")
    session = replay_session("dl-abc-1234567890")
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from opentelemetry import trace

from devloop.feedback.pipeline import (
    _clear_pipeline_timeout,
    _load_allowed_tools,
    _make_forced_failure,
    _set_pipeline_timeout,
    _span_id_hex,
    _trace_id_hex,
    _unclaim_issue,
)
from devloop.feedback.server import escalate_to_human, retry_agent
from devloop.feedback.types import SessionEvent, TB6Result
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
# OTel tracer
# ---------------------------------------------------------------------------

tracer_tb6 = trace.get_tracer("tb6", "0.1.0")

# ---------------------------------------------------------------------------
# Session directory
# ---------------------------------------------------------------------------

_SESSIONS_DIR = Path.home() / ".local" / "share" / "dev-loop" / "sessions"

# ---------------------------------------------------------------------------
# TB-6 helpers — session replay
# ---------------------------------------------------------------------------


def _generate_session_id(issue_id: str) -> str:
    """Generate a unique session ID from issue ID + timestamp."""
    return f"{issue_id}-{int(time.time())}"


def _parse_session_events(stdout: str) -> list[dict]:
    """Parse agent stdout (JSON array or NDJSON) into SessionEvent dicts.

    The Claude CLI emits either:
    - A JSON **array** on a single line: ``[{...}, {...}, ...]``
    - NDJSON with one object per line (older versions)

    Each valid JSON object becomes a SessionEvent with line_number, type, and data.
    Non-JSON lines are skipped.
    """
    # Collect all JSON objects — handle both array and NDJSON formats
    objects: list[tuple[int, dict]] = []
    for i, line in enumerate(stdout.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            for idx, obj in enumerate(parsed):
                if isinstance(obj, dict):
                    objects.append((i + idx, obj))
        elif isinstance(parsed, dict):
            objects.append((i, parsed))

    events: list[dict] = []
    for line_number, obj in objects:
        event_type = obj.get("type", "unknown")
        events.append(
            SessionEvent(
                line_number=line_number,
                type=event_type,
                data=obj,
            ).model_dump()
        )
    return events


def _save_session(
    session_id: str,
    stdout: str,
    metadata: dict,
) -> str:
    """Save agent stdout and metadata to session files on disk.

    Creates:
      SESSIONS_DIR/<session_id>.ndjson   (raw stdout)
      SESSIONS_DIR/<session_id>.meta.json (metadata)

    Returns the path to the NDJSON file.
    """
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    ndjson_path = _SESSIONS_DIR / f"{session_id}.ndjson"
    meta_path = _SESSIONS_DIR / f"{session_id}.meta.json"

    ndjson_path.write_text(stdout, encoding="utf-8")
    meta_path.write_text(
        json.dumps(metadata, indent=2),
        encoding="utf-8",
    )

    return str(ndjson_path)


def _load_session(session_id: str) -> dict:
    """Load a saved session from disk.

    Returns dict with keys: events (list), metadata (dict), session_id (str).
    Raises FileNotFoundError if the session doesn't exist.
    """
    ndjson_path = _SESSIONS_DIR / f"{session_id}.ndjson"
    meta_path = _SESSIONS_DIR / f"{session_id}.meta.json"

    if not ndjson_path.exists():
        raise FileNotFoundError(f"Session not found: {ndjson_path}")

    stdout = ndjson_path.read_text(encoding="utf-8")
    events = _parse_session_events(stdout)

    metadata = {}
    if meta_path.exists():
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))

    return {
        "session_id": session_id,
        "events": events,
        "metadata": metadata,
    }


def _format_session_timeline(
    session_id: str,
    events: list[dict],
    metadata: dict,
) -> str:
    """Format a session as a human-readable timeline string."""
    lines: list[str] = []
    lines.append(f"=== Session {session_id} ===")

    issue_id = metadata.get("issue_id", "?")
    duration = metadata.get("duration_seconds", "?")
    exit_code = metadata.get("exit_code", "?")
    lines.append(f"Issue: {issue_id} | Duration: {duration}s | Exit: {exit_code}")
    lines.append("")

    if not events:
        lines.append("(no events captured)")
    else:
        for event in events:
            ln = event.get("line_number", "?")
            etype = event.get("type", "unknown")
            data = event.get("data", {})

            if etype == "result":
                turns = data.get("num_turns", "?")
                usage = data.get("usage", {})
                inp = usage.get("input_tokens", "?")
                out = usage.get("output_tokens", "?")
                detail = f"turns={turns}, input={inp}, output={out}"
            elif etype in ("assistant", "text"):
                msg = data.get("message", data.get("content", ""))
                if isinstance(msg, dict):
                    msg = msg.get("content", str(msg))
                preview = str(msg)[:80].replace("\n", " ")
                detail = preview
            elif etype == "tool_use":
                tool = data.get("tool", data.get("name", "?"))
                detail = tool
            elif etype == "tool_result":
                detail = "(result)"
            else:
                detail = etype

            lines.append(f"[{ln:>3}] {etype:<15} | {detail}")

    gate_failure = metadata.get("gate_failure")
    suggested_fix = metadata.get("suggested_fix")
    if gate_failure:
        lines.append("")
        lines.append(f"Gate Result: FAILED at {gate_failure}")
    if suggested_fix:
        lines.append(f"Suggested Fix: {suggested_fix}")

    return "\n".join(lines)


def _suggest_claude_md_fix(gate_failures: list[dict]) -> str:
    """Suggest a CLAUDE.md rule based on which gate failed.

    Rule-based (not LLM-based) for speed and determinism.
    """
    if not gate_failures:
        return "No fix needed — all gates passed."

    latest = gate_failures[-1]
    first_failure = None

    if isinstance(latest, dict):
        first_failure = latest.get("first_failure")
        if not first_failure:
            gate_results = latest.get("gate_results", [])
            for gr in gate_results:
                if isinstance(gr, dict) and not gr.get("passed", True):
                    first_failure = gr.get("gate_name", "unknown")
                    break

    if not first_failure:
        return "Gate failure detected but could not identify which gate."

    gate_lower = first_failure.lower()

    if "sanity" in gate_lower or "gate_0" in gate_lower:
        return (
            "Always run the project's test suite before committing. "
            "Check for edge cases and ensure all existing tests still pass."
        )
    if "secret" in gate_lower or "gate_2" in gate_lower:
        return (
            "Never include API keys, tokens, passwords, or credentials "
            "in source code. Use environment variables."
        )
    if "security" in gate_lower or "gate_3" in gate_lower:
        return (
            "Use parameterized queries for all database operations. "
            "Avoid string interpolation in SQL or shell commands."
        )
    if "review" in gate_lower or "gate_4" in gate_lower:
        return (
            "Review code for race conditions, proper error handling, "
            "and resource cleanup before committing."
        )

    return f"Gate '{first_failure}' failed. Review gate output for details."


# ---------------------------------------------------------------------------
# TB-6 pipeline — Session Replay Debug
# ---------------------------------------------------------------------------


def run_tb6(
    issue_id: str,
    repo_path: str,
    force_gate_fail: bool = True,
    max_retries: int = 1,
) -> dict:
    """Run the full TB-6 session replay pipeline.

    Phases:
        1.  Poll + claim issue (intake)
        2.  Setup worktree (orchestration)
        3.  Select persona (orchestration)
        4.  Init tracing (observability)
        5.  Start heartbeat (observability)
        6.  Spawn agent (runtime)
        7.  Save session to disk (observability)
        8.  Run quality gates (gates)
        9.  Parse session timeline (observability)
        10. Retry loop if gates fail
        11. Suggest CLAUDE.md fix (feedback)
        12. Report + cleanup

    Args:
        issue_id: The beads issue ID to process.
        repo_path: Absolute path to the git repository.
        force_gate_fail: Force first gate run to fail (default True).
        max_retries: Maximum retries (default 1).

    Returns:
        A dict (TB6Result) with the outcome including session data.
    """
    pipeline_start = time.monotonic()
    _set_pipeline_timeout()
    provider = init_tracing()

    with tracer_tb6.start_as_current_span(
        "tb6.run",
        attributes={
            "tb6.issue_id": issue_id,
            "tb6.repo_path": repo_path,
            "tb6.force_gate_fail": force_gate_fail,
            "tb6.max_retries": max_retries,
        },
    ) as root_span:
        root_trace_id = _trace_id_hex(root_span)
        attempt_span_ids: list[str] = []

        heartbeat_event = None
        heartbeat_thread = None
        worktree_path: str | None = None
        persona_name: str | None = None
        session_id: str | None = None
        session_path: str | None = None
        session_events: list[dict] = []
        session_event_types: dict[str, int] = {}
        gate_failure: str | None = None
        suggested_fix: str | None = None
        retries_used = 0
        pipeline_success = False

        try:
            # Phase 1: Poll + claim
            with tracer_tb6.start_as_current_span(
                "tb6.phase.poll", attributes={"tb6.phase": "poll"},
            ) as poll_span:
                items = poll_ready(repo_path=repo_path)
                issue = None
                for item in items:
                    if item.id == issue_id:
                        issue = item
                        break
                if issue is None:
                    poll_span.set_attribute("tb6.issue_found_in_poll", False)
                    issue = get_issue(issue_id, repo_path=repo_path)
                if issue is None:
                    issue_title = issue_id
                    issue_description = ""
                    issue_labels: list[str] = []
                else:
                    poll_span.set_attribute("tb6.issue_found_in_poll", True)
                    issue_title = issue.title
                    issue_description = issue.description or ""
                    issue_labels = issue.labels
                    if not issue_labels:
                        full_issue = get_issue(issue_id, repo_path=repo_path)
                        if full_issue and full_issue.labels:
                            issue_labels = full_issue.labels

            with tracer_tb6.start_as_current_span(
                "tb6.phase.claim",
                attributes={"tb6.phase": "claim", "issue.id": issue_id},
            ) as claim_span:
                claimed = claim_issue(issue_id, repo_path=repo_path)
                claim_span.set_attribute("tb6.claimed", claimed)
                if not claimed:
                    elapsed = time.monotonic() - pipeline_start
                    return TB6Result(
                        issue_id=issue_id, repo_path=repo_path,
                        success=False, phase="claim",
                        error=f"Could not claim issue {issue_id}",
                        duration_seconds=round(elapsed, 2),
                        trace_id=root_trace_id,
                        force_gate_fail_used=force_gate_fail,
                    ).model_dump()

            # Phase 2: Worktree
            with tracer_tb6.start_as_current_span(
                "tb6.phase.setup_worktree", attributes={"tb6.phase": "setup_worktree"},
            ):
                wt_result = setup_worktree(issue_id, repo_path)
                if not wt_result.get("success"):
                    elapsed = time.monotonic() - pipeline_start
                    return TB6Result(
                        issue_id=issue_id, repo_path=repo_path,
                        success=False, phase="setup_worktree",
                        error=wt_result.get("message", "Worktree setup failed"),
                        duration_seconds=round(elapsed, 2),
                        trace_id=root_trace_id,
                        force_gate_fail_used=force_gate_fail,
                    ).model_dump()
                worktree_path = wt_result["worktree_path"]

            # Phase 3: Persona + CLAUDE.md overlay
            with tracer_tb6.start_as_current_span(
                "tb6.phase.persona", attributes={"tb6.phase": "persona"},
            ) as persona_span:
                persona_result = select_persona(issue_labels, issue_description=issue_description)
                persona_name = persona_result.get("name", "feature")
                persona_span.set_attribute("tb6.persona", persona_name)

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

            # Phase 5: Heartbeat
            with tracer_tb6.start_as_current_span(
                "tb6.phase.heartbeat_start", attributes={"tb6.phase": "heartbeat_start"},
            ):
                heartbeat_event, heartbeat_thread = start_heartbeat(
                    issue_id, interval_seconds=30, worktree_path=worktree_path,
                )

            # Phase 6: Spawn agent
            with tracer_tb6.start_as_current_span(
                "tb6.phase.spawn_agent",
                attributes={"tb6.phase": "spawn_agent", "tb6.attempt": 0},
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
                agent_stdout = agent_result.get("stdout", "")
                agent_span.set_attribute("tb6.agent_exit_code", agent_exit)
                attempt_span_ids.append(_span_id_hex(agent_span))

                if agent_exit != 0:
                    elapsed = time.monotonic() - pipeline_start
                    return TB6Result(
                        issue_id=issue_id, repo_path=repo_path,
                        success=False, phase="spawn_agent",
                        worktree_path=worktree_path, persona=persona_name,
                        error=agent_result.get("stderr", "Agent failed"),
                        duration_seconds=round(elapsed, 2),
                        trace_id=root_trace_id,
                        attempt_span_ids=attempt_span_ids,
                        force_gate_fail_used=force_gate_fail,
                    ).model_dump()

            # Phase 7: Save session to disk
            with tracer_tb6.start_as_current_span(
                "tb6.phase.save_session", attributes={"tb6.phase": "save_session"},
            ) as session_span:
                session_id = _generate_session_id(issue_id)
                session_metadata = {
                    "issue_id": issue_id,
                    "trace_id": root_trace_id,
                    "timestamp": int(time.time()),
                    "exit_code": agent_exit,
                    "duration_seconds": agent_result.get("duration_seconds", 0),
                    "model": persona_result.get("model", "sonnet"),
                    "persona": persona_name,
                    "worktree_path": worktree_path,
                }
                session_path = _save_session(session_id, agent_stdout, session_metadata)
                session_span.set_attribute("tb6.session_id", session_id)
                logger.info("TB-6: Session saved as %s", session_id)

            # Phase 8: Run gates
            with tracer_tb6.start_as_current_span(
                "tb6.phase.gates",
                attributes={"tb6.phase": "gates", "tb6.force_fail": force_gate_fail},
            ) as gates_span:
                if force_gate_fail:
                    logger.info("TB-6: FORCED FAILURE on initial gate run")
                    gate_raw = _make_forced_failure()
                else:
                    gate_raw = run_all_gates(
                        worktree_path=worktree_path,
                        issue_title=issue_title,
                        issue_description=issue_description,
                        num_turns=agent_result.get("num_turns", 0),
                        input_tokens=agent_result.get("input_tokens", 0),
                        output_tokens=agent_result.get("output_tokens", 0),
                    )
                try:
                    gate_suite = GateSuiteResult(**gate_raw)
                except Exception as exc:
                    elapsed = time.monotonic() - pipeline_start
                    return TB6Result(
                        issue_id=issue_id, repo_path=repo_path,
                        success=False, phase="gates",
                        error=f"Malformed gate result: {exc}",
                        duration_seconds=round(elapsed, 2),
                        trace_id=root_trace_id,
                        session_id=session_id, session_path=session_path,
                        force_gate_fail_used=force_gate_fail,
                    ).model_dump()

                gates_span.set_attribute("tb6.gates_passed", gate_suite.overall_passed)
                if gate_suite.first_failure:
                    gates_span.set_attribute("tb6.first_failure", gate_suite.first_failure)

            # Phase 9: Parse session
            with tracer_tb6.start_as_current_span(
                "tb6.phase.parse_session", attributes={"tb6.phase": "parse_session"},
            ) as parse_span:
                session_events = _parse_session_events(agent_stdout)
                for ev in session_events:
                    t = ev.get("type", "unknown")
                    session_event_types[t] = session_event_types.get(t, 0) + 1
                parse_span.set_attribute("tb6.session_event_count", len(session_events))

            # Gates passed first try
            if gate_suite.overall_passed:
                elapsed = time.monotonic() - pipeline_start
                root_span.set_attribute("tb6.outcome", "success_first_attempt")
                root_span.set_attribute("status.detail", "Gates passed")
                root_span.set_status(trace.StatusCode.OK)
                pipeline_success = True
                return TB6Result(
                    issue_id=issue_id, repo_path=repo_path,
                    success=True, phase="gates_passed",
                    worktree_path=worktree_path, persona=persona_name,
                    duration_seconds=round(elapsed, 2),
                    trace_id=root_trace_id,
                    attempt_span_ids=attempt_span_ids,
                    session_id=session_id, session_path=session_path,
                    session_event_count=len(session_events),
                    session_event_types=session_event_types,
                    force_gate_fail_used=force_gate_fail,
                    suggested_fix="No fix needed — all gates passed.",
                ).model_dump()

            # Phase 10: Retry loop
            all_gate_failures: list[dict] = [gate_raw]
            gate_failure = gate_suite.first_failure

            for attempt in range(1, max_retries + 1):
                retries_used = attempt
                with tracer_tb6.start_as_current_span(
                    "tb6.phase.retry",
                    attributes={"tb6.phase": "retry", "tb6.attempt": attempt},
                ) as retry_span:
                    attempt_span_ids.append(_span_id_hex(retry_span))
                    retry_raw = retry_agent(
                        worktree_path=worktree_path,
                        issue_id=issue_id,
                        issue_title=issue_title,
                        issue_description=issue_description,
                        gate_failures=all_gate_failures,
                        attempt=attempt,
                        max_retries=max_retries,
                        model=persona_result.get("model", "sonnet"),
                    )
                    retry_success = retry_raw.get("success", False)
                    retry_span.set_attribute("tb6.retry_success", retry_success)
                    if retry_success:
                        pipeline_success = True
                        gate_failure = None
                        break
                    retry_gate = retry_raw.get("gate_results")
                    if retry_gate:
                        all_gate_failures.append(retry_gate)
                        if isinstance(retry_gate, dict):
                            gate_failure = retry_gate.get("first_failure", gate_failure)

            # Phase 11: Suggest fix
            with tracer_tb6.start_as_current_span(
                "tb6.phase.suggest_fix", attributes={"tb6.phase": "suggest_fix"},
            ) as fix_span:
                suggested_fix = _suggest_claude_md_fix(all_gate_failures)
                fix_span.set_attribute("tb6.suggested_fix", suggested_fix)

                # Update session metadata
                session_metadata["gate_failure"] = gate_failure
                session_metadata["suggested_fix"] = suggested_fix
                session_metadata["success"] = pipeline_success
                meta_path = _SESSIONS_DIR / f"{session_id}.meta.json"
                meta_path.write_text(json.dumps(session_metadata, indent=2), encoding="utf-8")

            # Escalate if retries exhausted
            if not pipeline_success:
                with tracer_tb6.start_as_current_span(
                    "tb6.phase.escalate", attributes={"tb6.phase": "escalate"},
                ):
                    escalate_to_human(
                        issue_id=issue_id,
                        gate_failures=all_gate_failures,
                        attempts=retries_used + 1,
                        repo_path=repo_path,
                    )

            # Return result
            elapsed = time.monotonic() - pipeline_start
            outcome = "success" if pipeline_success else "escalated"
            root_span.set_attribute("tb6.outcome", outcome)
            if pipeline_success:
                root_span.set_attribute("status.detail", "Gates passed")
                root_span.set_status(trace.StatusCode.OK)
            else:
                root_span.set_status(trace.StatusCode.ERROR, "Retries exhausted")

            return TB6Result(
                issue_id=issue_id, repo_path=repo_path,
                success=pipeline_success,
                phase="gates_passed" if pipeline_success else "suggest_fix",
                worktree_path=worktree_path, persona=persona_name,
                retries_used=retries_used, max_retries=max_retries,
                escalated=not pipeline_success,
                duration_seconds=round(elapsed, 2),
                trace_id=root_trace_id,
                attempt_span_ids=attempt_span_ids,
                session_id=session_id, session_path=session_path,
                session_event_count=len(session_events),
                session_event_types=session_event_types,
                gate_failure=gate_failure, suggested_fix=suggested_fix,
                force_gate_fail_used=force_gate_fail,
            ).model_dump()

        except Exception as exc:
            elapsed = time.monotonic() - pipeline_start
            error_msg = f"Pipeline error: {type(exc).__name__}: {exc}"
            logger.exception("TB-6 pipeline error for issue %s", issue_id)
            root_span.set_status(trace.StatusCode.ERROR, error_msg)
            root_span.record_exception(exc)
            return TB6Result(
                issue_id=issue_id, repo_path=repo_path,
                success=False, phase="error",
                error=error_msg,
                duration_seconds=round(elapsed, 2),
                trace_id=root_trace_id,
                session_id=session_id, session_path=session_path,
                session_event_count=len(session_events),
                session_event_types=session_event_types,
                gate_failure=gate_failure, suggested_fix=suggested_fix,
                force_gate_fail_used=force_gate_fail,
            ).model_dump()

        finally:
            if heartbeat_event is not None:
                stop_heartbeat(heartbeat_event, heartbeat_thread)
            if worktree_path is not None:
                try:
                    cleanup_worktree(issue_id)
                except Exception:
                    logger.warning("Failed to clean up worktree for %s", issue_id)
            if not pipeline_success and worktree_path is not None:
                _unclaim_issue(issue_id, repo_path=repo_path)
            # Phase 13: Post-pipeline feedback channels (best-effort)
            try:
                from devloop.feedback.post_pipeline import run_post_pipeline
                run_post_pipeline(
                    issue_id=issue_id,
                    success=pipeline_success,
                )
            except Exception:
                pass  # Never fail the pipeline for post-pipeline channels
            try:
                with tracer_tb6.start_as_current_span(
                    "tb6.phase.cleanup", attributes={"tb6.phase": "cleanup"},
                ):
                    pass
            except Exception:
                pass
            if provider is not None:
                try:
                    provider.force_flush(timeout_millis=5000)
                except Exception:
                    pass

            _clear_pipeline_timeout()


# ---------------------------------------------------------------------------
# TB-6 replay — load and display saved sessions
# ---------------------------------------------------------------------------


def replay_session(session_id: str) -> dict:
    """Load a saved session and return its formatted timeline.

    Args:
        session_id: The session ID to replay.

    Returns:
        Dict with timeline (str), event_count (int), metadata (dict).
    """
    session = _load_session(session_id)
    events = session["events"]
    metadata = session["metadata"]

    timeline = _format_session_timeline(session_id, events, metadata)

    event_types: dict[str, int] = {}
    for ev in events:
        t = ev.get("type", "unknown")
        event_types[t] = event_types.get(t, 0) + 1

    return {
        "session_id": session_id,
        "timeline": timeline,
        "event_count": len(events),
        "event_types": event_types,
        "metadata": metadata,
    }
