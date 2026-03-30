"""TB-5: Cross-Repo Cascade — dependency detection, issue creation, cascade run.

Extracted from pipeline.py to keep the main module manageable.

Usage::

    from devloop.feedback.tb5_cascade import run_tb5
    result = run_tb5(
        source_issue_id="dl-abc",
        source_repo_path="/home/user/source-repo",
        target_repo_path="/home/user/target-repo",
    )
"""

from __future__ import annotations

import fnmatch
import json
import logging
import subprocess
import time
from pathlib import Path

import yaml
from opentelemetry import trace

from devloop.feedback.pipeline import (
    _clear_pipeline_timeout,
    _set_pipeline_timeout,
)
from devloop.feedback.types import TB5Result
from devloop.observability.tracing import init_tracing

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config paths (same as pipeline.py)
# ---------------------------------------------------------------------------

_DEVLOOP_ROOT = str(Path(__file__).resolve().parents[3])
_CONFIG_DIR = Path(_DEVLOOP_ROOT) / "config"

# ---------------------------------------------------------------------------
# OTel tracer
# ---------------------------------------------------------------------------

tracer_tb5 = trace.get_tracer("tb5", "0.1.0")

# ---------------------------------------------------------------------------
# TB-5 helpers — cross-repo cascade
# ---------------------------------------------------------------------------


def _load_dependency_map() -> list[dict]:
    """Load the dependency map from config/dependencies.yaml.

    Returns a list of dependency dicts, each with keys:
    source, target, watches (list[str]), type (str).
    """
    dep_file = _CONFIG_DIR / "dependencies.yaml"
    if not dep_file.exists():
        logger.warning("Dependency map not found: %s", dep_file)
        return []

    raw = yaml.safe_load(dep_file.read_text(encoding="utf-8"))
    if not raw or "dependencies" not in raw:
        logger.warning("Malformed dependency map: missing 'dependencies' key")
        return []

    return raw["dependencies"]


def _get_changed_files(repo_path: str, issue_id: str) -> list[str]:
    """Get files changed on the source branch vs main.

    Runs: git diff main..dl/<issue_id> --name-only
    Returns a list of relative file paths.
    """
    branch = f"dl/{issue_id}"
    result = subprocess.run(
        ["git", "diff", f"main..{branch}", "--name-only"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        error_msg = result.stderr.strip() or f"git diff failed with exit code {result.returncode}"
        raise RuntimeError(error_msg)

    files = [f for f in result.stdout.strip().split("\n") if f]
    return files


def _match_watches(changed_files: list[str], watches: list[str]) -> list[str]:
    """Match changed files against watch glob patterns.

    Uses fnmatch which treats ** and * equivalently (no / special handling),
    making it suitable for matching file paths against glob patterns like
    "src/api/**" or "src/types/**".

    Returns the list of watch patterns that had at least one match.
    """
    matched = []
    for pattern in watches:
        for f in changed_files:
            if fnmatch.fnmatch(f, pattern):
                matched.append(pattern)
                break
    return matched


def _resolve_repo_path(repo_name: str) -> str | None:
    """Resolve a repo name to its absolute path from config/dependencies.yaml.

    Returns None if the repo_paths map is missing or the name isn't found.
    """
    dep_file = _CONFIG_DIR / "dependencies.yaml"
    if not dep_file.exists():
        return None
    raw = yaml.safe_load(dep_file.read_text(encoding="utf-8"))
    if not raw:
        return None
    repo_paths = raw.get("repo_paths", {})
    return repo_paths.get(repo_name)


def find_cascade_targets(source_repo_path: str, issue_id: str) -> list[dict]:
    """Find all cascade targets for a completed issue.

    Called by TB-1 after successful PR creation. Returns a list of dicts,
    each with: target_repo_name, target_repo_path, matched_watches, dependency_type.
    Returns empty list if no cascades needed or on any error (fail-safe).
    """
    source_repo_name = Path(source_repo_path).name
    try:
        changed_files = _get_changed_files(source_repo_path, issue_id)
    except RuntimeError:
        logger.warning("TB-5: Could not get changed files for %s", issue_id)
        return []

    if not changed_files:
        return []

    deps = _load_dependency_map()
    targets = []
    for dep in deps:
        if dep["source"] != source_repo_name:
            continue
        watches = dep.get("watches", [])
        matched = _match_watches(changed_files, watches)
        if not matched:
            continue
        target_path = _resolve_repo_path(dep["target"])
        if target_path is None:
            logger.warning(
                "TB-5: No repo_path configured for target %s", dep["target"]
            )
            continue
        targets.append({
            "target_repo_name": dep["target"],
            "target_repo_path": target_path,
            "matched_watches": matched,
            "dependency_type": dep.get("type", "unknown"),
        })
    return targets


def _get_source_issue_details(issue_id: str, repo_path: str | None = None) -> dict:
    """Get source issue details via br show.

    Returns dict with title, description, labels keys.
    """
    result = subprocess.run(
        ["br", "show", issue_id, "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        cwd=repo_path or _DEVLOOP_ROOT,
    )
    if result.returncode != 0:
        error_msg = result.stderr.strip() or f"br show failed with exit code {result.returncode}"
        raise RuntimeError(error_msg)

    data = json.loads(result.stdout)
    # br show --format json may return a list (even for single ID)
    if isinstance(data, list):
        if not data:
            raise RuntimeError(f"Issue {issue_id} not found (empty result)")
        data = data[0]
    return {
        "title": data.get("title", ""),
        "description": data.get("description", ""),
        "labels": data.get("labels", []),
    }


def _create_cascade_issue(
    source_issue_id: str,
    source_title: str,
    target_repo_name: str,
    matched_watches: list[str],
    dependency_type: str,
    repo_path: str | None = None,
) -> str:
    """Create a cascade issue in beads for the target repo.

    The issue is created in the target repo's beads workspace (via repo_path cwd).
    Cross-repo cascades may have separate beads workspaces, so --parent is
    attempted first and dropped if the parent issue doesn't exist in the target DB.

    Returns the new issue ID.
    """
    title = f"[cascade] Adapt to upstream changes from {source_issue_id}: {source_title}"
    description = (
        f"Upstream issue {source_issue_id} changed files matching: {', '.join(matched_watches)}.\n"
        f"Dependency type: {dependency_type}.\n"
        f"Review and adapt {target_repo_name} as needed."
    )
    labels = f"cascade,repo:{target_repo_name}"

    # Try with --parent first (works when source+target share a beads workspace)
    cwd = repo_path or _DEVLOOP_ROOT
    cmd = [
        "br", "create", title,
        "--description", description,
        "--labels", labels,
        "--parent", source_issue_id,
        "--silent",
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        cwd=cwd,
    )

    # If --parent fails (cross-repo: parent issue not in target beads), retry without it
    if result.returncode != 0 and "ISSUE_NOT_FOUND" in (result.stdout + result.stderr):
        logger.info(
            "TB-5: Parent %s not in target beads; creating cascade issue without parent link",
            source_issue_id,
        )
        cmd_no_parent = [
            "br", "create", title,
            "--description", description,
            "--labels", labels,
            "--silent",
        ]
        result = subprocess.run(
            cmd_no_parent,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            cwd=cwd,
        )

    if result.returncode != 0:
        error_msg = result.stderr.strip() or f"br create failed with exit code {result.returncode}"
        raise RuntimeError(error_msg)

    # --silent outputs just the issue ID
    return result.stdout.strip()


def _report_cascade_outcome(
    source_issue_id: str,
    target_issue_id: str | None,
    target_repo_name: str,
    success: bool,
    cascade_skipped: bool,
    error: str | None = None,
    repo_path: str | None = None,
) -> bool:
    """Add a comment to the source issue reporting cascade outcome.

    Returns True if the comment was added successfully.
    """
    if cascade_skipped:
        msg = (
            f"Cascade to {target_repo_name}: SKIPPED — "
            "no changed files matched any watch patterns."
        )
    elif success:
        msg = (
            f"Cascade to {target_repo_name}: SUCCESS — "
            f"target issue {target_issue_id} completed."
        )
    else:
        detail = f" Error: {error}" if error else ""
        msg = (
            f"Cascade to {target_repo_name}: FAILED — "
            f"target issue {target_issue_id} did not complete.{detail}"
        )

    result = subprocess.run(
        ["br", "comments", "add", source_issue_id, "--message", msg],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        cwd=repo_path or _DEVLOOP_ROOT,
    )
    if result.returncode != 0:
        logger.warning(
            "Failed to add cascade outcome comment to %s: %s",
            source_issue_id,
            result.stderr.strip(),
        )
        return False
    return True


# ---------------------------------------------------------------------------
# TB-5 pipeline — Cross-Repo Cascade
# ---------------------------------------------------------------------------


def run_tb5(source_issue_id: str, source_repo_path: str, target_repo_path: str) -> dict:
    """Run the full TB-5 cross-repo cascade pipeline.

    Phases:
        1. Get source issue details (intake — br show)
        2. Detect changed files on source branch (git diff main..dl/<id>)
        3. Load dependency map + match changed files against watches
        4. Init OTel tracing (observability)
        5. Create cascade issue in beads for target repo (intake — br create)
        6. Run TB-1 pipeline on target repo with cascade issue (all 6 layers)
        7. Report outcome back to source issue (feedback — br comments add)
        8. Cleanup (flush OTel)

    Args:
        source_issue_id: The beads issue ID in the source repo.
        source_repo_path: Absolute path to the source git repository.
        target_repo_path: Absolute path to the target git repository.

    Returns:
        A dict (TB5Result) with the outcome of the cascade run.
    """
    # Import run_tb1 here to avoid circular import (pipeline -> tb5_cascade -> pipeline)
    from devloop.feedback.pipeline import run_tb1

    pipeline_start = time.monotonic()
    _set_pipeline_timeout()

    # Phase 4 — init tracing early so all subsequent spans are captured
    provider = init_tracing()

    # Derive repo names from paths for labeling
    source_repo_name = Path(source_repo_path).name
    target_repo_name = Path(target_repo_path).name

    with tracer_tb5.start_as_current_span(
        "tb5.run",
        attributes={
            "tb5.source_issue_id": source_issue_id,
            "tb5.source_repo_path": source_repo_path,
            "tb5.target_repo_path": target_repo_path,
            "tb5.source_repo": source_repo_name,
            "tb5.target_repo": target_repo_name,
        },
    ) as root_span:
        # Track state
        target_issue_id: str | None = None
        source_title = ""
        changed_files: list[str] = []
        matched_watches: list[str] = []
        dependency_type: str | None = None
        cascade_skipped = False
        tb1_result: dict | None = None
        source_comment_added = False

        try:
            # ----------------------------------------------------------
            # Phase 1: Get source issue details
            # ----------------------------------------------------------
            with tracer_tb5.start_as_current_span(
                "tb5.phase.get_source_issue",
                attributes={"tb5.phase": "get_source_issue"},
            ) as phase_span:
                source_details = _get_source_issue_details(source_issue_id, repo_path=source_repo_path)
                source_title = source_details["title"]
                phase_span.set_attribute("tb5.source_title", source_title)

            # ----------------------------------------------------------
            # Phase 2: Detect changed files on source branch
            # ----------------------------------------------------------
            with tracer_tb5.start_as_current_span(
                "tb5.phase.detect_changes",
                attributes={"tb5.phase": "detect_changes"},
            ) as phase_span:
                changed_files = _get_changed_files(source_repo_path, source_issue_id)
                phase_span.set_attribute("tb5.changed_file_count", len(changed_files))
                logger.info(
                    "TB-5: %d files changed on dl/%s",
                    len(changed_files),
                    source_issue_id,
                )

            # ----------------------------------------------------------
            # Phase 3: Load dependency map + match watches
            # ----------------------------------------------------------
            with tracer_tb5.start_as_current_span(
                "tb5.phase.match_dependencies",
                attributes={"tb5.phase": "match_dependencies"},
            ) as phase_span:
                deps = _load_dependency_map()

                # Find the dependency entry matching source→target
                dep_entry = None
                for dep in deps:
                    if dep["source"] == source_repo_name and dep["target"] == target_repo_name:
                        dep_entry = dep
                        break

                if dep_entry is None:
                    # No dependency configured — cascade skipped
                    cascade_skipped = True
                    phase_span.set_attribute("tb5.cascade_skipped", True)
                    phase_span.set_attribute("tb5.skip_reason", "no_dependency_configured")
                    logger.info(
                        "TB-5: No dependency %s → %s configured; skipping cascade",
                        source_repo_name,
                        target_repo_name,
                    )
                else:
                    watches = dep_entry.get("watches", [])
                    dependency_type = dep_entry.get("type", "unknown")
                    matched_watches = _match_watches(changed_files, watches)

                    phase_span.set_attribute("tb5.dependency_type", dependency_type)
                    phase_span.set_attribute("tb5.watch_count", len(watches))
                    phase_span.set_attribute("tb5.matched_watch_count", len(matched_watches))

                    if not matched_watches:
                        cascade_skipped = True
                        phase_span.set_attribute("tb5.cascade_skipped", True)
                        phase_span.set_attribute("tb5.skip_reason", "no_watch_match")
                        logger.info(
                            "TB-5: No watch patterns matched for %s → %s; skipping cascade",
                            source_repo_name,
                            target_repo_name,
                        )

            # ----------------------------------------------------------
            # Early return if cascade not needed
            # ----------------------------------------------------------
            if cascade_skipped:
                elapsed = time.monotonic() - pipeline_start
                root_span.set_attribute("tb5.outcome", "cascade_skipped")
                root_span.set_attribute("status.detail", "Cascade not needed")
                root_span.set_status(trace.StatusCode.OK)

                # Phase 7: Report skip to source issue
                with tracer_tb5.start_as_current_span(
                    "tb5.phase.report_outcome",
                    attributes={"tb5.phase": "report_outcome"},
                ):
                    source_comment_added = _report_cascade_outcome(
                        source_issue_id=source_issue_id,
                        target_issue_id=None,
                        target_repo_name=target_repo_name,
                        success=True,
                        cascade_skipped=True,
                        repo_path=source_repo_path,
                    )

                return TB5Result(
                    issue_id=source_issue_id,
                    repo_path=source_repo_path,
                    success=True,
                    phase="match_dependencies",
                    target_repo_path=target_repo_path,
                    changed_files=changed_files,
                    matched_watches=[],
                    dependency_type=dependency_type,
                    cascade_skipped=True,
                    source_comment_added=source_comment_added,
                    duration_seconds=round(elapsed, 2),
                ).model_dump()

            # ----------------------------------------------------------
            # Phase 5: Create cascade issue in beads for target repo
            # ----------------------------------------------------------
            with tracer_tb5.start_as_current_span(
                "tb5.phase.create_cascade_issue",
                attributes={"tb5.phase": "create_cascade_issue"},
            ) as phase_span:
                target_issue_id = _create_cascade_issue(
                    source_issue_id=source_issue_id,
                    source_title=source_title,
                    target_repo_name=target_repo_name,
                    matched_watches=matched_watches,
                    dependency_type=dependency_type or "unknown",
                    repo_path=target_repo_path,
                )
                phase_span.set_attribute("tb5.target_issue_id", target_issue_id)
                logger.info(
                    "TB-5: Created cascade issue %s for %s",
                    target_issue_id,
                    target_repo_name,
                )

            # ----------------------------------------------------------
            # Phase 6: Run TB-1 pipeline on target repo
            # ----------------------------------------------------------
            with tracer_tb5.start_as_current_span(
                "tb5.phase.cascade_tb1",
                attributes={
                    "tb5.phase": "cascade_tb1",
                    "tb5.target_issue_id": target_issue_id,
                    "tb5.target_repo_path": target_repo_path,
                },
            ) as phase_span:
                logger.info(
                    "TB-5: Running TB-1 on %s with issue %s",
                    target_repo_name,
                    target_issue_id,
                )
                tb1_result = run_tb1(target_issue_id, target_repo_path)
                tb1_success = tb1_result.get("success", False)
                phase_span.set_attribute("tb5.tb1_success", tb1_success)

            # ----------------------------------------------------------
            # Phase 7: Report outcome back to source issue
            # ----------------------------------------------------------
            with tracer_tb5.start_as_current_span(
                "tb5.phase.report_outcome",
                attributes={"tb5.phase": "report_outcome"},
            ):
                source_comment_added = _report_cascade_outcome(
                    source_issue_id=source_issue_id,
                    target_issue_id=target_issue_id,
                    target_repo_name=target_repo_name,
                    success=tb1_success,
                    cascade_skipped=False,
                    error=tb1_result.get("error"),
                    repo_path=source_repo_path,
                )

            elapsed = time.monotonic() - pipeline_start
            outcome = "success" if tb1_success else "tb1_failed"
            root_span.set_attribute("tb5.outcome", outcome)
            if tb1_success:
                root_span.set_attribute("status.detail", "Cascade completed successfully")
                root_span.set_status(trace.StatusCode.OK)
            else:
                root_span.set_status(trace.StatusCode.ERROR, "TB-1 failed on target repo")

            return TB5Result(
                issue_id=source_issue_id,
                repo_path=source_repo_path,
                success=tb1_success,
                phase="cascade_tb1" if not tb1_success else "report_outcome",
                target_repo_path=target_repo_path,
                target_issue_id=target_issue_id,
                changed_files=changed_files,
                matched_watches=matched_watches,
                dependency_type=dependency_type,
                cascade_skipped=False,
                tb1_result=tb1_result,
                source_comment_added=source_comment_added,
                duration_seconds=round(elapsed, 2),
            ).model_dump()

        except Exception as exc:
            elapsed = time.monotonic() - pipeline_start
            error_msg = f"Pipeline error: {type(exc).__name__}: {exc}"
            logger.exception("TB-5 pipeline error for issue %s", source_issue_id)
            root_span.set_status(trace.StatusCode.ERROR, error_msg)
            root_span.record_exception(exc)
            return TB5Result(
                issue_id=source_issue_id,
                repo_path=source_repo_path,
                success=False,
                phase="error",
                target_repo_path=target_repo_path,
                target_issue_id=target_issue_id,
                changed_files=changed_files,
                matched_watches=matched_watches,
                dependency_type=dependency_type,
                cascade_skipped=cascade_skipped,
                tb1_result=tb1_result,
                source_comment_added=source_comment_added,
                error=error_msg,
                duration_seconds=round(elapsed, 2),
            ).model_dump()

        finally:
            # ----------------------------------------------------------
            # Phase 8: Cleanup — flush OTel
            # ----------------------------------------------------------
            try:
                with tracer_tb5.start_as_current_span(
                    "tb5.phase.cleanup",
                    attributes={"tb5.phase": "cleanup"},
                ):
                    pass
            except Exception:
                pass

            if provider is not None:
                try:
                    provider.force_flush(timeout_millis=5000)
                except Exception:
                    pass

            _clear_pipeline_timeout()
