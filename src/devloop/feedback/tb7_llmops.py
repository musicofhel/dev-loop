"""TB-7: LLMOps A/B Comparison — DSPy optimized path vs CLI baseline.

Proves Layer 7 works end-to-end: artifact loaded, DSPy review executes,
compared with CLI baseline, difference measurable.

Usage::

    from devloop.feedback.tb7_llmops import run_tb7
    result = run_tb7(repo_path="/home/user/OOTestProject1")
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from opentelemetry import trace

from devloop.feedback.types import TB7Result

logger = logging.getLogger(__name__)

tracer = trace.get_tracer("tb7", "0.1.0")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEST_DIFF = """\
diff --git a/main.py b/main.py
--- a/main.py
+++ b/main.py
@@ -1,5 +1,12 @@
+import os
+import sqlite3
+
+password = os.environ["DB_PASS"]
+
 def connect():
-    pass
+    db = sqlite3.connect("app.db")
+    query = f"SELECT * FROM users WHERE name = '{os.environ.get('USER_INPUT')}'"
+    return db.execute(query)
"""

_TEST_ISSUE_CONTEXT = "Add database connection with user lookup"
_TEST_CRITERIA = "missing_error_handling_at_boundaries,logic_errors,race_conditions"


def _get_repo_diff(repo_path: str) -> str | None:
    """Try to get a real diff from the repo, fall back to None."""
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD~1", "--", "*.py"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _word_overlap(a: str, b: str) -> float:
    """Jaccard similarity between word sets."""
    import re

    def normalize(t: str) -> set[str]:
        t = t.lower().replace("`", "").replace("'", "").replace('"', "")
        return set(re.sub(r"\s+", " ", t).strip().split())

    a_w, b_w = normalize(a), normalize(b)
    if not a_w or not b_w:
        return 0.0
    return len(a_w & b_w) / len(a_w | b_w)


def _run_dspy_path(
    diff: str, issue_context: str, criteria: str
) -> tuple[list[dict], float]:
    """Run Gate 4 review via DSPy optimized path. Returns (findings, latency)."""
    import dspy

    from devloop.llmops.programs import load_program
    from devloop.llmops.server import _load_llmops_config
    from devloop.llmops.types import OptimizationConfig

    cfg = _load_llmops_config()
    api_key = os.environ.get(cfg.api_key_env)
    if not api_key:
        raise RuntimeError(f"{cfg.api_key_env} not set")

    pcfg = cfg.programs.get("code_review", OptimizationConfig())
    if cfg.provider == "openrouter":
        model_str = f"openrouter/anthropic/{pcfg.model}"
    else:
        model_str = f"anthropic/{pcfg.model}"

    dspy.configure(lm=dspy.LM(model_str, api_key=api_key, max_tokens=2048))
    module = load_program("code_review")

    start = time.monotonic()
    result = module(
        diff=diff,
        issue_context=issue_context,
        review_criteria=criteria,
    )
    latency = time.monotonic() - start

    findings = json.loads(result.findings_json)
    if not isinstance(findings, list):
        raise ValueError("DSPy findings_json is not a JSON array")

    return findings, latency


def _run_cli_path(
    diff: str, issue_context: str, criteria: str
) -> tuple[list[dict], float]:
    """Run Gate 4 review via CLI baseline (claude --print). Returns (findings, latency)."""
    claude_path = shutil.which("claude")
    if claude_path is None:
        raise FileNotFoundError("claude CLI not found on PATH")

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
        },
        "required": ["findings"],
    })

    prompt = (
        "You are a senior code reviewer performing an automated quality gate check.\n\n"
        f"## Issue Context\n**Title:** {issue_context}\n\n"
        f"## Review Criteria\nCheck the diff for: {criteria}\n\n"
        f"## Diff to Review\n```\n{diff}\n```\n\n"
        "Return findings as JSON matching the provided schema."
    )

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    cmd = [
        claude_path,
        "--print",
        "--output-format", "json",
        "--model", "claude-sonnet-4-6",
        "--max-turns", "1",
        "--json-schema", review_schema,
        prompt,
    ]

    start = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)
    latency = time.monotonic() - start

    if proc.returncode != 0:
        raise RuntimeError(f"claude --print failed: {proc.stderr[:500]}")

    raw = proc.stdout.strip()
    parsed = json.loads(raw)

    # Handle multiple output formats from claude --print --output-format json:
    # 1. Direct {"findings": [...]}
    # 2. {"result": "..."} wrapping
    # 3. Stream-json array of events (find StructuredOutput tool_use)
    findings: list[dict] = []
    if isinstance(parsed, dict):
        if "result" in parsed:
            inner = parsed["result"]
            if isinstance(inner, str):
                inner = json.loads(inner)
            if isinstance(inner, dict):
                findings = inner.get("findings", [])
        else:
            findings = parsed.get("findings", [])
    elif isinstance(parsed, list):
        # Stream-json array: scan for StructuredOutput tool_use or result event
        for event in parsed:
            if not isinstance(event, dict):
                continue
            # Check assistant messages for StructuredOutput tool_use
            msg = event.get("message", {})
            if not isinstance(msg, dict):
                continue
            for content in (msg.get("content") or []):
                if (isinstance(content, dict)
                        and content.get("type") == "tool_use"
                        and content.get("name") == "StructuredOutput"
                        and isinstance(content.get("input"), dict)):
                    findings = content["input"].get("findings", [])
                    break
            if findings:
                break

    return findings, latency


def _compare_findings(
    dspy_findings: list[dict], cli_findings: list[dict]
) -> tuple[float, float]:
    """Compare findings from both paths. Returns (message_overlap, severity_agreement)."""
    if not dspy_findings or not cli_findings:
        return 0.0, 0.0

    dspy_msgs = [str(f.get("message", "")) for f in dspy_findings]
    cli_msgs = [str(f.get("message", "")) for f in cli_findings]

    # Match DSPy findings to CLI findings by best message overlap
    matched_cli: set[int] = set()
    similarities: list[float] = []
    severity_matches = 0
    severity_total = 0

    for di, dm in enumerate(dspy_msgs):
        best_score = 0.0
        best_ci = -1
        for ci, cm in enumerate(cli_msgs):
            if ci in matched_cli:
                continue
            sim = _word_overlap(dm, cm)
            if sim > best_score:
                best_score = sim
                best_ci = ci
        if best_score >= 0.15 and best_ci >= 0:
            matched_cli.add(best_ci)
            similarities.append(best_score)
            severity_total += 1
            if dspy_findings[di].get("severity") == cli_findings[best_ci].get("severity"):
                severity_matches += 1

    avg_sim = sum(similarities) / len(similarities) if similarities else 0.0
    sev_agree = severity_matches / severity_total if severity_total > 0 else 0.0

    return round(avg_sim, 3), round(sev_agree, 3)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_tb7(repo_path: str) -> dict:
    """Run TB-7: LLMOps A/B comparison.

    Phases:
        1. Validate prerequisites (API key, artifact, training data)
        2. Get a diff (from repo or use test diff)
        3. Run DSPy optimized path — record findings + latency
        4. Run CLI baseline path — record findings + latency
        5. Compare: finding count, severity overlap, message similarity, latency ratio
        6. Return TB7Result

    Args:
        repo_path: Absolute path to a git repository (used for real diffs).

    Returns:
        A dict (TB7Result) with comparison metrics.
    """
    pipeline_start = time.monotonic()

    with tracer.start_as_current_span(
        "tb7.run",
        attributes={"tb7.repo_path": repo_path},
    ) as root_span:

        # ----------------------------------------------------------
        # Phase 1: Validate prerequisites
        # ----------------------------------------------------------
        with tracer.start_as_current_span(
            "tb7.phase.validate",
            attributes={"tb7.phase": "validate"},
        ) as val_span:
            from devloop.llmops.server import _latest_artifact, _load_llmops_config

            cfg = _load_llmops_config()

            # Check API key
            api_key = os.environ.get(cfg.api_key_env)
            if not api_key:
                elapsed = time.monotonic() - pipeline_start
                val_span.set_status(trace.StatusCode.ERROR, "API key not set")
                return TB7Result(
                    repo_path=repo_path,
                    success=False,
                    phase="validate",
                    error=f"{cfg.api_key_env} not set",
                    duration_seconds=round(elapsed, 2),
                ).model_dump()

            # Check artifact
            artifact = _latest_artifact("code_review")
            artifact_version = artifact.version if artifact else None
            artifact_score = artifact.metric_score if artifact else None

            if artifact is None:
                val_span.set_attribute("tb7.artifact_exists", False)
                logger.warning(
                    "No optimization artifact — DSPy uses unoptimized module"
                )
            else:
                val_span.set_attribute("tb7.artifact_exists", True)
                val_span.set_attribute("tb7.artifact_version", artifact_version)

            # Check training data count
            training_path = Path(cfg.training_dir).expanduser() / "code_review.jsonl"
            training_count = 0
            if training_path.exists():
                with open(training_path) as f:
                    training_count = sum(1 for line in f if line.strip())
            val_span.set_attribute("tb7.training_examples", training_count)

            if training_count < 5:
                elapsed = time.monotonic() - pipeline_start
                val_span.set_status(trace.StatusCode.ERROR, "Insufficient training data")
                return TB7Result(
                    repo_path=repo_path,
                    success=False,
                    phase="validate",
                    error=(
                        f"Need >= 5 training examples, found {training_count}. "
                        "Run 'just llmops-export'."
                    ),
                    training_example_count=training_count,
                    duration_seconds=round(elapsed, 2),
                ).model_dump()

            # Check LLMOps is enabled
            if not cfg.enabled:
                elapsed = time.monotonic() - pipeline_start
                return TB7Result(
                    repo_path=repo_path,
                    success=False,
                    phase="validate",
                    error="LLMOps not enabled in config/llmops.yaml",
                    duration_seconds=round(elapsed, 2),
                ).model_dump()

        # ----------------------------------------------------------
        # Phase 2: Get diff
        # ----------------------------------------------------------
        with tracer.start_as_current_span(
            "tb7.phase.get_diff",
            attributes={"tb7.phase": "get_diff"},
        ) as diff_span:
            diff = _get_repo_diff(repo_path)
            if diff:
                issue_context = "Recent changes in repository"
                diff_span.set_attribute("tb7.diff_source", "repo")
            else:
                diff = _TEST_DIFF
                issue_context = _TEST_ISSUE_CONTEXT
                diff_span.set_attribute("tb7.diff_source", "test_fixture")
            diff_span.set_attribute("tb7.diff_length", len(diff))

        # ----------------------------------------------------------
        # Phase 3: Run DSPy path
        # ----------------------------------------------------------
        dspy_findings: list[dict] = []
        dspy_latency = 0.0
        with tracer.start_as_current_span(
            "tb7.phase.dspy_path",
            attributes={"tb7.phase": "dspy_path"},
        ) as dspy_span:
            try:
                dspy_findings, dspy_latency = _run_dspy_path(
                    diff, issue_context, _TEST_CRITERIA
                )
                dspy_span.set_attribute("tb7.dspy_finding_count", len(dspy_findings))
                dspy_span.set_attribute("tb7.dspy_latency", dspy_latency)
            except Exception as exc:
                elapsed = time.monotonic() - pipeline_start
                dspy_span.set_status(trace.StatusCode.ERROR, str(exc))
                root_span.set_status(trace.StatusCode.ERROR, "DSPy path failed")
                return TB7Result(
                    repo_path=repo_path,
                    success=False,
                    phase="dspy_path",
                    error=f"DSPy path failed: {exc}",
                    artifact_version=artifact_version,
                    artifact_metric_score=artifact_score,
                    training_example_count=training_count,
                    duration_seconds=round(elapsed, 2),
                ).model_dump()

        # ----------------------------------------------------------
        # Phase 4: Run CLI path
        # ----------------------------------------------------------
        cli_findings: list[dict] = []
        cli_latency = 0.0
        with tracer.start_as_current_span(
            "tb7.phase.cli_path",
            attributes={"tb7.phase": "cli_path"},
        ) as cli_span:
            try:
                cli_findings, cli_latency = _run_cli_path(
                    diff, issue_context, _TEST_CRITERIA
                )
                cli_span.set_attribute("tb7.cli_finding_count", len(cli_findings))
                cli_span.set_attribute("tb7.cli_latency", cli_latency)
            except Exception as exc:
                elapsed = time.monotonic() - pipeline_start
                cli_span.set_status(trace.StatusCode.ERROR, str(exc))
                root_span.set_status(trace.StatusCode.ERROR, "CLI path failed")
                return TB7Result(
                    repo_path=repo_path,
                    success=False,
                    phase="cli_path",
                    error=f"CLI path failed: {exc}",
                    artifact_version=artifact_version,
                    artifact_metric_score=artifact_score,
                    training_example_count=training_count,
                    dspy_finding_count=len(dspy_findings),
                    dspy_findings=dspy_findings,
                    dspy_latency_seconds=round(dspy_latency, 2),
                    duration_seconds=round(elapsed, 2),
                ).model_dump()

        # ----------------------------------------------------------
        # Phase 5: Compare
        # ----------------------------------------------------------
        with tracer.start_as_current_span(
            "tb7.phase.compare",
            attributes={"tb7.phase": "compare"},
        ) as cmp_span:
            msg_overlap, sev_agreement = _compare_findings(dspy_findings, cli_findings)
            finding_delta = len(dspy_findings) - len(cli_findings)
            latency_ratio = dspy_latency / cli_latency if cli_latency > 0 else 0.0

            cmp_span.set_attribute("tb7.message_overlap", msg_overlap)
            cmp_span.set_attribute("tb7.severity_agreement", sev_agreement)
            cmp_span.set_attribute("tb7.finding_delta", finding_delta)
            cmp_span.set_attribute("tb7.latency_ratio", latency_ratio)

        elapsed = time.monotonic() - pipeline_start
        root_span.set_status(trace.StatusCode.OK)

        return TB7Result(
            repo_path=repo_path,
            success=True,
            phase="compare",
            duration_seconds=round(elapsed, 2),
            artifact_version=artifact_version,
            artifact_metric_score=artifact_score,
            training_example_count=training_count,
            dspy_finding_count=len(dspy_findings),
            cli_finding_count=len(cli_findings),
            dspy_findings=dspy_findings,
            cli_findings=cli_findings,
            dspy_latency_seconds=round(dspy_latency, 2),
            cli_latency_seconds=round(cli_latency, 2),
            latency_ratio=round(latency_ratio, 2),
            finding_count_delta=finding_delta,
            message_overlap_score=msg_overlap,
            severity_agreement_rate=sev_agreement,
        ).model_dump()
