"""Tests for Gate 0.1 — Differential Test Gate.

Tests parsers (_parse_pytest_junit, _parse_node_json, _parse_cargo_test),
test runner helper (_run_tests_with_parsing), config loader (_is_differential_enabled),
and the gate itself (run_gate_01_differential).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# JUnit XML parser tests
# ---------------------------------------------------------------------------


class TestParsePytestJunit:
    """Tests for _parse_pytest_junit()."""

    def test_parse_clean_junit(self):
        from devloop.gates.server import _parse_pytest_junit

        xml = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="4" failures="1" errors="0">
  <testcase classname="tests.test_math" name="test_add" time="0.01"/>
  <testcase classname="tests.test_math" name="test_sub" time="0.01"/>
  <testcase classname="tests.test_math" name="test_mul" time="0.01"/>
  <testcase classname="tests.test_math" name="test_div" time="0.02">
    <failure message="ZeroDivisionError">Traceback ...</failure>
  </testcase>
</testsuite>"""
        outcomes = _parse_pytest_junit(xml)
        assert len(outcomes) == 4
        passed = [o for o in outcomes if o["passed"]]
        failed = [o for o in outcomes if not o["passed"]]
        assert len(passed) == 3
        assert len(failed) == 1
        assert failed[0]["name"] == "tests.test_math.test_div"
        assert failed[0]["error_message"] == "ZeroDivisionError"

    def test_parse_empty_junit(self):
        from devloop.gates.server import _parse_pytest_junit

        xml = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="0" failures="0" errors="0">
</testsuite>"""
        outcomes = _parse_pytest_junit(xml)
        assert outcomes == []

    def test_parse_malformed_xml(self):
        from devloop.gates.server import _parse_pytest_junit

        outcomes = _parse_pytest_junit("<not valid xml!!! <<<")
        assert outcomes == []


# ---------------------------------------------------------------------------
# Jest/Vitest JSON parser tests
# ---------------------------------------------------------------------------


class TestParseNodeJson:
    """Tests for _parse_node_json()."""

    def test_parse_jest_json(self):
        from devloop.gates.server import _parse_node_json

        data = {
            "testResults": [
                {
                    "testResults": [
                        {
                            "fullName": "math > add",
                            "status": "passed",
                            "failureMessages": [],
                        },
                        {
                            "fullName": "math > divide",
                            "status": "failed",
                            "failureMessages": ["Expected 0 to be 1"],
                        },
                    ]
                }
            ]
        }
        outcomes = _parse_node_json(json.dumps(data))
        assert len(outcomes) == 2
        passed = [o for o in outcomes if o["passed"]]
        failed = [o for o in outcomes if not o["passed"]]
        assert len(passed) == 1
        assert len(failed) == 1
        assert failed[0]["name"] == "math > divide"
        assert "Expected 0 to be 1" in failed[0]["error_message"]

    def test_parse_invalid_json(self):
        from devloop.gates.server import _parse_node_json

        outcomes = _parse_node_json("this is not json {{{")
        assert outcomes == []


# ---------------------------------------------------------------------------
# Cargo test output parser tests
# ---------------------------------------------------------------------------


class TestParseCargoTest:
    """Tests for _parse_cargo_test()."""

    def test_parse_cargo_output(self):
        from devloop.gates.server import _parse_cargo_test

        output = """\
running 4 tests
test utils::test_add ... ok
test utils::test_sub ... ok
test utils::test_mul ... FAILED
test utils::test_ignored ... ignored

failures:
    utils::test_mul

test result: FAILED. 2 passed; 1 failed; 1 ignored
"""
        outcomes = _parse_cargo_test(output)
        assert len(outcomes) == 3  # ignored is excluded
        passed = [o for o in outcomes if o["passed"]]
        failed = [o for o in outcomes if not o["passed"]]
        assert len(passed) == 2
        assert len(failed) == 1
        assert failed[0]["name"] == "utils::test_mul"

    def test_parse_cargo_empty(self):
        from devloop.gates.server import _parse_cargo_test

        outcomes = _parse_cargo_test("")
        assert outcomes == []


# ---------------------------------------------------------------------------
# _run_tests_with_parsing tests
# ---------------------------------------------------------------------------


class TestRunTestsWithParsing:
    """Tests for _run_tests_with_parsing()."""

    @patch("devloop.gates.server._run_cmd")
    def test_python_uses_junitxml(self, mock_run_cmd):
        from devloop.gates.server import _run_tests_with_parsing

        mock_run_cmd.return_value = MagicMock(returncode=0, stdout="", stderr="")
        rc, outcomes = _run_tests_with_parsing("/tmp/fake-worktree", "python")

        # Verify the pytest command included --junitxml
        call_args = mock_run_cmd.call_args_list[0]
        cmd = call_args[0][0]  # first positional arg is the args list
        assert any("--junitxml=" in arg for arg in cmd), f"Expected --junitxml in {cmd}"

    @patch("devloop.gates.server._run_cmd")
    def test_node_uses_json_flag(self, mock_run_cmd):
        from devloop.gates.server import _run_tests_with_parsing

        mock_run_cmd.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
        rc, outcomes = _run_tests_with_parsing("/tmp/fake-worktree", "node")

        call_args = mock_run_cmd.call_args_list[0]
        cmd = call_args[0][0]
        assert "--json" in cmd, f"Expected --json in {cmd}"


# ---------------------------------------------------------------------------
# Gate 0.1 differential tests
# ---------------------------------------------------------------------------


def _mock_run_cmd_factory(
    *,
    merge_base_sha: str = "abc123",
    merge_base_ok: bool = True,
    log_output: str = "def456 agent commit",
    branch_name: str = "feature/test",
    checkout_ok: bool = True,
    baseline_test_rc: int = 0,
    baseline_junit_xml: str = "",
    head_test_rc: int = 0,
    head_junit_xml: str = "",
    project_type: str = "python",
):
    """Build a side_effect callable for mocking _run_cmd calls in Gate 0.1.

    The call sequence for a python project is:
      0: git merge-base HEAD main
      1: git log {sha}..HEAD --oneline
      2: git rev-parse --abbrev-ref HEAD
      3: git checkout {sha} --quiet
      4: uv sync --dev --reinstall  (baseline deps)
      5: uv run pytest ... --junitxml=...  (baseline tests)
      6: git checkout {branch} --quiet  (restore)
      7: uv sync --dev --reinstall  (head deps)
      8: uv run pytest ... --junitxml=...  (head tests)
    """
    call_idx = {"n": 0}
    baseline_xml_written = {"done": False}
    head_xml_written = {"done": False}

    def side_effect(args, cwd=None, timeout=120):
        idx = call_idx["n"]
        call_idx["n"] += 1

        # merge-base
        if idx == 0:
            return MagicMock(
                returncode=0 if merge_base_ok else 1,
                stdout=merge_base_sha + "\n" if merge_base_ok else "",
                stderr="" if merge_base_ok else "fatal: no merge base",
            )
        # git log
        if idx == 1:
            return MagicMock(returncode=0, stdout=log_output, stderr="")
        # git rev-parse --abbrev-ref HEAD
        if idx == 2:
            return MagicMock(returncode=0, stdout=branch_name + "\n", stderr="")
        # git checkout baseline
        if idx == 3:
            return MagicMock(
                returncode=0 if checkout_ok else 1,
                stdout="",
                stderr="" if checkout_ok else "error: checkout failed",
            )
        # uv sync (baseline deps)
        if idx == 4:
            return MagicMock(returncode=0, stdout="", stderr="")
        # baseline pytest with --junitxml
        if idx == 5:
            # Write the junit XML to the temp file specified in the command
            if project_type == "python" and baseline_junit_xml:
                for arg in args:
                    if isinstance(arg, str) and arg.startswith("--junitxml="):
                        junit_path = arg.split("=", 1)[1]
                        Path(junit_path).write_text(baseline_junit_xml)
                        baseline_xml_written["done"] = True
                        break
            return MagicMock(returncode=baseline_test_rc, stdout="", stderr="")
        # git checkout restore
        if idx == 6:
            return MagicMock(returncode=0, stdout="", stderr="")
        # uv sync (head deps)
        if idx == 7:
            return MagicMock(returncode=0, stdout="", stderr="")
        # head pytest with --junitxml
        if idx == 8:
            if project_type == "python" and head_junit_xml:
                for arg in args:
                    if isinstance(arg, str) and arg.startswith("--junitxml="):
                        junit_path = arg.split("=", 1)[1]
                        Path(junit_path).write_text(head_junit_xml)
                        head_xml_written["done"] = True
                        break
            return MagicMock(returncode=head_test_rc, stdout="", stderr="")

        # Default fallback
        return MagicMock(returncode=0, stdout="", stderr="")

    return side_effect


def _make_junit_xml(test_results: list[tuple[str, str, bool]]) -> str:
    """Build JUnit XML from list of (classname, testname, passed) tuples."""
    cases = []
    failures = 0
    for classname, testname, passed in test_results:
        if passed:
            cases.append(f'  <testcase classname="{classname}" name="{testname}" time="0.01"/>')
        else:
            failures += 1
            cases.append(
                f'  <testcase classname="{classname}" name="{testname}" time="0.01">'
                f'\n    <failure message="AssertionError">assert False</failure>'
                f"\n  </testcase>"
            )
    return (
        f'<?xml version="1.0" encoding="utf-8"?>\n'
        f'<testsuite name="pytest" tests="{len(test_results)}" failures="{failures}">\n'
        + "\n".join(cases)
        + "\n</testsuite>"
    )


class TestGate01Differential:
    """Tests for run_gate_01_differential()."""

    @patch("devloop.gates.server._run_cmd")
    @patch("devloop.gates.server._detect_project_type", return_value="python")
    def test_all_preexisting_passes(self, mock_detect, mock_run_cmd, tmp_path):
        """Baseline {A,B} fails, HEAD {A,B} fails -> PASS, warnings for preexisting."""
        from devloop.gates.server import run_gate_01_differential

        worktree = tmp_path / "repo"
        worktree.mkdir()

        baseline_xml = _make_junit_xml([
            ("tests.mod", "test_a", False),
            ("tests.mod", "test_b", False),
            ("tests.mod", "test_c", True),
        ])
        head_xml = _make_junit_xml([
            ("tests.mod", "test_a", False),
            ("tests.mod", "test_b", False),
            ("tests.mod", "test_c", True),
        ])

        mock_run_cmd.side_effect = _mock_run_cmd_factory(
            baseline_test_rc=1,
            baseline_junit_xml=baseline_xml,
            head_test_rc=1,
            head_junit_xml=head_xml,
        )

        result = run_gate_01_differential(str(worktree))

        assert result["passed"] is True
        assert result["gate_name"] == "gate_01_differential"
        assert result["differential"]["new_failures"] == []
        assert len(result["differential"]["preexisting_failures"]) == 2
        # Check warnings for preexisting
        warnings = [f for f in result["findings"] if f["severity"] == "warning"]
        assert len(warnings) == 2

    @patch("devloop.gates.server._run_cmd")
    @patch("devloop.gates.server._detect_project_type", return_value="python")
    def test_new_failure_fails(self, mock_detect, mock_run_cmd, tmp_path):
        """Baseline {A} fails, HEAD {A,B} fails -> FAIL, critical for B."""
        from devloop.gates.server import run_gate_01_differential

        worktree = tmp_path / "repo"
        worktree.mkdir()

        baseline_xml = _make_junit_xml([
            ("tests.mod", "test_a", False),
            ("tests.mod", "test_b", True),
            ("tests.mod", "test_c", True),
        ])
        head_xml = _make_junit_xml([
            ("tests.mod", "test_a", False),
            ("tests.mod", "test_b", False),
            ("tests.mod", "test_c", True),
        ])

        mock_run_cmd.side_effect = _mock_run_cmd_factory(
            baseline_test_rc=1,
            baseline_junit_xml=baseline_xml,
            head_test_rc=1,
            head_junit_xml=head_xml,
        )

        result = run_gate_01_differential(str(worktree))

        assert result["passed"] is False
        assert "tests.mod.test_b" in result["differential"]["new_failures"]
        criticals = [f for f in result["findings"] if f["severity"] == "critical"]
        assert any("test_b" in c["message"] for c in criticals)

    @patch("devloop.gates.server._run_cmd")
    @patch("devloop.gates.server._detect_project_type", return_value="python")
    def test_agent_fixed_test(self, mock_detect, mock_run_cmd, tmp_path):
        """Baseline {A,B} fails, HEAD {A} fails -> PASS, info for fixed B."""
        from devloop.gates.server import run_gate_01_differential

        worktree = tmp_path / "repo"
        worktree.mkdir()

        baseline_xml = _make_junit_xml([
            ("tests.mod", "test_a", False),
            ("tests.mod", "test_b", False),
        ])
        head_xml = _make_junit_xml([
            ("tests.mod", "test_a", False),
            ("tests.mod", "test_b", True),
        ])

        mock_run_cmd.side_effect = _mock_run_cmd_factory(
            baseline_test_rc=1,
            baseline_junit_xml=baseline_xml,
            head_test_rc=1,
            head_junit_xml=head_xml,
        )

        result = run_gate_01_differential(str(worktree))

        assert result["passed"] is True
        assert result["differential"]["new_failures"] == []
        info_findings = [f for f in result["findings"] if f["severity"] == "info"]
        assert any("fixed" in f["message"].lower() and "test_b" in f["message"] for f in info_findings)

    @patch("devloop.gates.server._run_cmd")
    def test_no_commits_skips(self, mock_run_cmd, tmp_path):
        """Git log returns empty -> skipped."""
        from devloop.gates.server import run_gate_01_differential

        worktree = tmp_path / "repo"
        worktree.mkdir()

        mock_run_cmd.side_effect = [
            MagicMock(returncode=0, stdout="abc123\n", stderr=""),  # merge-base
            MagicMock(returncode=0, stdout="", stderr=""),  # git log (empty = no commits)
        ]

        result = run_gate_01_differential(str(worktree))

        assert result["passed"] is True
        assert result["skipped"] is True

    @patch("devloop.gates.server._run_cmd")
    def test_merge_base_not_found(self, mock_run_cmd, tmp_path):
        """Git merge-base fails -> FAIL with error."""
        from devloop.gates.server import run_gate_01_differential

        worktree = tmp_path / "repo"
        worktree.mkdir()

        mock_run_cmd.side_effect = [
            MagicMock(returncode=1, stdout="", stderr="fatal"),  # merge-base HEAD main
            MagicMock(returncode=1, stdout="", stderr="fatal"),  # merge-base HEAD origin/main
        ]

        result = run_gate_01_differential(str(worktree))

        assert result["passed"] is False
        assert any("merge-base" in f["message"].lower() for f in result["findings"])

    @patch("devloop.gates.server._run_cmd")
    @patch("devloop.gates.server._detect_project_type", return_value="python")
    def test_unparsable_output_fails(self, mock_detect, mock_run_cmd, tmp_path):
        """Empty outcomes with non-zero RC -> FAIL conservative."""
        from devloop.gates.server import run_gate_01_differential

        worktree = tmp_path / "repo"
        worktree.mkdir()

        # Baseline produces valid output, HEAD produces nothing parseable
        baseline_xml = _make_junit_xml([("tests.mod", "test_a", True)])

        mock_run_cmd.side_effect = _mock_run_cmd_factory(
            baseline_test_rc=0,
            baseline_junit_xml=baseline_xml,
            head_test_rc=1,
            head_junit_xml="",  # No XML written -> no outcomes parsed
        )

        result = run_gate_01_differential(str(worktree))

        assert result["passed"] is False
        assert result["differential"]["baseline_parse_error"] is True

    def test_worktree_not_found(self, tmp_path):
        """Bad path -> FAIL."""
        from devloop.gates.server import run_gate_01_differential

        result = run_gate_01_differential(str(tmp_path / "does-not-exist"))

        assert result["passed"] is False
        assert result["gate_name"] == "gate_01_differential"
        assert any("not found" in f["message"].lower() for f in result["findings"])


# ---------------------------------------------------------------------------
# _is_differential_enabled tests
# ---------------------------------------------------------------------------


class TestIsDifferentialEnabled:
    """Tests for _is_differential_enabled()."""

    def test_enabled_from_config(self, tmp_path):
        from devloop.gates.server import _is_differential_enabled

        # Create worktree with metadata
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        metadata = {"repo_path": "/home/user/OOTestProject1"}
        (worktree / ".dev-loop-metadata.json").write_text(json.dumps(metadata))

        # Build a fake project root so that Path(__file__).resolve().parents[3]
        # points to tmp_path, and config/projects/OOTestProject1.yaml is there.
        fake_root = tmp_path
        config_dir = fake_root / "config" / "projects"
        config_dir.mkdir(parents=True)
        (config_dir / "OOTestProject1.yaml").write_text(
            "quality_gates:\n  differential:\n    enabled: true\n"
        )

        fake_server = fake_root / "src" / "devloop" / "gates" / "server.py"
        fake_server.parent.mkdir(parents=True, exist_ok=True)
        fake_server.touch()

        import devloop.gates.server as srv

        original_file = srv.__file__
        try:
            srv.__file__ = str(fake_server)
            result = _is_differential_enabled(str(worktree))
        finally:
            srv.__file__ = original_file

        assert result is True

    def test_disabled_by_default(self, tmp_path):
        from devloop.gates.server import _is_differential_enabled

        # Create worktree with metadata pointing to a project with no differential key
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        metadata = {"repo_path": "/home/user/some-project"}
        (worktree / ".dev-loop-metadata.json").write_text(json.dumps(metadata))

        # Config exists but no differential section
        fake_root = tmp_path
        config_dir = fake_root / "config" / "projects"
        config_dir.mkdir(parents=True)
        (config_dir / "some-project.yaml").write_text("quality_gates:\n  sanity:\n    enabled: true\n")

        fake_server = fake_root / "src" / "devloop" / "gates" / "server.py"
        fake_server.parent.mkdir(parents=True, exist_ok=True)
        fake_server.touch()

        import devloop.gates.server as srv

        original_file = srv.__file__
        try:
            srv.__file__ = str(fake_server)
            result = _is_differential_enabled(str(worktree))
        finally:
            srv.__file__ = original_file

        assert result is False
