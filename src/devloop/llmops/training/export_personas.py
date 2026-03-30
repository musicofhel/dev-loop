"""Export persona selection training data from Claude Code session JSONLs.

Scans sessions for persona selection events and correlates with outcomes.

Usage:
    python -m devloop.llmops.training.export_personas
"""

from __future__ import annotations

import glob
import json
import os
import re
from pathlib import Path

from opentelemetry import trace

from devloop.llmops.training import (
    _collect_session_files,
    _is_external_user,
    _load_session_events,
)

tracer = trace.get_tracer("llmops.training", "0.1.0")


def _detect_repo_type(events: list[dict]) -> str:
    """Infer repository type from session events (file extensions, commands)."""
    file_ext_counts: dict[str, int] = {}
    for evt in events:
        msg = evt.get("message", {})
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    content = block.get("text", "")
                    break
        if not isinstance(content, str):
            continue
        # Count file extensions mentioned
        for ext in re.findall(r"\b\w+\.(py|ts|tsx|js|jsx|rs|go|java|rb|kt)\b", content):
            file_ext_counts[ext] = file_ext_counts.get(ext, 0) + 1

    if not file_ext_counts:
        return "unknown"

    ext_to_type = {
        "py": "python", "ts": "typescript", "tsx": "typescript",
        "js": "javascript", "jsx": "javascript", "rs": "rust",
        "go": "go", "java": "java", "rb": "ruby", "kt": "kotlin",
    }
    top_ext = max(file_ext_counts, key=file_ext_counts.get)
    return ext_to_type.get(top_ext, "unknown")


def _extract_persona_data(events: list[dict]) -> dict | None:
    """Extract persona selection data from a session.

    Looks for:
    1. Issue labels/description from the task context
    2. Persona selection (from CLAUDE.md overlay or explicit mention)
    3. Session outcome (success/failure)
    """
    issue_labels: list[str] = []
    issue_description = ""
    persona_id = ""
    task_succeeded = False

    for evt in events:
        msg = evt.get("message", {})
        content = msg.get("content", "")

        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    content = block.get("text", "")
                    break
        if not isinstance(content, str):
            continue

        lower = content.lower()

        valid = {
            "bug-fix", "feature", "refactor", "security-fix",
            "docs", "chore", "performance", "infrastructure", "test",
        }

        # Detect persona from multiple sources:
        # 1. Explicit "persona: X" or "persona_id: X" (pipeline/config output)
        persona_match = re.search(
            r"persona(?:_id)?[:\s]+['\"]?(\w[\w-]*)['\"]?", lower
        )
        if persona_match:
            candidate = persona_match.group(1)
            if candidate in valid:
                persona_id = candidate

        # 2. CLAUDE.md overlay content (contains persona-specific instructions)
        if not persona_id:
            for name in valid:
                # Match overlay markers like "## bug-fix persona" or
                # agent config references like "persona: bug-fix"
                if f"## {name}" in lower or f"persona: {name}" in lower:
                    persona_id = name
                    break

        # 3. Label-based inference (mirrors _match_persona logic from
        # orchestration/server.py) — fall back to deriving from labels
        _label_to_persona = {
            "bug": "bug-fix", "feature": "feature", "refactor": "refactor",
            "security": "security-fix", "docs": "docs", "chore": "chore",
            "performance": "performance", "perf": "performance",
            "infrastructure": "infrastructure", "ci": "infrastructure",
            "ci-cd": "infrastructure", "devops": "infrastructure",
            "test": "test", "testing": "test",
        }

        # Detect labels from multiple formats
        label_match = re.search(r"labels?[:\s]+\[([^\]]+)\]", lower)
        if label_match:
            issue_labels = [
                label.strip().strip("'\"")
                for label in label_match.group(1).split(",")
            ]
        # Also detect "label: X" or "#X" tag patterns
        if not issue_labels:
            tag_matches = re.findall(r"#(bug|feature|refactor|security|docs|chore|performance|perf|infrastructure|test|testing)\b", lower)
            if tag_matches:
                issue_labels = tag_matches

        # Infer persona from labels if not found directly
        if not persona_id and issue_labels:
            for label in issue_labels:
                if label in _label_to_persona:
                    persona_id = _label_to_persona[label]
                    break

        # Capture first substantial human message as task description
        if not issue_description and _is_external_user(evt) and len(content) > 30:
            issue_description = content[:2000]

        # Detect outcome
        if "all gates passed" in lower or "pr created" in lower or "success" in lower:
            task_succeeded = True

    if not persona_id or not issue_description:
        return None

    repo_type = _detect_repo_type(events)

    return {
        "inputs": {
            "issue_labels": ",".join(issue_labels) if issue_labels else "",
            "issue_description": issue_description,
            "repo_type": repo_type,
        },
        "outputs": {
            "persona_id": persona_id,
            "custom_guidelines": "",
            "task_succeeded": str(task_succeeded),
        },
    }


def export_personas(
    sessions_dir: str | None = None,
    output_path: str | None = None,
    max_sessions: int = 200,
    force: bool = False,
) -> int:
    """Export persona selection data from session JSONLs.

    Returns the number of examples exported.
    """
    from devloop.llmops.training import safe_write_jsonl

    if output_path is None:
        output_path = os.path.expanduser(
            "~/.local/share/dev-loop/llmops/training/persona_select.jsonl"
        )

    with tracer.start_as_current_span(
        "llmops.training.export_personas",
        attributes={"llmops.output_path": output_path},
    ) as span:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        files = _collect_session_files(sessions_dir, max_sessions)
        examples: list[dict] = []

        for fpath in files:
            events = _load_session_events(fpath)
            stem = Path(fpath).stem

            # For pipeline sessions, try the .meta.json sidecar first —
            # it has authoritative persona/success data from orchestration.
            meta_path = re.sub(r"\.(ndjson|jsonl)$", ".meta.json", fpath)
            meta: dict | None = None
            if os.path.isfile(meta_path):
                try:
                    with open(meta_path) as mf:
                        meta = json.load(mf)
                except (json.JSONDecodeError, OSError):
                    meta = None

            if meta and meta.get("persona"):
                # Build example directly from structured metadata.
                issue_desc = ""
                for evt in events:
                    msg = evt.get("message", {})
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                content = block.get("text", "")
                                break
                    if isinstance(content, str) and _is_external_user(evt) and len(content) > 30:
                        issue_desc = content[:2000]
                        break

                repo_type = _detect_repo_type(events)
                example = {
                    "inputs": {
                        "issue_labels": "",
                        "issue_description": issue_desc or meta.get("issue_id", ""),
                        "repo_type": repo_type,
                    },
                    "outputs": {
                        "persona_id": meta["persona"],
                        "custom_guidelines": "",
                        "task_succeeded": str(meta.get("success", False)),
                    },
                    "metadata": {
                        "session_id": stem,
                        "source": "persona_meta",
                    },
                }
                examples.append(example)
                continue

            # Fall back to parsing persona from conversation text.
            example = _extract_persona_data(events)
            if example is None:
                continue

            example["metadata"] = {
                "session_id": stem,
                "source": "persona_session",
            }
            examples.append(example)

        span.set_attribute("llmops.sessions_scanned", len(files))
        span.set_attribute("llmops.examples_exported", len(examples))

        return safe_write_jsonl(output_path, examples, force=force)


if __name__ == "__main__":
    import sys

    files = _collect_session_files()
    if not files:
        print("WARNING: No session files found", file=sys.stderr)

    force = "--force" in sys.argv
    count = export_personas(force=force)
    print(f"Exported {count} persona selection examples")
    print("Output: ~/.local/share/dev-loop/llmops/training/persona_select.jsonl")
