"""Tests for TB-4: Runaway-to-Stop — turn limits, usage parsing, escalation with usage table."""

from __future__ import annotations

from unittest.mock import patch

from devloop.feedback.types import TB4Result, UsageBreakdown
from devloop.orchestration.types import PersonaConfig
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
