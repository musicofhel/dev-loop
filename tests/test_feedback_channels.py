"""Tests for feedback channels 2, 3, 5, 7."""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Channel 3: Cost Monitor
# ---------------------------------------------------------------------------


class TestCostMonitor:
    """Tests for cost_monitor.py."""

    def test_empty_sessions_dir(self, tmp_path):
        from devloop.feedback.cost_monitor import get_usage_summary

        with patch("devloop.feedback.cost_monitor._SESSIONS_DIR", tmp_path / "nope"):
            result = get_usage_summary(hours=24)
        assert result["total_runs"] == 0
        assert result["total_turns"] == 0

    def test_aggregates_sessions(self, tmp_path):
        from devloop.feedback.cost_monitor import get_usage_summary

        # Write two session metadata files
        meta1 = {
            "issue_id": "dl-a",
            "timestamp": time.time(),
            "num_turns": 5,
            "input_tokens": 1000,
            "output_tokens": 500,
        }
        meta2 = {
            "issue_id": "dl-b",
            "timestamp": time.time(),
            "num_turns": 3,
            "input_tokens": 2000,
            "output_tokens": 800,
        }
        (tmp_path / "dl-a-123.meta.json").write_text(json.dumps(meta1))
        (tmp_path / "dl-b-456.meta.json").write_text(json.dumps(meta2))

        with patch("devloop.feedback.cost_monitor._SESSIONS_DIR", tmp_path):
            result = get_usage_summary(hours=24)

        assert result["total_runs"] == 2
        assert result["total_turns"] == 8
        assert result["total_input_tokens"] == 3000
        assert result["total_output_tokens"] == 1300

    def test_filters_old_sessions(self, tmp_path):
        from devloop.feedback.cost_monitor import get_usage_summary

        old_meta = {
            "issue_id": "dl-old",
            "timestamp": time.time() - 100000,  # way in the past
            "num_turns": 99,
            "input_tokens": 99999,
            "output_tokens": 99999,
        }
        (tmp_path / "dl-old-1.meta.json").write_text(json.dumps(old_meta))

        with patch("devloop.feedback.cost_monitor._SESSIONS_DIR", tmp_path):
            result = get_usage_summary(hours=1)

        assert result["total_runs"] == 0

    def test_check_budget_within(self):
        from devloop.feedback.cost_monitor import check_budget

        summary = {
            "total_turns": 10,
            "total_input_tokens": 50000,
            "total_output_tokens": 10000,
        }
        result = check_budget(summary)
        assert result["within_budget"] is True
        assert result["pause_recommended"] is False

    def test_check_budget_exceeded(self):
        from devloop.feedback.cost_monitor import check_budget

        summary = {
            "total_turns": 300,
            "total_input_tokens": 6000000,
            "total_output_tokens": 2000000,
        }
        result = check_budget(summary)
        assert result["within_budget"] is False
        assert result["pause_recommended"] is True
        assert len(result["warnings"]) > 0

    def test_check_budget_warning(self):
        from devloop.feedback.cost_monitor import check_budget

        summary = {
            "total_turns": 170,  # 85% of 200
            "total_input_tokens": 100,
            "total_output_tokens": 100,
        }
        result = check_budget(summary)
        assert result["within_budget"] is True
        assert any("WARNING" in w for w in result["warnings"])


# ---------------------------------------------------------------------------
# Channel 2: Pattern Detector
# ---------------------------------------------------------------------------


class TestPatternDetector:
    """Tests for pattern_detector.py."""

    def test_no_sessions(self, tmp_path):
        from devloop.feedback.pattern_detector import detect_patterns

        with patch("devloop.feedback.pattern_detector._SESSIONS_DIR", tmp_path / "nope"):
            result = detect_patterns(hours=24)
        assert result["patterns_found"] == 0

    def test_detects_repeated_failures(self, tmp_path):
        from devloop.feedback.pattern_detector import detect_patterns

        now = time.time()
        for i in range(5):
            meta = {
                "issue_id": f"dl-{i}",
                "timestamp": now,
                "gate_failure": "Gate 0 (sanity)",
            }
            (tmp_path / f"dl-{i}-{int(now)}.meta.json").write_text(json.dumps(meta))

        with patch("devloop.feedback.pattern_detector._SESSIONS_DIR", tmp_path):
            result = detect_patterns(hours=24, threshold=3)

        assert result["patterns_found"] == 1
        assert result["patterns"][0]["gate"] == "gate_0_sanity"
        assert result["patterns"][0]["failure_count"] == 5
        assert "test suite" in result["patterns"][0]["suggested_fix"].lower()

    def test_below_threshold(self, tmp_path):
        from devloop.feedback.pattern_detector import detect_patterns

        now = time.time()
        meta = {"issue_id": "dl-1", "timestamp": now, "gate_failure": "Gate 3 (security)"}
        (tmp_path / "dl-1-1.meta.json").write_text(json.dumps(meta))

        with patch("devloop.feedback.pattern_detector._SESSIONS_DIR", tmp_path):
            result = detect_patterns(hours=24, threshold=3)

        assert result["patterns_found"] == 0
        assert result["gate_failure_counts"]["gate_3_security"] == 1

    def test_normalize_gate_name(self):
        from devloop.feedback.pattern_detector import _normalize_gate_name

        assert _normalize_gate_name("Gate 0 (sanity)") == "gate_0_sanity"
        assert _normalize_gate_name("Gate 2.5 (dangerous ops)") == "gate_25_dangerous_ops"
        assert _normalize_gate_name("gate_3_security") == "gate_3_security"


# ---------------------------------------------------------------------------
# Channel 5: Changelog
# ---------------------------------------------------------------------------


class TestChangelog:
    """Tests for changelog.py."""

    def test_generate_with_issues(self):
        from devloop.feedback.changelog import generate_changelog

        issues = [
            {"id": "dl-1", "title": "Fix auth bug", "labels": ["bug", "repo:backend"], "status": "done"},
            {"id": "dl-2", "title": "Add rate limiter", "labels": ["feature", "repo:backend"], "status": "done"},
            {"id": "dl-3", "title": "Update docs", "labels": ["docs"], "status": "done"},
        ]

        with patch("devloop.feedback.changelog._run_br") as mock_br:
            mock_br.return_value = type("R", (), {"returncode": 0, "stdout": json.dumps(issues), "stderr": ""})()
            result = generate_changelog(days=7)

        assert result["issue_count"] == 3
        assert "backend" in result["markdown"]
        assert "**Fixed** Fix auth bug" in result["markdown"]
        assert "**Added** Add rate limiter" in result["markdown"]
        assert "**Documented** Update docs" in result["markdown"]

    def test_generate_no_issues(self):
        from devloop.feedback.changelog import generate_changelog

        with patch("devloop.feedback.changelog._run_br") as mock_br:
            mock_br.return_value = type("R", (), {"returncode": 0, "stdout": "[]", "stderr": ""})()
            result = generate_changelog(days=7)

        assert result["issue_count"] == 0
        assert "No completed issues" in result["markdown"]

    def test_generate_br_fails(self):
        from devloop.feedback.changelog import generate_changelog

        with patch("devloop.feedback.changelog._run_br") as mock_br:
            mock_br.return_value = type("R", (), {"returncode": 1, "stdout": "", "stderr": "error"})()
            result = generate_changelog(days=7)

        assert result["issue_count"] == 0
        assert "failed" in result["markdown"].lower()


# ---------------------------------------------------------------------------
# Channel 7: Efficiency
# ---------------------------------------------------------------------------


class TestEfficiency:
    """Tests for efficiency.py."""

    def test_empty_events(self):
        from devloop.feedback.efficiency import analyze_efficiency

        result = analyze_efficiency([])
        assert result["score"] == 1.0
        assert result["total_tool_calls"] == 0

    def test_efficient_session(self):
        from devloop.feedback.efficiency import analyze_efficiency

        events = [
            {"type": "tool_use", "data": {"tool": "Read", "args": {"path": "a.py"}}},
            {"type": "tool_use", "data": {"tool": "Edit", "args": {}}},
            {"type": "tool_use", "data": {"tool": "Read", "args": {"path": "b.py"}}},
            {"type": "tool_use", "data": {"tool": "Edit", "args": {}}},
        ]
        result = analyze_efficiency(events)
        assert result["score"] >= 0.8
        assert result["total_tool_calls"] == 4

    def test_repeated_reads_detected(self):
        from devloop.feedback.efficiency import analyze_efficiency

        events = [
            {"type": "tool_use", "data": {"tool": "Read", "args": {"path": "same.py"}}},
            {"type": "tool_use", "data": {"tool": "Read", "args": {"path": "same.py"}}},
            {"type": "tool_use", "data": {"tool": "Read", "args": {"path": "same.py"}}},
            {"type": "tool_use", "data": {"tool": "Read", "args": {"path": "same.py"}}},
        ]
        result = analyze_efficiency(events)
        assert any(p["type"] == "repeated_read" for p in result["patterns"])
        assert len(result["suggestions"]) > 0

    def test_no_edits_detected(self):
        from devloop.feedback.efficiency import analyze_efficiency

        events = [{"type": "tool_use", "data": {"tool": "Read", "args": {"path": f"f{i}.py"}}} for i in range(15)]
        result = analyze_efficiency(events)
        assert any(p["type"] == "no_edits" for p in result["patterns"])

    def test_excessive_search_detected(self):
        from devloop.feedback.efficiency import analyze_efficiency

        events = [{"type": "tool_use", "data": {"tool": "Glob", "args": {}}} for _ in range(20)]
        result = analyze_efficiency(events)
        assert any(p["type"] == "excessive_search" for p in result["patterns"])

    def test_tool_breakdown(self):
        from devloop.feedback.efficiency import analyze_efficiency

        events = [
            {"type": "tool_use", "data": {"tool": "Read", "args": {}}},
            {"type": "tool_use", "data": {"tool": "Read", "args": {}}},
            {"type": "tool_use", "data": {"tool": "Edit", "args": {}}},
            {"type": "tool_use", "data": {"tool": "Bash", "args": {}}},
        ]
        result = analyze_efficiency(events)
        assert result["tool_breakdown"]["Read"] == 2
        assert result["tool_breakdown"]["Edit"] == 1
        assert result["tool_breakdown"]["Bash"] == 1
