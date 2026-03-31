"""Quality-gates MCP server — sequential automated checks on agent output.

This is Layer 4 of the dev-loop harness. Every agent output passes through
a gauntlet of automated checks before it becomes a PR. Gates run sequentially
— fail fast, fail cheap.

Gates:
  Gate 0   (Sanity)         — compile + test
  Gate 0.1 (Differential)   — baseline vs HEAD test comparison (opt-in, on Gate 0 fail)
  Gate 0.5 (Relevance)      — keyword overlap between issue and diff
  Gate 2   (Secrets)        — gitleaks scan
  Gate 2.5 (Dangerous Ops)  — DB migrations, CI/CD, auth, lock files
  Gate 3   (Security)       — bandit SAST scan
  Gate 4   (Review)         — LLM-as-judge code review
  Gate 5   (Cost)           — turn/token usage check (called separately)

Run standalone:  uv run python -m devloop.gates.server
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import yaml
from fastmcp import FastMCP
from opentelemetry import trace

from devloop.gates.types import DifferentialResult, Finding, GateResult, GateSuiteResult

# ---------------------------------------------------------------------------
# OTel tracer for gates layer
# ---------------------------------------------------------------------------

tracer = trace.get_tracer("gates", "0.1.0")

# ---------------------------------------------------------------------------
# Worktree validation helper (E-6)
# ---------------------------------------------------------------------------


def _verify_worktree(worktree_path: str, gate_name: str) -> dict | None:
    """Returns GateResult error dict if worktree is invalid, None if OK."""
    wt = Path(worktree_path)
    if not wt.is_dir():
        return GateResult(
            gate_name=gate_name,
            passed=False,
            findings=[Finding(severity="critical", message=f"Worktree not found: {wt}")],
            error=f"Worktree not found: {wt}",
        ).model_dump()
    if not (wt / ".git").exists():
        return GateResult(
            gate_name=gate_name,
            passed=False,
            findings=[Finding(severity="critical", message=f"Worktree is not a git repo: {wt}")],
            error=f"Not a git repository: {wt}",
        ).model_dump()
    return None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "review-gate.yaml"
_GITLEAKS_FALLBACK = Path.home() / ".local" / "bin" / "gitleaks"


def _find_gitleaks() -> str | None:
    """Find gitleaks binary: PATH first, then ~/.local/bin fallback."""
    found = shutil.which("gitleaks")
    if found:
        return found
    if _GITLEAKS_FALLBACK.exists():
        return str(_GITLEAKS_FALLBACK)
    return None


def _find_bandit() -> str | None:
    """Find bandit binary on PATH."""
    return shutil.which("bandit")


def _load_review_config() -> dict:
    """Load the review gate YAML config."""
    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    return {}


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="quality-gates",
    instructions=(
        "Quality gates layer for dev-loop. "
        "Use these tools to run automated checks on agent output before PR creation. "
        "Gates run sequentially in fail-fast order: sanity → secrets → review."
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_cmd(
    args: list[str],
    cwd: str | Path | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess command with timeout in a clean env."""
    env = os.environ.copy()
    # Remove dev-loop's own venv so uv/pytest in the worktree use their own
    env.pop("VIRTUAL_ENV", None)
    # Prevent nested Claude Code session errors (L6 fix)
    env.pop("CLAUDECODE", None)
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=timeout,
        env=env,
    )


def _detect_project_type(worktree: Path) -> str:
    """Detect whether the worktree is a Node/Python/Rust project."""
    if (worktree / "package.json").exists():
        return "node"
    if (worktree / "pyproject.toml").exists():
        return "python"
    if (worktree / "Cargo.toml").exists():
        return "rust"
    return "unknown"


def _build_review_prompt(diff: str, issue_title: str, issue_description: str, config: dict) -> str:
    """Build the LLM-as-judge review prompt from config and context."""
    review_cfg = config.get("review", {})
    criteria = review_cfg.get("criteria", [])
    severity_levels = review_cfg.get("severity_levels", {})

    criteria_text = "\n".join(f"  - {c}" for c in criteria) if criteria else "  (none configured)"
    severity_text = "\n".join(
        f"  - {level}: {action}" for level, action in severity_levels.items()
    ) if severity_levels else "  (none configured)"

    return f"""You are a senior code reviewer performing an automated quality gate check.

## Issue Context
**Title:** {issue_title}
**Description:** {issue_description}

## Review Criteria
Check the diff for the following issues:
{criteria_text}

## Severity Levels
{severity_text}

## Instructions
1. Review the diff below carefully.
2. For each finding, classify it as: critical, warning, or suggestion.
3. Respond with ONLY valid JSON — no markdown fences, no explanation outside the JSON.
4. Use this exact schema:

{{
  "findings": [
    {{
      "severity": "critical|warning|suggestion",
      "message": "description of the issue",
      "file": "path/to/file or null",
      "line": line_number_or_null,
      "rule": "which criteria this violates"
    }}
  ],
  "summary": "one-line overall assessment"
}}

If there are no findings, return: {{"findings": [], "summary": "No issues found."}}

## Diff to Review
```
{diff}
```"""


# ---------------------------------------------------------------------------
# Gate 0: Sanity Check
# ---------------------------------------------------------------------------


@mcp.tool(
    description=(
        "Gate 0 — Sanity check: verifies the worktree has changes and that "
        "compile/test commands pass. Detects project type (Node/Python/Rust) "
        "and runs the appropriate test suite."
    ),
    tags={"gates", "sanity"},
)
def run_gate_0_sanity(worktree_path: str) -> dict:
    """Run compile + test sanity checks on the worktree."""
    with tracer.start_as_current_span(
        "gates.gate_0_sanity",
        attributes={"gate.name": "sanity", "gate.order": 0},
    ) as span:
        start = time.monotonic()
        worktree = Path(worktree_path)
        findings: list[Finding] = []
        passed = True

        # --- Check worktree exists ---
        if not worktree.is_dir():
            elapsed = time.monotonic() - start
            span.set_status(trace.StatusCode.ERROR, "Worktree does not exist")
            return GateResult(
                gate_name="gate_0_sanity",
                passed=False,
                findings=[Finding(severity="critical", message=f"Worktree not found: {worktree}")],
                duration_seconds=round(elapsed, 3),
                error=f"Worktree not found: {worktree}",
            ).model_dump()

        # --- Check: any file changes (unstaged, staged, or committed) ---
        diff_output = ""
        # 1. Unstaged changes
        r = _run_cmd(["git", "diff", "--stat"], cwd=worktree)
        if r.stdout.strip():
            diff_output = r.stdout.strip()
        # 2. Staged changes
        if not diff_output:
            r = _run_cmd(["git", "diff", "--cached", "--stat"], cwd=worktree)
            if r.stdout.strip():
                diff_output = r.stdout.strip()
        # 3. Commits on this branch vs parent (agent may have committed)
        if not diff_output:
            # Count commits and use a safe lookback (handles short histories)
            count_r = _run_cmd(
                ["git", "rev-list", "--count", "HEAD"],
                cwd=worktree,
            )
            if count_r.returncode == 0 and count_r.stdout.strip():
                total = int(count_r.stdout.strip())
                lookback = min(10, total - 1)
                if lookback > 0:
                    r = _run_cmd(
                        ["git", "diff", f"HEAD~{lookback}", "HEAD", "--stat"],
                        cwd=worktree,
                    )
                    if r.stdout.strip():
                        diff_output = r.stdout.strip()

        if not diff_output:
            findings.append(
                Finding(
                    severity="critical",
                    message="No file changes detected in worktree (git diff shows 0 files changed)",
                )
            )
            passed = False

        # --- Detect project type and run tests ---
        project_type = _detect_project_type(worktree)
        span.set_attribute("gate.project_type", project_type)

        if project_type == "node":
            # Check if node_modules exists; if not, install first
            if not (worktree / "node_modules").is_dir():
                install_result = _run_cmd(["npm", "install", "--ignore-scripts"], cwd=worktree)
                if install_result.returncode != 0:
                    findings.append(
                        Finding(
                            severity="warning",
                            message=f"npm install failed: {install_result.stderr[:500]}",
                        )
                    )

            test_result = _run_cmd(["npm", "test"], cwd=worktree, timeout=300)
            if test_result.returncode != 0:
                findings.append(
                    Finding(
                        severity="critical",
                        message=f"npm test failed (exit {test_result.returncode})",
                    )
                )
                # Include test output for debugging
                test_output = (test_result.stdout + test_result.stderr)[:2000]
                if test_output.strip():
                    findings.append(
                        Finding(severity="info", message=f"Test output:\n{test_output}")
                    )
                passed = False

        elif project_type == "python":
            # Force a clean reinstall so pytest can import the local package
            # even if the agent modified source files since the last sync.
            _run_cmd(["uv", "sync", "--dev", "--reinstall"], cwd=worktree, timeout=120)
            test_result = _run_cmd(
                ["uv", "run", "pytest", "--tb=short", "-q"], cwd=worktree, timeout=300
            )
            if test_result.returncode != 0:
                findings.append(
                    Finding(
                        severity="critical",
                        message=f"pytest failed (exit {test_result.returncode})",
                    )
                )
                test_output = (test_result.stdout + test_result.stderr)[:2000]
                if test_output.strip():
                    findings.append(
                        Finding(severity="info", message=f"Test output:\n{test_output}")
                    )
                passed = False

        elif project_type == "rust":
            test_result = _run_cmd(["cargo", "check"], cwd=worktree, timeout=300)
            if test_result.returncode != 0:
                findings.append(
                    Finding(
                        severity="critical",
                        message=f"cargo check failed (exit {test_result.returncode})",
                    )
                )
                test_output = (test_result.stdout + test_result.stderr)[:2000]
                if test_output.strip():
                    findings.append(
                        Finding(severity="info", message=f"Test output:\n{test_output}")
                    )
                passed = False

        else:
            findings.append(
                Finding(
                    severity="warning",
                    message=(
                        "Unknown project type — no package.json, pyproject.toml, "
                        "or Cargo.toml found. Skipping test execution."
                    ),
                )
            )

        elapsed = time.monotonic() - start
        span.set_attribute("gate.status", "pass" if passed else "fail")
        span.set_attribute("gate.duration_ms", round(elapsed * 1000))
        span.set_attribute("gate.findings_count", len(findings))

        if not passed:
            span.set_status(trace.StatusCode.ERROR, "Gate 0 sanity failed")
        else:
            span.set_status(trace.StatusCode.OK)

        return GateResult(
            gate_name="gate_0_sanity",
            passed=passed,
            findings=findings,
            duration_seconds=round(elapsed, 3),
        ).model_dump()


# ---------------------------------------------------------------------------
# Gate 0.1: Differential Test Check — parsers, helpers, and gate
# ---------------------------------------------------------------------------


def _parse_pytest_junit(xml_content: str) -> list[dict]:
    """Parse JUnit XML from pytest --junitxml. Returns list of TestOutcome dicts."""
    import xml.etree.ElementTree as ET

    outcomes: list[dict] = []
    try:
        root = ET.fromstring(xml_content)
        for tc in root.iter("testcase"):
            name = f"{tc.get('classname', '')}.{tc.get('name', '')}"
            failure_el = tc.find("failure")
            error_el = tc.find("error")
            failed = failure_el is not None or error_el is not None
            msg = None
            if failed:
                el = failure_el if failure_el is not None else error_el
                msg = el.get("message", "") if el is not None else None
            outcomes.append({"name": name, "passed": not failed, "error_message": msg})
    except ET.ParseError:
        pass
    return outcomes


def _parse_node_json(json_str: str) -> list[dict]:
    """Parse Jest/Vitest --json output. Returns list of TestOutcome dicts."""
    outcomes: list[dict] = []
    try:
        data = json.loads(json_str)
        for suite in data.get("testResults", []):
            for test in suite.get("testResults", suite.get("assertionResults", [])):
                name = (
                    test.get("fullName")
                    or test.get("ancestorTitles", [""])[0] + " > " + test.get("title", "")
                )
                status = test.get("status", "")
                outcomes.append({
                    "name": name,
                    "passed": status == "passed",
                    "error_message": "\n".join(test.get("failureMessages", [])) or None,
                })
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    return outcomes


def _parse_cargo_test(output: str) -> list[dict]:
    """Parse cargo test output. Returns list of TestOutcome dicts."""
    outcomes: list[dict] = []
    pattern = re.compile(r"^test\s+(\S+)\s+\.\.\.\s+(ok|FAILED|ignored)$", re.MULTILINE)
    for match in pattern.finditer(output):
        name, status = match.group(1), match.group(2)
        if status != "ignored":
            outcomes.append({"name": name, "passed": status == "ok", "error_message": None})
    return outcomes


def _run_tests_with_parsing(worktree_path: str, project_type: str) -> tuple[int, list[dict]]:
    """Run tests and parse results. Returns (return_code, list_of_TestOutcome_dicts)."""
    import tempfile

    if project_type == "python":
        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as f:
            junit_path = f.name
        try:
            result = _run_cmd(
                ["uv", "run", "pytest", "--tb=short", "-q", f"--junitxml={junit_path}"],
                cwd=worktree_path,
                timeout=120,
            )
            try:
                xml_content = Path(junit_path).read_text()
                outcomes = _parse_pytest_junit(xml_content)
            except FileNotFoundError:
                outcomes = []
        finally:
            Path(junit_path).unlink(missing_ok=True)
        return result.returncode, outcomes

    elif project_type == "node":
        result = _run_cmd(
            ["npx", "jest", "--json", "--no-coverage"],
            cwd=worktree_path,
            timeout=120,
        )
        outcomes = _parse_node_json(result.stdout or "")
        return result.returncode, outcomes

    elif project_type == "rust":
        result = _run_cmd(["cargo", "test"], cwd=worktree_path, timeout=120)
        outcomes = _parse_cargo_test(result.stdout or "")
        return result.returncode, outcomes

    return 0, []


def _is_differential_enabled(worktree_path: str) -> bool:
    """Check if differential gate is enabled for the project in this worktree."""
    worktree = Path(worktree_path)
    metadata_path = worktree / ".dev-loop-metadata.json"
    if not metadata_path.exists():
        return False
    try:
        metadata = json.loads(metadata_path.read_text())
        repo_name = Path(metadata.get("repo_path", "")).name
        config_path = (
            Path(__file__).resolve().parents[3] / "config" / "projects" / f"{repo_name}.yaml"
        )
        if config_path.exists():
            with open(config_path) as f:
                config = yaml.safe_load(f) or {}
            return config.get("quality_gates", {}).get("differential", {}).get("enabled", False)
    except Exception:
        pass
    return False


@mcp.tool(
    description=(
        "Gate 0.1 — Differential test check. Compares test results between "
        "merge-base (pre-agent) and HEAD (post-agent). Only NEW failures block."
    ),
    tags={"gates", "differential"},
)
def run_gate_01_differential(
    worktree_path: str, gate_0_findings: list[dict] | None = None
) -> dict:
    """Run differential test analysis between baseline and HEAD."""
    start = time.monotonic()
    findings: list[Finding] = []
    worktree = Path(worktree_path)

    if not worktree.is_dir():
        return GateResult(
            gate_name="gate_01_differential",
            passed=False,
            findings=[Finding(severity="critical", message=f"Worktree not found: {worktree_path}")],
            duration_seconds=round(time.monotonic() - start, 2),
        ).model_dump()

    # 1. Find merge-base
    mb_result = _run_cmd(["git", "merge-base", "HEAD", "main"], cwd=worktree_path, timeout=30)
    if mb_result.returncode != 0:
        mb_result = _run_cmd(
            ["git", "merge-base", "HEAD", "origin/main"], cwd=worktree_path, timeout=30
        )
    if mb_result.returncode != 0:
        return GateResult(
            gate_name="gate_01_differential",
            passed=False,
            findings=[
                Finding(
                    severity="critical",
                    message="Cannot determine merge-base for differential analysis",
                )
            ],
            duration_seconds=round(time.monotonic() - start, 2),
        ).model_dump()

    baseline_sha = mb_result.stdout.strip()

    # 2. Check for agent commits
    log_result = _run_cmd(
        ["git", "log", f"{baseline_sha}..HEAD", "--oneline"], cwd=worktree_path, timeout=30
    )
    if not (log_result.stdout or "").strip():
        return GateResult(
            gate_name="gate_01_differential",
            passed=True,
            skipped=True,
            findings=[
                Finding(severity="info", message="No agent commits found — differential check skipped")
            ],
            duration_seconds=round(time.monotonic() - start, 2),
        ).model_dump()

    # 3. Detect project type
    project_type = _detect_project_type(worktree)
    if project_type == "unknown":
        return GateResult(
            gate_name="gate_01_differential",
            passed=False,
            findings=[
                Finding(
                    severity="warning",
                    message="Unknown project type — cannot run differential tests",
                )
            ],
            duration_seconds=round(time.monotonic() - start, 2),
        ).model_dump()

    # 4. Save current branch
    branch_result = _run_cmd(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=worktree_path, timeout=10
    )
    current_branch = (branch_result.stdout or "").strip() or "HEAD"

    # 5. Checkout baseline and run tests
    checkout_result = _run_cmd(
        ["git", "checkout", baseline_sha, "--quiet"], cwd=worktree_path, timeout=30
    )
    if checkout_result.returncode != 0:
        return GateResult(
            gate_name="gate_01_differential",
            passed=False,
            findings=[
                Finding(severity="critical", message=f"Cannot checkout baseline {baseline_sha}")
            ],
            duration_seconds=round(time.monotonic() - start, 2),
        ).model_dump()

    try:
        # Install deps for baseline if needed
        if project_type == "python":
            _run_cmd(["uv", "sync", "--dev", "--reinstall"], cwd=worktree_path, timeout=120)
        elif project_type == "node":
            _run_cmd(["npm", "install", "--ignore-scripts"], cwd=worktree_path, timeout=120)

        baseline_rc, baseline_outcomes = _run_tests_with_parsing(worktree_path, project_type)
    finally:
        # 6. Always restore to agent branch
        _run_cmd(["git", "checkout", current_branch, "--quiet"], cwd=worktree_path, timeout=30)
        if project_type == "python":
            _run_cmd(["uv", "sync", "--dev", "--reinstall"], cwd=worktree_path, timeout=120)
        elif project_type == "node":
            _run_cmd(["npm", "install", "--ignore-scripts"], cwd=worktree_path, timeout=120)

    # 7. Run HEAD tests
    head_rc, head_outcomes = _run_tests_with_parsing(worktree_path, project_type)

    # 8. Compute differential
    baseline_fail_names = {t["name"] for t in baseline_outcomes if not t["passed"]}
    head_fail_names = {t["name"] for t in head_outcomes if not t["passed"]}
    new_failures = sorted(head_fail_names - baseline_fail_names)
    preexisting = sorted(head_fail_names & baseline_fail_names)

    # Handle unparsable output
    parse_error = False
    if head_rc != 0 and not head_outcomes:
        parse_error = True
        findings.append(
            Finding(
                severity="critical",
                message="Test output unparsable — cannot determine differential. Deferring to Gate 0 result.",
            )
        )
        return GateResult(
            gate_name="gate_01_differential",
            passed=False,
            findings=findings,
            differential=DifferentialResult(
                baseline_commit=baseline_sha,
                baseline_failures=sorted(baseline_fail_names),
                head_failures=sorted(head_fail_names),
                new_failures=new_failures,
                preexisting_failures=preexisting,
                baseline_parse_error=parse_error,
            ),
            duration_seconds=round(time.monotonic() - start, 2),
        ).model_dump()

    # Log pre-existing failures as warnings
    for name in preexisting:
        findings.append(
            Finding(
                severity="warning",
                message=f"Pre-existing test failure (not agent-caused): {name}",
                rule="preexisting_failure",
            )
        )

    # Log new failures as critical
    for name in new_failures:
        findings.append(
            Finding(
                severity="critical",
                message=f"New test failure introduced by agent: {name}",
                rule="new_failure",
            )
        )

    # Log fixed tests as info
    fixed = sorted(baseline_fail_names - head_fail_names)
    for name in fixed:
        findings.append(
            Finding(
                severity="info",
                message=f"Agent fixed pre-existing failure: {name}",
                rule="fixed_test",
            )
        )

    passed = len(new_failures) == 0

    return GateResult(
        gate_name="gate_01_differential",
        passed=passed,
        findings=findings,
        differential=DifferentialResult(
            baseline_commit=baseline_sha,
            baseline_failures=sorted(baseline_fail_names),
            head_failures=sorted(head_fail_names),
            new_failures=new_failures,
            preexisting_failures=preexisting,
            baseline_parse_error=parse_error,
        ),
        duration_seconds=round(time.monotonic() - start, 2),
    ).model_dump()


# ---------------------------------------------------------------------------
# Gate 0.5: Relevance Check
# ---------------------------------------------------------------------------

# Stopwords filtered out when extracting issue keywords
_RELEVANCE_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "and", "or", "but", "not", "no", "nor", "so", "yet",
    "to", "in", "for", "of", "on", "at", "by", "with", "from", "as",
    "it", "its", "this", "that", "these", "those",
    "fix", "add", "update", "remove", "change", "make", "use", "set",
    "should", "would", "could", "can", "will", "may", "must",
    "we", "i", "you", "they", "he", "she",
    "if", "when", "then", "than", "also", "just", "about",
    "all", "any", "each", "every", "some", "more", "most",
    "new", "get", "has", "have", "had", "do", "does", "did",
})


def _extract_keywords(text: str) -> set[str]:
    """Extract meaningful lowercase keywords from text, filtering stopwords."""
    words = re.findall(r"[a-zA-Z0-9_]+", text.lower())
    return {w for w in words if w not in _RELEVANCE_STOPWORDS and len(w) > 1}


@mcp.tool(
    description=(
        "Gate 0.5 — Relevance check: verifies the diff has some relation to "
        "the issue being worked on. Uses keyword overlap between the issue "
        "description and the diff content."
    ),
    tags={"gates", "relevance"},
)
def run_gate_05_relevance(
    worktree_path: str,
    issue_title: str,
    issue_description: str,
    *,
    strict: bool = False,
) -> dict:
    """Check that the diff is relevant to the issue being worked on."""
    with tracer.start_as_current_span(
        "gates.gate_05_relevance",
        attributes={"gate.name": "relevance", "gate.order": 0.5},
    ) as span:
        start = time.monotonic()
        worktree = Path(worktree_path)
        findings: list[Finding] = []

        # --- Check worktree exists ---
        if not worktree.is_dir():
            elapsed = time.monotonic() - start
            span.set_status(trace.StatusCode.ERROR, "Worktree does not exist")
            return GateResult(
                gate_name="gate_05_relevance",
                passed=False,
                findings=[Finding(severity="critical", message=f"Worktree not found: {worktree}")],
                duration_seconds=round(elapsed, 3),
                error=f"Worktree not found: {worktree}",
            ).model_dump()

        # --- Get the diff text ---
        diff_result = _run_cmd(["git", "diff", "HEAD~1"], cwd=worktree)
        diff_text = diff_result.stdout.strip()

        if not diff_text:
            # Try unstaged diff
            diff_result = _run_cmd(["git", "diff"], cwd=worktree)
            diff_text = diff_result.stdout.strip()

        if not diff_text:
            # Try staged diff
            diff_result = _run_cmd(["git", "diff", "--cached"], cwd=worktree)
            diff_text = diff_result.stdout.strip()

        if not diff_text:
            elapsed = time.monotonic() - start
            span.set_attribute("gate.status", "fail")
            span.set_status(trace.StatusCode.ERROR, "No diff found")
            return GateResult(
                gate_name="gate_05_relevance",
                passed=False,
                findings=[
                    Finding(
                        severity="critical",
                        message="No diff found — cannot check relevance",
                    )
                ],
                duration_seconds=round(elapsed, 3),
            ).model_dump()

        # --- Get changed file list ---
        files_result = _run_cmd(["git", "diff", "HEAD~1", "--name-only"], cwd=worktree)
        changed_files = files_result.stdout.strip()
        if not changed_files:
            files_result = _run_cmd(["git", "diff", "--name-only"], cwd=worktree)
            changed_files = files_result.stdout.strip()
        if not changed_files:
            files_result = _run_cmd(["git", "diff", "--cached", "--name-only"], cwd=worktree)
            changed_files = files_result.stdout.strip()

        # --- Extract keywords from issue ---
        issue_text = f"{issue_title} {issue_description}"
        keywords = _extract_keywords(issue_text)
        span.set_attribute("gate.keyword_count", len(keywords))

        if not keywords:
            elapsed = time.monotonic() - start
            findings.append(
                Finding(
                    severity="warning",
                    message="No meaningful keywords extracted from issue — relevance check skipped",
                )
            )
            span.set_attribute("gate.status", "pass")
            span.set_attribute("status.detail", "No keywords to check")
            span.set_status(trace.StatusCode.OK)
            return GateResult(
                gate_name="gate_05_relevance",
                passed=True,
                findings=findings,
                duration_seconds=round(elapsed, 3),
            ).model_dump()

        # --- Check keyword overlap with diff content ---
        diff_lower = diff_text.lower()
        files_lower = changed_files.lower()
        matched_keywords = {kw for kw in keywords if kw in diff_lower or kw in files_lower}

        passed = True
        if matched_keywords:
            findings.append(
                Finding(
                    severity="info",
                    message=(
                        f"Relevance confirmed: {len(matched_keywords)}/{len(keywords)} "
                        f"issue keywords found in diff. "
                        f"Matched: {', '.join(sorted(matched_keywords)[:10])}"
                    ),
                )
            )
        else:
            if strict:
                # Strict mode: zero overlap = fail
                passed = False
                findings.append(
                    Finding(
                        severity="critical",
                        message=(
                            f"No issue keywords found in diff (strict mode). "
                            f"Keywords checked: {', '.join(sorted(keywords)[:15])}. "
                            f"Changed files: {changed_files[:200]}. "
                            f"The diff appears unrelated to the issue."
                        ),
                    )
                )
            else:
                # Soft gate: pass with a warning
                findings.append(
                    Finding(
                        severity="warning",
                        message=(
                            f"No issue keywords found in diff content or filenames. "
                            f"Keywords checked: {', '.join(sorted(keywords)[:15])}. "
                            f"Changed files: {changed_files[:200]}. "
                            f"This may indicate the diff is unrelated to the issue."
                        ),
                    )
                )

        elapsed = time.monotonic() - start
        span.set_attribute("gate.status", "pass" if passed else "fail")
        span.set_attribute("gate.duration_ms", round(elapsed * 1000))
        span.set_attribute("gate.findings_count", len(findings))
        span.set_attribute("gate.matched_keywords", len(matched_keywords))

        span.set_status(trace.StatusCode.OK)

        return GateResult(
            gate_name="gate_05_relevance",
            passed=passed,
            findings=findings,
            duration_seconds=round(elapsed, 3),
        ).model_dump()


# ---------------------------------------------------------------------------
# Gate 2: Secrets Scan
# ---------------------------------------------------------------------------


@mcp.tool(
    description=(
        "Gate 2 — Secrets scan: runs gitleaks on the worktree to detect "
        "leaked API keys, passwords, private keys, and other credentials. "
        "Must pass before any code leaves the machine."
    ),
    tags={"gates", "secrets"},
)
def run_gate_2_secrets(worktree_path: str) -> dict:
    """Run gitleaks secret scanning on the worktree."""
    with tracer.start_as_current_span(
        "gates.gate_2_secrets",
        attributes={"gate.name": "secrets", "gate.order": 2},
    ) as span:
        start = time.monotonic()
        worktree = Path(worktree_path)
        findings: list[Finding] = []

        # --- Check worktree exists ---
        if not worktree.is_dir():
            elapsed = time.monotonic() - start
            span.set_status(trace.StatusCode.ERROR, "Worktree does not exist")
            return GateResult(
                gate_name="gate_2_secrets",
                passed=False,
                findings=[Finding(severity="critical", message=f"Worktree not found: {worktree}")],
                duration_seconds=round(elapsed, 3),
                error=f"Worktree not found: {worktree}",
            ).model_dump()

        # --- Resolve gitleaks binary ---
        gitleaks_bin = _find_gitleaks()
        if gitleaks_bin is None:
            elapsed = time.monotonic() - start
            return GateResult(
                gate_name="gate_2_secrets",
                passed=False,
                findings=[
                    Finding(
                        severity="critical",
                        message=(
                            "gitleaks not found. Install it: "
                            "https://github.com/gitleaks/gitleaks#installing"
                        ),
                    )
                ],
                duration_seconds=round(elapsed, 3),
                error="gitleaks binary not found on PATH or at ~/.local/bin/gitleaks",
            ).model_dump()

        # --- Run gitleaks ---
        report_path = worktree / ".gitleaks-report.json"
        result = _run_cmd(
            [
                gitleaks_bin,
                "detect",
                "--source",
                str(worktree),
                "--no-git",
                "--exit-code",
                "1",
                "--report-format",
                "json",
                "--report-path",
                str(report_path),
            ],
            cwd=worktree,
            timeout=120,
        )

        passed = result.returncode == 0

        # --- Parse report if it exists ---
        if report_path.exists():
            try:
                report_text = report_path.read_text()
                if report_text.strip():
                    leaks = json.loads(report_text)
                    if isinstance(leaks, list):
                        for leak in leaks:
                            findings.append(
                                Finding(
                                    severity="critical",
                                    message=(
                                        f"Secret detected: {leak.get('Description', 'unknown')} "
                                        f"({leak.get('RuleID', 'unknown rule')})"
                                    ),
                                    file=leak.get("File"),
                                    line=leak.get("StartLine"),
                                    rule=leak.get("RuleID"),
                                )
                            )
            except (json.JSONDecodeError, OSError):
                findings.append(
                    Finding(
                        severity="warning",
                        message="Could not parse gitleaks report JSON",
                    )
                )
            finally:
                # Clean up report file
                try:
                    report_path.unlink()
                except OSError:
                    pass

        # If gitleaks failed but we have no findings from report, check stderr
        if not passed and not findings:
            stderr = result.stderr.strip()
            if stderr:
                findings.append(
                    Finding(
                        severity="critical",
                        message=f"gitleaks detected secrets: {stderr[:500]}",
                    )
                )
            else:
                findings.append(
                    Finding(
                        severity="critical",
                        message="gitleaks exited with non-zero status (secrets likely detected)",
                    )
                )

        elapsed = time.monotonic() - start
        span.set_attribute("gate.status", "pass" if passed else "fail")
        span.set_attribute("gate.duration_ms", round(elapsed * 1000))
        span.set_attribute("gate.findings_count", len(findings))

        if not passed:
            span.set_status(trace.StatusCode.ERROR, "Gate 2 secrets scan failed")
        else:
            span.set_status(trace.StatusCode.OK)

        return GateResult(
            gate_name="gate_2_secrets",
            passed=passed,
            findings=findings,
            duration_seconds=round(elapsed, 3),
        ).model_dump()


# ---------------------------------------------------------------------------
# Gate 2.5: Dangerous Operations Check
# ---------------------------------------------------------------------------

# Dangerous SQL patterns (case-insensitive)
_DANGEROUS_SQL_PATTERNS = [
    (r"\bDROP\s+(TABLE|DATABASE|INDEX|VIEW|SCHEMA)\b", "DROP statement"),
    (r"\bDELETE\s+FROM\b", "DELETE FROM statement"),
    (r"\bTRUNCATE\s+(TABLE\s+)?\w+", "TRUNCATE statement"),
    (r"\bALTER\s+TABLE\b", "ALTER TABLE statement"),
    (r"\bRENAME\s+(TABLE|COLUMN|INDEX)\b", "RENAME statement"),
]

# CI/CD file patterns (glob-style)
_CICD_FILE_PATTERNS = [
    ".github/workflows/*",
    "Dockerfile",
    "Dockerfile.*",
    "docker-compose*",
    ".gitlab-ci*",
    "Jenkinsfile",
]

# Auth/permission file patterns (glob-style, matched against basename and full path)
_AUTH_FILE_PATTERNS = [
    "*auth*",
    "*permission*",
    "*rbac*",
    "*oauth*",
]

# Lock file pairs: (source, lock)
_LOCK_FILE_PAIRS = [
    ("package.json", "package-lock.json"),
    ("Cargo.toml", "Cargo.lock"),
    ("pyproject.toml", "uv.lock"),
    ("pyproject.toml", "poetry.lock"),
    ("Gemfile", "Gemfile.lock"),
    ("composer.json", "composer.lock"),
]

# Migration file patterns (glob-style, matched against changed file paths)
_MIGRATION_FILE_PATTERNS = [
    "prisma/migrations/*",
    "prisma/migrations/**/*",
    "alembic/versions/*",
    "alembic/versions/**/*",
    "db/migrate/*",
    "db/migrate/**/*",
    "migrations/*",
    "migrations/**/*",
]

# Destructive SQL keywords to detect within migration files
_DESTRUCTIVE_MIGRATION_SQL = [
    (r"\bDROP\b", "DROP"),
    (r"\bDELETE\b", "DELETE"),
    (r"\bTRUNCATE\b", "TRUNCATE"),
    (r"\bALTER\s+TABLE\s+\w+\s+DROP\b", "ALTER TABLE ... DROP"),
    (r"\bRENAME\b", "RENAME"),
]


@mcp.tool(
    description=(
        "Gate 2.5 — Dangerous operations: scans the diff for database migrations, "
        "CI/CD config changes, auth modifications, and lock file inconsistencies "
        "that require human review regardless of code quality."
    ),
    tags={"gates", "dangerous_ops"},
)
def run_gate_25_dangerous_ops(worktree_path: str) -> dict:
    """Scan the diff for dangerous patterns that require human review."""
    with tracer.start_as_current_span(
        "gates.gate_25_dangerous_ops",
        attributes={"gate.name": "dangerous_ops", "gate.order": 2.5},
    ) as span:
        start = time.monotonic()
        worktree = Path(worktree_path)
        findings: list[Finding] = []

        # --- Check worktree exists ---
        if not worktree.is_dir():
            elapsed = time.monotonic() - start
            span.set_status(trace.StatusCode.ERROR, "Worktree does not exist")
            return GateResult(
                gate_name="gate_25_dangerous_ops",
                passed=False,
                findings=[Finding(severity="critical", message=f"Worktree not found: {worktree}")],
                duration_seconds=round(elapsed, 3),
                error=f"Worktree not found: {worktree}",
            ).model_dump()

        # --- Get the diff text ---
        diff_result = _run_cmd(["git", "diff", "HEAD~1"], cwd=worktree)
        diff_text = diff_result.stdout.strip()

        if not diff_text:
            diff_result = _run_cmd(["git", "diff"], cwd=worktree)
            diff_text = diff_result.stdout.strip()

        if not diff_text:
            diff_result = _run_cmd(["git", "diff", "--cached"], cwd=worktree)
            diff_text = diff_result.stdout.strip()

        # --- Get changed file list ---
        files_result = _run_cmd(["git", "diff", "HEAD~1", "--name-only"], cwd=worktree)
        changed_files_text = files_result.stdout.strip()
        if not changed_files_text:
            files_result = _run_cmd(["git", "diff", "--name-only"], cwd=worktree)
            changed_files_text = files_result.stdout.strip()
        if not changed_files_text:
            files_result = _run_cmd(["git", "diff", "--cached", "--name-only"], cwd=worktree)
            changed_files_text = files_result.stdout.strip()

        changed_files = [f.strip() for f in changed_files_text.splitlines() if f.strip()]

        # --- 1. Database migration patterns ---
        if diff_text:
            for pattern, description in _DANGEROUS_SQL_PATTERNS:
                matches = re.findall(pattern, diff_text, re.IGNORECASE)
                if matches:
                    findings.append(
                        Finding(
                            severity="critical",
                            message=(
                                f"Dangerous database operation: {description} "
                                f"found in diff ({len(matches)} occurrence(s))"
                            ),
                            rule="dangerous_sql",
                        )
                    )

        # --- 1b. Migration file detection ---
        for changed_file in changed_files:
            is_migration = any(
                fnmatch.fnmatch(changed_file, pat) for pat in _MIGRATION_FILE_PATTERNS
            )
            if not is_migration:
                continue

            # Get the file-specific diff to check for destructive SQL
            file_diff_result = _run_cmd(
                ["git", "diff", "HEAD~1", "--", changed_file], cwd=worktree
            )
            file_diff = file_diff_result.stdout if file_diff_result.stdout else ""
            # Also check staged diff
            if not file_diff:
                file_diff_result = _run_cmd(
                    ["git", "diff", "--cached", "--", changed_file], cwd=worktree
                )
                file_diff = file_diff_result.stdout if file_diff_result.stdout else ""

            has_destructive = False
            for sql_pattern, sql_desc in _DESTRUCTIVE_MIGRATION_SQL:
                if re.search(sql_pattern, file_diff, re.IGNORECASE):
                    has_destructive = True
                    findings.append(
                        Finding(
                            severity="critical",
                            message=(
                                f"Destructive migration: {sql_desc} in {changed_file} "
                                "— requires human review before merge"
                            ),
                            file=changed_file,
                            rule="destructive_migration",
                        )
                    )

            if not has_destructive:
                findings.append(
                    Finding(
                        severity="warning",
                        message=(
                            f"Migration file changed: {changed_file} "
                            "— additive migration, review recommended"
                        ),
                        file=changed_file,
                        rule="migration_file",
                    )
                )

        # --- 2. CI/CD config changes ---
        for changed_file in changed_files:
            for cicd_pattern in _CICD_FILE_PATTERNS:
                if fnmatch.fnmatch(changed_file, cicd_pattern):
                    findings.append(
                        Finding(
                            severity="critical",
                            message=f"CI/CD config changed: {changed_file}",
                            file=changed_file,
                            rule="cicd_change",
                        )
                    )
                    break  # Don't double-report the same file

        # --- 3. Auth/permission changes ---
        for changed_file in changed_files:
            basename = Path(changed_file).name.lower()
            full_lower = changed_file.lower()
            for auth_pattern in _AUTH_FILE_PATTERNS:
                if fnmatch.fnmatch(basename, auth_pattern) or fnmatch.fnmatch(full_lower, auth_pattern):
                    findings.append(
                        Finding(
                            severity="critical",
                            message=f"Auth/permission file changed: {changed_file}",
                            file=changed_file,
                            rule="auth_change",
                        )
                    )
                    break  # Don't double-report the same file

        # --- 4. Lock file inconsistency ---
        changed_set = set(changed_files)
        for source_file, lock_file in _LOCK_FILE_PAIRS:
            source_changed = source_file in changed_set
            lock_changed = lock_file in changed_set
            if source_changed and not lock_changed:
                findings.append(
                    Finding(
                        severity="critical",
                        message=(
                            f"Lock file inconsistency: {source_file} changed but "
                            f"{lock_file} not updated"
                        ),
                        file=source_file,
                        rule="lock_file_mismatch",
                    )
                )
            elif lock_changed and not source_changed:
                # Known lock files from recognized package managers can change
                # without their source file changing (e.g. `uv lock --upgrade`,
                # transitive dep updates).  This is expected — not dangerous.
                pass

        # --- Determine pass/fail ---
        passed = not any(f.severity == "critical" for f in findings)

        if passed and not findings:
            findings.append(
                Finding(
                    severity="info",
                    message="No dangerous operations detected",
                )
            )

        elapsed = time.monotonic() - start
        span.set_attribute("gate.status", "pass" if passed else "fail")
        span.set_attribute("gate.duration_ms", round(elapsed * 1000))
        span.set_attribute("gate.findings_count", len(findings))

        if not passed:
            span.set_status(
                trace.StatusCode.ERROR,
                "Gate 2.5 detected dangerous operations requiring review",
            )
        else:
            span.set_status(trace.StatusCode.OK)

        return GateResult(
            gate_name="gate_25_dangerous_ops",
            passed=passed,
            findings=findings,
            duration_seconds=round(elapsed, 3),
        ).model_dump()


# ---------------------------------------------------------------------------
# Gate 3: Security Scan (SAST)
# ---------------------------------------------------------------------------


@mcp.tool(
    description=(
        "Gate 3 — Security SAST scan: runs bandit on Python code to detect "
        "security vulnerabilities (SQL injection, hardcoded passwords, shell "
        "injection, etc.) with CWE classification. Skips gracefully if bandit "
        "is not installed."
    ),
    tags={"gates", "security"},
)
def run_gate_3_security(worktree_path: str, *, fail_on_missing_tool: bool = False) -> dict:
    """Run SAST security scan on the worktree."""
    with tracer.start_as_current_span(
        "gates.gate_3_security",
        attributes={"gate.name": "security", "gate.order": 3},
    ) as span:
        start = time.monotonic()
        worktree = Path(worktree_path)
        findings: list[Finding] = []

        # --- Check worktree exists ---
        if not worktree.is_dir():
            elapsed = time.monotonic() - start
            span.set_status(trace.StatusCode.ERROR, "Worktree does not exist")
            return GateResult(
                gate_name="gate_3_security",
                passed=False,
                findings=[Finding(severity="critical", message=f"Worktree not found: {worktree}")],
                duration_seconds=round(elapsed, 3),
                error=f"Worktree not found: {worktree}",
            ).model_dump()

        # --- Detect project type ---
        project_type = _detect_project_type(worktree)
        span.set_attribute("gate.project_type", project_type)

        if project_type != "python":
            elapsed = time.monotonic() - start
            span.set_attribute("gate.status", "skipped")
            span.set_attribute("status.detail", f"Skipped — {project_type} project")
            span.set_status(trace.StatusCode.OK)
            return GateResult(
                gate_name="gate_3_security",
                passed=True,
                findings=[
                    Finding(
                        severity="info",
                        message=f"Security scan skipped for {project_type} project (Python only)",
                    )
                ],
                duration_seconds=round(elapsed, 3),
                skipped=True,
            ).model_dump()

        # --- Resolve bandit binary ---
        bandit_bin = _find_bandit()
        if bandit_bin is None:
            elapsed = time.monotonic() - start
            if fail_on_missing_tool:
                span.set_attribute("gate.status", "fail")
                span.set_status(trace.StatusCode.ERROR, "bandit not installed")
                return GateResult(
                    gate_name="gate_3_security",
                    passed=False,
                    findings=[
                        Finding(
                            severity="critical",
                            message="bandit not installed — required for security scan",
                        )
                    ],
                    duration_seconds=round(elapsed, 3),
                    error="Required tool 'bandit' not found",
                ).model_dump()
            span.set_attribute("gate.status", "skipped")
            span.set_attribute("status.detail", "Skipped — bandit not installed")
            span.set_status(trace.StatusCode.OK)
            return GateResult(
                gate_name="gate_3_security",
                passed=True,
                findings=[
                    Finding(
                        severity="warning",
                        message=(
                            "bandit not found on PATH — security scan skipped. "
                            "Install: pip install bandit"
                        ),
                    )
                ],
                duration_seconds=round(elapsed, 3),
                skipped=True,
            ).model_dump()

        # --- Determine scan target ---
        # Prefer src/ directory if it exists, otherwise scan the whole worktree
        scan_target = worktree / "src"
        if not scan_target.is_dir():
            scan_target = worktree

        # --- Run bandit ---
        exclusions = ",".join([
            str(worktree / ".git"),
            str(worktree / ".venv"),
            str(worktree / "__pycache__"),
            str(worktree / "node_modules"),
        ])

        try:
            result = _run_cmd(
                [
                    bandit_bin,
                    "-r",
                    str(scan_target),
                    "-f", "json",
                    "-x", exclusions,
                ],
                cwd=worktree,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - start
            error_msg = "bandit scan timed out after 120s"
            span.set_status(trace.StatusCode.ERROR, error_msg)
            return GateResult(
                gate_name="gate_3_security",
                passed=False,
                findings=[Finding(severity="critical", message=error_msg)],
                duration_seconds=round(elapsed, 3),
                error=error_msg,
            ).model_dump()

        # bandit exit codes: 0=no issues, 1=issues found, 2=error
        if result.returncode == 2:
            elapsed = time.monotonic() - start
            error_msg = f"bandit error: {result.stderr.strip()[:500]}"
            span.set_status(trace.StatusCode.ERROR, error_msg)
            return GateResult(
                gate_name="gate_3_security",
                passed=False,
                findings=[Finding(severity="critical", message=error_msg)],
                duration_seconds=round(elapsed, 3),
                error=error_msg,
            ).model_dump()

        # --- Parse bandit JSON output ---
        try:
            report = json.loads(result.stdout)
            for item in report.get("results", []):
                severity = item.get("issue_severity", "MEDIUM").upper()
                cwe_info = item.get("issue_cwe", {})
                cwe_id = f"CWE-{cwe_info.get('id', '?')}" if cwe_info else None

                # Map bandit severity to our severity levels
                if severity == "HIGH":
                    finding_severity = "critical"
                elif severity == "MEDIUM":
                    finding_severity = "critical"
                else:
                    finding_severity = "warning"

                # Get relative file path
                file_path = item.get("filename", "")
                try:
                    file_path = str(Path(file_path).relative_to(worktree))
                except ValueError:
                    pass

                findings.append(
                    Finding(
                        severity=finding_severity,
                        message=(
                            f"{item.get('issue_text', 'Security issue')}"
                            f"{f' [{cwe_id}]' if cwe_id else ''}"
                        ),
                        file=file_path,
                        line=item.get("line_number"),
                        rule=item.get("test_id"),
                        cwe=cwe_id,
                    )
                )

                # Set CWE as span attribute for observability
                if cwe_id:
                    span.set_attribute(f"security.finding.{item.get('test_id', 'unknown')}", cwe_id)

        except json.JSONDecodeError:
            if result.stdout.strip():
                findings.append(
                    Finding(
                        severity="warning",
                        message=f"Could not parse bandit JSON output: {result.stdout[:200]}",
                    )
                )

        # --- Determine pass/fail ---
        passed = not any(f.severity == "critical" for f in findings)

        elapsed = time.monotonic() - start
        span.set_attribute("gate.status", "pass" if passed else "fail")
        span.set_attribute("gate.duration_ms", round(elapsed * 1000))
        span.set_attribute("gate.findings_count", len(findings))
        span.set_attribute("gate.scanner", "bandit")

        if not passed:
            cwe_list = [f.cwe for f in findings if f.cwe]
            if cwe_list:
                span.set_attribute("security.cwe_ids", ",".join(cwe_list))
            span.set_status(trace.StatusCode.ERROR, "Gate 3 security scan found vulnerabilities")
        else:
            span.set_status(trace.StatusCode.OK)

        return GateResult(
            gate_name="gate_3_security",
            passed=passed,
            findings=findings,
            duration_seconds=round(elapsed, 3),
        ).model_dump()


# ---------------------------------------------------------------------------
# Gate 3 standalone (for TB-3 pre-flight)
# ---------------------------------------------------------------------------


def run_gate_3_security_standalone(worktree_path: str) -> dict:
    """Run only Gate 3 (security scan). Used by TB-3 pre-flight to capture
    security findings without being blocked by earlier gates in fail-fast mode."""
    with tracer.start_as_current_span(
        "gates.run_gate_3_standalone",
        attributes={"gate.name": "gate_3_security_standalone"},
    ):
        g3 = GateResult(**run_gate_3_security(worktree_path))
        return GateSuiteResult(
            overall_passed=g3.passed,
            gate_results=[g3],
            first_failure=g3.gate_name if not g3.passed else None,
            total_duration_seconds=g3.duration_seconds,
        ).model_dump()


# ---------------------------------------------------------------------------
# Gate 4: LLM-as-Judge Code Review
# ---------------------------------------------------------------------------


@mcp.tool(
    description=(
        "Gate 4 — Code review: LLM-as-judge review of the diff using Claude API. "
        "Evaluates the code change against review criteria from config/review-gate.yaml "
        "(race conditions, memory leaks, logic errors, missing error handling, "
        "performance antipatterns). Critical findings fail the gate."
    ),
    tags={"gates", "review"},
)
def run_gate_4_review(
    worktree_path: str,
    issue_title: str,
    issue_description: str,
) -> dict:
    """Run LLM-as-judge code review on the worktree diff."""
    with tracer.start_as_current_span(
        "gates.gate_4_review",
        attributes={"gate.name": "review", "gate.order": 4},
    ) as span:
        start = time.monotonic()
        worktree = Path(worktree_path)
        findings: list[Finding] = []

        # --- Check worktree exists ---
        if not worktree.is_dir():
            elapsed = time.monotonic() - start
            span.set_status(trace.StatusCode.ERROR, "Worktree does not exist")
            return GateResult(
                gate_name="gate_4_review",
                passed=False,
                findings=[Finding(severity="critical", message=f"Worktree not found: {worktree}")],
                duration_seconds=round(elapsed, 3),
                error=f"Worktree not found: {worktree}",
            ).model_dump()

        # --- Get the diff ---
        diff_result = _run_cmd(["git", "diff", "HEAD~1"], cwd=worktree)
        diff_text = diff_result.stdout.strip()

        if not diff_text:
            # Try unstaged diff
            diff_result = _run_cmd(["git", "diff"], cwd=worktree)
            diff_text = diff_result.stdout.strip()

        if not diff_text:
            # Try staged diff
            diff_result = _run_cmd(["git", "diff", "--cached"], cwd=worktree)
            diff_text = diff_result.stdout.strip()

        if not diff_text:
            elapsed = time.monotonic() - start
            return GateResult(
                gate_name="gate_4_review",
                passed=True,
                findings=[
                    Finding(severity="info", message="No diff found to review — gate skipped")
                ],
                duration_seconds=round(elapsed, 3),
                skipped=True,
            ).model_dump()

        # Truncate very large diffs to avoid token limits
        max_diff_chars = 100_000
        if len(diff_text) > max_diff_chars:
            diff_text = (
                diff_text[:max_diff_chars]
                + "\n\n... [diff truncated — too large for review]"
            )
            findings.append(
                Finding(
                    severity="warning",
                    message=(
                        f"Diff truncated from {len(diff_result.stdout)} "
                        f"to {max_diff_chars} chars"
                    ),
                )
            )

        # --- Load config and build prompt ---
        config = _load_review_config()
        review_cfg = config.get("review", {})
        model = review_cfg.get("model", "claude-sonnet-4-6")
        severity_levels = review_cfg.get("severity_levels", {})

        # --- LLMOps feature flag: DSPy path vs CLI path ---
        _used_llmops = False
        try:
            from devloop.llmops.server import _load_llmops_config

            llmops_cfg = _load_llmops_config()
        except ImportError:
            llmops_cfg = None

        if llmops_cfg and llmops_cfg.enabled:
            # --- DSPy path: use optimized CodeReviewModule via LLMOps config ---
            span.set_attribute("gate.llmops_path", True)
            try:
                import dspy

                from devloop.llmops.programs import load_program
                from devloop.llmops.types import OptimizationConfig

                # Initialize Langfuse bridge for inference tracing
                try:
                    from devloop.llmops.langfuse_bridge import init_langfuse_bridge

                    init_langfuse_bridge()
                except ImportError:
                    pass

                api_key = os.environ.get(llmops_cfg.api_key_env)
                if not api_key:
                    raise RuntimeError(
                        f"LLMOps enabled but {llmops_cfg.api_key_env} not set"
                    )

                # Use model from LLMOps program config (matches optimization artifact)
                pcfg = llmops_cfg.programs.get("code_review", OptimizationConfig())
                llmops_model = pcfg.model
                if llmops_cfg.provider == "openrouter":
                    model_str = f"openrouter/anthropic/{llmops_model}"
                else:
                    model_str = f"anthropic/{llmops_model}"
                dspy.configure(lm=dspy.LM(model_str, api_key=api_key, max_tokens=2048))
                module = load_program("code_review")

                criteria = ",".join(review_cfg.get("criteria", []))
                result = module(
                    diff=diff_text,
                    issue_context=f"{issue_title}\n{issue_description}",
                    review_criteria=criteria,
                )

                review_findings = json.loads(result.findings_json)
                if not isinstance(review_findings, list):
                    raise ValueError("findings_json is not a JSON array")

                for rf in review_findings:
                    findings.append(
                        Finding(
                            severity=rf.get("severity", "suggestion"),
                            message=rf.get("message", ""),
                            file=rf.get("file"),
                            line=rf.get("line"),
                            rule=rf.get("rule"),
                        )
                    )
                _used_llmops = True

            except Exception as dspy_exc:
                # Fall back to CLI path on any DSPy failure
                span.set_attribute("gate.llmops_fallback", True)
                span.set_attribute("gate.llmops_error", str(dspy_exc)[:200])
                findings.append(
                    Finding(
                        severity="info",
                        message=f"LLMOps DSPy path failed, falling back to CLI: {dspy_exc}",
                    )
                )

        if not _used_llmops:
            # --- CLI path: existing claude --print flow (unchanged) ---
            span.set_attribute("gate.llmops_path", False)

            prompt = _build_review_prompt(diff_text, issue_title, issue_description, config)

            try:
                claude_path = shutil.which("claude")
                if claude_path is None:
                    raise FileNotFoundError(
                        "claude CLI not found on PATH. "
                        "Install it: https://docs.anthropic.com/en/docs/claude-code"
                    )

                review_env = os.environ.copy()
                review_env.pop("CLAUDECODE", None)

                review_schema = json.dumps({
                    "type": "object",
                    "properties": {
                        "findings": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "severity": {"type": "string"},
                                    "message": {"type": "string"},
                                    "file": {"type": "string"},
                                    "line": {"type": "integer"},
                                    "rule": {"type": "string"},
                                },
                                "required": ["severity", "message"],
                            },
                        },
                        "summary": {"type": "string"},
                    },
                    "required": ["findings"],
                })

                review_result = subprocess.run(
                    [
                        claude_path,
                        "--print",
                        "--model", model,
                        "--output-format", "json",
                        "--json-schema", review_schema,
                    ],
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    env=review_env,
                )

                response_text = review_result.stdout.strip()
                span.set_attribute("gate.review_model", model)

                if review_result.returncode != 0:
                    raise RuntimeError(
                        f"claude --print exited with code {review_result.returncode}: "
                        f"{review_result.stderr[:500]}"
                    )

            except (FileNotFoundError, RuntimeError, subprocess.TimeoutExpired) as exc:
                elapsed = time.monotonic() - start
                error_msg = f"Claude review error: {exc}"
                span.set_status(trace.StatusCode.ERROR, error_msg)
                return GateResult(
                    gate_name="gate_4_review",
                    passed=False,
                    findings=[Finding(severity="critical", message=error_msg)],
                    duration_seconds=round(elapsed, 3),
                    error=error_msg,
                ).model_dump()

            # --- Parse response JSON ---
            parsed = None
            try:
                if response_text.startswith("["):
                    objects = json.loads(response_text)
                else:
                    objects = []
                    for line in response_text.split("\n"):
                        if not line.strip():
                            continue
                        try:
                            objects.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue

                for obj in objects:
                    if obj.get("type") == "result" and "structured_output" in obj:
                        parsed = obj["structured_output"]
                        break

                if parsed is None:
                    for obj in objects:
                        if obj.get("type") == "result" and obj.get("result"):
                            try:
                                parsed = json.loads(obj["result"])
                            except (json.JSONDecodeError, TypeError):
                                pass
                            break

            except (json.JSONDecodeError, TypeError):
                brace_start = response_text.find("{")
                brace_end = response_text.rfind("}")
                if brace_start != -1 and brace_end != -1:
                    try:
                        parsed = json.loads(response_text[brace_start : brace_end + 1])
                    except json.JSONDecodeError:
                        pass

            if parsed is None:
                findings.append(
                    Finding(
                        severity="critical",
                        message=(
                            "Could not parse review response as JSON. "
                            f"Raw response: {response_text[:500]}"
                        ),
                    )
                )
                passed = False
            else:
                review_findings = parsed.get("findings", [])
                for rf in review_findings:
                    findings.append(
                        Finding(
                            severity=rf.get("severity", "suggestion"),
                            message=rf.get("message", ""),
                            file=rf.get("file"),
                            line=rf.get("line"),
                            rule=rf.get("rule"),
                        )
                    )

        # --- Determine pass/fail based on severity levels ---
        passed = True
        for f in findings:
            action = severity_levels.get(f.severity, "pass")
            if action == "fail":
                passed = False
                break

        elapsed = time.monotonic() - start
        span.set_attribute("gate.status", "pass" if passed else "fail")
        span.set_attribute("gate.duration_ms", round(elapsed * 1000))
        span.set_attribute("gate.findings_count", len(findings))

        if not passed:
            span.set_status(trace.StatusCode.ERROR, "Gate 4 review found critical issues")
        else:
            span.set_status(trace.StatusCode.OK)

        return GateResult(
            gate_name="gate_4_review",
            passed=passed,
            findings=findings,
            duration_seconds=round(elapsed, 3),
        ).model_dump()


# ---------------------------------------------------------------------------
# Gate 5: Cost / Usage Check
# ---------------------------------------------------------------------------


@mcp.tool(
    description=(
        "Gate 5 — Usage check: verifies the agent run stayed within reasonable "
        "resource bounds. Checks turn count and token usage against thresholds. "
        "On Claude Code Max, cost is always $0; this gates on turns/tokens."
    ),
    tags={"gates", "cost"},
)
def run_gate_5_cost(
    num_turns: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    max_turns: int = 25,
    max_input_tokens: int = 500_000,
    max_output_tokens: int = 100_000,
) -> dict:
    """Check that the agent run stayed within resource bounds."""
    with tracer.start_as_current_span(
        "gates.gate_5_cost",
        attributes={"gate.name": "cost", "gate.order": 5},
    ) as span:
        start = time.monotonic()
        findings: list[Finding] = []
        passed = True

        # --- Check turn count ---
        if num_turns > max_turns:
            findings.append(
                Finding(
                    severity="critical",
                    message=(
                        f"Turn count exceeded: {num_turns} turns "
                        f"(max {max_turns})"
                    ),
                    rule="max_turns",
                )
            )
            passed = False

        # --- Check input tokens ---
        if input_tokens > max_input_tokens:
            findings.append(
                Finding(
                    severity="critical",
                    message=(
                        f"Input token usage exceeded: {input_tokens:,} tokens "
                        f"(max {max_input_tokens:,})"
                    ),
                    rule="max_input_tokens",
                )
            )
            passed = False

        # --- Check output tokens ---
        if output_tokens > max_output_tokens:
            findings.append(
                Finding(
                    severity="critical",
                    message=(
                        f"Output token usage exceeded: {output_tokens:,} tokens "
                        f"(max {max_output_tokens:,})"
                    ),
                    rule="max_output_tokens",
                )
            )
            passed = False

        # --- Add usage summary ---
        total_tokens = input_tokens + output_tokens
        findings.append(
            Finding(
                severity="info",
                message=(
                    f"Usage summary: {num_turns} turns, "
                    f"{input_tokens:,} input tokens, "
                    f"{output_tokens:,} output tokens, "
                    f"{total_tokens:,} total tokens"
                ),
                rule="usage_summary",
            )
        )

        elapsed = time.monotonic() - start
        span.set_attribute("gate.status", "pass" if passed else "fail")
        span.set_attribute("gate.duration_ms", round(elapsed * 1000))
        span.set_attribute("gate.findings_count", len(findings))
        span.set_attribute("gate.num_turns", num_turns)
        span.set_attribute("gate.input_tokens", input_tokens)
        span.set_attribute("gate.output_tokens", output_tokens)
        span.set_attribute("gate.total_tokens", total_tokens)

        if not passed:
            span.set_status(
                trace.StatusCode.ERROR,
                "Gate 5 usage check exceeded thresholds",
            )
        else:
            span.set_status(trace.StatusCode.OK)

        return GateResult(
            gate_name="gate_5_cost",
            passed=passed,
            findings=findings,
            duration_seconds=round(elapsed, 3),
        ).model_dump()


# ---------------------------------------------------------------------------
# Run All Gates (fail-fast)
# ---------------------------------------------------------------------------


@mcp.tool(
    description=(
        "Run all quality gates in fail-fast order: "
        "Gate 0 (sanity) → [Gate 0.1 (differential, if Gate 0 fails)] → "
        "Gate 0.5 (relevance) → Gate 2 (secrets) → "
        "Gate 2.5 (dangerous ops) → Gate 3 (security) → Gate 4 (review) → "
        "Gate 5 (cost, informational). "
        "Stops at first failure (gates 0-4). Gate 5 is informational and never fails the suite. "
        "Returns overall pass/fail with per-gate results."
    ),
    tags={"gates", "suite"},
)
def run_all_gates(
    worktree_path: str,
    issue_title: str,
    issue_description: str,
    num_turns: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> dict:
    """Run gates 0 → 0.5 → 2 → 2.5 → 3 → 4 → 5 sequentially, stopping at first failure (0-4)."""
    with tracer.start_as_current_span(
        "gates.run_all",
        attributes={"gate.name": "run_all"},
    ) as span:
        suite_start = time.monotonic()
        gate_results: list[GateResult] = []
        first_failure: str | None = None
        total_gates = 7

        def _fail_fast(gate_result: GateResult) -> dict | None:
            """Check if we should stop. Returns suite result dict on failure, None to continue."""
            nonlocal first_failure
            if not gate_result.passed and not gate_result.skipped:
                first_failure = gate_result.gate_name
                elapsed = time.monotonic() - suite_start
                passed_count = sum(1 for g in gate_results if g.passed)
                span.set_attribute("gates.total", total_gates)
                span.set_attribute("gates.passed", passed_count)
                span.set_attribute("gates.failed", 1)
                span.set_attribute("gates.first_failure", first_failure)
                span.set_attribute("gates.total_duration_ms", round(elapsed * 1000))
                span.set_status(trace.StatusCode.ERROR, f"Failed at {first_failure}")
                return GateSuiteResult(
                    overall_passed=False,
                    gate_results=gate_results,
                    first_failure=first_failure,
                    total_duration_seconds=round(elapsed, 3),
                ).model_dump()
            return None

        # --- Gate 0: Sanity ---
        g0 = GateResult(**run_gate_0_sanity(worktree_path))
        gate_results.append(g0)
        if not g0.passed and not g0.skipped:
            if _is_differential_enabled(worktree_path):
                g01 = GateResult(
                    **run_gate_01_differential(
                        worktree_path,
                        gate_0_findings=[f.model_dump() for f in g0.findings],
                    )
                )
                gate_results.append(g01)
                if not g01.passed:
                    if (bail := _fail_fast(g01)) is not None:
                        return bail
                # Gate 0.1 passed — all failures pre-existing, continue pipeline
            else:
                if (bail := _fail_fast(g0)) is not None:
                    return bail

        # --- Gate 0.5: Relevance ---
        g05 = GateResult(**run_gate_05_relevance(worktree_path, issue_title, issue_description))
        gate_results.append(g05)
        if (bail := _fail_fast(g05)) is not None:
            return bail

        # --- Gate 2: Secrets ---
        g2 = GateResult(**run_gate_2_secrets(worktree_path))
        gate_results.append(g2)
        if (bail := _fail_fast(g2)) is not None:
            return bail

        # --- Gate 2.5: Dangerous Ops ---
        g25 = GateResult(**run_gate_25_dangerous_ops(worktree_path))
        gate_results.append(g25)
        if (bail := _fail_fast(g25)) is not None:
            return bail

        # --- Gate 3: Security ---
        g3 = GateResult(**run_gate_3_security(worktree_path))
        gate_results.append(g3)
        if (bail := _fail_fast(g3)) is not None:
            return bail

        # --- Gate 4: Review ---
        g4 = GateResult(**run_gate_4_review(worktree_path, issue_title, issue_description))
        gate_results.append(g4)

        if not g4.passed:
            first_failure = g4.gate_name

        # --- Gate 5: Cost (informational, never fail-fast) ---
        g5 = GateResult(**run_gate_5_cost(
            num_turns=num_turns,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        ))
        gate_results.append(g5)

        elapsed = time.monotonic() - suite_start
        overall_passed = all(g.passed or g.skipped for g in gate_results)
        passed_count = sum(1 for g in gate_results if g.passed)
        failed_count = sum(1 for g in gate_results if not g.passed and not g.skipped)

        span.set_attribute("gates.total", total_gates)
        span.set_attribute("gates.passed", passed_count)
        span.set_attribute("gates.failed", failed_count)
        span.set_attribute("gates.total_duration_ms", round(elapsed * 1000))
        if first_failure:
            span.set_attribute("gates.first_failure", first_failure)
            span.set_status(trace.StatusCode.ERROR, f"Failed at {first_failure}")
        else:
            span.set_status(trace.StatusCode.OK)

        return GateSuiteResult(
            overall_passed=overall_passed,
            gate_results=gate_results,
            first_failure=first_failure,
            total_duration_seconds=round(elapsed, 3),
        ).model_dump()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
