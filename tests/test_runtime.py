"""Tests for devloop.runtime.server — subprocess calls are mocked."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from devloop.runtime.server import (
    _is_claude_process,
    _parse_usage_from_output,
    _run_agent,
    kill_agent,
    spawn_agent,
)
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


# ---------------------------------------------------------------------------
# _is_claude_process tests (L1 PID validation)
# ---------------------------------------------------------------------------


class TestIsClaudeProcess:
    """Tests for _is_claude_process() — /proc/{pid}/cmdline validation."""

    @patch("devloop.runtime.server.Path")
    def test_returns_true_for_claude(self, mock_path_cls):
        """Returns True when cmdline contains 'claude'."""
        mock_path_cls.return_value.read_bytes.return_value = (
            b"/usr/bin/claude\x00--print\x00--model\x00sonnet"
        )
        assert _is_claude_process(12345) is True

    @patch("devloop.runtime.server.Path")
    def test_returns_false_for_non_claude(self, mock_path_cls):
        """Returns False when cmdline doesn't contain 'claude'."""
        mock_path_cls.return_value.read_bytes.return_value = (
            b"/usr/bin/python\x00-m\x00flask"
        )
        assert _is_claude_process(12345) is False

    @patch("devloop.runtime.server.Path")
    def test_returns_false_on_no_process(self, mock_path_cls):
        """Returns False when /proc/{pid}/cmdline doesn't exist."""
        mock_path_cls.return_value.read_bytes.side_effect = FileNotFoundError
        assert _is_claude_process(99999) is False

    @patch("devloop.runtime.server.Path")
    def test_returns_false_on_permission_error(self, mock_path_cls):
        """Returns False when /proc/{pid}/cmdline is not readable."""
        mock_path_cls.return_value.read_bytes.side_effect = PermissionError
        assert _is_claude_process(99999) is False


# ---------------------------------------------------------------------------
# kill_agent tests (L1 PID validation)
# ---------------------------------------------------------------------------


class TestKillAgent:
    """Tests for kill_agent() — PID validation before SIGTERM."""

    @patch("devloop.runtime.server.os.kill")
    @patch("devloop.runtime.server._is_claude_process", return_value=True)
    def test_kills_claude_process(self, _mock_check, mock_kill):
        """Sends SIGTERM when PID is a claude process."""
        result = kill_agent(12345)
        assert result["success"] is True
        mock_kill.assert_called_once()

    @patch("devloop.runtime.server.os.kill")
    @patch("devloop.runtime.server._is_claude_process", return_value=False)
    def test_refuses_non_claude_process(self, _mock_check, mock_kill):
        """Refuses to kill non-claude processes."""
        result = kill_agent(12345)
        assert result["success"] is False
        assert "not a claude process" in result["message"]
        mock_kill.assert_not_called()


# ---------------------------------------------------------------------------
# _parse_usage_from_output tests (TB-4)
# ---------------------------------------------------------------------------


class TestParseUsageFromOutput:
    """Tests for _parse_usage_from_output() — NDJSON result parsing."""

    def test_parses_result_line(self):
        """Extracts num_turns and token usage from result NDJSON line."""
        stdout = (
            '{"type":"assistant","message":"working..."}\n'
            '{"type":"result","num_turns":7,"usage":{"input_tokens":1234,"output_tokens":567}}\n'
        )
        usage = _parse_usage_from_output(stdout)
        assert usage["num_turns"] == 7
        assert usage["input_tokens"] == 1234
        assert usage["output_tokens"] == 567

    def test_returns_zeros_on_empty(self):
        """Returns all zeros when stdout is empty."""
        usage = _parse_usage_from_output("")
        assert usage == {"num_turns": 0, "input_tokens": 0, "output_tokens": 0}

    def test_returns_zeros_on_no_result_line(self):
        """Returns zeros when no result-type line exists."""
        stdout = '{"type":"assistant","message":"hello"}\n'
        usage = _parse_usage_from_output(stdout)
        assert usage["num_turns"] == 0

    def test_handles_malformed_json(self):
        """Skips malformed JSON lines without crashing."""
        stdout = 'not json\n{"type":"result","num_turns":3,"usage":{"input_tokens":100,"output_tokens":50}}\n'
        usage = _parse_usage_from_output(stdout)
        assert usage["num_turns"] == 3
        assert usage["input_tokens"] == 100

    def test_handles_result_without_usage(self):
        """Returns zero tokens when result line has no usage field."""
        stdout = '{"type":"result","num_turns":5}\n'
        usage = _parse_usage_from_output(stdout)
        assert usage["num_turns"] == 5
        assert usage["input_tokens"] == 0
        assert usage["output_tokens"] == 0


# ---------------------------------------------------------------------------
# _build_command tests (TB-4: --output-format json, --max-turns)
# ---------------------------------------------------------------------------


class TestBuildCommand:
    """Tests for _build_command() — CLI flag construction."""

    @patch("devloop.runtime.server._find_claude_cli", return_value="/usr/bin/claude")
    def test_includes_output_format_json(self, _mock_cli):
        """--output-format json is always included."""
        from devloop.runtime.server import _build_command

        config = AgentConfig(
            worktree_path="/tmp/wt",
            task_prompt="test",
        )
        cmd = _build_command("/usr/bin/claude", config)
        assert "--output-format" in cmd
        idx = cmd.index("--output-format")
        assert cmd[idx + 1] == "json"

    @patch("devloop.runtime.server._find_claude_cli", return_value="/usr/bin/claude")
    def test_includes_max_turns_when_set(self, _mock_cli):
        """--max-turns N is included when config has max_turns."""
        from devloop.runtime.server import _build_command

        config = AgentConfig(
            worktree_path="/tmp/wt",
            task_prompt="test",
            max_turns=5,
        )
        cmd = _build_command("/usr/bin/claude", config)
        assert "--max-turns" in cmd
        idx = cmd.index("--max-turns")
        assert cmd[idx + 1] == "5"

    @patch("devloop.runtime.server._find_claude_cli", return_value="/usr/bin/claude")
    def test_no_max_turns_when_none(self, _mock_cli):
        """--max-turns is omitted when config.max_turns is None."""
        from devloop.runtime.server import _build_command

        config = AgentConfig(
            worktree_path="/tmp/wt",
            task_prompt="test",
            max_turns=None,
        )
        cmd = _build_command("/usr/bin/claude", config)
        assert "--max-turns" not in cmd


# ---------------------------------------------------------------------------
# _run_agent usage parsing integration tests (TB-4)
# ---------------------------------------------------------------------------


class TestRunAgentUsageParsing:
    """Tests for _run_agent() — usage fields populated from NDJSON output."""

    @patch("devloop.runtime.server.subprocess.Popen")
    @patch("devloop.runtime.server._find_claude_cli", return_value="/usr/bin/claude")
    def test_usage_fields_populated(self, _mock_cli, mock_popen):
        """num_turns, input_tokens, output_tokens parsed from stdout."""
        ndjson_output = '{"type":"result","num_turns":4,"usage":{"input_tokens":800,"output_tokens":200}}\n'
        proc = MagicMock()
        proc.communicate.return_value = (ndjson_output, "")
        proc.returncode = 0
        proc.pid = 1
        mock_popen.return_value = proc

        config = AgentConfig(
            worktree_path="/tmp/wt",
            task_prompt="test",
            max_turns=10,
        )
        result = _run_agent(config)

        assert result.num_turns == 4
        assert result.input_tokens == 800
        assert result.output_tokens == 200
