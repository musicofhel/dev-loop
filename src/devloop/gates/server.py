"""Quality-gates MCP server — sequential automated checks on agent output.

This is Layer 4 of the dev-loop harness. Every agent output passes through
a gauntlet of automated checks before it becomes a PR. Gates run sequentially
— fail fast, fail cheap.

TB-1 wires only three gates:
  Gate 0 (Sanity)  — compile + test
  Gate 2 (Secrets) — gitleaks scan
  Gate 4 (Review)  — LLM-as-judge code review

Run standalone:  uv run python -m devloop.gates.server
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import yaml
from fastmcp import FastMCP
from opentelemetry import trace

from devloop.gates.types import Finding, GateResult, GateSuiteResult

# ---------------------------------------------------------------------------
# OTel tracer for gates layer
# ---------------------------------------------------------------------------

tracer = trace.get_tracer("gates", "0.1.0")

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
            # Find the merge-base to detect new commits on the worktree branch
            base_r = _run_cmd(
                ["git", "merge-base", "HEAD", "HEAD~10"],
                cwd=worktree,
            )
            if base_r.returncode == 0 and base_r.stdout.strip():
                base = base_r.stdout.strip()
                r = _run_cmd(
                    ["git", "diff", base, "HEAD", "--stat"],
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
            # Ensure deps are installed in the worktree before testing
            _run_cmd(["uv", "sync", "--dev"], cwd=worktree, timeout=120)
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

        prompt = _build_review_prompt(diff_text, issue_title, issue_description, config)

        # --- Call Claude via CLI (uses existing Claude Code auth) ---
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
            # Try to extract JSON from the response (handle markdown fences)
            text = response_text.strip()
            if text.startswith("```"):
                # Strip markdown code fences
                lines = text.split("\n")
                # Remove first line (```json or ```) and last line (```)
                json_lines = []
                in_fence = False
                for line in lines:
                    if line.strip().startswith("```") and not in_fence:
                        in_fence = True
                        continue
                    if line.strip() == "```" and in_fence:
                        break
                    if in_fence:
                        json_lines.append(line)
                text = "\n".join(json_lines)

            parsed = json.loads(text)
        except json.JSONDecodeError:
            # Try to find JSON object in the response
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
                    severity="warning",
                    message=(
                        "Could not parse review response as JSON. "
                        f"Raw response: {response_text[:500]}"
                    ),
                )
            )
        else:
            # Extract findings from parsed response
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
# Run All Gates (fail-fast)
# ---------------------------------------------------------------------------


@mcp.tool(
    description=(
        "Run all TB-1 quality gates in fail-fast order: "
        "Gate 0 (sanity) → Gate 2 (secrets) → Gate 4 (review). "
        "Stops at first failure. Returns overall pass/fail with per-gate results."
    ),
    tags={"gates", "suite"},
)
def run_all_gates(
    worktree_path: str,
    issue_title: str,
    issue_description: str,
) -> dict:
    """Run gates 0 → 2 → 4 sequentially, stopping at first failure."""
    with tracer.start_as_current_span(
        "gates.run_all",
        attributes={"gate.name": "run_all"},
    ) as span:
        suite_start = time.monotonic()
        gate_results: list[GateResult] = []
        first_failure: str | None = None

        # --- Gate 0: Sanity ---
        g0_raw = run_gate_0_sanity(worktree_path)
        g0 = GateResult(**g0_raw)
        gate_results.append(g0)

        if not g0.passed:
            first_failure = g0.gate_name
            elapsed = time.monotonic() - suite_start
            span.set_attribute("gates.total", 3)
            span.set_attribute("gates.passed", 0)
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

        # --- Gate 2: Secrets ---
        g2_raw = run_gate_2_secrets(worktree_path)
        g2 = GateResult(**g2_raw)
        gate_results.append(g2)

        if not g2.passed:
            first_failure = g2.gate_name
            elapsed = time.monotonic() - suite_start
            passed_count = sum(1 for g in gate_results if g.passed)
            span.set_attribute("gates.total", 3)
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

        # --- Gate 4: Review ---
        g4_raw = run_gate_4_review(worktree_path, issue_title, issue_description)
        g4 = GateResult(**g4_raw)
        gate_results.append(g4)

        if not g4.passed:
            first_failure = g4.gate_name

        elapsed = time.monotonic() - suite_start
        overall_passed = all(g.passed for g in gate_results)
        passed_count = sum(1 for g in gate_results if g.passed)
        failed_count = sum(1 for g in gate_results if not g.passed)

        span.set_attribute("gates.total", 3)
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
