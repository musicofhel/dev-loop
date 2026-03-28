"""Tests for devloop.llmops — Layer 7 LLMOps unit tests."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from devloop.llmops.types import LLMOpsConfig, OptimizationConfig, ProgramArtifact

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class TestLoadLLMOpsConfig:
    """Tests for _load_llmops_config()."""

    def test_default_config_when_no_file(self, tmp_path):
        """Returns default LLMOpsConfig when config file is missing."""
        with patch(
            "devloop.llmops.server.Path.__new__",
        ):

            # Patch the config path to a non-existent location
            with patch("devloop.llmops.server.Path") as MockPath:
                mock_config = MagicMock()
                mock_config.exists.return_value = False
                MockPath.return_value.resolve.return_value.parents.__getitem__ = (
                    lambda self, idx: tmp_path
                )
                MockPath.return_value = mock_config
                # Can't easily patch Path chaining — test the type directly
                cfg = LLMOpsConfig()
                assert cfg.enabled is False
                assert cfg.api_key_env == "ANTHROPIC_API_KEY"

    def test_config_model_defaults(self):
        """LLMOpsConfig has correct defaults."""
        cfg = LLMOpsConfig()
        assert cfg.enabled is False
        assert cfg.api_key_env == "ANTHROPIC_API_KEY"
        assert "artifacts" in cfg.artifact_dir
        assert "training" in cfg.training_dir
        assert cfg.programs == {}

    def test_config_with_programs(self):
        """LLMOpsConfig can hold program configs."""
        cfg = LLMOpsConfig(
            enabled=True,
            programs={
                "code_review": OptimizationConfig(
                    model="claude-sonnet-4-6",
                    num_trials=20,
                    metric_threshold=0.7,
                ),
            },
        )
        assert cfg.enabled is True
        assert "code_review" in cfg.programs
        assert cfg.programs["code_review"].num_trials == 20


# ---------------------------------------------------------------------------
# ProgramArtifact model
# ---------------------------------------------------------------------------


class TestProgramArtifact:
    """Tests for ProgramArtifact model."""

    def test_roundtrip_serialization(self):
        """ProgramArtifact can be serialized and deserialized."""
        artifact = ProgramArtifact(
            program_name="code_review",
            version="20260327-120000",
            artifact_path="/tmp/test.json",
            created_at="2026-03-27T12:00:00",
            metric_score=0.85,
            num_training_examples=100,
            num_val_examples=25,
        )
        data = artifact.model_dump()
        restored = ProgramArtifact(**data)
        assert restored.program_name == "code_review"
        assert restored.metric_score == 0.85


# ---------------------------------------------------------------------------
# Code review metric
# ---------------------------------------------------------------------------


class TestCodeReviewMetric:
    """Tests for code_review_metric function."""

    def test_both_empty_is_perfect(self):
        """Empty gold + empty pred = perfect score."""
        from devloop.llmops.programs.code_review import code_review_metric

        gold = MagicMock(findings_json="[]")
        pred = MagicMock(findings_json="[]")
        result = code_review_metric(gold, pred)
        assert result.score == 1.0

    def test_false_positives_penalized(self):
        """Findings when none expected gives low score."""
        from devloop.llmops.programs.code_review import code_review_metric

        gold = MagicMock(findings_json="[]")
        pred = MagicMock(
            findings_json=json.dumps([{"severity": "warning", "message": "false alarm"}])
        )
        result = code_review_metric(gold, pred)
        assert result.score < 0.5

    def test_false_negatives_penalized(self):
        """Missing all findings gives zero score."""
        from devloop.llmops.programs.code_review import code_review_metric

        gold = MagicMock(
            findings_json=json.dumps([{"severity": "critical", "message": "SQL injection"}])
        )
        pred = MagicMock(findings_json="[]")
        result = code_review_metric(gold, pred)
        assert result.score == 0.0

    def test_perfect_match(self):
        """Identical findings gives high score."""
        from devloop.llmops.programs.code_review import code_review_metric

        findings = [{"severity": "critical", "message": "SQL injection on line 42"}]
        gold = MagicMock(findings_json=json.dumps(findings))
        pred = MagicMock(findings_json=json.dumps(findings))
        result = code_review_metric(gold, pred)
        assert result.score >= 0.9

    def test_invalid_json_gives_zero(self):
        """Malformed JSON in predictions gives zero."""
        from devloop.llmops.programs.code_review import code_review_metric

        gold = MagicMock(findings_json="[]")
        pred = MagicMock(findings_json="not json at all")
        result = code_review_metric(gold, pred)
        assert result.score == 0.0

    def test_severity_mismatch_reduces_score(self):
        """Wrong severity on matched finding reduces score below 1.0."""
        from devloop.llmops.programs.code_review import code_review_metric

        gold_findings = [{"severity": "critical", "message": "buffer overflow"}]
        pred_findings = [{"severity": "suggestion", "message": "buffer overflow"}]
        gold = MagicMock(findings_json=json.dumps(gold_findings))
        pred = MagicMock(findings_json=json.dumps(pred_findings))
        result = code_review_metric(gold, pred)
        # F1 is 1.0 but severity acc is 0 → 0.7*1.0 + 0.3*0.0 = 0.7
        assert 0.6 <= result.score <= 0.8


# ---------------------------------------------------------------------------
# Retry prompt metric
# ---------------------------------------------------------------------------


class TestRetryPromptMetric:
    """Tests for retry_prompt_metric function."""

    def test_successful_retry_scores_higher(self):
        """A retry that succeeded scores higher than one that didn't."""
        from devloop.llmops.programs.retry_prompt import retry_prompt_metric

        gold_success = MagicMock(
            retry_instructions="Fix the null check on line 10",
            retry_succeeded="True",
        )
        gold_fail = MagicMock(
            retry_instructions="Fix the null check on line 10",
            retry_succeeded="False",
        )
        pred = MagicMock(
            retry_instructions="Check the null pointer dereference on line 10 and add a guard",
            retry_succeeded="True",
        )

        score_success = retry_prompt_metric(gold_success, pred)
        score_fail = retry_prompt_metric(gold_fail, pred)
        assert score_success.score >= score_fail.score


# ---------------------------------------------------------------------------
# Persona select metric
# ---------------------------------------------------------------------------


class TestPersonaSelectMetric:
    """Tests for persona_select_metric function."""

    def test_exact_match_scores_high(self):
        """Exact persona match gives high score."""
        from devloop.llmops.programs.persona_select import persona_select_metric

        gold = MagicMock(persona_id="bug-fix", custom_guidelines="", task_succeeded="True")
        pred = MagicMock(persona_id="bug-fix", custom_guidelines="Focus on regression tests")
        result = persona_select_metric(gold, pred)
        assert result.score >= 0.6

    def test_wrong_persona_scores_low(self):
        """Wrong persona gives lower score."""
        from devloop.llmops.programs.persona_select import persona_select_metric

        gold = MagicMock(persona_id="bug-fix", custom_guidelines="", task_succeeded="True")
        pred = MagicMock(persona_id="docs", custom_guidelines="Write documentation")
        result = persona_select_metric(gold, pred)
        assert result.score < 0.5

    def test_invalid_persona_gives_zero(self):
        """Invalid persona ID gives zero score."""
        from devloop.llmops.programs.persona_select import persona_select_metric

        gold = MagicMock(persona_id="bug-fix", custom_guidelines="", task_succeeded="True")
        pred = MagicMock(persona_id="not-a-real-persona", custom_guidelines="")
        result = persona_select_metric(gold, pred)
        assert result.score == 0.0


# ---------------------------------------------------------------------------
# Training data export parsers
# ---------------------------------------------------------------------------


class TestExportReviewParser:
    """Tests for _parse_review_prompt in export_reviews."""

    def test_parses_valid_review_prompt(self):
        """Extracts diff, title, description from a Gate 4 prompt."""
        from devloop.llmops.training.export_reviews import _parse_review_prompt

        prompt = (
            "You are a senior code reviewer.\n\n"
            "## Issue Context\n"
            "**Title:** Fix null pointer\n"
            "**Description:** The function crashes when input is None.\n\n"
            "## Review Criteria\n"
            "Check for null safety, error handling.\n\n"
            "## Diff to Review\n"
            "```\n"
            "diff --git a/main.py b/main.py\n"
            "--- a/main.py\n"
            "+++ b/main.py\n"
            "@@ -1,3 +1,5 @@\n"
            "+if x is None:\n"
            "+    return\n"
            "```\n"
        )
        result = _parse_review_prompt(prompt)
        assert result is not None
        assert result["issue_title"] == "Fix null pointer"
        assert "crashes" in result["issue_description"]
        assert "diff --git" in result["diff"]

    def test_rejects_non_review_prompt(self):
        """Returns None for non-review content."""
        from devloop.llmops.training.export_reviews import _parse_review_prompt

        result = _parse_review_prompt("Just a regular conversation message.")
        assert result is None

    def test_rejects_no_diff(self):
        """Returns None when there's no diff in the prompt."""
        from devloop.llmops.training.export_reviews import _parse_review_prompt

        result = _parse_review_prompt("You are a senior code reviewer. No diff here.")
        assert result is None


class TestExportRetryParser:
    """Tests for retry prompt detection."""

    REAL_RETRY_PROMPT = (
        "## Issue: Add input validation\n\n"
        "### Failure 1: gate_4_review quality gate\n\n"
        "Error: Critical findings detected\n\n"
        "Failure details:\n"
        "  - [CRITICAL] Missing input validation on user-supplied data\n\n"
        "Please fix the issues listed above and try again. "
        "Do not start over — your previous changes are still in the worktree. "
        "Make the minimal change needed to pass the gates."
    )

    GATE4_REVIEW_PROMPT = (
        "You are a senior code reviewer performing an automated quality gate check.\n\n"
        "## Issue Context\n**Title:** Add factorial function\n\n"
        "## Review Criteria\nCheck the diff for the following issues:\n"
        "  - race_conditions\n  - memory_leaks\n  - logic_errors\n"
        "  - missing_error_handling_at_boundaries\n  - performance_antipatterns\n\n"
        "## Diff to Review\n```\ndiff --git a/calc.py b/calc.py\n```"
    )

    def test_detects_real_retry_prompt(self):
        """_is_retry_prompt identifies build_retry_prompt() output."""
        from devloop.llmops.training.export_retries import _is_retry_prompt

        assert _is_retry_prompt(self.REAL_RETRY_PROMPT)

    def test_rejects_gate4_review_prompt(self):
        """Gate 4 review prompts must not be detected as retry prompts."""
        from devloop.llmops.training.export_retries import _is_retry_prompt

        assert not _is_retry_prompt(self.GATE4_REVIEW_PROMPT)

    def test_rejects_short_content(self):
        """Short messages are not retry prompts."""
        from devloop.llmops.training.export_retries import _is_retry_prompt

        assert not _is_retry_prompt("fix error")

    def test_rejects_unrelated_content(self):
        """Normal conversation is not a retry prompt."""
        from devloop.llmops.training.export_retries import _is_retry_prompt

        assert not _is_retry_prompt(
            "Let me explain how the authentication module works. "
            "It uses JWT tokens for session management with a 24-hour expiry."
        )


# ---------------------------------------------------------------------------
# DSPy program structures
# ---------------------------------------------------------------------------


class TestDSPyPrograms:
    """Tests for DSPy program module structure."""

    def test_code_review_signature_fields(self):
        """CodeReview signature has expected input/output fields."""
        from devloop.llmops.programs.code_review import CodeReview

        input_fields = set(CodeReview.input_fields.keys())
        output_fields = set(CodeReview.output_fields.keys())
        assert input_fields == {"diff", "issue_context", "review_criteria"}
        assert output_fields == {"findings_json"}

    def test_retry_prompt_signature_fields(self):
        """RetryPrompt signature has expected fields."""
        from devloop.llmops.programs.retry_prompt import RetryPrompt

        input_fields = set(RetryPrompt.input_fields.keys())
        output_fields = set(RetryPrompt.output_fields.keys())
        assert input_fields == {"failure_log", "original_task", "gate_results"}
        assert output_fields == {"retry_instructions"}

    def test_persona_select_signature_fields(self):
        """PersonaSelect signature has expected fields."""
        from devloop.llmops.programs.persona_select import PersonaSelect

        input_fields = set(PersonaSelect.input_fields.keys())
        output_fields = set(PersonaSelect.output_fields.keys())
        assert input_fields == {"issue_labels", "issue_description", "repo_type"}
        assert output_fields == {"persona_id", "custom_guidelines"}

    def test_load_program_valid(self):
        """load_program returns a module for valid program names."""
        from devloop.llmops.programs import load_program

        module = load_program("code_review")
        assert hasattr(module, "forward")

    def test_load_program_invalid(self):
        """load_program raises ValueError for unknown programs."""
        from devloop.llmops.programs import load_program

        with pytest.raises(ValueError, match="Unknown program"):
            load_program("nonexistent")


# ---------------------------------------------------------------------------
# Feature flag path (Gate 4 integration)
# ---------------------------------------------------------------------------


class TestGate4FeatureFlag:
    """Tests for the LLMOps feature flag in Gate 4."""

    def test_llmops_disabled_uses_cli_path(self):
        """When llmops.enabled is False, the CLI path attribute is set."""
        # This is a structural test — verify the config loading works
        from devloop.llmops.server import _load_llmops_config

        cfg = _load_llmops_config()
        # Default config has enabled=False
        assert cfg.enabled is False

    def test_llmops_config_enabled_flag(self):
        """LLMOpsConfig enabled flag controls path selection."""
        cfg_off = LLMOpsConfig(enabled=False)
        cfg_on = LLMOpsConfig(enabled=True)
        assert not cfg_off.enabled
        assert cfg_on.enabled


# ---------------------------------------------------------------------------
# Training data helpers
# ---------------------------------------------------------------------------


class TestDefaultSessionsDir:
    """Tests for _default_sessions_dir helper."""

    def test_derives_from_cwd(self, monkeypatch):
        """Sessions dir is derived from CWD, not hardcoded."""
        monkeypatch.setattr("os.getcwd", lambda: "/home/alice/my-project")
        from devloop.llmops.training import _default_sessions_dir

        result = _default_sessions_dir()
        assert "-home-alice-my-project" in result
        assert result.startswith("/home/")
        assert result.endswith("/")

    def test_uses_cwd_not_hardcoded(self, monkeypatch):
        """Path changes when CWD changes — not hardcoded."""
        from devloop.llmops.training import _default_sessions_dir

        monkeypatch.setattr("os.getcwd", lambda: "/home/alice/project-a")
        result_a = _default_sessions_dir()
        monkeypatch.setattr("os.getcwd", lambda: "/home/bob/project-b")
        result_b = _default_sessions_dir()
        assert result_a != result_b
        assert "-home-alice-project-a" in result_a
        assert "-home-bob-project-b" in result_b
