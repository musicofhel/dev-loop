"""Orchestration MCP server — worktree setup, persona selection, CLAUDE.md overlay.

This is Layer 2 of the dev-loop harness. It takes a work item from intake and
prepares an isolated, configured environment for an agent run: worktree creation,
persona matching, and CLAUDE.md overlay generation.

Run standalone:  uv run python -m devloop.orchestration.server
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import yaml
from fastmcp import FastMCP
from opentelemetry import trace

logger = logging.getLogger(__name__)

VALID_MODELS = {"opus", "sonnet", "haiku"}


def _extract_model_override(labels: list[str]) -> str | None:
    """Extract model override from labels like 'model:opus'."""
    for lbl in labels:
        if lbl.startswith("model:"):
            candidate = lbl.split(":", 1)[1]
            if candidate in VALID_MODELS:
                return candidate
    return None


def budget_aware_model(model: str, budget_pct: float) -> str:
    """Downgrade model when budget is tight."""
    if budget_pct >= 95 and model in ("opus", "sonnet"):
        return "haiku"
    if budget_pct >= 80 and model == "opus":
        return "sonnet"
    return model


from devloop.orchestration.types import (
    ClaudeOverlay,
    CleanupResult,
    PersonaConfig,
    PRResult,
    WorktreeInfo,
)
from devloop.paths import HANDOFF_DIR, LOCK_DIR, WORKTREE_BASE
from devloop.runtime.deny_list import generate_deny_rules

# ---------------------------------------------------------------------------
# OTel tracer for orchestration layer
# ---------------------------------------------------------------------------

tracer = trace.get_tracer("orchestration", "0.1.0")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

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

        # File-based locking to prevent concurrent processing (R-1)
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        lock_file = LOCK_DIR / f"{issue_id}.lock"
        try:
            lock_fd = open(lock_file, "w")
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            error_msg = f"Issue {issue_id} is already being processed (locked)"
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
def select_persona(labels: list[str], issue_description: str = "") -> dict:
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

        # --- LLMOps feature flag: DSPy path vs label-matching path ---
        dspy_match = None
        try:
            from devloop.llmops.server import _load_llmops_config

            llmops_cfg = _load_llmops_config()
        except ImportError:
            llmops_cfg = None

        if llmops_cfg and llmops_cfg.enabled and issue_description:
            span.set_attribute("persona.llmops_path", True)
            try:
                import dspy

                from devloop.llmops.programs import load_program
                from devloop.llmops.programs.persona_select import VALID_PERSONAS
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

                pcfg = llmops_cfg.programs.get("persona_select", OptimizationConfig())
                if llmops_cfg.provider == "openrouter":
                    model_str = f"openrouter/anthropic/{pcfg.model}"
                else:
                    model_str = f"anthropic/{pcfg.model}"
                dspy.configure(lm=dspy.LM(model_str, api_key=api_key, max_tokens=1024))
                module = load_program("persona_select")

                result = module(
                    issue_labels=",".join(labels),
                    issue_description=issue_description,
                    repo_type="python",  # Default; could be inferred from project config
                )

                predicted_persona = (result.persona_id or "").strip().lower()
                if predicted_persona not in VALID_PERSONAS:
                    raise ValueError(
                        f"DSPy predicted invalid persona '{predicted_persona}'"
                    )

                # Look up persona config from agents.yaml
                personas = config.get("personas", {})
                if predicted_persona in personas:
                    persona_data = dict(personas[predicted_persona])
                    # Append custom guidelines from DSPy to the overlay
                    custom = (result.custom_guidelines or "").strip()
                    if custom:
                        existing_overlay = persona_data.get("claude_md_overlay", "")
                        persona_data["claude_md_overlay"] = (
                            f"{existing_overlay}\n{custom}" if existing_overlay else custom
                        )
                    dspy_match = (predicted_persona, persona_data)
                    span.set_attribute("persona.dspy_selected", predicted_persona)
                else:
                    raise ValueError(
                        f"DSPy persona '{predicted_persona}' not in agents.yaml"
                    )

            except Exception as dspy_exc:
                span.set_attribute("persona.llmops_fallback", True)
                span.set_attribute("persona.llmops_error", str(dspy_exc)[:200])
                logger.warning("DSPy persona_select failed, falling back to label matching: %s", dspy_exc)
        else:
            span.set_attribute("persona.llmops_path", False)

        match = dspy_match if dspy_match is not None else _match_persona(labels, config)

        if match is None:
            # Default to feature persona if no match
            span.set_attribute("persona.matched", False)
            span.set_attribute("persona.fallback", "feature")
            personas = config.get("personas", {})
            fallback = personas.get("feature", {})
            fallback_model = fallback.get("model", "opus")
            override = _extract_model_override(labels)
            if override:
                fallback_model = override
                span.set_attribute("persona.model_override", override)
            persona = PersonaConfig(
                name="feature",
                labels=fallback.get("labels", ["feature"]),
                claude_md_overlay=fallback.get("claude_md_overlay", ""),
                cost_ceiling_default=fallback.get("cost_ceiling_default", 5.00),
                retry_max=fallback.get("retry_max", 1),
                model=fallback_model,
                max_turns_default=fallback.get("max_turns_default", 15),
                max_context_pct=fallback.get("max_context_pct", 75),
                timeout_seconds=fallback.get("timeout_seconds", 300),
            )
            span.set_status(trace.StatusCode.OK)
            return persona.model_dump()

        name, data = match
        span.set_attribute("persona.matched", True)
        span.set_attribute("persona.name", name)

        # Validate model (E-5)
        model = data.get("model", "sonnet")
        if model not in VALID_MODELS:
            logger.warning(
                "Persona '%s' has invalid model '%s', falling back to 'sonnet'",
                name, model,
            )
            model = "sonnet"

        # Check for model override from labels (e.g. "model:opus")
        override = _extract_model_override(labels)
        if override:
            model = override
            span.set_attribute("persona.model_override", override)

        span.set_attribute("persona.model", model)
        span.set_attribute("persona.cost_ceiling", data.get("cost_ceiling_default", 1.00))

        persona = PersonaConfig(
            name=name,
            labels=data.get("labels", []),
            claude_md_overlay=data.get("claude_md_overlay", ""),
            cost_ceiling_default=data.get("cost_ceiling_default", 1.00),
            retry_max=data.get("retry_max", 1),
            model=model,
            max_turns_default=data.get("max_turns_default", 15),
            max_context_pct=data.get("max_context_pct", 75),
            timeout_seconds=data.get("timeout_seconds", 300),
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
def _detect_project_type(worktree: Path) -> str:
    """Detect whether the worktree is a Node/Python/Rust project."""
    if (worktree / "package.json").exists():
        return "node"
    if (worktree / "pyproject.toml").exists():
        return "python"
    if (worktree / "Cargo.toml").exists():
        return "rust"
    return "unknown"


def _backpressure_rules(project_type: str) -> str:
    """Generate in-process feedback rules based on project type."""
    common = (
        "## In-Process Feedback\n\n"
        "Run checks frequently during your work — do not wait until the end.\n\n"
    )
    if project_type == "node":
        return common + (
            "- After editing TypeScript/JavaScript files, run `npx tsc --noEmit` to catch type errors.\n"
            "- After completing a logical group of edits, run `npm test` on affected test files.\n"
            "- Fix any errors before moving to the next file.\n"
        )
    if project_type == "python":
        return common + (
            "- After editing Python files, run `uv run pytest --tb=short -q` on affected test files.\n"
            "- If the project uses type checking (mypy/pyright), run it after edits.\n"
            "- Fix any errors before moving to the next file.\n"
        )
    if project_type == "rust":
        return common + (
            "- After editing Rust files, run `cargo check` to catch compilation errors.\n"
            "- After completing a logical group of edits, run `cargo test` on affected modules.\n"
            "- Fix any errors before moving to the next file.\n"
        )
    return common + (
        "- After each group of edits, run the project's test command to catch errors early.\n"
        "- Fix any errors before moving to the next file.\n"
    )


_ANTI_HALLUCINATION_RULES = (
    "## Code Verification\n\n"
    "- Always read a function's implementation before calling it. "
    "Never assume what a function does from its name alone.\n"
    "- Before importing a module, verify it exists in the project.\n"
    "- Before using an API endpoint, verify it exists in the route definitions.\n"
    "- If you are unsure whether a function/class/module exists, search for it first.\n"
)

_LOCK_FILE_RULES = {
    "node": (
        "## Lock File Rules\n\n"
        "- After modifying `package.json`, run `npm install` immediately "
        "to keep `package-lock.json` in sync.\n"
        "- Do not run `npm update` or `npm audit fix` without explicit approval.\n"
    ),
    "python": (
        "## Lock File Rules\n\n"
        "- After modifying `pyproject.toml` dependencies, run `uv lock` "
        "to keep the lock file in sync.\n"
    ),
    "rust": (
        "## Lock File Rules\n\n"
        "- After modifying `Cargo.toml`, run `cargo check` to regenerate `Cargo.lock`.\n"
    ),
}


def build_claude_md_overlay(
    persona: str,
    issue_title: str,
    issue_description: str,
    issue_id: str = "",
    max_context_pct: int = 75,
    repo_path: str = "",
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

        lines.append("## Setup")
        lines.append("")
        lines.append("- Before running tests, install the project in the worktree:")
        lines.append("  `uv sync --dev` (or `pip install -e '.[dev]'` if uv is not available)")
        lines.append("- If pyproject.toml or setup.py exists, always install before running pytest.")
        lines.append("")

        lines.append("## Rules")
        lines.append("")
        lines.append("- Work only on the issue described above.")
        lines.append("- Do not modify files outside the scope of this issue.")
        lines.append("- Commit your changes with a clear message referencing the issue ID.")
        lines.append("- If you are blocked or unsure, stop and report rather than guessing.")
        lines.append("")

        # Context limit / handoff instructions
        handoff_id = issue_id or "current-issue"
        handoff_path = str(HANDOFF_DIR / f"{handoff_id}.md")
        lines.append("## Context Window Management")
        lines.append("")
        lines.append(
            f"- If you notice your context window is getting large (above ~{max_context_pct}%), "
            "commit your current changes immediately."
        )
        lines.append(
            f"- Write a brief handoff note to `{handoff_path}` describing: "
            "what is done, what files were changed, what remains to be done, "
            "and any important context for a fresh session."
        )
        lines.append("- Then exit cleanly. A fresh session will pick up where you left off.")
        lines.append("")

        # Deny list rules (prevents agent from reading secrets)
        deny_rules = generate_deny_rules()
        if deny_rules:
            lines.append(deny_rules)
            lines.append("")

        # --- Edge case hardening rules ---
        # Detect project type for context-aware rules
        project_type = "unknown"
        if repo_path:
            project_type = _detect_project_type(Path(repo_path))
        span.set_attribute("overlay.project_type", project_type)

        # #28: In-process backpressure
        lines.append(_backpressure_rules(project_type))
        lines.append("")

        # #35: Anti-hallucination
        lines.append(_ANTI_HALLUCINATION_RULES)
        lines.append("")

        # #34: Lock file consistency
        if project_type in _LOCK_FILE_RULES:
            lines.append(_LOCK_FILE_RULES[project_type])
            lines.append("")

        # #26: Large repo context — tiered file map + scope hints
        if repo_path:
            try:
                from devloop.orchestration.file_map import (
                    _list_files,
                    extract_scope_hints,
                    generate_directory_summary,
                )

                dir_summary = generate_directory_summary(repo_path)
                if dir_summary:
                    lines.append("## Repository Structure")
                    lines.append("")
                    lines.append(dir_summary)
                    lines.append("")

                    known_paths = _list_files(repo_path)
                    scope_hints = extract_scope_hints(
                        issue_title, issue_description, known_paths,
                    )
                    if scope_hints:
                        lines.append("## Focus Areas")
                        lines.append("")
                        lines.append(
                            "The following paths appear relevant to this issue — "
                            "start your investigation here:"
                        )
                        lines.append("")
                        for hint in scope_hints:
                            lines.append(f"- `{hint}`")
                        lines.append("")
                        span.set_attribute("overlay.scope_hints", len(scope_hints))
            except Exception:
                logger.debug("Failed to generate file map context", exc_info=True)

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
                try:
                    shutil.rmtree(worktree_path)
                    worktree_removed = True
                except OSError as e:
                    logger.error("Failed to remove worktree %s: %s", worktree_path, e)
                    worktree_removed = False

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

        # Release lock file (R-1)
        lock_file = LOCK_DIR / f"{issue_id}.lock"
        try:
            lock_file.unlink(missing_ok=True)
        except OSError:
            pass

        return CleanupResult(
            issue_id=issue_id,
            worktree_removed=worktree_removed,
            branch_removed=branch_removed,
            success=success,
            message=message,
        ).model_dump()


@mcp.tool(
    description=(
        "Create a GitHub pull request from the worktree branch. "
        "Runs `gh pr create` with the issue title and a generated body. "
        "Requires the gh CLI to be installed and authenticated."
    ),
    tags={"orchestration", "pr"},
)
def create_pull_request(
    issue_id: str,
    repo_path: str,
    worktree_path: str,
    branch_name: str,
    issue_title: str,
    issue_description: str,
    gate_summary: str = "",
) -> dict:
    """Create a GitHub PR from the worktree branch after gates pass."""
    with tracer.start_as_current_span(
        "orchestration.create_pull_request",
        attributes={
            "orchestration.operation": "create_pull_request",
            "issue.id": issue_id,
            "pr.branch": branch_name,
        },
    ) as span:
        # Check that gh CLI is available
        if not shutil.which("gh"):
            msg = "gh CLI not found — install GitHub CLI to create PRs"
            span.set_status(trace.StatusCode.ERROR, msg)
            return PRResult(
                issue_id=issue_id,
                success=False,
                branch_name=branch_name,
                message=msg,
            ).model_dump()

        # Check if the remote branch already exists
        remote_check = _run(
            "git", "ls-remote", "--heads", "origin", branch_name,
            cwd=worktree_path,
        )
        remote_exists = bool(remote_check.stdout.strip())

        # Push the branch from the worktree
        if remote_exists:
            logger.warning(
                "Remote branch %s already exists — force-pushing with lease",
                branch_name,
            )
            push_result = _run(
                "git", "push", "--force-with-lease", "origin", branch_name,
                cwd=worktree_path,
            )
        else:
            push_result = _run(
                "git", "push", "origin", branch_name,
                cwd=worktree_path,
            )
        if push_result.returncode != 0:
            msg = (
                push_result.stderr.strip()
                or f"git push failed with exit code {push_result.returncode}"
            )
            span.set_status(trace.StatusCode.ERROR, msg)
            return PRResult(
                issue_id=issue_id,
                success=False,
                branch_name=branch_name,
                message=f"Push failed: {msg}",
            ).model_dump()

        # Detect the default branch
        default_branch_result = _run(
            "gh", "repo", "view", "--json", "defaultBranchRef",
            "--jq", ".defaultBranchRef.name",
            cwd=repo_path,
        )
        base_branch = default_branch_result.stdout.strip() or "main"

        # Build the PR body
        body_parts: list[str] = []
        if issue_description:
            body_parts.append("## Description")
            body_parts.append("")
            body_parts.append(issue_description.strip())
            body_parts.append("")
        if gate_summary:
            body_parts.append("## Gate Summary")
            body_parts.append("")
            body_parts.append(gate_summary.strip())
            body_parts.append("")
        body_parts.append("---")
        body_parts.append("*Generated by dev-loop*")
        pr_body = "\n".join(body_parts)

        pr_title = f"[{issue_id}] {issue_title}"

        # Create the PR
        pr_result = _run(
            "gh", "pr", "create",
            "--title", pr_title,
            "--body", pr_body,
            "--base", base_branch,
            "--head", branch_name,
            cwd=repo_path,
        )

        if pr_result.returncode != 0:
            msg = (
                pr_result.stderr.strip()
                or f"gh pr create failed with exit code {pr_result.returncode}"
            )
            span.set_status(trace.StatusCode.ERROR, msg)
            return PRResult(
                issue_id=issue_id,
                success=False,
                branch_name=branch_name,
                message=f"PR creation failed: {msg}",
            ).model_dump()

        # gh pr create prints the PR URL to stdout
        pr_url = pr_result.stdout.strip()
        span.set_attribute("pr.url", pr_url)
        span.set_status(trace.StatusCode.OK)

        return PRResult(
            issue_id=issue_id,
            success=True,
            pr_url=pr_url,
            branch_name=branch_name,
            message=f"PR created: {pr_url}",
        ).model_dump()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
