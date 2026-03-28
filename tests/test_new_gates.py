"""Tests for new quality gates: 0.5 Relevance, 2.5 Dangerous Ops, 5 Cost, worktree validation."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Worktree validation helper (E-6)
# ---------------------------------------------------------------------------


class TestVerifyWorktree:
    """Tests for _verify_worktree() helper."""

    def test_missing_dir_returns_error(self, tmp_path):
        from devloop.gates.server import _verify_worktree
        result = _verify_worktree(str(tmp_path / "nope"), "test_gate")
        assert result is not None
        assert result["passed"] is False
        assert "not found" in result["findings"][0]["message"].lower()

    def test_dir_without_git_returns_error(self, tmp_path):
        from devloop.gates.server import _verify_worktree
        worktree = tmp_path / "not-a-repo"
        worktree.mkdir()
        result = _verify_worktree(str(worktree), "test_gate")
        assert result is not None
        assert result["passed"] is False
        assert "not a git repo" in result["findings"][0]["message"].lower()

    def test_valid_git_dir_returns_none(self, tmp_path):
        from devloop.gates.server import _verify_worktree
        worktree = tmp_path / "valid-repo"
        worktree.mkdir()
        (worktree / ".git").mkdir()
        result = _verify_worktree(str(worktree), "test_gate")
        assert result is None

    def test_git_file_accepted(self, tmp_path):
        """Worktrees have a .git file (not dir) — this should also pass."""
        from devloop.gates.server import _verify_worktree
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: /some/path/.git/worktrees/test")
        result = _verify_worktree(str(worktree), "test_gate")
        assert result is None


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

        diff_text = """diff --git a/src/db.sql b/src/db.sql
+DROP TABLE users;
"""
        with patch("devloop.gates.server._run_cmd") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=diff_text, stderr=""),
                MagicMock(returncode=0, stdout="src/db.sql\n", stderr=""),
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

    # --- Migration file detection (section 1b) ---

    def test_prisma_migration_with_drop_is_critical(self, tmp_path):
        """Prisma migration containing DROP → critical finding, gate fails."""
        from devloop.gates.server import run_gate_25_dangerous_ops

        worktree = tmp_path / "repo"
        worktree.mkdir()

        diff_text = "+-- CreateTable\n+ALTER TABLE users ADD COLUMN age INT;"
        file_diff = "+DROP TABLE old_users;"
        with patch("devloop.gates.server._run_cmd") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=diff_text, stderr=""),
                MagicMock(
                    returncode=0,
                    stdout="prisma/migrations/20260328_drop/migration.sql\n",
                    stderr="",
                ),
                MagicMock(returncode=0, stdout=file_diff, stderr=""),
            ]
            result = run_gate_25_dangerous_ops(str(worktree))

        assert result["passed"] is False
        destructive = [f for f in result["findings"] if f.get("rule") == "destructive_migration"]
        assert len(destructive) >= 1
        assert "DROP" in destructive[0]["message"]

    def test_alembic_migration_with_truncate_is_critical(self, tmp_path):
        """Alembic migration containing TRUNCATE → critical finding."""
        from devloop.gates.server import run_gate_25_dangerous_ops

        worktree = tmp_path / "repo"
        worktree.mkdir()

        diff_text = "some changes"
        file_diff = "+TRUNCATE TABLE sessions;"
        with patch("devloop.gates.server._run_cmd") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=diff_text, stderr=""),
                MagicMock(
                    returncode=0,
                    stdout="alembic/versions/abc123_cleanup.py\n",
                    stderr="",
                ),
                MagicMock(returncode=0, stdout=file_diff, stderr=""),
            ]
            result = run_gate_25_dangerous_ops(str(worktree))

        assert result["passed"] is False
        destructive = [f for f in result["findings"] if f.get("rule") == "destructive_migration"]
        assert len(destructive) >= 1
        assert "TRUNCATE" in destructive[0]["message"]

    def test_rails_migration_with_rename_is_critical(self, tmp_path):
        """Rails migration containing RENAME → critical finding."""
        from devloop.gates.server import run_gate_25_dangerous_ops

        worktree = tmp_path / "repo"
        worktree.mkdir()

        diff_text = "some changes"
        file_diff = "+RENAME TABLE users TO accounts;"
        with patch("devloop.gates.server._run_cmd") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=diff_text, stderr=""),
                MagicMock(
                    returncode=0,
                    stdout="db/migrate/20260328_rename.rb\n",
                    stderr="",
                ),
                MagicMock(returncode=0, stdout=file_diff, stderr=""),
            ]
            result = run_gate_25_dangerous_ops(str(worktree))

        assert result["passed"] is False
        destructive = [f for f in result["findings"] if f.get("rule") == "destructive_migration"]
        assert len(destructive) >= 1

    def test_additive_migration_is_warning_not_critical(self, tmp_path):
        """Migration with only CREATE/ADD → warning (gate passes)."""
        from devloop.gates.server import run_gate_25_dangerous_ops

        worktree = tmp_path / "repo"
        worktree.mkdir()

        diff_text = "+CREATE TABLE new_feature (id SERIAL PRIMARY KEY);"
        file_diff = "+CREATE TABLE new_feature (id SERIAL PRIMARY KEY);"
        with patch("devloop.gates.server._run_cmd") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=diff_text, stderr=""),
                MagicMock(
                    returncode=0,
                    stdout="prisma/migrations/20260328_add/migration.sql\n",
                    stderr="",
                ),
                MagicMock(returncode=0, stdout=file_diff, stderr=""),
            ]
            result = run_gate_25_dangerous_ops(str(worktree))

        assert result["passed"] is True
        migration_warnings = [
            f for f in result["findings"] if f.get("rule") == "migration_file"
        ]
        assert len(migration_warnings) == 1
        assert "additive" in migration_warnings[0]["message"].lower()

    def test_alter_table_drop_column_in_migration_is_critical(self, tmp_path):
        """ALTER TABLE ... DROP COLUMN in a migration → critical."""
        from devloop.gates.server import run_gate_25_dangerous_ops

        worktree = tmp_path / "repo"
        worktree.mkdir()

        diff_text = "migration changes"
        file_diff = "+ALTER TABLE users DROP COLUMN legacy_field;"
        with patch("devloop.gates.server._run_cmd") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=diff_text, stderr=""),
                MagicMock(
                    returncode=0,
                    stdout="prisma/migrations/20260328/migration.sql\n",
                    stderr="",
                ),
                MagicMock(returncode=0, stdout=file_diff, stderr=""),
            ]
            result = run_gate_25_dangerous_ops(str(worktree))

        assert result["passed"] is False
        destructive = [f for f in result["findings"] if f.get("rule") == "destructive_migration"]
        assert any("ALTER TABLE" in f["message"] or "DROP" in f["message"] for f in destructive)

    def test_non_migration_file_not_flagged(self, tmp_path):
        """Regular SQL file (not in migration path) → no migration finding."""
        from devloop.gates.server import run_gate_25_dangerous_ops

        worktree = tmp_path / "repo"
        worktree.mkdir()

        diff_text = "some diff"
        with patch("devloop.gates.server._run_cmd") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=diff_text, stderr=""),
                MagicMock(returncode=0, stdout="src/queries/users.sql\n", stderr=""),
            ]
            result = run_gate_25_dangerous_ops(str(worktree))

        assert result["passed"] is True
        migration_findings = [
            f for f in result["findings"]
            if f.get("rule") in ("destructive_migration", "migration_file")
        ]
        assert len(migration_findings) == 0

    def test_multiple_migration_files_each_checked(self, tmp_path):
        """Multiple migration files → each gets individual assessment."""
        from devloop.gates.server import run_gate_25_dangerous_ops

        worktree = tmp_path / "repo"
        worktree.mkdir()

        diff_text = "changes"
        with patch("devloop.gates.server._run_cmd") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=diff_text, stderr=""),
                MagicMock(
                    returncode=0,
                    stdout="prisma/migrations/001/migration.sql\nprisma/migrations/002/migration.sql\n",
                    stderr="",
                ),
                # First migration: additive
                MagicMock(returncode=0, stdout="+CREATE TABLE t1 (id INT);", stderr=""),
                # Second migration: destructive
                MagicMock(returncode=0, stdout="+DROP TABLE t2;", stderr=""),
            ]
            result = run_gate_25_dangerous_ops(str(worktree))

        assert result["passed"] is False
        additive = [f for f in result["findings"] if f.get("rule") == "migration_file"]
        destructive = [f for f in result["findings"] if f.get("rule") == "destructive_migration"]
        assert len(additive) == 1
        assert len(destructive) >= 1

    def test_migration_file_with_cached_diff_fallback(self, tmp_path):
        """When HEAD~1 diff is empty, falls back to --cached diff."""
        from devloop.gates.server import run_gate_25_dangerous_ops

        worktree = tmp_path / "repo"
        worktree.mkdir()

        diff_text = "some staged content"
        with patch("devloop.gates.server._run_cmd") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=diff_text, stderr=""),
                MagicMock(
                    returncode=0,
                    stdout="migrations/0001_initial.py\n",
                    stderr="",
                ),
                # HEAD~1 diff empty
                MagicMock(returncode=0, stdout="", stderr=""),
                # --cached diff has content
                MagicMock(returncode=0, stdout="+DROP TABLE old;", stderr=""),
            ]
            result = run_gate_25_dangerous_ops(str(worktree))

        assert result["passed"] is False
        destructive = [f for f in result["findings"] if f.get("rule") == "destructive_migration"]
        assert len(destructive) >= 1

    def test_delete_in_migration_is_critical(self, tmp_path):
        """DELETE statement in migration → critical."""
        from devloop.gates.server import run_gate_25_dangerous_ops

        worktree = tmp_path / "repo"
        worktree.mkdir()

        diff_text = "changes"
        file_diff = "+DELETE FROM users WHERE active = false;"
        with patch("devloop.gates.server._run_cmd") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=diff_text, stderr=""),
                MagicMock(
                    returncode=0,
                    stdout="prisma/migrations/20260328_cleanup/migration.sql\n",
                    stderr="",
                ),
                MagicMock(returncode=0, stdout=file_diff, stderr=""),
            ]
            result = run_gate_25_dangerous_ops(str(worktree))

        assert result["passed"] is False
        destructive = [f for f in result["findings"] if f.get("rule") == "destructive_migration"]
        assert len(destructive) >= 1


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


# ---------------------------------------------------------------------------
# Gate 0.5: Strict mode (E-3)
# ---------------------------------------------------------------------------


class TestGate05StrictMode:
    """Tests for run_gate_05_relevance with strict=True."""

    def test_strict_fails_on_zero_overlap(self, tmp_path):
        from devloop.gates.server import run_gate_05_relevance

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
            mock_run.return_value = MagicMock(returncode=0, stdout=diff_text, stderr="")
            result = run_gate_05_relevance(
                str(worktree),
                "Fix database migration rollback",
                "The migration rollback fails on PostgreSQL",
                strict=True,
            )

        assert result["passed"] is False
        assert any("strict mode" in f.get("message", "") for f in result["findings"])

    def test_non_strict_passes_on_zero_overlap(self, tmp_path):
        from devloop.gates.server import run_gate_05_relevance

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
            mock_run.return_value = MagicMock(returncode=0, stdout=diff_text, stderr="")
            result = run_gate_05_relevance(
                str(worktree),
                "Fix database migration rollback",
                "The migration rollback fails on PostgreSQL",
                strict=False,
            )

        assert result["passed"] is True  # Soft gate


# ---------------------------------------------------------------------------
# Gate 3: Bandit missing with fail_on_missing_tool (S-1)
# ---------------------------------------------------------------------------


class TestGate3MissingTool:
    """Tests for run_gate_3_security with fail_on_missing_tool."""

    def test_fail_when_bandit_missing_and_strict(self, tmp_path):
        from devloop.gates.server import run_gate_3_security

        worktree = tmp_path / "repo"
        worktree.mkdir()
        (worktree / "pyproject.toml").write_text("[project]\nname = 'test'\n")

        with patch("devloop.gates.server._find_bandit", return_value=None):
            result = run_gate_3_security(str(worktree), fail_on_missing_tool=True)

        assert result["passed"] is False
        assert any("not installed" in f.get("message", "") for f in result["findings"])

    def test_skip_when_bandit_missing_and_not_strict(self, tmp_path):
        from devloop.gates.server import run_gate_3_security

        worktree = tmp_path / "repo"
        worktree.mkdir()
        (worktree / "pyproject.toml").write_text("[project]\nname = 'test'\n")

        with patch("devloop.gates.server._find_bandit", return_value=None):
            result = run_gate_3_security(str(worktree), fail_on_missing_tool=False)

        assert result["passed"] is True
        assert result.get("skipped") is True


# ---------------------------------------------------------------------------
# run_all_gates sequencing (T-3)
# ---------------------------------------------------------------------------


class TestRunAllGatesSequencing:
    """Tests for run_all_gates() execution order and fail-fast behavior."""

    def test_fail_fast_on_gate0_failure(self):
        """Gate 0 failure prevents gates 0.5-4 from running."""
        from devloop.gates.server import run_all_gates

        with patch("devloop.gates.server.run_gate_0_sanity") as mock_g0, \
             patch("devloop.gates.server.run_gate_05_relevance") as mock_g05:
            mock_g0.return_value = {
                "gate_name": "gate_0_sanity",
                "passed": False,
                "findings": [{"severity": "critical", "message": "tests failed"}],
                "duration_seconds": 0.1,
                "skipped": False,
            }

            result = run_all_gates("/tmp/wt", "Fix bug", "Description")

            assert result["overall_passed"] is False
            assert result["first_failure"] == "gate_0_sanity"
            mock_g05.assert_not_called()  # Gate 0.5 never ran

    def test_all_gates_run_on_success(self):
        """All 6 gates run when each passes."""
        from devloop.gates.server import run_all_gates

        gate_pass = lambda name: {
            "gate_name": name,
            "passed": True,
            "findings": [],
            "duration_seconds": 0.01,
            "skipped": False,
        }

        with patch("devloop.gates.server.run_gate_0_sanity", return_value=gate_pass("gate_0_sanity")), \
             patch("devloop.gates.server.run_gate_05_relevance", return_value=gate_pass("gate_05_relevance")), \
             patch("devloop.gates.server.run_gate_2_secrets", return_value=gate_pass("gate_2_secrets")), \
             patch("devloop.gates.server.run_gate_25_dangerous_ops", return_value=gate_pass("gate_25_dangerous_ops")), \
             patch("devloop.gates.server.run_gate_3_security", return_value=gate_pass("gate_3_security")), \
             patch("devloop.gates.server.run_gate_4_review", return_value=gate_pass("gate_4_review")):

            result = run_all_gates("/tmp/wt", "Fix bug", "Description")

            assert result["overall_passed"] is True
            assert result["first_failure"] is None
            assert len(result["gate_results"]) == 6

    def test_skip_propagation(self):
        """Skipped gates don't cause fail-fast."""
        from devloop.gates.server import run_all_gates

        gate_pass = lambda name: {
            "gate_name": name,
            "passed": True,
            "findings": [],
            "duration_seconds": 0.01,
            "skipped": False,
        }
        gate_skip = lambda name: {
            "gate_name": name,
            "passed": True,
            "findings": [{"severity": "info", "message": "skipped"}],
            "duration_seconds": 0.01,
            "skipped": True,
        }

        with patch("devloop.gates.server.run_gate_0_sanity", return_value=gate_pass("gate_0_sanity")), \
             patch("devloop.gates.server.run_gate_05_relevance", return_value=gate_skip("gate_05_relevance")), \
             patch("devloop.gates.server.run_gate_2_secrets", return_value=gate_pass("gate_2_secrets")), \
             patch("devloop.gates.server.run_gate_25_dangerous_ops", return_value=gate_pass("gate_25_dangerous_ops")), \
             patch("devloop.gates.server.run_gate_3_security", return_value=gate_skip("gate_3_security")), \
             patch("devloop.gates.server.run_gate_4_review", return_value=gate_pass("gate_4_review")):

            result = run_all_gates("/tmp/wt", "Fix bug", "Description")

            assert result["overall_passed"] is True
            assert len(result["gate_results"]) == 6
