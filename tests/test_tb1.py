"""Tests for TB-1 golden path pipeline (A-5 post-decomposition)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class TestTB1GoldenPath:
    """Tests for run_tb1() — mocked at every layer boundary."""

    @patch("devloop.feedback.tb1_golden_path.init_tracing", return_value=MagicMock())
    @patch("devloop.feedback.tb1_golden_path.poll_ready", return_value=[])
    @patch("devloop.feedback.tb1_golden_path.get_issue")
    def test_issue_not_found_returns_error(self, mock_get, mock_poll, mock_tracing):
        """run_tb1 returns error when issue can't be fetched."""
        from devloop.feedback.tb1_golden_path import run_tb1

        mock_get.return_value = None

        result = run_tb1("NONEXISTENT", "/tmp/fake-repo")

        assert result["success"] is False
        assert "not found" in result.get("error", "").lower() or "not found" in result.get("message", "").lower()

    @patch("devloop.feedback.tb1_golden_path.init_tracing", return_value=MagicMock())
    @patch("devloop.feedback.tb1_golden_path.cleanup_worktree")
    @patch("devloop.feedback.tb1_golden_path.stop_heartbeat")
    @patch("devloop.feedback.tb1_golden_path.start_heartbeat", return_value=(MagicMock(), MagicMock()))
    @patch("devloop.feedback.tb1_golden_path.run_all_gates")
    @patch("devloop.feedback.tb1_golden_path.spawn_agent")
    @patch("devloop.feedback.tb1_golden_path.build_claude_md_overlay")
    @patch("devloop.feedback.tb1_golden_path.select_persona")
    @patch("devloop.feedback.tb1_golden_path.setup_worktree")
    @patch("devloop.feedback.tb1_golden_path.claim_issue", return_value=True)
    @patch("devloop.feedback.tb1_golden_path.get_issue")
    @patch("devloop.feedback.tb1_golden_path.poll_ready", return_value=[])
    def test_gates_pass_returns_success(
        self, mock_poll, mock_get, mock_claim, mock_setup, mock_persona, mock_overlay,
        mock_spawn, mock_gates, mock_hb_start, mock_hb_stop, mock_cleanup, mock_tracing,
        tmp_path,
    ):
        """run_tb1 returns success when all gates pass."""
        from devloop.feedback.tb1_golden_path import run_tb1
        from devloop.intake.beads_poller import WorkItem

        # Create a real temp worktree dir so CLAUDE.md can be written
        worktree_dir = tmp_path / "TEST-001"
        worktree_dir.mkdir()

        mock_get.return_value = WorkItem(
            id="TEST-001", title="Fix bug", type="bug", priority=1,
            labels=["bug", "repo:prompt-bench"], description="Fix it",
        )
        mock_setup.return_value = {
            "success": True, "worktree_path": str(worktree_dir),
            "branch_name": "dl/TEST-001",
        }
        mock_persona.return_value = {
            "name": "bug-fix", "model": "sonnet", "max_turns_default": 10,
            "retry_max": 2, "cost_ceiling_default": 1.0,
            "claude_md_overlay": "Fix bugs",
        }
        mock_overlay.return_value = {"overlay_text": "# Instructions\nFix the bug"}
        mock_spawn.return_value = {
            "exit_code": 0, "stdout": "", "num_turns": 3,
            "input_tokens": 100, "output_tokens": 50,
        }
        mock_gates.return_value = {
            "overall_passed": True, "first_failure": None,
            "gate_results": [], "total_duration_seconds": 0.5,
        }

        result = run_tb1("TEST-001", "/tmp/prompt-bench")

        assert result["success"] is True

    @patch("devloop.feedback.tb1_golden_path.init_tracing", return_value=MagicMock())
    @patch("devloop.feedback.tb1_golden_path.cleanup_worktree")
    @patch("devloop.feedback.tb1_golden_path.stop_heartbeat")
    @patch("devloop.feedback.tb1_golden_path.start_heartbeat", return_value=(MagicMock(), MagicMock()))
    @patch("devloop.feedback.tb1_golden_path.run_all_gates")
    @patch("devloop.feedback.tb1_golden_path.spawn_agent")
    @patch("devloop.feedback.tb1_golden_path.build_claude_md_overlay")
    @patch("devloop.feedback.tb1_golden_path.select_persona")
    @patch("devloop.feedback.tb1_golden_path.setup_worktree")
    @patch("devloop.feedback.tb1_golden_path.claim_issue", return_value=True)
    @patch("devloop.feedback.tb1_golden_path.get_issue")
    @patch("devloop.feedback.tb1_golden_path.poll_ready", return_value=[])
    def test_setup_worktree_failure_returns_error(
        self, mock_poll, mock_get, mock_claim, mock_setup, mock_persona, mock_overlay,
        mock_spawn, mock_gates, mock_hb_start, mock_hb_stop, mock_cleanup, mock_tracing,
    ):
        """run_tb1 returns error when worktree setup fails."""
        from devloop.feedback.tb1_golden_path import run_tb1
        from devloop.intake.beads_poller import WorkItem

        mock_get.return_value = WorkItem(
            id="TEST-002", title="Fix bug", type="bug", priority=1,
            labels=["bug"], description="Fix it",
        )
        mock_setup.return_value = {
            "success": False, "worktree_path": "",
            "message": "Not a git repository",
        }

        result = run_tb1("TEST-002", "/tmp/prompt-bench")

        assert result["success"] is False
        mock_spawn.assert_not_called()  # Agent never spawned


# ---------------------------------------------------------------------------
# TB-1 -> TB-5 cascade integration tests
# ---------------------------------------------------------------------------


class TestTB1CascadeIntegration:
    """Tests for TB-1 -> TB-5 cascade wiring after PR creation."""

    @patch("devloop.feedback.tb1_golden_path.init_tracing", return_value=MagicMock())
    @patch("devloop.feedback.tb1_golden_path.cleanup_worktree")
    @patch("devloop.feedback.tb1_golden_path.stop_heartbeat")
    @patch("devloop.feedback.tb1_golden_path.start_heartbeat", return_value=(MagicMock(), MagicMock()))
    @patch("devloop.feedback.tb1_golden_path.create_pull_request")
    @patch("devloop.feedback.tb1_golden_path.run_all_gates")
    @patch("devloop.feedback.tb1_golden_path.spawn_agent")
    @patch("devloop.feedback.tb1_golden_path.build_claude_md_overlay")
    @patch("devloop.feedback.tb1_golden_path.select_persona")
    @patch("devloop.feedback.tb1_golden_path.setup_worktree")
    @patch("devloop.feedback.tb1_golden_path.claim_issue", return_value=True)
    @patch("devloop.feedback.tb1_golden_path.get_issue")
    @patch("devloop.feedback.tb1_golden_path.poll_ready", return_value=[])
    @patch("devloop.feedback.tb1_golden_path.run_tb5")
    @patch("devloop.feedback.tb1_golden_path.find_cascade_targets")
    def test_cascade_triggered_after_pr(
        self, mock_find, mock_tb5, mock_poll, mock_get, mock_claim,
        mock_setup, mock_persona, mock_overlay, mock_spawn, mock_gates,
        mock_pr, mock_hb_start, mock_hb_stop, mock_cleanup, mock_tracing,
        tmp_path,
    ):
        """When gates pass and PR is created, cascade targets are checked."""
        from devloop.feedback.tb1_golden_path import run_tb1
        from devloop.intake.beads_poller import WorkItem

        worktree_dir = tmp_path / "TEST-CASCADE"
        worktree_dir.mkdir()

        mock_get.return_value = WorkItem(
            id="TEST-CASCADE", title="Update API", type="feature",
            priority=1, labels=["feature"], description="Change API",
        )
        mock_setup.return_value = {
            "success": True, "worktree_path": str(worktree_dir),
            "branch_name": "dl/TEST-CASCADE",
        }
        mock_persona.return_value = {
            "name": "feature", "model": "sonnet", "retry_max": 2,
            "max_context_pct": 75,
        }
        mock_overlay.return_value = {"overlay_text": "# Instructions"}
        mock_spawn.return_value = {
            "exit_code": 0, "stdout": "", "num_turns": 3,
            "input_tokens": 100, "output_tokens": 50,
        }
        mock_gates.return_value = {
            "overall_passed": True, "first_failure": None,
            "gate_results": [], "total_duration_seconds": 0.5,
        }
        mock_pr.return_value = {"success": True, "pr_url": "https://github.com/test/pr/1"}
        mock_find.return_value = [{
            "target_repo_name": "omniswipe-backend",
            "target_repo_path": "/home/user/omniswipe-backend",
            "matched_watches": ["src/api/**"],
            "dependency_type": "api-contract",
        }]
        mock_tb5.return_value = {"success": True, "cascade_skipped": False}

        result = run_tb1("TEST-CASCADE", "/tmp/prompt-bench")

        assert result["success"] is True
        mock_find.assert_called_once()
        mock_tb5.assert_called_once()
        assert len(result.get("cascade_results", [])) == 1

    @patch("devloop.feedback.tb1_golden_path.init_tracing", return_value=MagicMock())
    @patch("devloop.feedback.tb1_golden_path.cleanup_worktree")
    @patch("devloop.feedback.tb1_golden_path.stop_heartbeat")
    @patch("devloop.feedback.tb1_golden_path.start_heartbeat", return_value=(MagicMock(), MagicMock()))
    @patch("devloop.feedback.tb1_golden_path.create_pull_request")
    @patch("devloop.feedback.tb1_golden_path.run_all_gates")
    @patch("devloop.feedback.tb1_golden_path.spawn_agent")
    @patch("devloop.feedback.tb1_golden_path.build_claude_md_overlay")
    @patch("devloop.feedback.tb1_golden_path.select_persona")
    @patch("devloop.feedback.tb1_golden_path.setup_worktree")
    @patch("devloop.feedback.tb1_golden_path.claim_issue", return_value=True)
    @patch("devloop.feedback.tb1_golden_path.get_issue")
    @patch("devloop.feedback.tb1_golden_path.poll_ready", return_value=[])
    @patch("devloop.feedback.tb1_golden_path.find_cascade_targets")
    def test_cascade_failure_does_not_break_tb1(
        self, mock_find, mock_poll, mock_get, mock_claim,
        mock_setup, mock_persona, mock_overlay, mock_spawn, mock_gates,
        mock_pr, mock_hb_start, mock_hb_stop, mock_cleanup, mock_tracing,
        tmp_path,
    ):
        """If cascade check raises, TB-1 still returns success."""
        from devloop.feedback.tb1_golden_path import run_tb1
        from devloop.intake.beads_poller import WorkItem

        worktree_dir = tmp_path / "TEST-CASCADE-FAIL"
        worktree_dir.mkdir()

        mock_get.return_value = WorkItem(
            id="TEST-CASCADE-FAIL", title="Fix", type="bug",
            priority=1, labels=["bug"], description="Fix it",
        )
        mock_setup.return_value = {
            "success": True, "worktree_path": str(worktree_dir),
            "branch_name": "dl/TEST-CASCADE-FAIL",
        }
        mock_persona.return_value = {
            "name": "bug-fix", "model": "sonnet", "retry_max": 2,
            "max_context_pct": 75,
        }
        mock_overlay.return_value = {"overlay_text": "# Instructions"}
        mock_spawn.return_value = {
            "exit_code": 0, "stdout": "", "num_turns": 3,
            "input_tokens": 100, "output_tokens": 50,
        }
        mock_gates.return_value = {
            "overall_passed": True, "first_failure": None,
            "gate_results": [], "total_duration_seconds": 0.5,
        }
        mock_pr.return_value = {"success": True, "pr_url": "https://github.com/test/pr/1"}
        mock_find.side_effect = RuntimeError("cascade exploded")

        result = run_tb1("TEST-CASCADE-FAIL", "/tmp/prompt-bench")

        assert result["success"] is True  # TB-1 still succeeded


# ---------------------------------------------------------------------------
# TB-1 -> TB-6 session capture integration tests
# ---------------------------------------------------------------------------


class TestTB1SessionCapture:
    """Tests for TB-1 -> TB-6 session capture wiring."""

    @patch("devloop.feedback.tb1_golden_path.init_tracing", return_value=MagicMock())
    @patch("devloop.feedback.tb1_golden_path.cleanup_worktree")
    @patch("devloop.feedback.tb1_golden_path.stop_heartbeat")
    @patch("devloop.feedback.tb1_golden_path.start_heartbeat", return_value=(MagicMock(), MagicMock()))
    @patch("devloop.feedback.tb1_golden_path.create_pull_request")
    @patch("devloop.feedback.tb1_golden_path.run_all_gates")
    @patch("devloop.feedback.tb1_golden_path.spawn_agent")
    @patch("devloop.feedback.tb1_golden_path.build_claude_md_overlay")
    @patch("devloop.feedback.tb1_golden_path.select_persona")
    @patch("devloop.feedback.tb1_golden_path.setup_worktree")
    @patch("devloop.feedback.tb1_golden_path.claim_issue", return_value=True)
    @patch("devloop.feedback.tb1_golden_path.get_issue")
    @patch("devloop.feedback.tb1_golden_path.poll_ready", return_value=[])
    @patch("devloop.feedback.tb1_golden_path.find_cascade_targets", return_value=[])
    @patch("devloop.feedback.tb1_golden_path._save_session")
    @patch("devloop.feedback.tb1_golden_path._generate_session_id", return_value="TEST-SESSION-123")
    def test_session_saved_on_success(
        self, mock_gen_id, mock_save, mock_find, mock_poll, mock_get,
        mock_claim, mock_setup, mock_persona, mock_overlay, mock_spawn,
        mock_gates, mock_pr, mock_hb_start, mock_hb_stop, mock_cleanup,
        mock_tracing, tmp_path,
    ):
        """Agent stdout is saved as a session when non-empty."""
        from devloop.feedback.tb1_golden_path import run_tb1
        from devloop.intake.beads_poller import WorkItem

        worktree_dir = tmp_path / "TEST-SESSION"
        worktree_dir.mkdir()

        mock_get.return_value = WorkItem(
            id="TEST-SESSION", title="Fix", type="bug",
            priority=1, labels=["bug"], description="Fix",
        )
        mock_setup.return_value = {
            "success": True, "worktree_path": str(worktree_dir),
            "branch_name": "dl/TEST-SESSION",
        }
        mock_persona.return_value = {
            "name": "bug-fix", "model": "sonnet", "retry_max": 2,
            "max_context_pct": 75,
        }
        mock_overlay.return_value = {"overlay_text": "# Instructions"}
        mock_spawn.return_value = {
            "exit_code": 0,
            "stdout": '{"type": "result", "num_turns": 3}\n',
            "num_turns": 3, "input_tokens": 100, "output_tokens": 50,
        }
        mock_gates.return_value = {
            "overall_passed": True, "first_failure": None,
            "gate_results": [], "total_duration_seconds": 0.5,
        }
        mock_pr.return_value = {"success": True}
        mock_save.return_value = "/tmp/sessions/TEST-SESSION-123.ndjson"

        result = run_tb1("TEST-SESSION", "/tmp/prompt-bench")

        assert result["success"] is True
        assert result.get("session_id") == "TEST-SESSION-123"
        mock_save.assert_called_once()

    @patch("devloop.feedback.tb1_golden_path.init_tracing", return_value=MagicMock())
    @patch("devloop.feedback.tb1_golden_path.cleanup_worktree")
    @patch("devloop.feedback.tb1_golden_path.stop_heartbeat")
    @patch("devloop.feedback.tb1_golden_path.start_heartbeat", return_value=(MagicMock(), MagicMock()))
    @patch("devloop.feedback.tb1_golden_path.create_pull_request")
    @patch("devloop.feedback.tb1_golden_path.run_all_gates")
    @patch("devloop.feedback.tb1_golden_path.spawn_agent")
    @patch("devloop.feedback.tb1_golden_path.build_claude_md_overlay")
    @patch("devloop.feedback.tb1_golden_path.select_persona")
    @patch("devloop.feedback.tb1_golden_path.setup_worktree")
    @patch("devloop.feedback.tb1_golden_path.claim_issue", return_value=True)
    @patch("devloop.feedback.tb1_golden_path.get_issue")
    @patch("devloop.feedback.tb1_golden_path.poll_ready", return_value=[])
    @patch("devloop.feedback.tb1_golden_path.find_cascade_targets", return_value=[])
    @patch("devloop.feedback.tb1_golden_path._save_session")
    @patch("devloop.feedback.tb1_golden_path._generate_session_id", return_value="TEST-FAIL-123")
    def test_session_capture_failure_does_not_break_tb1(
        self, mock_gen_id, mock_save, mock_find, mock_poll, mock_get,
        mock_claim, mock_setup, mock_persona, mock_overlay, mock_spawn,
        mock_gates, mock_pr, mock_hb_start, mock_hb_stop, mock_cleanup,
        mock_tracing, tmp_path,
    ):
        """If session capture raises, TB-1 still succeeds."""
        from devloop.feedback.tb1_golden_path import run_tb1
        from devloop.intake.beads_poller import WorkItem

        worktree_dir = tmp_path / "TEST-FAIL"
        worktree_dir.mkdir()

        mock_get.return_value = WorkItem(
            id="TEST-FAIL", title="Fix", type="bug",
            priority=1, labels=["bug"], description="Fix",
        )
        mock_setup.return_value = {
            "success": True, "worktree_path": str(worktree_dir),
            "branch_name": "dl/TEST-FAIL",
        }
        mock_persona.return_value = {
            "name": "bug-fix", "model": "sonnet", "retry_max": 2,
            "max_context_pct": 75,
        }
        mock_overlay.return_value = {"overlay_text": "# Instructions"}
        mock_spawn.return_value = {
            "exit_code": 0,
            "stdout": '{"type": "result"}\n',
            "num_turns": 3, "input_tokens": 100, "output_tokens": 50,
        }
        mock_gates.return_value = {
            "overall_passed": True, "first_failure": None,
            "gate_results": [], "total_duration_seconds": 0.5,
        }
        mock_pr.return_value = {"success": True}
        mock_save.side_effect = OSError("disk full")

        result = run_tb1("TEST-FAIL", "/tmp/prompt-bench")

        assert result["success"] is True  # TB-1 still succeeded
