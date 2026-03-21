"""Tests for TB-4: Runaway-to-Stop — turn limits, usage parsing, escalation with usage table."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from devloop.feedback.tb4_runaway import (
    _build_context_restart_prompt,
    _clear_handoff,
    _read_handoff,
    HANDOFF_DIR,
)
from devloop.feedback.types import TB4Result, UsageBreakdown
from devloop.orchestration.types import PersonaConfig
from devloop.runtime.server import _estimate_context_pct, _parse_usage_from_output
from devloop.runtime.types import AgentConfig, AgentResult


# ---------------------------------------------------------------------------
# Type tests
# ---------------------------------------------------------------------------


class TestTB4Types:
    """Tests for TB-4 Pydantic models."""

    def test_usage_breakdown_defaults(self):
        """UsageBreakdown defaults to zero for all counters."""
        ub = UsageBreakdown(attempt=0)
        assert ub.num_turns == 0
        assert ub.input_tokens == 0
        assert ub.output_tokens == 0
        assert ub.cumulative_turns == 0

    def test_usage_breakdown_with_values(self):
        """UsageBreakdown stores all provided values."""
        ub = UsageBreakdown(
            attempt=1,
            num_turns=5,
            input_tokens=1000,
            output_tokens=500,
            cumulative_turns=8,
        )
        assert ub.attempt == 1
        assert ub.num_turns == 5
        assert ub.cumulative_turns == 8

    def test_tb4_result_defaults(self):
        """TB4Result has sensible defaults."""
        r = TB4Result(
            issue_id="dl-test",
            repo_path="/tmp/repo",
            success=False,
            phase="claim",
        )
        assert r.turns_used_total == 0
        assert r.max_turns_total == 0
        assert r.usage_breakdown == []
        assert r.attempt_span_ids == []
        assert r.trace_id is None
        assert r.escalated is False

    def test_tb4_result_with_usage(self):
        """TB4Result stores usage breakdown correctly."""
        breakdown = [
            UsageBreakdown(attempt=0, num_turns=3, input_tokens=500, output_tokens=200, cumulative_turns=3),
            UsageBreakdown(attempt=1, num_turns=2, input_tokens=300, output_tokens=100, cumulative_turns=5),
        ]
        r = TB4Result(
            issue_id="dl-test",
            repo_path="/tmp/repo",
            success=False,
            phase="escalated",
            turns_used_total=5,
            max_turns_total=5,
            usage_breakdown=breakdown,
            escalated=True,
        )
        assert r.turns_used_total == 5
        assert len(r.usage_breakdown) == 2
        assert r.usage_breakdown[1].cumulative_turns == 5

    def test_tb4_result_serializes(self):
        """TB4Result.model_dump() includes all TB-4 specific fields."""
        r = TB4Result(
            issue_id="dl-test",
            repo_path="/tmp/repo",
            success=True,
            phase="gates_passed",
            turns_used_total=3,
            max_turns_total=10,
        )
        d = r.model_dump()
        assert d["turns_used_total"] == 3
        assert d["max_turns_total"] == 10
        assert "usage_breakdown" in d


# ---------------------------------------------------------------------------
# AgentConfig.max_turns tests
# ---------------------------------------------------------------------------


class TestAgentConfigMaxTurns:
    """Tests for AgentConfig.max_turns field."""

    def test_max_turns_default_none(self):
        """max_turns defaults to None."""
        config = AgentConfig(
            worktree_path="/tmp/wt",
            task_prompt="test",
        )
        assert config.max_turns is None

    def test_max_turns_set(self):
        """max_turns can be set to an integer."""
        config = AgentConfig(
            worktree_path="/tmp/wt",
            task_prompt="test",
            max_turns=5,
        )
        assert config.max_turns == 5


# ---------------------------------------------------------------------------
# AgentResult usage fields tests
# ---------------------------------------------------------------------------


class TestAgentResultUsageFields:
    """Tests for AgentResult.num_turns, input_tokens, output_tokens."""

    def test_usage_defaults_zero(self):
        """Usage fields default to 0."""
        r = AgentResult(exit_code=0)
        assert r.num_turns == 0
        assert r.input_tokens == 0
        assert r.output_tokens == 0

    def test_usage_populated(self):
        """Usage fields can be set."""
        r = AgentResult(
            exit_code=0,
            num_turns=7,
            input_tokens=1234,
            output_tokens=567,
        )
        assert r.num_turns == 7
        assert r.input_tokens == 1234
        assert r.output_tokens == 567


# ---------------------------------------------------------------------------
# PersonaConfig.max_turns_default tests
# ---------------------------------------------------------------------------


class TestPersonaConfigMaxTurns:
    """Tests for PersonaConfig.max_turns_default field."""

    def test_max_turns_default(self):
        """max_turns_default defaults to 15."""
        p = PersonaConfig(name="test")
        assert p.max_turns_default == 15

    def test_max_turns_override(self):
        """max_turns_default can be overridden."""
        p = PersonaConfig(name="test", max_turns_default=25)
        assert p.max_turns_default == 25


# ---------------------------------------------------------------------------
# Escalation with usage_breakdown tests
# ---------------------------------------------------------------------------


class TestEscalationUsageTable:
    """Tests for escalate_to_human with usage_breakdown parameter."""

    @patch("devloop.feedback.server._run_br")
    def test_usage_table_in_comment(self, mock_br):
        """Usage breakdown table is included in escalation comment."""
        from devloop.feedback.server import escalate_to_human

        mock_br.return_value.returncode = 0
        mock_br.return_value.stderr = ""

        usage = [
            {"attempt": 0, "num_turns": 3, "input_tokens": 500, "output_tokens": 200, "cumulative_turns": 3},
            {"attempt": 1, "num_turns": 2, "input_tokens": 300, "output_tokens": 100, "cumulative_turns": 5},
        ]

        result = escalate_to_human(
            issue_id="dl-test",
            gate_failures=[],
            attempts=2,
            usage_breakdown=usage,
        )

        assert result["success"] is True

        # Check that the comment contains the usage table
        comment_call = mock_br.call_args_list[-1]
        comment_text = comment_call[0][-1]  # last positional arg to _run_br
        assert "Usage Breakdown" in comment_text
        assert "Turns" in comment_text
        assert "Input Tokens" in comment_text

    @patch("devloop.feedback.server._run_br")
    def test_no_usage_table_without_breakdown(self, mock_br):
        """No usage table when usage_breakdown is None."""
        from devloop.feedback.server import escalate_to_human

        mock_br.return_value.returncode = 0
        mock_br.return_value.stderr = ""

        result = escalate_to_human(
            issue_id="dl-test",
            gate_failures=[],
            attempts=1,
        )

        assert result["success"] is True

        # Check that the comment does NOT contain usage table
        comment_call = mock_br.call_args_list[-1]
        comment_text = comment_call[0][-1]
        assert "Usage Breakdown" not in comment_text


# ---------------------------------------------------------------------------
# Context percentage estimation tests
# ---------------------------------------------------------------------------


class TestContextPctEstimation:
    """Tests for _estimate_context_pct and _parse_usage_from_output context tracking."""

    def test_estimate_from_peak_input(self):
        """Peak input tokens produce correct percentage."""
        # 100k tokens out of 200k context = 50%
        pct = _estimate_context_pct(
            peak_input_tokens=100_000,
            total_input_tokens=500_000,
            num_turns=10,
            model="sonnet",
        )
        assert pct == 50.0

    def test_estimate_from_peak_zero_falls_back(self):
        """Falls back to heuristic when peak_input_tokens is 0."""
        pct = _estimate_context_pct(
            peak_input_tokens=0,
            total_input_tokens=200_000,
            num_turns=10,
            model="sonnet",
        )
        # Heuristic: 2 * 200000 / (10+1) / 200000 * 100 ≈ 18.2%
        assert 15.0 < pct < 25.0

    def test_estimate_high_context(self):
        """High peak input produces percentage above threshold."""
        pct = _estimate_context_pct(
            peak_input_tokens=160_000,
            total_input_tokens=800_000,
            num_turns=5,
            model="opus",
        )
        assert pct == 80.0

    def test_estimate_zero_turns(self):
        """Zero turns returns 0%."""
        pct = _estimate_context_pct(
            peak_input_tokens=0,
            total_input_tokens=0,
            num_turns=0,
            model="sonnet",
        )
        assert pct == 0.0

    def test_parse_output_includes_peak_input(self):
        """_parse_usage_from_output extracts peak_input_tokens."""
        output = '\n'.join([
            '{"type":"assistant","message":{"usage":{"input_tokens":50000,"output_tokens":1000}}}',
            '{"type":"assistant","message":{"usage":{"input_tokens":120000,"output_tokens":2000}}}',
            '{"type":"result","num_turns":2,"usage":{"input_tokens":170000,"output_tokens":3000}}',
        ])
        result = _parse_usage_from_output(output)
        assert result["peak_input_tokens"] == 120000
        assert result["num_turns"] == 2
        assert result["input_tokens"] == 170000

    def test_parse_output_no_assistant_messages(self):
        """peak_input_tokens defaults to 0 when no assistant messages."""
        output = '{"type":"result","num_turns":1,"usage":{"input_tokens":5000,"output_tokens":1000}}'
        result = _parse_usage_from_output(output)
        assert result["peak_input_tokens"] == 0


# ---------------------------------------------------------------------------
# AgentResult context fields tests
# ---------------------------------------------------------------------------


class TestAgentResultContextFields:
    """Tests for AgentResult.context_pct and context_limited."""

    def test_context_defaults(self):
        """Context fields default to 0 / False."""
        r = AgentResult(exit_code=0)
        assert r.context_pct == 0.0
        assert r.context_limited is False

    def test_context_limited_flag(self):
        """context_limited can be set."""
        r = AgentResult(
            exit_code=0,
            context_pct=82.5,
            context_limited=True,
        )
        assert r.context_pct == 82.5
        assert r.context_limited is True


# ---------------------------------------------------------------------------
# PersonaConfig.max_context_pct tests
# ---------------------------------------------------------------------------


class TestPersonaConfigMaxContextPct:
    """Tests for PersonaConfig.max_context_pct field."""

    def test_max_context_pct_default(self):
        """max_context_pct defaults to 75."""
        p = PersonaConfig(name="test")
        assert p.max_context_pct == 75

    def test_max_context_pct_override(self):
        """max_context_pct can be overridden."""
        p = PersonaConfig(name="test", max_context_pct=60)
        assert p.max_context_pct == 60


# ---------------------------------------------------------------------------
# TB4Result context_restarts field tests
# ---------------------------------------------------------------------------


class TestTB4ResultContextRestarts:
    """Tests for TB4Result.context_restarts field."""

    def test_context_restarts_default(self):
        """context_restarts defaults to 0."""
        r = TB4Result(
            issue_id="dl-test",
            repo_path="/tmp/repo",
            success=True,
            phase="gates_passed",
        )
        assert r.context_restarts == 0

    def test_context_restarts_set(self):
        """context_restarts can be set."""
        r = TB4Result(
            issue_id="dl-test",
            repo_path="/tmp/repo",
            success=True,
            phase="gates_passed",
            context_restarts=2,
        )
        assert r.context_restarts == 2

    def test_context_restarts_in_serialized(self):
        """context_restarts appears in model_dump()."""
        r = TB4Result(
            issue_id="dl-test",
            repo_path="/tmp/repo",
            success=True,
            phase="gates_passed",
            context_restarts=1,
        )
        d = r.model_dump()
        assert d["context_restarts"] == 1


# ---------------------------------------------------------------------------
# UsageBreakdown context fields tests
# ---------------------------------------------------------------------------


class TestUsageBreakdownContextFields:
    """Tests for UsageBreakdown context_pct_at_exit and context_restart fields."""

    def test_context_fields_defaults(self):
        """Context fields default to 0 / False."""
        ub = UsageBreakdown(attempt=0)
        assert ub.context_pct_at_exit == 0.0
        assert ub.context_restart is False

    def test_context_fields_set(self):
        """Context fields can be set."""
        ub = UsageBreakdown(
            attempt=1,
            context_pct_at_exit=78.3,
            context_restart=True,
        )
        assert ub.context_pct_at_exit == 78.3
        assert ub.context_restart is True


# ---------------------------------------------------------------------------
# Handoff file helpers tests
# ---------------------------------------------------------------------------


class TestHandoffHelpers:
    """Tests for _read_handoff, _clear_handoff, and _build_context_restart_prompt."""

    def test_read_handoff_exists(self, tmp_path):
        """_read_handoff returns content when file exists."""
        handoff = tmp_path / "test-issue.md"
        handoff.write_text("## Done\n- Fixed foo\n\n## Remaining\n- Fix bar\n")

        with patch("devloop.feedback.tb4_runaway.HANDOFF_DIR", tmp_path):
            note = _read_handoff("test-issue")
            assert note is not None
            assert "Fixed foo" in note
            assert "Fix bar" in note

    def test_read_handoff_missing(self, tmp_path):
        """_read_handoff returns None when file doesn't exist."""
        with patch("devloop.feedback.tb4_runaway.HANDOFF_DIR", tmp_path):
            assert _read_handoff("nonexistent") is None

    def test_read_handoff_empty(self, tmp_path):
        """_read_handoff returns None for empty files."""
        handoff = tmp_path / "empty.md"
        handoff.write_text("")

        with patch("devloop.feedback.tb4_runaway.HANDOFF_DIR", tmp_path):
            assert _read_handoff("empty") is None

    def test_clear_handoff(self, tmp_path):
        """_clear_handoff removes the handoff file."""
        handoff = tmp_path / "test-issue.md"
        handoff.write_text("some content")

        with patch("devloop.feedback.tb4_runaway.HANDOFF_DIR", tmp_path):
            _clear_handoff("test-issue")
            assert not handoff.exists()

    def test_clear_handoff_missing(self, tmp_path):
        """_clear_handoff is a no-op when file doesn't exist."""
        with patch("devloop.feedback.tb4_runaway.HANDOFF_DIR", tmp_path):
            _clear_handoff("nonexistent")  # should not raise

    def test_build_context_restart_prompt(self):
        """_build_context_restart_prompt includes handoff and task info."""
        prompt = _build_context_restart_prompt(
            issue_title="Fix login bug",
            issue_description="Users can't log in",
            handoff_note="## Done\n- Fixed auth check\n\n## Remaining\n- Add tests",
            overlay_text="",
        )
        assert "Context Restart" in prompt
        assert "Handoff Note from Previous Session" in prompt
        assert "Fixed auth check" in prompt
        assert "Add tests" in prompt
        assert "Fix login bug" in prompt
        assert "Continue from where the previous session left off" in prompt

    def test_build_context_restart_prompt_with_overlay(self):
        """_build_context_restart_prompt uses overlay_text when available."""
        prompt = _build_context_restart_prompt(
            issue_title="Fix bug",
            issue_description="desc",
            handoff_note="Done: X\nRemaining: Y",
            overlay_text="# Custom Overlay\nDo this specific thing.",
        )
        assert "Custom Overlay" in prompt
        assert "Done: X" in prompt
