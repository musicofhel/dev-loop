"""Tests for ambiguity detection (#32)."""

import subprocess
from unittest.mock import patch

from devloop.intake.ambiguity import (
    AmbiguityResult,
    _check_acceptance_criteria,
    _check_description_length,
    _check_specificity,
    _check_vague_verbs,
    defer_ambiguous_issue,
    detect_ambiguity,
)


class TestDetectAmbiguity:
    # Clear issues (should NOT be flagged)
    def test_specific_bug_not_ambiguous(self):
        r = detect_ambiguity(
            "Fix crash in auth/login.py on empty password",
            "The function `validate_credentials()` throws TypeError when password is None. Should return 401.",
        )
        assert not r.is_ambiguous

    def test_inline_code_not_ambiguous(self):
        r = detect_ambiguity(
            "TypeError in parse_config() when YAML is malformed",
            "Stack trace shows error at line 42 in `config.py`",
        )
        assert not r.is_ambiguous

    def test_acceptance_criteria_not_ambiguous(self):
        r = detect_ambiguity(
            "Add rate limiting to API",
            "When more than 100 requests per minute, should return 429. Must apply to all /api/* routes.",
        )
        assert not r.is_ambiguous

    def test_file_path_not_ambiguous(self):
        r = detect_ambiguity(
            "Add rate limiting to src/api/routes.ts",
            "Implement token bucket algorithm with configurable limits per endpoint",
        )
        assert not r.is_ambiguous

    # Ambiguous issues (SHOULD be flagged)
    def test_vague_improve_performance(self):
        r = detect_ambiguity("Improve performance", "")
        assert r.is_ambiguous

    def test_vague_clean_up(self):
        r = detect_ambiguity("Clean up auth code", "It's messy")
        assert r.is_ambiguous

    def test_vague_make_better(self):
        r = detect_ambiguity("Make the API better", "")
        assert r.is_ambiguous

    def test_no_description(self):
        r = detect_ambiguity("Optimize database", None)
        assert r.is_ambiguous

    def test_very_short_description(self):
        r = detect_ambiguity("Enhance logging", "Add more logs")
        assert r.is_ambiguous

    # Edge cases
    def test_threshold_boundary(self):
        # Score exactly at threshold is ambiguous
        r = detect_ambiguity("Improve things", "", threshold=0.0)
        assert r.is_ambiguous

    def test_custom_threshold(self):
        r = detect_ambiguity("Improve performance", "", threshold=0.99)
        assert not r.is_ambiguous

    def test_vague_verb_with_specific_context(self):
        r = detect_ambiguity(
            "Improve error handling in auth/login.py:42",
            "The function should return 401 instead of 500",
        )
        assert not r.is_ambiguous

    def test_empty_title(self):
        r = detect_ambiguity("", "")
        assert r.is_ambiguous


class TestVagueVerbs:
    def test_detects_improve(self):
        signals = _check_vague_verbs("Improve the login flow")
        assert len(signals) == 1
        assert signals[0].signal_type == "vague_verb"

    def test_detects_clean_up(self):
        signals = _check_vague_verbs("Clean up the auth code")
        assert len(signals) == 1

    def test_case_insensitive(self):
        signals = _check_vague_verbs("OPTIMIZE the database")
        assert len(signals) == 1

    def test_no_false_positive_fix(self):
        signals = _check_vague_verbs("Fix the login bug")
        assert len(signals) == 0

    def test_no_false_positive_add(self):
        signals = _check_vague_verbs("Add rate limiting")
        assert len(signals) == 0

    def test_no_false_positive_implement(self):
        signals = _check_vague_verbs("Implement caching layer")
        assert len(signals) == 0


class TestDescriptionLength:
    def test_no_description(self):
        s = _check_description_length(None)
        assert s is not None
        assert s.signal_type == "short_description"

    def test_short(self):
        s = _check_description_length("It's broken")
        assert s is not None

    def test_adequate(self):
        s = _check_description_length(
            "The login function in auth.py throws a TypeError when the password field is None "
            "because it tries to call .strip() on NoneType which we need to handle",
        )
        assert s is None


class TestSpecificity:
    def test_no_specifics(self):
        s = _check_specificity("Make it faster and better")
        assert s is not None
        assert s.signal_type == "no_specifics"

    def test_file_path_found(self):
        s = _check_specificity("Fix bug in auth.py")
        assert s is None

    def test_inline_code_found(self):
        s = _check_specificity("The `parse()` function is broken")
        assert s is None

    def test_error_keyword_found(self):
        s = _check_specificity("There is a crash on startup")
        assert s is None


class TestAcceptanceCriteria:
    def test_no_criteria(self):
        s = _check_acceptance_criteria("Make it faster")
        assert s is not None
        assert s.signal_type == "no_acceptance_criteria"

    def test_should_found(self):
        s = _check_acceptance_criteria("It should return 200")
        assert s is None

    def test_must_found(self):
        s = _check_acceptance_criteria("Must handle null inputs")
        assert s is None

    def test_returns_found(self):
        s = _check_acceptance_criteria("returns 404 on missing resource")
        assert s is None


class TestDeferAmbiguousIssue:
    @patch("devloop.intake.ambiguity.subprocess.run")
    def test_defers_with_calls(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        result = AmbiguityResult(
            is_ambiguous=True, score=0.8, signals=[], title="test", description="",
        )
        assert defer_ambiguous_issue("dl-123", result) is True
        assert mock_run.call_count == 3  # label, comment, status

    @patch("devloop.intake.ambiguity.subprocess.run")
    def test_returns_false_on_failure(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=1)
        result = AmbiguityResult(
            is_ambiguous=True, score=0.8, signals=[], title="test", description="",
        )
        assert defer_ambiguous_issue("dl-123", result) is False
