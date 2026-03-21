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
