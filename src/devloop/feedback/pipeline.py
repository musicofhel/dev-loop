"""Pipeline orchestrators for dev-loop tracer bullets.

TB-1 through TB-6 are vertical slices through all six layers:

    intake -> orchestration -> runtime -> gates -> observability -> feedback

Usage::

    from devloop.feedback.pipeline import run_tb1, run_tb2
    result = run_tb1(issue_id="dl-abc", repo_path="/home/user/some-repo")
    result = run_tb2(issue_id="dl-xyz", repo_path="/home/user/some-repo")

Functions are synchronous (single-threaded, blocking). They return
result dicts with full details of what happened at each phase.
"""

from __future__ import annotations

import logging
import signal
import subprocess
from pathlib import Path

import yaml
from opentelemetry import trace

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pipeline timeout (R-4)
# ---------------------------------------------------------------------------

PIPELINE_TIMEOUT_SECONDS = 1200  # 20 minutes total cap


class PipelineTimeout(Exception):
    """Raised when a pipeline run exceeds the total time budget."""
    pass


def _timeout_handler(signum: int, frame: object) -> None:
    raise PipelineTimeout(
        f"Pipeline exceeded {PIPELINE_TIMEOUT_SECONDS}s total budget"
    )


def _set_pipeline_timeout(seconds: int = PIPELINE_TIMEOUT_SECONDS) -> None:
    """Arm a SIGALRM-based timeout for the pipeline (Unix only)."""
    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(seconds)


def _clear_pipeline_timeout() -> None:
    """Disarm the pipeline timeout."""
    signal.alarm(0)


# ---------------------------------------------------------------------------
# OTel tracers — use the global provider set up by init_tracing()
# ---------------------------------------------------------------------------

tracer_tb2 = trace.get_tracer("tb2", "0.1.0")
tracer_tb3 = trace.get_tracer("tb3", "0.1.0")


# ---------------------------------------------------------------------------
# TB-2 test fixtures path
# ---------------------------------------------------------------------------

_DEVLOOP_ROOT = str(Path(__file__).resolve().parents[3])
_FIXTURES_DIR = Path(_DEVLOOP_ROOT) / "test-fixtures"
_CONFIG_DIR = Path(_DEVLOOP_ROOT) / "config"
_CAPABILITIES_CONFIG = _CONFIG_DIR / "capabilities.yaml"

def _latest_failure_gate(all_gate_failures: list[dict]) -> str | None:
    """Extract the gate name from the most recent failure record (L8 fix).

    Used in retry log messages so they report the *current* failure,
    not the stale initial gate_suite.first_failure.
    """
    if not all_gate_failures:
        return None
    last = all_gate_failures[-1]
    # GateSuiteResult-shaped dict
    if "first_failure" in last:
        return last["first_failure"]
    # Individual gate results
    if "gate_results" in last:
        for gr in last["gate_results"]:
            if not gr.get("passed", True):
                return gr.get("gate_name")
    # Single gate result dict
    if "gate_name" in last:
        return last.get("gate_name")
    return None


def _unclaim_issue(issue_id: str, repo_path: str | None = None) -> None:
    """Release a claimed issue back to open status (M8 fix).

    Called in finally blocks when a pipeline fails without completing
    successfully, so the issue doesn't stay stuck as in_progress.
    """
    cwd = repo_path or _DEVLOOP_ROOT
    try:
        subprocess.run(
            ["br", "update", issue_id, "--status", "open"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            cwd=cwd,
        )
        logger.info("Unclaimed issue %s (set to open)", issue_id)
    except Exception:
        logger.warning("Failed to unclaim issue %s", issue_id)


def _load_allowed_tools(repo_path: str) -> list[str] | None:
    """Load per-project allowed tools from capabilities.yaml."""
    if not _CAPABILITIES_CONFIG.exists():
        return None
    try:
        with open(_CAPABILITIES_CONFIG) as f:
            config = yaml.safe_load(f) or {}
    except Exception:
        return None
    # Match by repo basename (e.g. "OOTestProject1")
    repo_name = Path(repo_path).name
    project = config.get(repo_name, {})
    tools = project.get("allowed_tools")
    if isinstance(tools, list) and tools:
        return tools
    return None


# ---------------------------------------------------------------------------
# Shared helpers (kept in pipeline.py — used by other TB modules)
# ---------------------------------------------------------------------------


def _span_id_hex(span: trace.Span) -> str:
    """Extract the hex span ID from an OTel span."""
    ctx = span.get_span_context()
    return format(ctx.span_id, "016x")


def _trace_id_hex(span: trace.Span) -> str:
    """Extract the hex trace ID from an OTel span."""
    ctx = span.get_span_context()
    return format(ctx.trace_id, "032x")


# ---------------------------------------------------------------------------
# Backward-compatible re-exports for extracted modules
# ---------------------------------------------------------------------------
# Uses __getattr__ to avoid circular imports — tb3_security, tb4_runaway,
# tb5_cascade and tb6_replay import from this module, so top-level imports
# here would create cycles.

_TB1_RE_EXPORTS = {
    "run_tb1",
}

_TB2_RE_EXPORTS = {
    "_make_forced_failure",
    "_seed_test_fixture",
    "_verify_blocked_status",
    "run_tb2",
}

_TB3_RE_EXPORTS = {
    "_extract_security_findings",
    "_make_forced_security_failure",
    "_seed_vulnerable_code",
    "run_tb3",
}

_TB4_RE_EXPORTS = {
    "run_tb4",
    "tracer_tb4",
}


_TB5_RE_EXPORTS = {
    "_create_cascade_issue",
    "_get_changed_files",
    "_get_source_issue_details",
    "_load_dependency_map",
    "_match_watches",
    "_report_cascade_outcome",
    "_resolve_repo_path",
    "find_cascade_targets",
    "run_tb5",
    "tracer_tb5",
}

_TB6_RE_EXPORTS = {
    "_format_session_timeline",
    "_generate_session_id",
    "_load_session",
    "_parse_session_events",
    "_save_session",
    "_suggest_claude_md_fix",
    "_SESSIONS_DIR",
    "replay_session",
    "run_tb6",
}

_TB7_RE_EXPORTS = {
    "run_tb7",
}


def __getattr__(name: str):  # noqa: E302
    if name in _TB1_RE_EXPORTS:
        import devloop.feedback.tb1_golden_path as _tb1  # noqa: F811

        return getattr(_tb1, name)
    if name in _TB2_RE_EXPORTS:
        import devloop.feedback.tb2_retry as _tb2  # noqa: F811

        return getattr(_tb2, name)
    if name in _TB3_RE_EXPORTS:
        import devloop.feedback.tb3_security as _tb3  # noqa: F811

        return getattr(_tb3, name)
    if name in _TB4_RE_EXPORTS:
        import devloop.feedback.tb4_runaway as _tb4  # noqa: F811

        return getattr(_tb4, name)
    if name in _TB5_RE_EXPORTS:
        import devloop.feedback.tb5_cascade as _tb5  # noqa: F811

        return getattr(_tb5, name)
    if name in _TB6_RE_EXPORTS:
        import devloop.feedback.tb6_replay as _tb6  # noqa: F811

        return getattr(_tb6, name)
    if name in _TB7_RE_EXPORTS:
        import devloop.feedback.tb7_llmops as _tb7  # noqa: F811

        return getattr(_tb7, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
