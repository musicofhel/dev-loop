"""Tests for the replay harness: parse_sessions, run_replay, score."""

import json
import os
import sys
import tempfile

import pytest

# Add scripts to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "replay"))

from parse_sessions import parse_session, CHECKABLE_TOOLS
from score import (
    compute_precision_recall,
    analyze_unlabeled,
    compare_baseline,
    is_sensitive_file,
    is_known_fp,
)


# ═══════════════════════════════════════════════════════════════════════════════
# p2.1: parse_sessions tests
# ═══════════════════════════════════════════════════════════════════════════════


def _make_jsonl(entries: list[dict]) -> str:
    """Write entries to a temp JSONL file, return path."""
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
    for entry in entries:
        tmp.write(json.dumps(entry) + "\n")
    tmp.close()
    return tmp.name


def _assistant_entry(tool_name: str, tool_input: dict, timestamp: str = "2026-03-16T12:00:00Z") -> dict:
    return {
        "type": "assistant",
        "timestamp": timestamp,
        "message": {
            "content": [
                {"type": "tool_use", "name": tool_name, "input": tool_input}
            ]
        },
    }


def test_parse_extracts_write_tool():
    path = _make_jsonl([
        _assistant_entry("Write", {"file_path": "/tmp/foo.py", "content": "hello"}),
    ])
    try:
        calls = parse_session(path)
        assert len(calls) == 1
        assert calls[0]["tool_name"] == "Write"
        assert calls[0]["file_path"] == "/tmp/foo.py"
        assert calls[0]["timestamp"] == "2026-03-16T12:00:00Z"
    finally:
        os.unlink(path)


def test_parse_extracts_bash_command():
    path = _make_jsonl([
        _assistant_entry("Bash", {"command": "ls -la"}, "2026-03-16T13:00:00Z"),
    ])
    try:
        calls = parse_session(path)
        assert len(calls) == 1
        assert calls[0]["tool_name"] == "Bash"
        assert calls[0]["command"] == "ls -la"
    finally:
        os.unlink(path)


def test_parse_skips_non_checkable_tools():
    path = _make_jsonl([
        _assistant_entry("Read", {"file_path": "/tmp/foo.py"}),
        _assistant_entry("Glob", {"pattern": "*.py"}),
        _assistant_entry("Write", {"file_path": "/tmp/bar.py", "content": "x"}),
    ])
    try:
        calls = parse_session(path)
        assert len(calls) == 1
        assert calls[0]["tool_name"] == "Write"
    finally:
        os.unlink(path)


def test_parse_skips_user_messages():
    path = _make_jsonl([
        {"type": "user", "message": {"content": "hello"}, "timestamp": "2026-03-16T12:00:00Z"},
        _assistant_entry("Write", {"file_path": "/tmp/foo.py", "content": "x"}),
    ])
    try:
        calls = parse_session(path)
        assert len(calls) == 1
    finally:
        os.unlink(path)


def test_parse_handles_malformed_lines():
    """Malformed JSON lines should be skipped without crashing."""
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
    tmp.write("not json\n")
    tmp.write("{}\n")
    tmp.write(json.dumps(_assistant_entry("Write", {"file_path": "a.py", "content": "x"})) + "\n")
    tmp.close()
    try:
        calls = parse_session(tmp.name)
        assert len(calls) == 1
    finally:
        os.unlink(tmp.name)


def test_parse_session_id_from_filename():
    path = _make_jsonl([
        _assistant_entry("Bash", {"command": "echo hi"}),
    ])
    try:
        calls = parse_session(path)
        # session_id should be the stem of the file path
        expected_id = os.path.splitext(os.path.basename(path))[0]
        assert calls[0]["session_id"] == expected_id
    finally:
        os.unlink(path)


def test_parse_multiple_tools_in_one_message():
    """An assistant message can have multiple tool_use blocks."""
    entry = {
        "type": "assistant",
        "timestamp": "2026-03-16T14:00:00Z",
        "message": {
            "content": [
                {"type": "tool_use", "name": "Write", "input": {"file_path": "a.py", "content": "x"}},
                {"type": "text", "text": "Writing files"},
                {"type": "tool_use", "name": "Write", "input": {"file_path": "b.py", "content": "y"}},
            ]
        },
    }
    path = _make_jsonl([entry])
    try:
        calls = parse_session(path)
        assert len(calls) == 2
        assert calls[0]["file_path"] == "a.py"
        assert calls[1]["file_path"] == "b.py"
    finally:
        os.unlink(path)


# ═══════════════════════════════════════════════════════════════════════════════
# p2.3: score tests
# ═══════════════════════════════════════════════════════════════════════════════


def test_precision_recall_perfect():
    """All predictions correct → P=1, R=1, F1=1."""
    results = [
        {"verdict": "block", "expected_verdict": "block", "check_type": "deny_list"},
        {"verdict": "allow", "expected_verdict": "allow", "check_type": "none"},
        {"verdict": "warn", "expected_verdict": "warn", "check_type": "secrets"},
    ]
    pr = compute_precision_recall(results)
    assert pr["overall"]["precision"] == 1.0
    assert pr["overall"]["recall"] == 1.0
    assert pr["overall"]["f1"] == 1.0


def test_precision_recall_all_fp():
    """Everything flagged but nothing should be → P=0, R undefined (no positives expected)."""
    results = [
        {"verdict": "block", "expected_verdict": "allow", "check_type": "deny_list"},
        {"verdict": "block", "expected_verdict": "allow", "check_type": "deny_list"},
    ]
    pr = compute_precision_recall(results)
    assert pr["overall"]["precision"] == 0.0
    assert pr["overall"]["fp"] == 2


def test_precision_recall_all_fn():
    """Nothing flagged but everything should be → R=0."""
    results = [
        {"verdict": "allow", "expected_verdict": "block", "check_type": "deny_list"},
        {"verdict": "allow", "expected_verdict": "warn", "check_type": "secrets"},
    ]
    pr = compute_precision_recall(results)
    assert pr["overall"]["recall"] == 0.0
    assert pr["overall"]["fn"] == 2


def test_precision_recall_per_check():
    """Per-check-type breakdown works."""
    results = [
        {"verdict": "block", "expected_verdict": "block", "check_type": "deny_list"},
        {"verdict": "block", "expected_verdict": "allow", "check_type": "deny_list"},
        {"verdict": "warn", "expected_verdict": "warn", "check_type": "secrets"},
    ]
    pr = compute_precision_recall(results)
    assert pr["per_check"]["deny_list"]["tp"] == 1
    assert pr["per_check"]["deny_list"]["fp"] == 1
    assert pr["per_check"]["secrets"]["tp"] == 1


def test_analyze_unlabeled_basic():
    results = [
        {"verdict": "allow", "check_type": "none", "tool_name": "Write", "file_path": "foo.py"},
        {"verdict": "block", "check_type": "deny_list", "tool_name": "Write", "file_path": ".env", "reason": "Blocked: matches deny pattern '.env'"},
        {"verdict": "warn", "check_type": "dangerous_ops", "tool_name": "Bash", "command": "rm -rf /", "reason": "Dangerous: rm -rf"},
    ]
    analysis = analyze_unlabeled(results)
    assert analysis["total"] == 3
    assert analysis["verdicts"]["allow"] == 1
    assert analysis["verdicts"]["block"] == 1
    assert analysis["verdicts"]["warn"] == 1


def test_analyze_detects_false_negatives():
    """Sensitive-looking files that were allowed should be flagged."""
    results = [
        {"verdict": "allow", "check_type": "none", "tool_name": "Write", "file_path": "config/secrets.yaml", "session_id": "abc-123"},
    ]
    analysis = analyze_unlabeled(results)
    assert len(analysis["potential_false_negatives"]) == 1
    assert "secrets.yaml" in analysis["potential_false_negatives"][0]["file"]


def test_analyze_detects_known_fp():
    results = [
        {"verdict": "block", "check_type": "deny_list", "tool_name": "Write", "file_path": ".env.example", "reason": "Blocked: .env"},
    ]
    analysis = analyze_unlabeled(results)
    assert len(analysis["likely_false_positives"]) == 1


def test_is_sensitive_file():
    assert is_sensitive_file(".env") is True
    assert is_sensitive_file("server.pem") is True
    assert is_sensitive_file("credentials.json") is True
    assert is_sensitive_file("id_rsa") is True
    assert is_sensitive_file("main.py") is False
    assert is_sensitive_file("README.md") is False


def test_is_known_fp():
    assert is_known_fp(".env.example") is True
    assert is_known_fp(".env.template") is True
    assert is_known_fp(".env.sample") is True
    assert is_known_fp(".env") is False
    assert is_known_fp("main.py") is False


def test_compare_baseline_no_regression():
    current = {"total": 100, "verdicts": {"allow": 98, "block": 1, "warn": 1}, "repeat_blocked_files": {}}
    baseline = {"total": 100, "verdicts": {"allow": 98, "block": 1, "warn": 1}, "repeat_blocked_files": {}}
    result = compare_baseline(current, baseline)
    assert result["has_regressions"] is False


def test_compare_baseline_block_rate_increase():
    current = {"total": 100, "verdicts": {"allow": 90, "block": 8, "warn": 2}, "repeat_blocked_files": {}}
    baseline = {"total": 100, "verdicts": {"allow": 98, "block": 1, "warn": 1}, "repeat_blocked_files": {}}
    result = compare_baseline(current, baseline)
    assert result["has_regressions"] is True
    assert any(r["type"] == "block_rate_increase" for r in result["regressions"])


def test_compare_baseline_new_repeat_blocks():
    current = {"total": 100, "verdicts": {"allow": 98, "block": 2}, "repeat_blocked_files": {"foo.py": 6}}
    baseline = {"total": 100, "verdicts": {"allow": 98, "block": 2}, "repeat_blocked_files": {}}
    result = compare_baseline(current, baseline)
    assert result["has_regressions"] is True
    assert any(r["type"] == "new_repeat_blocks" for r in result["regressions"])
