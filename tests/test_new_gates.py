"""Tests for new quality gates: 0.5 Relevance, 2.5 Dangerous Ops, 5 Cost."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Gate 0.5: Relevance
# ---------------------------------------------------------------------------


class TestGate05Relevance:
    """Tests for run_gate_05_relevance."""

    def test_missing_worktree(self, tmp_path):
        from devloop.gates.server import run_gate_05_relevance

        result = run_gate_05_relevance(
            str(tmp_path / "nope"),
            "Fix auth bug",
            "The auth bug causes 500 errors",
        )
        assert result["passed"] is False
        assert result["gate_name"] == "gate_05_relevance"

    def test_no_diff_fails(self, tmp_path):
        from devloop.gates.server import run_gate_05_relevance

        worktree = tmp_path / "repo"
        worktree.mkdir()

        with patch("devloop.gates.server._run_cmd") as mock_run:
            # All diff commands return empty
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = run_gate_05_relevance(
                str(worktree),
                "Fix auth bug",
                "The auth module has a bug",
            )

        # No diff at all = pass with warning (relevance can't be checked)
        assert result["gate_name"] == "gate_05_relevance"

    def test_keyword_match_passes(self, tmp_path):
        from devloop.gates.server import run_gate_05_relevance

        worktree = tmp_path / "repo"
        worktree.mkdir()

        diff_text = """diff --git a/src/auth.py b/src/auth.py
--- a/src/auth.py
+++ b/src/auth.py
@@ -10,3 +10,5 @@
 def authenticate(token):
-    return verify(token)
+    if not token:
+        raise ValueError("Missing token")
+    return verify(token)
"""
        with patch("devloop.gates.server._run_cmd") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=diff_text, stderr="")
            result = run_gate_05_relevance(
                str(worktree),
                "Fix auth token validation",
                "Add validation for missing auth tokens",
            )

        assert result["passed"] is True
        assert result["gate_name"] == "gate_05_relevance"


# ---------------------------------------------------------------------------
# Gate 2.5: Dangerous Ops
# ---------------------------------------------------------------------------


class TestGate25DangerousOps:
    """Tests for run_gate_25_dangerous_ops."""

    def test_missing_worktree(self, tmp_path):
        from devloop.gates.server import run_gate_25_dangerous_ops

        result = run_gate_25_dangerous_ops(str(tmp_path / "nope"))
        assert result["passed"] is False
        assert result["gate_name"] == "gate_25_dangerous_ops"

    def test_clean_diff_passes(self, tmp_path):
        from devloop.gates.server import run_gate_25_dangerous_ops

        worktree = tmp_path / "repo"
        worktree.mkdir()

        diff_text = """diff --git a/src/utils.py b/src/utils.py
--- a/src/utils.py
+++ b/src/utils.py
@@ -1,3 +1,5 @@
 def helper():
-    return 1
+    return 2
"""
        with patch("devloop.gates.server._run_cmd") as mock_run:
            # First call: diff text. Second call: file names.
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=diff_text, stderr=""),
                MagicMock(returncode=0, stdout="src/utils.py\n", stderr=""),
            ]
            result = run_gate_25_dangerous_ops(str(worktree))

        assert result["passed"] is True
        assert result["gate_name"] == "gate_25_dangerous_ops"

    def test_detects_drop_table(self, tmp_path):
        from devloop.gates.server import run_gate_25_dangerous_ops

        worktree = tmp_path / "repo"
        worktree.mkdir()

        diff_text = """diff --git a/migrations/001.sql b/migrations/001.sql
+DROP TABLE users;
"""
        with patch("devloop.gates.server._run_cmd") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=diff_text, stderr=""),
                MagicMock(returncode=0, stdout="migrations/001.sql\n", stderr=""),
            ]
            result = run_gate_25_dangerous_ops(str(worktree))

        assert result["passed"] is False
        assert any("DROP" in f.get("message", "") for f in result["findings"])

    def test_detects_ci_changes(self, tmp_path):
        from devloop.gates.server import run_gate_25_dangerous_ops

        worktree = tmp_path / "repo"
        worktree.mkdir()

        diff_text = "some diff content"
        with patch("devloop.gates.server._run_cmd") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=diff_text, stderr=""),
                MagicMock(returncode=0, stdout=".github/workflows/ci.yml\n", stderr=""),
            ]
            result = run_gate_25_dangerous_ops(str(worktree))

        assert result["passed"] is False
        assert any("CI/CD" in f.get("message", "") or "workflow" in f.get("message", "").lower()
                    for f in result["findings"])

    def test_detects_dockerfile(self, tmp_path):
        from devloop.gates.server import run_gate_25_dangerous_ops

        worktree = tmp_path / "repo"
        worktree.mkdir()

        with patch("devloop.gates.server._run_cmd") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="some diff", stderr=""),
                MagicMock(returncode=0, stdout="Dockerfile\n", stderr=""),
            ]
            result = run_gate_25_dangerous_ops(str(worktree))

        assert result["passed"] is False


# ---------------------------------------------------------------------------
# Gate 5: Cost
# ---------------------------------------------------------------------------


class TestGate5Cost:
    """Tests for run_gate_5_cost."""

    def test_within_budget_passes(self):
        from devloop.gates.server import run_gate_5_cost

        result = run_gate_5_cost(
            num_turns=5,
            input_tokens=10000,
            output_tokens=5000,
        )
        assert result["passed"] is True
        assert result["gate_name"] == "gate_5_cost"

    def test_exceeds_turns_fails(self):
        from devloop.gates.server import run_gate_5_cost

        result = run_gate_5_cost(
            num_turns=30,
            input_tokens=10000,
            output_tokens=5000,
            max_turns=25,
        )
        assert result["passed"] is False
        assert any("turns" in f.get("message", "").lower() for f in result["findings"])

    def test_exceeds_input_tokens_fails(self):
        from devloop.gates.server import run_gate_5_cost

        result = run_gate_5_cost(
            num_turns=5,
            input_tokens=600000,
            output_tokens=5000,
            max_input_tokens=500000,
        )
        assert result["passed"] is False

    def test_exceeds_output_tokens_fails(self):
        from devloop.gates.server import run_gate_5_cost

        result = run_gate_5_cost(
            num_turns=5,
            input_tokens=10000,
            output_tokens=200000,
            max_output_tokens=100000,
        )
        assert result["passed"] is False

    def test_zero_usage_passes(self):
        from devloop.gates.server import run_gate_5_cost

        result = run_gate_5_cost()
        assert result["passed"] is True

    def test_custom_thresholds(self):
        from devloop.gates.server import run_gate_5_cost

        result = run_gate_5_cost(
            num_turns=3,
            max_turns=2,
        )
        assert result["passed"] is False
