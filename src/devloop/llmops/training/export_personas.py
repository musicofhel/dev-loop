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

        # Detect persona from CLAUDE.md overlay or pipeline output
        persona_match = re.search(
            r"persona[:\s]+['\"]?(\w[\w-]*)['\"]?", lower
        )
        if persona_match:
            candidate = persona_match.group(1)
            valid = {
                "bug-fix", "feature", "refactor", "security-fix",
                "docs", "chore", "performance", "infrastructure", "test",
            }
            if candidate in valid:
                persona_id = candidate

        # Detect labels
        label_match = re.search(r"labels?[:\s]+\[([^\]]+)\]", lower)
        if label_match:
            issue_labels = [
                label.strip().strip("'\"")
                for label in label_match.group(1).split(",")
            ]

        # Capture first substantial human message as task description
        if not issue_description and evt.get("userType") == "external" and len(content) > 30:
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
) -> int:
    """Export persona selection data from session JSONLs.

    Returns the number of examples exported.
    """
    if sessions_dir is None:
        from devloop.llmops.training import _default_sessions_dir

        sessions_dir = _default_sessions_dir()

    if output_path is None:
        output_path = os.path.expanduser(
            "~/.local/share/dev-loop/llmops/training/persona_select.jsonl"
        )

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    files = sorted(glob.glob(os.path.join(sessions_dir, "*.jsonl")))[:max_sessions]
    exported = 0

    with open(output_path, "w") as out:
        for fpath in files:
            events = []
            with open(fpath) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

            example = _extract_persona_data(events)
            if example is None:
                continue

            example["metadata"] = {
                "session_id": os.path.basename(fpath).replace(".jsonl", ""),
                "source": "persona_session",
            }
            out.write(json.dumps(example) + "\n")
            exported += 1

    return exported


if __name__ == "__main__":
    import sys

    from devloop.llmops.training import _default_sessions_dir

    sessions_dir = _default_sessions_dir()
    if not os.path.isdir(sessions_dir):
        print(f"WARNING: Sessions dir not found: {sessions_dir}", file=sys.stderr)
    elif not glob.glob(os.path.join(sessions_dir, "*.jsonl")):
        print(f"WARNING: No .jsonl files in {sessions_dir}", file=sys.stderr)

    count = export_personas()
    print(f"Exported {count} persona selection examples")
    print("Output: ~/.local/share/dev-loop/llmops/training/persona_select.jsonl")
