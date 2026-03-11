"""Orchestration MCP server — worktree setup, persona selection, CLAUDE.md overlay.

This is Layer 2 of the dev-loop harness. It takes a work item from intake and
prepares an isolated, configured environment for an agent run: worktree creation,
persona matching, and CLAUDE.md overlay generation.

Run standalone:  uv run python -m devloop.orchestration.server
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import yaml
from fastmcp import FastMCP
from opentelemetry import trace

from devloop.orchestration.types import (
    ClaudeOverlay,
    CleanupResult,
    PersonaConfig,
    WorktreeInfo,
)

# ---------------------------------------------------------------------------
# OTel tracer for orchestration layer
# ---------------------------------------------------------------------------

tracer = trace.get_tracer("orchestration", "0.1.0")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WORKTREE_BASE = Path("/tmp/dev-loop/worktrees")
BRANCH_PREFIX = "dl/"
CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"
AGENTS_CONFIG = CONFIG_DIR / "agents.yaml"

# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="orchestration",
    instructions=(
        "Orchestration layer for dev-loop. "
        "Use these tools to create isolated git worktrees for issues, "
        "select agent personas based on labels, generate CLAUDE.md overlays, "
        "and clean up worktrees after completion."
    ),
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(
    *args: str,
    cwd: str | Path | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a shell command and return the result."""
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        cwd=cwd,
        check=check,
    )


def _load_agents_config() -> dict:
    """Load and parse the agents.yaml configuration file."""
    if not AGENTS_CONFIG.exists():
        raise FileNotFoundError(f"Agents config not found at {AGENTS_CONFIG}")
    with open(AGENTS_CONFIG) as f:
        return yaml.safe_load(f)


def _match_persona(labels: list[str], config: dict) -> tuple[str, dict] | None:
    """Find the best persona match for a set of labels.

    Matching priority: first persona whose label set intersects with the
    provided labels wins.  If multiple personas match, the one with the
    most overlapping labels is preferred.
    """
    personas: dict = config.get("personas", {})
    best_name: str | None = None
    best_data: dict | None = None
    best_overlap = 0

    label_set = {lbl.lower() for lbl in labels}

    for name, data in personas.items():
        persona_labels = {lbl.lower() for lbl in data.get("labels", [])}
        overlap = len(label_set & persona_labels)
        if overlap > best_overlap:
            best_overlap = overlap
            best_name = name
            best_data = data

    if best_name is not None and best_data is not None:
        return best_name, best_data
    return None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool(
    description=(
        "Create an isolated git worktree and branch for an issue. "
        "The worktree is created at /tmp/dev-loop/worktrees/<issue_id> "
        "with branch name dl/<issue_id>. A .dev-loop-metadata.json file "
        "is written into the worktree root."
    ),
    tags={"orchestration", "worktree"},
)
def setup_worktree(issue_id: str, repo_path: str) -> dict:
    """Create a git worktree + branch for the given issue."""
    with tracer.start_as_current_span(
        "orchestration.setup_worktree",
        attributes={
            "orchestration.operation": "setup_worktree",
            "issue.id": issue_id,
            "worktree.repo_path": repo_path,
        },
    ) as span:
        branch_name = f"{BRANCH_PREFIX}{issue_id}"
        worktree_path = WORKTREE_BASE / issue_id

        # Validate repo path
        repo = Path(repo_path).resolve()
        if not (repo / ".git").exists() and not repo.joinpath(".git").is_file():
            # .git can be a file (for worktrees) or dir
            error_msg = f"Not a git repository: {repo_path}"
            span.set_status(trace.StatusCode.ERROR, error_msg)
            return WorktreeInfo(
                issue_id=issue_id,
                repo_path=repo_path,
                worktree_path=str(worktree_path),
                branch_name=branch_name,
                created_at=datetime.now(UTC).isoformat(),
                success=False,
                message=error_msg,
            ).model_dump()

        # Ensure parent dir exists
        WORKTREE_BASE.mkdir(parents=True, exist_ok=True)

        # Remove stale worktree if path already exists
        if worktree_path.exists():
            _run("git", "worktree", "remove", "--force", str(worktree_path), cwd=str(repo))
            if worktree_path.exists():
                shutil.rmtree(worktree_path, ignore_errors=True)

        # Create the worktree with a new branch
        result = _run(
            "git", "worktree", "add",
            "-b", branch_name,
            str(worktree_path),
            cwd=str(repo),
        )

        if result.returncode != 0:
            # Branch might already exist — try without -b
            result = _run(
                "git", "worktree", "add",
                str(worktree_path),
                branch_name,
                cwd=str(repo),
            )

        if result.returncode != 0:
            error_msg = (
                result.stderr.strip()
                or f"git worktree add failed with exit code {result.returncode}"
            )
            span.set_status(trace.StatusCode.ERROR, error_msg)
            return WorktreeInfo(
                issue_id=issue_id,
                repo_path=repo_path,
                worktree_path=str(worktree_path),
                branch_name=branch_name,
                created_at=datetime.now(UTC).isoformat(),
                success=False,
                message=error_msg,
            ).model_dump()

        # Write metadata file
        now = datetime.now(UTC).isoformat()
        metadata = {
            "issue_id": issue_id,
            "repo_path": str(repo),
            "branch_name": branch_name,
            "worktree_path": str(worktree_path),
            "created_at": now,
        }
        metadata_path = worktree_path / ".dev-loop-metadata.json"
        metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")

        span.set_attribute("worktree.path", str(worktree_path))
        span.set_attribute("worktree.branch", branch_name)
        span.set_status(trace.StatusCode.OK)

        return WorktreeInfo(
            issue_id=issue_id,
            repo_path=str(repo),
            worktree_path=str(worktree_path),
            branch_name=branch_name,
            created_at=now,
            success=True,
            message=f"Worktree created at {worktree_path} on branch {branch_name}",
        ).model_dump()


@mcp.tool(
    description=(
        "Select an agent persona based on issue labels. "
        "Reads config/agents.yaml and matches labels to the best persona. "
        "Returns the persona config including model, overlay, cost ceiling, "
        "and retry max."
    ),
    tags={"orchestration", "config"},
)
def select_persona(labels: list[str]) -> dict:
    """Match labels to a persona from agents.yaml."""
    with tracer.start_as_current_span(
        "orchestration.select_persona",
        attributes={
            "orchestration.operation": "select_persona",
            "persona.input_labels": labels,
        },
    ) as span:
        try:
            config = _load_agents_config()
        except FileNotFoundError as exc:
            error_msg = str(exc)
            span.set_status(trace.StatusCode.ERROR, error_msg)
            return {"error": error_msg, "labels": labels}

        match = _match_persona(labels, config)

        if match is None:
            # Default to feature persona if no match
            span.set_attribute("persona.matched", False)
            span.set_attribute("persona.fallback", "feature")
            personas = config.get("personas", {})
            fallback = personas.get("feature", {})
            persona = PersonaConfig(
                name="feature",
                labels=fallback.get("labels", ["feature"]),
                claude_md_overlay=fallback.get("claude_md_overlay", ""),
                cost_ceiling_default=fallback.get("cost_ceiling_default", 5.00),
                retry_max=fallback.get("retry_max", 1),
                model=fallback.get("model", "opus"),
            )
            span.set_status(trace.StatusCode.OK)
            return persona.model_dump()

        name, data = match
        span.set_attribute("persona.matched", True)
        span.set_attribute("persona.name", name)
        span.set_attribute("persona.model", data.get("model", "sonnet"))
        span.set_attribute("persona.cost_ceiling", data.get("cost_ceiling_default", 1.00))

        persona = PersonaConfig(
            name=name,
            labels=data.get("labels", []),
            claude_md_overlay=data.get("claude_md_overlay", ""),
            cost_ceiling_default=data.get("cost_ceiling_default", 1.00),
            retry_max=data.get("retry_max", 1),
            model=data.get("model", "sonnet"),
        )

        span.set_status(trace.StatusCode.OK)
        return persona.model_dump()


@mcp.tool(
    description=(
        "Generate a CLAUDE.md overlay for a worktree. Combines the persona's "
        "overlay from config/agents.yaml with issue-specific context (title, "
        "description) to produce the text that should be injected into the "
        "worktree's CLAUDE.md."
    ),
    tags={"orchestration", "config"},
)
def build_claude_md_overlay(
    persona: str,
    issue_title: str,
    issue_description: str,
) -> dict:
    """Generate CLAUDE.md overlay text from persona + issue context."""
    with tracer.start_as_current_span(
        "orchestration.build_claude_md_overlay",
        attributes={
            "orchestration.operation": "build_overlay",
            "persona.name": persona,
            "issue.title": issue_title,
        },
    ) as span:
        # Load persona overlay from config
        persona_overlay = ""
        try:
            config = _load_agents_config()
            personas = config.get("personas", {})
            persona_data = personas.get(persona, {})
            persona_overlay = persona_data.get("claude_md_overlay", "")
        except FileNotFoundError:
            span.set_attribute("config.loaded", False)

        # Build the overlay text
        lines: list[str] = []
        lines.append("# Dev-Loop Agent Instructions")
        lines.append("")
        lines.append(f"## Issue: {issue_title}")
        lines.append("")

        if issue_description:
            lines.append("### Description")
            lines.append("")
            lines.append(issue_description.strip())
            lines.append("")

        if persona_overlay:
            lines.append(f"## Persona: {persona}")
            lines.append("")
            lines.append(persona_overlay.strip())
            lines.append("")

        lines.append("## Rules")
        lines.append("")
        lines.append("- Work only on the issue described above.")
        lines.append("- Do not modify files outside the scope of this issue.")
        lines.append("- Commit your changes with a clear message referencing the issue ID.")
        lines.append("- If you are blocked or unsure, stop and report rather than guessing.")
        lines.append("")

        overlay_text = "\n".join(lines)

        span.set_attribute("overlay.length", len(overlay_text))
        span.set_attribute("overlay.persona_included", bool(persona_overlay))
        span.set_status(trace.StatusCode.OK)

        return ClaudeOverlay(
            persona=persona,
            issue_title=issue_title,
            overlay_text=overlay_text,
        ).model_dump()


@mcp.tool(
    description=(
        "Remove a worktree and its branch after an agent run is complete "
        "or abandoned. Runs `git worktree remove` and `git branch -D`."
    ),
    tags={"orchestration", "worktree"},
)
def cleanup_worktree(issue_id: str) -> dict:
    """Remove the worktree and branch for the given issue."""
    with tracer.start_as_current_span(
        "orchestration.cleanup_worktree",
        attributes={
            "orchestration.operation": "cleanup_worktree",
            "issue.id": issue_id,
        },
    ) as span:
        worktree_path = WORKTREE_BASE / issue_id
        branch_name = f"{BRANCH_PREFIX}{issue_id}"
        worktree_removed = False
        branch_removed = False

        # Read metadata to find the source repo
        metadata_path = worktree_path / ".dev-loop-metadata.json"
        repo_path: str | None = None
        if metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text())
                repo_path = metadata.get("repo_path")
            except (json.JSONDecodeError, OSError):
                pass

        # Remove the worktree via git
        if repo_path and Path(repo_path).exists():
            result = _run(
                "git", "worktree", "remove", "--force", str(worktree_path),
                cwd=repo_path,
            )
            worktree_removed = result.returncode == 0

            # Prune stale worktree entries
            if worktree_removed:
                _run("git", "worktree", "prune", cwd=repo_path)

            # Remove the branch
            branch_result = _run(
                "git", "branch", "-D", branch_name,
                cwd=repo_path,
            )
            branch_removed = branch_result.returncode == 0
        else:
            # No metadata or repo gone — force remove the directory
            if worktree_path.exists():
                shutil.rmtree(worktree_path, ignore_errors=True)
                worktree_removed = not worktree_path.exists()

        span.set_attribute("worktree.path", str(worktree_path))
        span.set_attribute("worktree.removed", worktree_removed)
        span.set_attribute("branch.name", branch_name)
        span.set_attribute("branch.removed", branch_removed)

        success = worktree_removed
        if success:
            span.set_status(trace.StatusCode.OK)
            message = f"Worktree {worktree_path} removed"
            if branch_removed:
                message += f", branch {branch_name} deleted"
            else:
                message += f" (branch {branch_name} not deleted — may be merged or missing)"
        else:
            error_msg = f"Failed to remove worktree at {worktree_path}"
            span.set_status(trace.StatusCode.ERROR, error_msg)
            message = error_msg

        return CleanupResult(
            issue_id=issue_id,
            worktree_removed=worktree_removed,
            branch_removed=branch_removed,
            success=success,
            message=message,
        ).model_dump()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
