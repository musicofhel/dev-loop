"""Planted-defect regression suite for Tier 2 checkpoint gates.

Parametrizes over corpus scenarios, creates temp git repos with staged
changes, runs `dl checkpoint --json`, and asserts gate results match
expected outcomes from expected.yaml.
"""

import pytest

from .conftest import SCENARIOS, create_git_repo, run_checkpoint


# ── Parametrized test over all corpus scenarios ──────────────────


@pytest.mark.parametrize("scenario", SCENARIOS, ids=SCENARIOS)
def test_checkpoint_scenario(tmp_path, dl_binary, scenario):
    """Run checkpoint on a corpus scenario and verify gate results."""
    # Determine config overrides based on scenario
    config = _scenario_config(scenario)
    repo_path, expected = create_git_repo(tmp_path, scenario, config)

    result = run_checkpoint(dl_binary, repo_path)
    expected_gates = expected.get("expected_gates", {})

    # Build a lookup of actual gate results by name
    actual_gates = {gr["gate"]: gr for gr in result.get("gate_results", [])}

    failures = []
    for gate_name, expectations in expected_gates.items():
        actual = actual_gates.get(gate_name)

        # Gate may not have run (fail-fast stops after first failure)
        if actual is None:
            # If we expected pass, the gate might have been skipped because
            # an earlier gate failed — that's OK for pass expectations
            if expectations.get("passed") is True:
                continue
            # If we expected fail and the overall checkpoint failed,
            # check if a different gate failed first (fail-fast)
            if not result.get("passed", True):
                # Some gate failed, just not this one — acceptable if
                # the first_failure is from a different gate
                first_fail = result.get("first_failure", "")
                if first_fail and first_fail != gate_name:
                    continue
            failures.append(
                f"Gate '{gate_name}': expected to run but not found in results. "
                f"Gates run: {list(actual_gates.keys())}"
            )
            continue

        # Check passed/failed
        expected_passed = expectations.get("passed")
        if expected_passed is not None and actual["passed"] != expected_passed:
            failures.append(
                f"Gate '{gate_name}': expected passed={expected_passed}, "
                f"got passed={actual['passed']}. "
                f"Reason: {actual.get('reason', 'none')}. "
                f"Findings: {actual.get('findings', [])}"
            )

        # Check reason contains (if specified)
        reason_contains = expectations.get("reason_contains")
        if reason_contains and actual.get("reason"):
            if reason_contains.lower() not in actual["reason"].lower():
                failures.append(
                    f"Gate '{gate_name}': expected reason to contain "
                    f"'{reason_contains}', got: {actual['reason']}"
                )

    if failures:
        # Dump full result for debugging
        import json

        detail = json.dumps(result, indent=2)
        pytest.fail(
            f"Scenario '{scenario}' failed:\n"
            + "\n".join(f"  - {f}" for f in failures)
            + f"\n\nFull checkpoint result:\n{detail}"
        )


# ── Scenario-specific config overrides ───────────────────────────


def _scenario_config(scenario: str) -> dict | None:
    """Return .devloop.yaml overrides for specific scenarios.

    Note: RepoConfig uses skip_gates (not gates) to exclude gates.
    Default gates are: sanity, semgrep, secrets, atdd, review.
    """
    ALL_GATES = ["sanity", "semgrep", "secrets", "atdd", "review"]

    def skip_all_except(*keep):
        return {"checkpoint": {"skip_gates": [g for g in ALL_GATES if g not in keep]}}

    if scenario == "missing_tests":
        # Need a test command that will fail (no test files)
        return {
            "checkpoint": {
                "test_command": "python -m pytest --co -q",
                "skip_gates": [g for g in ALL_GATES if g != "sanity"],
            }
        }
    if scenario in ("has_sql_injection", "has_xss_vulnerability"):
        return skip_all_except("semgrep")
    if scenario in (
        "has_leaked_aws_key",
        "has_leaked_github_pat",
        "has_hardcoded_password",
        "has_private_key",
        "has_db_connection_string",
        "has_slack_token",
        "commented_secret",
        "placeholder_secret",
    ):
        return skip_all_except("secrets")
    if scenario in ("clean_python_project", "clean_rust_project"):
        # Skip sanity (would need proper test runner in temp repo)
        return skip_all_except("semgrep", "secrets")
    return None


# ── Focused gate tests ───────────────────────────────────────────


class TestSecretsGate:
    """Focused tests for the secrets gate."""

    SECRETS_SHOULD_FAIL = [
        "has_leaked_aws_key",
        "has_private_key",
        "has_slack_token",
    ]

    SECRETS_SHOULD_PASS = [
        "clean_python_project",
        "clean_rust_project",
        "placeholder_secret",
    ]

    # Known gaps: gitleaks/betterleaks don't detect these patterns
    SECRETS_KNOWN_GAPS = [
        "has_leaked_github_pat",       # ghp_ tokens not in rule set
        "has_hardcoded_password",      # generic passwords not detected
        "has_db_connection_string",    # connection strings not detected
    ]

    @pytest.mark.parametrize("scenario", SECRETS_SHOULD_FAIL)
    def test_secrets_gate_catches_leaked_secrets(self, tmp_path, dl_binary, scenario):
        """Secrets gate must catch real leaked secrets."""
        config = {"checkpoint": {"skip_gates": ["sanity", "semgrep", "atdd", "review"]}}
        repo_path, _ = create_git_repo(tmp_path, scenario, config)
        result = run_checkpoint(dl_binary, repo_path)

        secrets_gate = _find_gate(result, "secrets")
        assert secrets_gate is not None, f"Secrets gate didn't run for {scenario}"
        assert not secrets_gate["passed"], (
            f"Secrets gate should have FAILED for {scenario} but passed. "
            f"Reason: {secrets_gate.get('reason')}"
        )

    @pytest.mark.parametrize("scenario", SECRETS_KNOWN_GAPS)
    def test_secrets_gate_known_gaps(self, tmp_path, dl_binary, scenario):
        """Document known gaps: secrets that gitleaks/betterleaks don't detect."""
        config = {"checkpoint": {"skip_gates": ["sanity", "semgrep", "atdd", "review"]}}
        repo_path, _ = create_git_repo(tmp_path, scenario, config)
        result = run_checkpoint(dl_binary, repo_path)

        secrets_gate = _find_gate(result, "secrets")
        assert secrets_gate is not None, f"Secrets gate didn't run for {scenario}"
        # These currently PASS (known gap) — if a future tool update catches them,
        # the test will fail, alerting us to update expectations
        assert secrets_gate["passed"], (
            f"Known gap {scenario} is now being detected! Update expectations. "
            f"Findings: {secrets_gate.get('findings', [])}"
        )

    @pytest.mark.parametrize("scenario", SECRETS_SHOULD_PASS)
    def test_secrets_gate_allows_clean_code(self, tmp_path, dl_binary, scenario):
        """Secrets gate must not flag clean code."""
        config = {"checkpoint": {"skip_gates": ["sanity", "semgrep", "atdd", "review"]}}
        repo_path, _ = create_git_repo(tmp_path, scenario, config)
        result = run_checkpoint(dl_binary, repo_path)

        secrets_gate = _find_gate(result, "secrets")
        assert secrets_gate is not None, f"Secrets gate didn't run for {scenario}"
        assert secrets_gate["passed"], (
            f"Secrets gate should have PASSED for {scenario} but failed. "
            f"Reason: {secrets_gate.get('reason')}. "
            f"Findings: {secrets_gate.get('findings', [])}"
        )


class TestSemgrepGate:
    """Focused tests for the semgrep SAST gate."""

    SEMGREP_SHOULD_FAIL = [
        "has_sql_injection",
        "has_xss_vulnerability",
    ]

    SEMGREP_SHOULD_PASS = [
        "clean_python_project",
        "clean_rust_project",
        "placeholder_secret",
    ]

    @pytest.mark.parametrize("scenario", SEMGREP_SHOULD_FAIL)
    def test_semgrep_catches_vulnerabilities(self, tmp_path, dl_binary, scenario):
        """Semgrep gate must catch known vulnerability patterns."""
        config = {"checkpoint": {"skip_gates": ["sanity", "secrets", "atdd", "review"]}}
        repo_path, _ = create_git_repo(tmp_path, scenario, config)
        result = run_checkpoint(dl_binary, repo_path)

        semgrep_gate = _find_gate(result, "semgrep")
        assert semgrep_gate is not None, f"Semgrep gate didn't run for {scenario}"
        assert not semgrep_gate["passed"], (
            f"Semgrep gate should have FAILED for {scenario} but passed. "
            f"Reason: {semgrep_gate.get('reason')}"
        )

    @pytest.mark.parametrize("scenario", SEMGREP_SHOULD_PASS)
    def test_semgrep_allows_clean_code(self, tmp_path, dl_binary, scenario):
        """Semgrep gate must not flag clean code."""
        config = {"checkpoint": {"skip_gates": ["sanity", "secrets", "atdd", "review"]}}
        repo_path, _ = create_git_repo(tmp_path, scenario, config)
        result = run_checkpoint(dl_binary, repo_path)

        semgrep_gate = _find_gate(result, "semgrep")
        assert semgrep_gate is not None, f"Semgrep gate didn't run for {scenario}"
        assert semgrep_gate["passed"], (
            f"Semgrep gate should have PASSED for {scenario} but failed. "
            f"Reason: {semgrep_gate.get('reason')}. "
            f"Findings: {semgrep_gate.get('findings', [])}"
        )


class TestSanityGate:
    """Focused tests for the sanity (test runner) gate."""

    def test_missing_tests_fails(self, tmp_path, dl_binary):
        """Sanity gate fails when test command finds no tests."""
        config = {
            "checkpoint": {
                "test_command": "python -m pytest --co -q",
                "gates": ["sanity"],
            }
        }
        repo_path, _ = create_git_repo(tmp_path, "missing_tests", config)
        result = run_checkpoint(dl_binary, repo_path)

        sanity_gate = _find_gate(result, "sanity")
        assert sanity_gate is not None, "Sanity gate didn't run"
        assert not sanity_gate["passed"], (
            f"Sanity gate should have FAILED but passed. "
            f"Reason: {sanity_gate.get('reason')}"
        )


# ── Helpers ──────────────────────────────────────────────────────


def _find_gate(result: dict, gate_name: str) -> dict | None:
    """Find a gate result by name."""
    for gr in result.get("gate_results", []):
        if gr["gate"] == gate_name:
            return gr
    return None
