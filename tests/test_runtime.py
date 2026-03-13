"""Tests for devloop.runtime.server — subprocess calls are mocked."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from devloop.runtime.server import _run_agent, spawn_agent
from devloop.runtime.types import AgentConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def config():
    return AgentConfig(
        worktree_path="/tmp/test-worktree",
        task_prompt="Fix the bug",
        model="sonnet",
        timeout_seconds=10.0,
    )


# ---------------------------------------------------------------------------
# _run_agent tests
# ---------------------------------------------------------------------------


class TestRunAgent:
    """Tests for _run_agent() — Popen-based subprocess management."""

    @patch("devloop.runtime.server.subprocess.Popen")
    @patch("devloop.runtime.server._find_claude_cli", return_value="/usr/bin/claude")
    def test_success(self, _mock_cli, mock_popen, config):
        """Successful agent run returns stdout, exit code 0, and PID."""
        proc = MagicMock()
        proc.communicate.return_value = ("agent output", "")
        proc.returncode = 0
        proc.pid = 12345
        mock_popen.return_value = proc

        result = _run_agent(config)

        assert result.exit_code == 0
        assert result.stdout == "agent output"
        assert result.pid == 12345
        assert result.timed_out is False

    @patch("devloop.runtime.server.subprocess.Popen")
    @patch("devloop.runtime.server._find_claude_cli", return_value="/usr/bin/claude")
    def test_nonzero_exit(self, _mock_cli, mock_popen, config):
        """Non-zero exit code is propagated."""
        proc = MagicMock()
        proc.communicate.return_value = ("", "error output")
        proc.returncode = 1
        proc.pid = 12345
        mock_popen.return_value = proc

        result = _run_agent(config)

        assert result.exit_code == 1
        assert result.stderr == "error output"
        assert result.timed_out is False

    @patch("devloop.runtime.server.subprocess.Popen")
    @patch("devloop.runtime.server._find_claude_cli", return_value="/usr/bin/claude")
    def test_timeout_kills_process(self, _mock_cli, mock_popen, config):
        """On timeout, process is killed and result has timed_out=True."""
        proc = MagicMock()
        proc.pid = 99999
        # First communicate() raises timeout, second (after kill) returns output
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd=["claude"], timeout=10),
            ("partial output", "timeout stderr"),
        ]
        mock_popen.return_value = proc

        result = _run_agent(config)

        # Verify proc.kill() was called
        proc.kill.assert_called_once()
        # Verify we reaped the zombie via second communicate()
        assert proc.communicate.call_count == 2
        assert result.exit_code == -1
        assert result.timed_out is True
        assert result.pid == 99999
        assert result.stdout == "partial output"

    @patch("devloop.runtime.server.subprocess.Popen")
    @patch("devloop.runtime.server._find_claude_cli", return_value="/usr/bin/claude")
    def test_env_claudecode_unset(self, _mock_cli, mock_popen, config):
        """CLAUDECODE env var is removed from subprocess environment."""
        proc = MagicMock()
        proc.communicate.return_value = ("", "")
        proc.returncode = 0
        proc.pid = 1
        mock_popen.return_value = proc

        import os
        with patch.dict(os.environ, {"CLAUDECODE": "1"}):
            _run_agent(config)

        # Check the env passed to Popen doesn't have CLAUDECODE
        call_kwargs = mock_popen.call_args[1]
        assert "CLAUDECODE" not in call_kwargs["env"]

    @patch("devloop.runtime.server._find_claude_cli")
    def test_claude_not_found(self, mock_cli, config):
        """FileNotFoundError raised when claude CLI not on PATH."""
        mock_cli.side_effect = FileNotFoundError("claude CLI not found")

        with pytest.raises(FileNotFoundError):
            _run_agent(config)


# ---------------------------------------------------------------------------
# spawn_agent integration tests (with mocked _run_agent)
# ---------------------------------------------------------------------------


class TestSpawnAgent:
    """Tests for spawn_agent() tool — validates path checking and error handling."""

    @patch("devloop.runtime.server._run_agent")
    def test_invalid_worktree_path(self, mock_run):
        """spawn_agent returns error when worktree path doesn't exist."""
        result = spawn_agent(
            worktree_path="/nonexistent/path",
            task_prompt="test",
        )

        assert result["exit_code"] == -1
        assert "does not exist" in result["stderr"]
        mock_run.assert_not_called()
