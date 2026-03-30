"""Tests for TB-1 golden path pipeline (A-5 post-decomposition)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from devloop.intake.ambiguity import AmbiguityResult, AmbiguitySignal
from devloop.intake.beads_poller import WorkItem

# ---------------------------------------------------------------------------
# Module path prefix — all patches target tb1_golden_path
# ---------------------------------------------------------------------------

_M = "devloop.feedback.tb1_golden_path"


# ---------------------------------------------------------------------------
# Shared fixture — patches all 16 layer-boundary dependencies with defaults
# ---------------------------------------------------------------------------


@pytest.fixture
def tb1_mocks(tmp_path):
    """Standard TB-1 mocks — all layer boundaries patched with sane defaults.

    Returns a SimpleNamespace with all mocks accessible by name.
    Each test overrides just what it needs.
    """
    worktree_dir = tmp_path / "TEST-FIXTURE"
    worktree_dir.mkdir()

    patches = {
        "init_tracing": patch(f"{_M}.init_tracing", return_value=MagicMock()),
        "poll_ready": patch(f"{_M}.poll_ready", return_value=[]),
        "get_issue": patch(f"{_M}.get_issue"),
        "claim_issue": patch(f"{_M}.claim_issue", return_value=True),
        "setup_worktree": patch(f"{_M}.setup_worktree"),
        "select_persona": patch(f"{_M}.select_persona"),
        "build_claude_md_overlay": patch(f"{_M}.build_claude_md_overlay"),
        "spawn_agent": patch(f"{_M}.spawn_agent"),
        "run_all_gates": patch(f"{_M}.run_all_gates"),
        "create_pull_request": patch(f"{_M}.create_pull_request"),
        "start_heartbeat": patch(f"{_M}.start_heartbeat", return_value=(MagicMock(), MagicMock())),
        "stop_heartbeat": patch(f"{_M}.stop_heartbeat"),
        "cleanup_worktree": patch(f"{_M}.cleanup_worktree"),
        "find_cascade_targets": patch(f"{_M}.find_cascade_targets", return_value=[]),
        "run_tb5": patch(f"{_M}.run_tb5"),
        "_generate_session_id": patch(f"{_M}._generate_session_id", return_value="SID-001"),
        "_save_session": patch(f"{_M}._save_session", return_value=str(tmp_path / "session.ndjson")),
        "_suggest_claude_md_fix": patch(f"{_M}._suggest_claude_md_fix", return_value=None),
        "retry_agent": patch(f"{_M}.retry_agent"),
        "escalate_to_human": patch(f"{_M}.escalate_to_human", return_value={"success": True}),
        "_unclaim_issue": patch(f"{_M}._unclaim_issue"),
        "_read_handoff": patch(f"{_M}._read_handoff", return_value=None),
        "_clear_handoff": patch(f"{_M}._clear_handoff"),
        "subprocess_run": patch(f"{_M}.subprocess.run"),
        "detect_ambiguity": patch(
            "devloop.intake.ambiguity.detect_ambiguity",
            return_value=AmbiguityResult(is_ambiguous=False, score=0.0, signals=[], title="", description=""),
        ),
    }

    mocks = {}
    for name, p in patches.items():
        mocks[name] = p.start()

    ns = SimpleNamespace(**mocks, worktree_dir=worktree_dir)

    # Sane defaults
    ns.get_issue.return_value = WorkItem(
        id="TEST-001", title="Fix bug", type="bug",
        priority=1, labels=["bug", "repo:OOTestProject1"], description="Fix it",
    )
    ns.setup_worktree.return_value = {
        "success": True, "worktree_path": str(worktree_dir),
        "branch_name": "dl/TEST-001",
    }
    ns.select_persona.return_value = {
        "name": "bug-fix", "model": "sonnet", "retry_max": 2,
        "max_turns_default": 10, "max_context_pct": 75,
    }
    ns.build_claude_md_overlay.return_value = {"overlay_text": "# Instructions\nFix the bug"}
    ns.spawn_agent.return_value = {
        "exit_code": 0, "stdout": '{"type": "result"}\n',
        "num_turns": 3, "input_tokens": 100, "output_tokens": 50,
    }
    ns.run_all_gates.return_value = {
        "overall_passed": True, "first_failure": None,
        "gate_results": [], "total_duration_seconds": 0.5,
    }
    ns.create_pull_request.return_value = {
        "success": True, "pr_url": "https://github.com/test/pr/1",
    }

    yield ns

    for p in patches.values():
        p.stop()


def _run_tb1(issue_id="TEST-001", repo_path="/tmp/OOTestProject1"):
    from devloop.feedback.tb1_golden_path import run_tb1
    return run_tb1(issue_id, repo_path)


# ===========================================================================
# TestTB1GoldenPath — happy path + early exit branches
# ===========================================================================


class TestTB1GoldenPath:
    """Tests for run_tb1() — mocked at every layer boundary."""

    def test_issue_not_found_returns_error(self, tb1_mocks):
        tb1_mocks.get_issue.return_value = None
        result = _run_tb1("NONEXISTENT")
        assert result["success"] is False
        assert "not found" in result.get("error", "").lower()

    def test_gates_pass_returns_success(self, tb1_mocks):
        result = _run_tb1()
        assert result["success"] is True
        assert result["phase"] == "gates_passed"

    def test_setup_worktree_failure_returns_error(self, tb1_mocks):
        tb1_mocks.setup_worktree.return_value = {
            "success": False, "worktree_path": "",
            "message": "Not a git repository",
        }
        result = _run_tb1()
        assert result["success"] is False
        assert result["phase"] == "setup_worktree"
        tb1_mocks.spawn_agent.assert_not_called()

    def test_claim_failure_returns_error(self, tb1_mocks):
        tb1_mocks.claim_issue.return_value = False
        result = _run_tb1()
        assert result["success"] is False
        assert result["phase"] == "claim"

    def test_agent_spawn_failure_returns_error(self, tb1_mocks):
        tb1_mocks.spawn_agent.return_value = {
            "exit_code": 1, "stderr": "Agent crashed",
            "num_turns": 0, "input_tokens": 0, "output_tokens": 0,
        }
        result = _run_tb1()
        assert result["success"] is False
        assert result["phase"] == "spawn_agent"
        assert result["agent_exit_code"] == 1

    def test_gate_suite_parse_error(self, tb1_mocks):
        # Return something that GateSuiteResult can't parse
        tb1_mocks.run_all_gates.return_value = {"bogus": "data"}
        result = _run_tb1()
        assert result["success"] is False
        assert result["phase"] == "gates"
        assert "malformed" in result.get("error", "").lower()

    def test_pr_creation_failure_still_succeeds(self, tb1_mocks):
        tb1_mocks.create_pull_request.return_value = {
            "success": False, "message": "gh CLI not found",
        }
        result = _run_tb1()
        assert result["success"] is True
        assert result.get("pr_url") is None

    def test_empty_agent_stdout_no_session(self, tb1_mocks):
        tb1_mocks.spawn_agent.return_value = {
            "exit_code": 0, "stdout": "",
            "num_turns": 1, "input_tokens": 50, "output_tokens": 25,
        }
        result = _run_tb1()
        assert result["success"] is True
        assert result.get("session_id") is None
        tb1_mocks._save_session.assert_not_called()

    def test_no_cascade_targets_no_tb5_called(self, tb1_mocks):
        tb1_mocks.find_cascade_targets.return_value = []
        result = _run_tb1()
        assert result["success"] is True
        tb1_mocks.run_tb5.assert_not_called()
        assert result.get("cascade_results") == []

    def test_ambiguous_issue_returns_error(self, tb1_mocks):
        tb1_mocks.detect_ambiguity.return_value = AmbiguityResult(
            is_ambiguous=True, score=0.85,
            signals=[AmbiguitySignal(signal_type="vague_title", detail="Too vague")],
            title="Refactor everything", description="Make it better",
        )
        result = _run_tb1()
        assert result["success"] is False
        assert result["phase"] == "ambiguity_check"
        assert "ambiguous" in result.get("error", "").lower()

    def test_cascade_issue_skips_ambiguity_check(self, tb1_mocks):
        """Cascade issues (auto-generated by TB-5) bypass ambiguity gating."""
        tb1_mocks.get_issue.return_value = WorkItem(
            id="CASCADE-001",
            title="[cascade] Adapt to upstream changes from SRC-001: update db",
            type="feature",
            priority=1,
            labels=["cascade", "repo:OOTestProject1"],
            description="Upstream issue SRC-001 changed files matching: src/db/**",
        )
        # Even though this would be flagged as ambiguous, cascade label exempts it
        tb1_mocks.detect_ambiguity.return_value = AmbiguityResult(
            is_ambiguous=True, score=0.9, signals=["vague"],
            title="[cascade] Adapt", description="upstream changes",
        )
        result = _run_tb1("CASCADE-001")
        assert result["success"] is True
        # detect_ambiguity should NOT have been called (skipped entirely)
        tb1_mocks.detect_ambiguity.assert_not_called()


# ===========================================================================
# TestTB1CascadeIntegration
# ===========================================================================


class TestTB1CascadeIntegration:
    """Tests for TB-1 -> TB-5 cascade wiring after PR creation."""

    def test_cascade_triggered_after_pr(self, tb1_mocks):
        tb1_mocks.find_cascade_targets.return_value = [{
            "target_repo_name": "OOTestProject1",
            "target_repo_path": "/home/user/OOTestProject1",
            "matched_watches": ["src/oo_test_project/db/**"],
            "dependency_type": "data-model",
        }]
        tb1_mocks.run_tb5.return_value = {"success": True, "cascade_skipped": False}

        result = _run_tb1()

        assert result["success"] is True
        tb1_mocks.find_cascade_targets.assert_called_once()
        tb1_mocks.run_tb5.assert_called_once()
        assert len(result.get("cascade_results", [])) == 1

    def test_cascade_failure_does_not_break_tb1(self, tb1_mocks):
        tb1_mocks.find_cascade_targets.side_effect = RuntimeError("cascade exploded")

        result = _run_tb1()

        assert result["success"] is True  # TB-1 still succeeded


# ===========================================================================
# TestTB1SessionCapture
# ===========================================================================


class TestTB1SessionCapture:
    """Tests for TB-1 -> TB-6 session capture wiring."""

    def test_session_saved_on_success(self, tb1_mocks):
        result = _run_tb1()
        assert result["success"] is True
        assert result.get("session_id") == "SID-001"
        tb1_mocks._save_session.assert_called_once()

    def test_session_capture_failure_does_not_break_tb1(self, tb1_mocks):
        tb1_mocks._save_session.side_effect = OSError("disk full")

        result = _run_tb1()

        assert result["success"] is True  # TB-1 still succeeded


# ===========================================================================
# TestTB1RetryPath
# ===========================================================================


class TestTB1RetryPath:
    """Tests for the retry loop when gates fail."""

    def _make_gates_fail(self, tb1_mocks):
        """Set up gate failure so the retry loop is entered."""
        tb1_mocks.run_all_gates.return_value = {
            "overall_passed": False,
            "first_failure": "gate_0_sanity",
            "gate_results": [
                {"gate_name": "gate_0_sanity", "passed": False,
                 "findings": [{"severity": "critical", "message": "Tests failed"}]},
            ],
            "total_duration_seconds": 0.5,
        }

    def test_retry_succeeds_after_gate_failure(self, tb1_mocks):
        self._make_gates_fail(tb1_mocks)
        tb1_mocks.retry_agent.return_value = {
            "success": True,
            "gate_results": {
                "overall_passed": True, "first_failure": None,
                "gate_results": [], "total_duration_seconds": 0.3,
            },
        }

        result = _run_tb1()

        assert result["success"] is True
        assert result["phase"] == "retry_passed"
        assert result["retries_used"] == 1

    def test_retry_all_exhausted_escalates(self, tb1_mocks):
        self._make_gates_fail(tb1_mocks)
        tb1_mocks.retry_agent.return_value = {
            "success": False,
            "gate_results": {
                "overall_passed": False, "first_failure": "gate_0_sanity",
                "gate_results": [
                    {"gate_name": "gate_0_sanity", "passed": False,
                     "findings": [{"severity": "critical", "message": "Still failing"}]},
                ],
                "total_duration_seconds": 0.2,
            },
        }

        result = _run_tb1()

        assert result["success"] is False
        assert result["phase"] == "escalated"
        assert result["escalated"] is True
        tb1_mocks.escalate_to_human.assert_called_once()

    def test_retry_pr_created_after_retry_success(self, tb1_mocks):
        self._make_gates_fail(tb1_mocks)
        tb1_mocks.retry_agent.return_value = {
            "success": True,
            "gate_results": {
                "overall_passed": True, "first_failure": None,
                "gate_results": [], "total_duration_seconds": 0.3,
            },
        }

        _run_tb1()

        # PR created twice: once in initial path (skipped because gates failed),
        # but create_pull_request is only called after gates pass.
        # After retry success, it should be called.
        tb1_mocks.create_pull_request.assert_called()

    def test_retry_cascade_triggered_after_retry_pr(self, tb1_mocks):
        self._make_gates_fail(tb1_mocks)
        tb1_mocks.retry_agent.return_value = {
            "success": True,
            "gate_results": {
                "overall_passed": True, "first_failure": None,
                "gate_results": [], "total_duration_seconds": 0.3,
            },
        }
        tb1_mocks.find_cascade_targets.return_value = [{
            "target_repo_name": "backend",
            "target_repo_path": "/tmp/backend",
            "matched_watches": ["src/**"],
            "dependency_type": "api-contract",
        }]
        tb1_mocks.run_tb5.return_value = {"success": True}

        result = _run_tb1()

        assert result["success"] is True
        # find_cascade_targets called in retry success path
        tb1_mocks.find_cascade_targets.assert_called()
        tb1_mocks.run_tb5.assert_called()

    def test_retry_gate_failure_accumulated(self, tb1_mocks):
        """Each retry receives the full history of gate failures."""
        self._make_gates_fail(tb1_mocks)
        # Track list length at each retry_agent call
        lengths_at_call = []

        def _capture_retry(**kwargs):
            lengths_at_call.append(len(kwargs["gate_failures"]))
            return {
                "success": False,
                "gate_results": {
                    "overall_passed": False, "first_failure": "gate_0_sanity",
                    "gate_results": [
                        {"gate_name": "gate_0_sanity", "passed": False,
                         "findings": [{"severity": "critical", "message": "Still failing"}]},
                    ],
                    "total_duration_seconds": 0.2,
                },
            }

        tb1_mocks.retry_agent.side_effect = _capture_retry

        _run_tb1()

        assert tb1_mocks.retry_agent.call_count == 2
        # First retry sees 1 failure (initial), second sees 2 (initial + retry 1)
        assert lengths_at_call == [1, 2]

    def test_retry_spawn_failure_synthesized(self, tb1_mocks):
        """When retry agent fails to spawn, a synthetic gate failure is created."""
        self._make_gates_fail(tb1_mocks)
        # First retry: spawn failure (no gate_results, has error)
        tb1_mocks.retry_agent.side_effect = [
            {"success": False, "error": "Agent process crashed", "gate_results": None},
            {"success": False, "error": "Agent process crashed again", "gate_results": None},
        ]

        result = _run_tb1()

        assert result["success"] is False
        assert result["phase"] == "escalated"
        # The escalate call should have received accumulated failures
        # including the synthetic agent_spawn failure
        esc_call = tb1_mocks.escalate_to_human.call_args
        gate_failures = esc_call.kwargs.get("gate_failures") or esc_call[1].get("gate_failures")
        # Should have: initial gate failure + 2 synthetic spawn failures
        assert len(gate_failures) == 3


# ===========================================================================
# TestTB1ContextRestart
# ===========================================================================


class TestTB1ContextRestart:
    """Tests for context restart when agent hits context limit."""

    def test_context_restart_with_handoff(self, tb1_mocks):
        # First spawn: context limited
        tb1_mocks.spawn_agent.side_effect = [
            {
                "exit_code": 0, "stdout": '{"type": "result"}\n',
                "num_turns": 3, "input_tokens": 100, "output_tokens": 50,
                "context_limited": True, "context_pct": 95.0,
            },
            {
                "exit_code": 0, "stdout": '{"type": "result"}\n',
                "num_turns": 2, "input_tokens": 80, "output_tokens": 40,
                "context_limited": False, "context_pct": 50.0,
            },
        ]
        tb1_mocks._read_handoff.return_value = "Continue from where I left off"

        result = _run_tb1()

        assert result["success"] is True
        assert tb1_mocks.spawn_agent.call_count == 2

    def test_context_restart_no_handoff_skips(self, tb1_mocks):
        tb1_mocks.spawn_agent.return_value = {
            "exit_code": 0, "stdout": '{"type": "result"}\n',
            "num_turns": 3, "input_tokens": 100, "output_tokens": 50,
            "context_limited": True, "context_pct": 95.0,
        }
        tb1_mocks._read_handoff.return_value = None  # no handoff

        result = _run_tb1()

        # Should NOT restart — only one spawn call
        assert tb1_mocks.spawn_agent.call_count == 1
        assert result["success"] is True

    def test_context_restart_max_reached(self, tb1_mocks):
        from devloop.feedback.tb4_runaway import MAX_CONTEXT_RESTARTS

        # Every spawn returns context_limited
        tb1_mocks.spawn_agent.return_value = {
            "exit_code": 0, "stdout": '{"type": "result"}\n',
            "num_turns": 3, "input_tokens": 100, "output_tokens": 50,
            "context_limited": True, "context_pct": 95.0,
        }
        tb1_mocks._read_handoff.return_value = "Continue"

        result = _run_tb1()

        # Initial spawn + MAX_CONTEXT_RESTARTS restarts
        assert tb1_mocks.spawn_agent.call_count == 1 + MAX_CONTEXT_RESTARTS


# ===========================================================================
# TestTB1Escalation
# ===========================================================================


class TestTB1Escalation:
    """Tests for escalation when retries are exhausted."""

    def _exhaust_retries(self, tb1_mocks):
        """Set up gate failure + retry failures to reach escalation."""
        tb1_mocks.run_all_gates.return_value = {
            "overall_passed": False,
            "first_failure": "gate_0_sanity",
            "gate_results": [
                {"gate_name": "gate_0_sanity", "passed": False,
                 "findings": [{"severity": "critical", "message": "Tests failed"}]},
            ],
            "total_duration_seconds": 0.5,
        }
        tb1_mocks.retry_agent.return_value = {
            "success": False,
            "gate_results": {
                "overall_passed": False, "first_failure": "gate_0_sanity",
                "gate_results": [
                    {"gate_name": "gate_0_sanity", "passed": False,
                     "findings": [{"severity": "critical", "message": "Still failing"}]},
                ],
                "total_duration_seconds": 0.2,
            },
        }

    def test_escalation_with_suggested_fix(self, tb1_mocks):
        self._exhaust_retries(tb1_mocks)
        tb1_mocks._suggest_claude_md_fix.return_value = "Add rule: always run tests before commit"

        result = _run_tb1()

        assert result["success"] is False
        assert result["phase"] == "escalated"
        assert result.get("suggested_fix") == "Add rule: always run tests before commit"
        # br comments add should have been called (among other subprocess.run calls)
        br_calls = [
            c for c in tb1_mocks.subprocess_run.call_args_list
            if c[0] and "br" in c[0][0] and "comments" in c[0][0]
        ]
        assert len(br_calls) == 1

    def test_escalation_suggested_fix_failure_silent(self, tb1_mocks):
        self._exhaust_retries(tb1_mocks)
        tb1_mocks._suggest_claude_md_fix.side_effect = RuntimeError("fix gen failed")

        result = _run_tb1()

        # Escalation still completes (fail-safe)
        assert result["success"] is False
        assert result["phase"] == "escalated"

    def test_escalation_unclaims_issue(self, tb1_mocks):
        self._exhaust_retries(tb1_mocks)

        result = _run_tb1()

        assert result["success"] is False
        # _unclaim_issue should be called in finally block for failed pipelines
        tb1_mocks._unclaim_issue.assert_called_once()


# ===========================================================================
# TestTB1Cleanup
# ===========================================================================


class TestTB1Cleanup:
    """Tests for cleanup — always runs regardless of outcome."""

    def test_cleanup_always_runs_on_success(self, tb1_mocks):
        result = _run_tb1()
        assert result["success"] is True
        tb1_mocks.stop_heartbeat.assert_called_once()
        tb1_mocks.cleanup_worktree.assert_called_once()
        tb1_mocks._clear_handoff.assert_called()

    def test_cleanup_runs_on_agent_failure(self, tb1_mocks):
        tb1_mocks.spawn_agent.return_value = {
            "exit_code": 1, "stderr": "crash",
            "num_turns": 0, "input_tokens": 0, "output_tokens": 0,
        }
        result = _run_tb1()
        assert result["success"] is False
        tb1_mocks.stop_heartbeat.assert_called_once()
        tb1_mocks.cleanup_worktree.assert_called_once()

    def test_cleanup_runs_on_unexpected_exception(self, tb1_mocks):
        tb1_mocks.spawn_agent.side_effect = RuntimeError("unexpected boom")
        result = _run_tb1()
        assert result["success"] is False
        assert result["phase"] == "error"
        tb1_mocks.stop_heartbeat.assert_called_once()
        tb1_mocks.cleanup_worktree.assert_called_once()


# ===========================================================================
# TestTB1ZeroDiff — zero-diff detection (#31)
# ===========================================================================


class TestTB1ZeroDiff:
    """Tests for zero-diff detection in TB-1 pipeline."""

    def test_zero_diff_returns_needs_verification(self, tb1_mocks):
        """Agent completes with no changes → phase=zero_diff, success=False."""
        import subprocess as real_subprocess

        tb1_mocks.subprocess_run.return_value = real_subprocess.CompletedProcess(
            args=["git", "diff"], returncode=0, stdout="", stderr=""
        )
        result = _run_tb1()
        assert result["success"] is False
        assert result["phase"] == "zero_diff"
        assert "zero changes" in result["error"].lower()
        # Gates should NOT have been called
        tb1_mocks.run_all_gates.assert_not_called()

    def test_diff_present_proceeds_to_gates(self, tb1_mocks):
        """Agent produces changes → zero-diff check passes, gates run."""
        import subprocess as real_subprocess

        tb1_mocks.subprocess_run.return_value = real_subprocess.CompletedProcess(
            args=["git", "diff"], returncode=0,
            stdout=" 1 file changed, 3 insertions(+)", stderr=""
        )
        result = _run_tb1()
        assert result["success"] is True
        assert result["phase"] != "zero_diff"
        tb1_mocks.run_all_gates.assert_called_once()

    def test_zero_diff_includes_session_id(self, tb1_mocks):
        """Zero-diff result includes the session capture from Phase 7c."""
        import subprocess as real_subprocess

        tb1_mocks.subprocess_run.return_value = real_subprocess.CompletedProcess(
            args=["git", "diff"], returncode=0, stdout="", stderr=""
        )
        result = _run_tb1()
        assert result["phase"] == "zero_diff"
        assert result.get("session_id") == "SID-001"
