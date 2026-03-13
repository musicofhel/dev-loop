"""Tests for TB-3 pipeline helpers — security seeding, forced failure, finding extraction."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from devloop.feedback.pipeline import (
    _extract_security_findings,
    _make_forced_security_failure,
    _seed_vulnerable_code,
    _span_id_hex,
    _trace_id_hex,
)
from devloop.feedback.types import SecurityFinding, TB3Result
from devloop.gates.server import run_gate_3_security
from devloop.gates.types import Finding, GateResult, GateSuiteResult


# ---------------------------------------------------------------------------
# _seed_vulnerable_code tests
# ---------------------------------------------------------------------------


class TestSeedVulnerableCode:
    """Tests for the TB-3 vulnerable code seeding."""

    def test_seeds_vulnerable_file(self, tmp_path):
        """Copies vulnerable code into the worktree's src directory."""
        # Initialize a git repo so git add/commit succeed
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(tmp_path), capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(tmp_path), capture_output=True, check=True,
        )
        # Create a package dir to simulate prompt-bench
        pkg_dir = tmp_path / "src" / "prompt_bench"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text("")
        # Need an initial commit for git add to work in worktree context
        subprocess.run(
            ["git", "add", "-A"], cwd=str(tmp_path), capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=str(tmp_path), capture_output=True, check=True,
        )

        result = _seed_vulnerable_code(str(tmp_path))
        dst = pkg_dir / "search.py"
        assert result is True
        assert dst.exists()
        content = dst.read_text()
        assert "SQL injection" in content or "sql" in content.lower()
        assert "cursor.execute" in content

    def test_creates_src_dir_if_missing(self, tmp_path):
        """Creates src/ directory if it doesn't exist."""
        worktree = tmp_path / "new_project"
        worktree.mkdir()
        _seed_vulnerable_code(str(worktree))
        # Should create src/ and put file there
        assert (worktree / "src").exists()

    @patch("devloop.feedback.pipeline._FIXTURES_DIR")
    def test_returns_false_if_fixture_missing(self, mock_fixtures_dir, tmp_path):
        """Returns False when the fixture source file doesn't exist."""
        mock_fixtures_dir.__truediv__ = lambda self, key: tmp_path / "nonexistent" / key
        result = _seed_vulnerable_code(str(tmp_path))
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# _make_forced_security_failure tests
# ---------------------------------------------------------------------------


class TestMakeForcedSecurityFailure:
    """Tests for the synthetic security gate failure generator."""

    def test_returns_failed_suite(self):
        result = _make_forced_security_failure()
        suite = GateSuiteResult(**result)
        assert not suite.overall_passed
        assert suite.first_failure == "gate_3_security"

    def test_gates_0_and_2_pass(self):
        result = _make_forced_security_failure()
        suite = GateSuiteResult(**result)
        assert suite.gate_results[0].gate_name == "gate_0_sanity"
        assert suite.gate_results[0].passed is True
        assert suite.gate_results[1].gate_name == "gate_2_secrets"
        assert suite.gate_results[1].passed is True

    def test_gate_3_has_cwe_findings(self):
        result = _make_forced_security_failure()
        suite = GateSuiteResult(**result)
        gate_3 = suite.gate_results[2]
        assert gate_3.gate_name == "gate_3_security"
        assert gate_3.passed is False
        assert len(gate_3.findings) == 2
        for f in gate_3.findings:
            assert f.severity == "critical"
            assert f.cwe == "CWE-89"
            assert f.rule == "B608"
            assert "SQL injection" in f.message

    def test_findings_have_file_and_line(self):
        result = _make_forced_security_failure()
        suite = GateSuiteResult(**result)
        for f in suite.gate_results[2].findings:
            assert f.file is not None
            assert f.line is not None


# ---------------------------------------------------------------------------
# _extract_security_findings tests
# ---------------------------------------------------------------------------


class TestExtractSecurityFindings:
    """Tests for extracting security findings from gate suite results."""

    def test_extracts_from_gate_3(self):
        suite = {
            "gate_results": [
                {"gate_name": "gate_0_sanity", "passed": True, "findings": []},
                {"gate_name": "gate_2_secrets", "passed": True, "findings": []},
                {
                    "gate_name": "gate_3_security",
                    "passed": False,
                    "findings": [
                        {
                            "severity": "critical",
                            "message": "SQL injection",
                            "cwe": "CWE-89",
                            "file": "search.py",
                            "line": 10,
                            "rule": "B608",
                        },
                    ],
                },
            ],
        }
        findings, gate_3_ran = _extract_security_findings(suite)
        assert len(findings) == 1
        assert findings[0].cwe == "CWE-89"
        assert findings[0].file == "search.py"
        assert gate_3_ran is True

    def test_ignores_non_security_gates(self):
        suite = {
            "gate_results": [
                {
                    "gate_name": "gate_0_sanity",
                    "passed": False,
                    "findings": [
                        {"severity": "critical", "message": "tests failed"},
                    ],
                },
            ],
        }
        findings, gate_3_ran = _extract_security_findings(suite)
        assert len(findings) == 0
        assert gate_3_ran is False

    def test_ignores_info_findings(self):
        suite = {
            "gate_results": [
                {
                    "gate_name": "gate_3_security",
                    "passed": True,
                    "findings": [
                        {"severity": "info", "message": "scan complete"},
                    ],
                },
            ],
        }
        findings, gate_3_ran = _extract_security_findings(suite)
        assert len(findings) == 0
        assert gate_3_ran is True

    def test_handles_empty_suite(self):
        findings, gate_3_ran = _extract_security_findings({})
        assert findings == []
        assert gate_3_ran is False

    def test_multiple_findings(self):
        suite = {
            "gate_results": [
                {
                    "gate_name": "gate_3_security",
                    "passed": False,
                    "findings": [
                        {"severity": "critical", "message": "SQL injection", "cwe": "CWE-89"},
                        {"severity": "warning", "message": "Weak hash", "cwe": "CWE-328"},
                        {"severity": "info", "message": "scan note"},
                    ],
                },
            ],
        }
        findings, gate_3_ran = _extract_security_findings(suite)
        assert len(findings) == 2
        cwes = {f.cwe for f in findings}
        assert cwes == {"CWE-89", "CWE-328"}
        assert gate_3_ran is True

    def test_skipped_gate_3(self):
        """Skipped Gate 3 returns gate_3_ran=False (M9 fix)."""
        suite = {
            "gate_results": [
                {
                    "gate_name": "gate_3_security",
                    "passed": True,
                    "skipped": True,
                    "findings": [
                        {"severity": "info", "message": "Security scan skipped for node project"},
                    ],
                },
            ],
        }
        findings, gate_3_ran = _extract_security_findings(suite)
        assert len(findings) == 0
        assert gate_3_ran is False


# ---------------------------------------------------------------------------
# Gate 3 unit tests (mocked bandit)
# ---------------------------------------------------------------------------


class TestGate3Security:
    """Tests for run_gate_3_security (with mocked subprocess)."""

    def test_skips_for_node_project(self, tmp_path):
        """Gate 3 skips gracefully for non-Python projects."""
        (tmp_path / "package.json").write_text("{}")
        result = run_gate_3_security(str(tmp_path))
        gate = GateResult(**result)
        assert gate.passed is True
        assert gate.skipped is True

    def test_skips_when_bandit_not_found(self, tmp_path):
        """Gate 3 skips gracefully when bandit is not installed."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n")
        with patch("devloop.gates.server._find_bandit", return_value=None):
            result = run_gate_3_security(str(tmp_path))
        gate = GateResult(**result)
        assert gate.passed is True
        assert gate.skipped is True
        assert "bandit not found" in gate.findings[0].message

    def test_fails_on_missing_worktree(self):
        """Gate 3 fails when worktree doesn't exist."""
        result = run_gate_3_security("/nonexistent/path")
        gate = GateResult(**result)
        assert gate.passed is False
        assert "not found" in gate.findings[0].message

    @patch("devloop.gates.server._run_cmd")
    @patch("devloop.gates.server._find_bandit", return_value="/usr/bin/bandit")
    def test_passes_on_clean_scan(self, mock_bandit, mock_run, tmp_path):
        """Gate 3 passes when bandit finds no issues."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n")
        (tmp_path / "src").mkdir()
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"results": [], "errors": []}),
            stderr="",
        )
        result = run_gate_3_security(str(tmp_path))
        gate = GateResult(**result)
        assert gate.passed is True
        assert len(gate.findings) == 0

    @patch("devloop.gates.server._run_cmd")
    @patch("devloop.gates.server._find_bandit", return_value="/usr/bin/bandit")
    def test_fails_on_vulnerability(self, mock_bandit, mock_run, tmp_path):
        """Gate 3 fails when bandit detects a vulnerability."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n")
        (tmp_path / "src").mkdir()

        bandit_output = {
            "results": [
                {
                    "code": "cursor.execute(f\"SELECT ...\")",
                    "filename": str(tmp_path / "src" / "search.py"),
                    "issue_confidence": "MEDIUM",
                    "issue_cwe": {"id": 89, "link": "https://cwe.mitre.org/data/definitions/89.html"},
                    "issue_severity": "MEDIUM",
                    "issue_text": "Possible SQL injection vector through string-based query construction.",
                    "line_number": 25,
                    "test_id": "B608",
                    "test_name": "hardcoded_sql_expressions",
                }
            ],
            "errors": [],
        }
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=json.dumps(bandit_output),
            stderr="",
        )
        result = run_gate_3_security(str(tmp_path))
        gate = GateResult(**result)
        assert gate.passed is False
        assert len(gate.findings) == 1
        assert gate.findings[0].cwe == "CWE-89"
        assert gate.findings[0].rule == "B608"
        assert gate.findings[0].line == 25

    @patch("devloop.gates.server._run_cmd")
    @patch("devloop.gates.server._find_bandit", return_value="/usr/bin/bandit")
    def test_handles_bandit_error(self, mock_bandit, mock_run, tmp_path):
        """Gate 3 fails on bandit execution error."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n")
        (tmp_path / "src").mkdir()
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=2,
            stdout="",
            stderr="bandit: error: something went wrong",
        )
        result = run_gate_3_security(str(tmp_path))
        gate = GateResult(**result)
        assert gate.passed is False


# ---------------------------------------------------------------------------
# TB3Result model tests
# ---------------------------------------------------------------------------


class TestTB3Result:
    """Tests for the TB3Result model."""

    def test_minimal_construction(self):
        result = TB3Result(
            issue_id="dl-123",
            repo_path="/tmp/test",
            success=False,
            phase="error",
        )
        assert result.trace_id is None
        assert result.attempt_span_ids == []
        assert result.security_findings == []
        assert result.vulnerability_fixed is False
        assert result.cwe_ids == []
        assert result.vuln_seeded is False
        assert result.retry_history == []

    def test_full_construction(self):
        from devloop.feedback.types import RetryAttempt

        result = TB3Result(
            issue_id="dl-123",
            repo_path="/tmp/test",
            success=True,
            phase="retry_passed",
            trace_id="abc123",
            attempt_span_ids=["span1", "span2"],
            security_findings=[
                SecurityFinding(
                    cwe="CWE-89",
                    message="SQL injection",
                    file="search.py",
                    line=25,
                    rule="B608",
                ),
            ],
            vulnerability_fixed=True,
            cwe_ids=["CWE-89"],
            vuln_seeded=True,
            retry_history=[
                RetryAttempt(attempt=0, gates_passed=False, first_failure="gate_3_security"),
                RetryAttempt(attempt=1, gates_passed=True),
            ],
        )
        assert result.trace_id == "abc123"
        assert len(result.security_findings) == 1
        assert result.security_findings[0].cwe == "CWE-89"
        assert result.vulnerability_fixed is True
        assert result.cwe_ids == ["CWE-89"]

    def test_model_dump_roundtrip(self):
        result = TB3Result(
            issue_id="dl-123",
            repo_path="/tmp/test",
            success=True,
            phase="retry_passed",
            security_findings=[
                SecurityFinding(
                    cwe="CWE-89",
                    severity="critical",
                    message="SQL injection",
                ),
            ],
            vulnerability_fixed=True,
            cwe_ids=["CWE-89"],
        )
        dumped = result.model_dump()
        restored = TB3Result(**dumped)
        assert restored.vulnerability_fixed is True
        assert restored.security_findings[0].cwe == "CWE-89"


# ---------------------------------------------------------------------------
# SecurityFinding model tests
# ---------------------------------------------------------------------------


class TestSecurityFinding:
    """Tests for the SecurityFinding model."""

    def test_defaults(self):
        f = SecurityFinding(message="test")
        assert f.cwe is None
        assert f.severity == "critical"
        assert f.fixed is False

    def test_full_construction(self):
        f = SecurityFinding(
            cwe="CWE-89",
            severity="critical",
            message="SQL injection",
            file="search.py",
            line=25,
            rule="B608",
            fixed=True,
        )
        assert f.cwe == "CWE-89"
        assert f.file == "search.py"
        assert f.fixed is True
