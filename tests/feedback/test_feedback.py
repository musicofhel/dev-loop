"""Tests for the feedback scoring and tuning suggestion scripts."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

# Add scripts to path for direct imports
SCRIPTS_DIR = Path(__file__).parent.parent.parent / "scripts" / "feedback"
sys.path.insert(0, str(SCRIPTS_DIR))

from score import compute_metrics, load_feedback, _prf
from suggest_tuning import (
    analyze,
    generate_yaml_diff,
    _extract_pattern_from_reason,
    _extract_file_from_reason,
    _dedup_suggestions,
)


# ── score.py tests ──────────────────────────────────────────────


class TestPRF:
    """Test precision/recall/F1 computation."""

    def test_perfect_scores(self):
        counts = {"tp": 10, "fp": 0, "fn": 0}
        result = _prf(counts)
        assert result["precision"] == 1.0
        assert result["recall"] == 1.0
        assert result["f1"] == 1.0

    def test_all_false_positives(self):
        counts = {"tp": 0, "fp": 10, "fn": 0}
        result = _prf(counts)
        assert result["precision"] == 0.0
        assert result["recall"] == 0.0
        assert result["f1"] == 0.0

    def test_mixed(self):
        counts = {"tp": 6, "fp": 2, "fn": 2}
        result = _prf(counts)
        assert result["precision"] == 0.75
        assert result["recall"] == 0.75
        assert result["f1"] == 0.75

    def test_empty(self):
        counts = {"tp": 0, "fp": 0, "fn": 0}
        result = _prf(counts)
        assert result["precision"] == 0.0
        assert result["f1"] == 0.0

    def test_high_precision_low_recall(self):
        counts = {"tp": 2, "fp": 0, "fn": 8}
        result = _prf(counts)
        assert result["precision"] == 1.0
        assert result["recall"] == 0.2


class TestComputeMetrics:
    """Test overall metrics computation."""

    def test_single_check_type(self):
        feedbacks = [
            {"check_type": "deny_list", "label": "correct"},
            {"check_type": "deny_list", "label": "correct"},
            {"check_type": "deny_list", "label": "false-positive"},
        ]
        metrics = compute_metrics(feedbacks)
        assert "deny_list" in metrics["per_check"]
        dl = metrics["per_check"]["deny_list"]
        assert dl["tp"] == 2
        assert dl["fp"] == 1
        assert dl["fn"] == 0
        # P = 2/3 = 0.6667
        assert abs(dl["precision"] - 0.6667) < 0.001

    def test_multiple_check_types(self):
        feedbacks = [
            {"check_type": "deny_list", "label": "correct"},
            {"check_type": "secrets", "label": "false-positive"},
            {"check_type": "dangerous_ops", "label": "missed"},
        ]
        metrics = compute_metrics(feedbacks)
        assert len(metrics["per_check"]) == 3
        assert metrics["total"]["tp"] == 1
        assert metrics["total"]["fp"] == 1
        assert metrics["total"]["fn"] == 1
        assert metrics["total"]["labeled_count"] == 3

    def test_empty_feedback(self):
        metrics = compute_metrics([])
        assert metrics["total"]["tp"] == 0
        assert metrics["total"]["labeled_count"] == 0

    def test_timestamp_present(self):
        metrics = compute_metrics([{"check_type": "test", "label": "correct"}])
        assert "timestamp" in metrics


class TestLoadFeedback:
    """Test feedback loading from YAML files."""

    def test_load_from_dir(self, tmp_path):
        fb = {
            "event_id": "L1",
            "label": "correct",
            "check_type": "deny_list",
            "tool_name": "Write",
            "verdict": "block",
            "original_ts": "14:00:00",
            "feedback_ts": "2026-03-16T14:00:00Z",
        }
        (tmp_path / "L1.yaml").write_text(yaml.dump(fb))

        with patch("score.FEEDBACK_DIR", tmp_path):
            feedbacks = load_feedback()
        assert len(feedbacks) == 1
        assert feedbacks[0]["label"] == "correct"

    def test_ignores_invalid_yaml(self, tmp_path):
        (tmp_path / "bad.yaml").write_text("{{invalid yaml")
        (tmp_path / "good.yaml").write_text(yaml.dump({"event_id": "L1", "label": "correct"}))

        with patch("score.FEEDBACK_DIR", tmp_path):
            feedbacks = load_feedback()
        assert len(feedbacks) == 1

    def test_empty_dir(self, tmp_path):
        with patch("score.FEEDBACK_DIR", tmp_path):
            feedbacks = load_feedback()
        assert len(feedbacks) == 0


# ── suggest_tuning.py tests ─────────────────────────────────────


class TestExtractPattern:
    """Test pattern extraction from check reasons."""

    def test_deny_list_reason(self):
        reason = "Blocked: matches deny pattern '.env'"
        assert _extract_pattern_from_reason(reason) == ".env"

    def test_pattern_colon_format(self):
        reason = "something pattern: *.key"
        assert _extract_pattern_from_reason(reason) == "*.key"

    def test_no_pattern(self):
        reason = "no pattern here"
        assert _extract_pattern_from_reason(reason) is None


class TestExtractFile:
    """Test file path extraction from reasons."""

    def test_file_path(self):
        reason = "Secret found in src/config/secrets.rs"
        result = _extract_file_from_reason(reason)
        assert result is not None
        assert "src/config/secrets.rs" in result

    def test_dotfile(self):
        reason = "Blocked .env.example"
        result = _extract_file_from_reason(reason)
        assert result is not None

    def test_no_file(self):
        reason = "no file path here"
        assert _extract_file_from_reason(reason) is None


class TestAnalyze:
    """Test feedback analysis for tuning suggestions."""

    def test_deny_list_fp_suggests_remove(self):
        feedbacks = [
            {
                "check_type": "deny_list",
                "label": "false-positive",
                "reason": "Blocked: matches deny pattern '*secret*'",
                "notes": "secrets.rs is code, not a secret",
            }
        ]
        suggestions = analyze(feedbacks)
        assert len(suggestions["remove_patterns"]) > 0
        assert suggestions["remove_patterns"][0]["pattern"] == "*secret*"

    def test_dangerous_ops_fp_suggests_allow(self):
        feedbacks = [
            {
                "check_type": "dangerous_ops",
                "label": "false-positive",
                "reason": "Dangerous: matches pattern: rm -rf",
                "notes": "rm -rf node_modules is safe",
            }
        ]
        suggestions = analyze(feedbacks)
        assert len(suggestions["allow_patterns"]) > 0

    def test_missed_suggests_extra_patterns(self):
        feedbacks = [
            {
                "check_type": "secrets",
                "label": "missed",
                "notes": "GitHub PAT not detected",
            },
            {
                "check_type": "secrets",
                "label": "missed",
                "notes": "GitHub PAT not detected",
            },
        ]
        suggestions = analyze(feedbacks)
        assert len(suggestions["extra_patterns"]) > 0
        assert suggestions["extra_patterns"][0]["count"] == 2

    def test_no_issues_clean_notes(self):
        feedbacks = [
            {"check_type": "deny_list", "label": "correct"},
            {"check_type": "secrets", "label": "correct"},
        ]
        suggestions = analyze(feedbacks)
        assert any("looks good" in n for n in suggestions["notes"])


class TestDedup:
    """Test suggestion deduplication."""

    def test_dedup_same_pattern(self):
        suggestions = [
            {"section": "deny_list", "pattern": ".env", "reason": "a"},
            {"section": "deny_list", "pattern": ".env", "reason": "b"},
        ]
        result = _dedup_suggestions(suggestions)
        assert len(result) == 1

    def test_keeps_different_patterns(self):
        suggestions = [
            {"section": "deny_list", "pattern": ".env", "reason": "a"},
            {"section": "deny_list", "pattern": ".key", "reason": "b"},
        ]
        result = _dedup_suggestions(suggestions)
        assert len(result) == 2


class TestGenerateYamlDiff:
    """Test YAML diff generation."""

    def test_remove_patterns(self):
        suggestions = {
            "remove_patterns": [{"section": "deny_list", "pattern": "*secret*", "reason": "FP"}],
            "allow_patterns": [],
            "extra_patterns": [],
        }
        yaml_diff = generate_yaml_diff(suggestions)
        parsed = yaml.safe_load(yaml_diff)
        assert "deny_list" in parsed
        assert "*secret*" in parsed["deny_list"]["remove_patterns"]

    def test_nested_secrets_allowlist(self):
        suggestions = {
            "remove_patterns": [],
            "allow_patterns": [{"section": "secrets.file_allowlist", "pattern": "test.env", "reason": "FP"}],
            "extra_patterns": [],
        }
        yaml_diff = generate_yaml_diff(suggestions)
        parsed = yaml.safe_load(yaml_diff)
        assert "secrets" in parsed
        assert "test.env" in parsed["secrets"]["file_allowlist"]

    def test_empty_no_changes(self):
        suggestions = {"remove_patterns": [], "allow_patterns": [], "extra_patterns": []}
        yaml_diff = generate_yaml_diff(suggestions)
        assert "No config changes" in yaml_diff
