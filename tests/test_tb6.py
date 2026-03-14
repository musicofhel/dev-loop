"""Tests for TB-6: Session Replay Debug — session capture, parsing, replay."""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

from devloop.feedback.pipeline import (
    _format_session_timeline,
    _generate_session_id,
    _load_session,
    _parse_session_events,
    _save_session,
    _suggest_claude_md_fix,
)
from devloop.feedback.types import SessionEvent, TB6Result

# ---------------------------------------------------------------------------
# TB6Result type tests
# ---------------------------------------------------------------------------


class TestTB6Result:
    """Tests for TB6Result Pydantic model."""

    def test_defaults(self):
        r = TB6Result(
            issue_id="dl-test",
            repo_path="/tmp/repo",
            success=False,
            phase="claim",
        )
        assert r.session_id is None
        assert r.session_path is None
        assert r.session_event_count == 0
        assert r.session_event_types == {}
        assert r.gate_failure is None
        assert r.suggested_fix is None
        assert r.force_gate_fail_used is False
        assert r.trace_id is None
        assert r.attempt_span_ids == []

    def test_full_fields(self):
        r = TB6Result(
            issue_id="dl-test",
            repo_path="/tmp/repo",
            success=False,
            phase="suggest_fix",
            session_id="dl-test-1741856400",
            session_path="/tmp/dev-loop/sessions/dl-test-1741856400.ndjson",
            session_event_count=5,
            session_event_types={"result": 1, "tool_use": 3, "assistant": 1},
            gate_failure="Gate 0 (sanity)",
            suggested_fix="Always run tests before committing.",
            force_gate_fail_used=True,
            trace_id="abc123",
            worktree_path="/tmp/wt",
            persona="feature",
            retries_used=1,
            max_retries=1,
            escalated=True,
        )
        assert r.session_id == "dl-test-1741856400"
        assert r.session_event_count == 5
        assert r.gate_failure == "Gate 0 (sanity)"
        assert r.suggested_fix is not None
        assert r.escalated is True

    def test_session_event_types_dict(self):
        r = TB6Result(
            issue_id="dl-test",
            repo_path="/tmp/repo",
            success=True,
            phase="gates_passed",
            session_event_types={"result": 1, "tool_use": 5, "assistant": 3},
        )
        assert r.session_event_types["tool_use"] == 5
        assert sum(r.session_event_types.values()) == 9

    def test_roundtrip(self):
        r = TB6Result(
            issue_id="dl-test",
            repo_path="/tmp/repo",
            success=False,
            phase="suggest_fix",
            session_id="dl-test-123",
            gate_failure="Gate 0 (sanity)",
            suggested_fix="Run tests first.",
        )
        d = r.model_dump()
        r2 = TB6Result(**d)
        assert r2.session_id == r.session_id
        assert r2.gate_failure == r.gate_failure
        assert r2.suggested_fix == r.suggested_fix


# ---------------------------------------------------------------------------
# SessionEvent type tests
# ---------------------------------------------------------------------------


class TestSessionEvent:
    """Tests for SessionEvent Pydantic model."""

    def test_defaults(self):
        e = SessionEvent(line_number=1, type="unknown")
        assert e.data == {}

    def test_full(self):
        e = SessionEvent(
            line_number=3,
            type="tool_use",
            data={"tool": "Read", "args": {"path": "src/main.py"}},
        )
        assert e.type == "tool_use"
        assert e.data["tool"] == "Read"

    def test_serialization(self):
        e = SessionEvent(
            line_number=1,
            type="result",
            data={"type": "result", "num_turns": 5},
        )
        d = e.model_dump()
        assert d["line_number"] == 1
        assert d["type"] == "result"
        assert d["data"]["num_turns"] == 5


# ---------------------------------------------------------------------------
# _parse_session_events tests
# ---------------------------------------------------------------------------


class TestParseSessionEvents:
    """Tests for NDJSON parsing."""

    def test_valid_ndjson(self):
        stdout = '{"type": "assistant", "message": "hello"}\n{"type": "result", "num_turns": 3}\n'
        events = _parse_session_events(stdout)
        assert len(events) == 2
        assert events[0]["type"] == "assistant"
        assert events[1]["type"] == "result"

    def test_empty_stdout(self):
        assert _parse_session_events("") == []
        assert _parse_session_events("\n\n") == []

    def test_non_json_lines_skipped(self):
        stdout = "plain text output\n{\"type\": \"result\"}\nmore text\n"
        events = _parse_session_events(stdout)
        assert len(events) == 1
        assert events[0]["type"] == "result"

    def test_mixed_types(self):
        stdout = (
            '{"type": "system", "data": "init"}\n'
            '{"type": "tool_use", "tool": "Read"}\n'
            '{"type": "tool_result", "output": "file content"}\n'
            '{"type": "assistant", "message": "I found..."}\n'
            '{"type": "result", "num_turns": 2}\n'
        )
        events = _parse_session_events(stdout)
        assert len(events) == 5
        types = [e["type"] for e in events]
        assert types == ["system", "tool_use", "tool_result", "assistant", "result"]

    def test_line_numbers_correct(self):
        stdout = '{"type": "a"}\n\n{"type": "b"}\n'
        events = _parse_session_events(stdout)
        assert events[0]["line_number"] == 1
        assert events[1]["line_number"] == 3  # line 2 is blank

    def test_unknown_type_default(self):
        stdout = '{"foo": "bar"}\n'
        events = _parse_session_events(stdout)
        assert len(events) == 1
        assert events[0]["type"] == "unknown"

    def test_json_array_format(self):
        """Parses a single-line JSON array (Claude CLI --output-format json)."""
        import json

        stdout = json.dumps([
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
            {"type": "user", "message": {"role": "user"}},
            {"type": "result", "num_turns": 5},
        ])
        events = _parse_session_events(stdout)
        assert len(events) == 4
        types = [e["type"] for e in events]
        assert types == ["system", "assistant", "user", "result"]

    def test_json_array_with_non_dicts(self):
        """Skips non-dict entries in JSON array."""
        import json

        stdout = json.dumps([
            42,
            "string",
            {"type": "result", "num_turns": 1},
            None,
        ])
        events = _parse_session_events(stdout)
        assert len(events) == 1
        assert events[0]["type"] == "result"


# ---------------------------------------------------------------------------
# _save_session / _load_session tests
# ---------------------------------------------------------------------------


class TestSaveSession:
    """Tests for session file persistence."""

    def test_creates_files(self, tmp_path):
        with patch("devloop.feedback.pipeline._SESSIONS_DIR", tmp_path):
            path = _save_session(
                "dl-test-123",
                '{"type": "result"}\n',
                {"issue_id": "dl-test", "exit_code": 0},
            )
        assert (tmp_path / "dl-test-123.ndjson").exists()
        assert (tmp_path / "dl-test-123.meta.json").exists()
        assert path == str(tmp_path / "dl-test-123.ndjson")

    def test_metadata_correct(self, tmp_path):
        with patch("devloop.feedback.pipeline._SESSIONS_DIR", tmp_path):
            _save_session(
                "dl-test-456",
                "",
                {"issue_id": "dl-test", "trace_id": "abc"},
            )
        meta = json.loads((tmp_path / "dl-test-456.meta.json").read_text())
        assert meta["issue_id"] == "dl-test"
        assert meta["trace_id"] == "abc"

    def test_creates_directory(self, tmp_path):
        sessions_dir = tmp_path / "nested" / "sessions"
        with patch("devloop.feedback.pipeline._SESSIONS_DIR", sessions_dir):
            _save_session("dl-test-789", "data", {})
        assert sessions_dir.exists()


class TestLoadSession:
    """Tests for loading saved sessions."""

    def test_loads_events_and_metadata(self, tmp_path):
        (tmp_path / "dl-s1.ndjson").write_text('{"type": "result", "num_turns": 3}\n')
        (tmp_path / "dl-s1.meta.json").write_text('{"issue_id": "dl-s1"}')
        with patch("devloop.feedback.pipeline._SESSIONS_DIR", tmp_path):
            result = _load_session("dl-s1")
        assert result["session_id"] == "dl-s1"
        assert len(result["events"]) == 1
        assert result["metadata"]["issue_id"] == "dl-s1"

    def test_missing_file_raises(self, tmp_path):
        with patch("devloop.feedback.pipeline._SESSIONS_DIR", tmp_path):
            with pytest.raises(FileNotFoundError):
                _load_session("nonexistent")

    def test_missing_metadata_ok(self, tmp_path):
        (tmp_path / "dl-s2.ndjson").write_text('{"type": "result"}\n')
        # No .meta.json file
        with patch("devloop.feedback.pipeline._SESSIONS_DIR", tmp_path):
            result = _load_session("dl-s2")
        assert result["metadata"] == {}
        assert len(result["events"]) == 1


# ---------------------------------------------------------------------------
# _format_session_timeline tests
# ---------------------------------------------------------------------------


class TestFormatSessionTimeline:
    """Tests for human-readable timeline formatting."""

    def test_basic_format(self):
        result_data = {
            "type": "result", "num_turns": 3,
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
        events = [
            {"line_number": 1, "type": "result", "data": result_data},
        ]
        metadata = {"issue_id": "dl-test", "duration_seconds": 42, "exit_code": 0}
        timeline = _format_session_timeline("dl-test-123", events, metadata)
        assert "=== Session dl-test-123 ===" in timeline
        assert "Duration: 42s" in timeline
        assert "turns=3" in timeline

    def test_tool_use_events(self):
        events = [
            {"line_number": 1, "type": "tool_use", "data": {"tool": "Read"}},
            {"line_number": 2, "type": "tool_result", "data": {}},
        ]
        timeline = _format_session_timeline("s1", events, {})
        assert "tool_use" in timeline
        assert "Read" in timeline

    def test_empty_events(self):
        timeline = _format_session_timeline("s1", [], {})
        assert "(no events captured)" in timeline

    def test_gate_failure_in_timeline(self):
        metadata = {
            "gate_failure": "Gate 0 (sanity)",
            "suggested_fix": "Run tests first.",
        }
        timeline = _format_session_timeline("s1", [], metadata)
        assert "FAILED at Gate 0" in timeline
        assert "Run tests first" in timeline


# ---------------------------------------------------------------------------
# _suggest_claude_md_fix tests
# ---------------------------------------------------------------------------


class TestSuggestClaudeMdFix:
    """Tests for rule-based fix suggestions."""

    def test_gate_0_sanity(self):
        failures = [{"first_failure": "Gate 0 (sanity)"}]
        fix = _suggest_claude_md_fix(failures)
        assert "test suite" in fix.lower()

    def test_gate_2_secrets(self):
        failures = [{"first_failure": "Gate 2 (secrets)"}]
        fix = _suggest_claude_md_fix(failures)
        assert "api key" in fix.lower() or "credential" in fix.lower()

    def test_gate_3_security(self):
        failures = [{"first_failure": "Gate 3 (security)"}]
        fix = _suggest_claude_md_fix(failures)
        assert "parameterized" in fix.lower()

    def test_gate_4_review(self):
        failures = [{"first_failure": "Gate 4 (review)"}]
        fix = _suggest_claude_md_fix(failures)
        assert "race condition" in fix.lower() or "error handling" in fix.lower()

    def test_no_failures(self):
        fix = _suggest_claude_md_fix([])
        assert "no fix needed" in fix.lower()


# ---------------------------------------------------------------------------
# _generate_session_id tests
# ---------------------------------------------------------------------------


class TestGenerateSessionId:
    """Tests for session ID generation."""

    def test_format(self):
        sid = _generate_session_id("dl-abc")
        assert sid.startswith("dl-abc-")
        # Should end with a unix timestamp
        ts_part = sid.split("-", 2)[-1]
        assert ts_part.isdigit()

    @patch("devloop.feedback.pipeline.time")
    def test_uniqueness(self, mock_time):
        """Different timestamps produce different session IDs."""
        mock_time.time.side_effect = [1000000, 1000001]
        mock_time.monotonic = time.monotonic  # don't break other code
        s1 = _generate_session_id("dl-test")
        s2 = _generate_session_id("dl-test")
        assert s1 != s2
        assert s1 == "dl-test-1000000"
        assert s2 == "dl-test-1000001"
