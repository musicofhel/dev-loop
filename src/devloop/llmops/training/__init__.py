"""Training data export scripts for GEPA optimization."""

from __future__ import annotations

import glob
import json
import os
import shutil
import sys
from datetime import datetime, timezone


def _default_sessions_dir() -> str:
    """Derive Claude Code sessions dir from CWD.

    Claude Code stores project sessions at ~/.claude/projects/-<mangled-cwd>/
    where the mangled name is the absolute CWD with '/' replaced by '-'.
    """
    cwd = os.getcwd()
    mangled = cwd.replace("/", "-").lstrip("-")
    return os.path.expanduser(f"~/.claude/projects/-{mangled}/")


def _default_pipeline_sessions_dir() -> str:
    """Dev-loop pipeline agent sessions directory.

    TB runs store agent sessions as .ndjson files with .meta.json sidecars
    at ``~/.local/share/dev-loop/sessions/``.
    """
    return os.path.expanduser("~/.local/share/dev-loop/sessions/")


def _collect_session_files(
    sessions_dir: str | None = None,
    max_files: int = 200,
) -> list[str]:
    """Collect session files from Claude Code logs AND pipeline sessions.

    When *sessions_dir* is provided, searches that single directory for both
    ``.jsonl`` and ``.ndjson`` files.  When ``None``, searches both the
    default Claude Code sessions dir and the pipeline sessions dir.
    """
    if sessions_dir is not None:
        dirs = [sessions_dir]
    else:
        dirs = [_default_sessions_dir(), _default_pipeline_sessions_dir()]

    files: list[str] = []
    for d in dirs:
        if not os.path.isdir(d):
            continue
        files.extend(glob.glob(os.path.join(d, "*.jsonl")))
        files.extend(glob.glob(os.path.join(d, "*.ndjson")))

    return sorted(set(files))[:max_files]


def _load_session_events(fpath: str) -> list[dict]:
    """Load events from a ``.jsonl`` or ``.ndjson`` session file.

    Handles two formats:
    - ``.jsonl``: one JSON object per line (Claude Code conversation logs).
    - ``.ndjson``: a single JSON array containing all events on one line
      (pipeline agent sessions).
    """
    events: list[dict] = []
    with open(fpath) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, list):
                # ndjson: single array of event dicts
                events.extend(e for e in parsed if isinstance(e, dict))
            elif isinstance(parsed, dict):
                events.append(parsed)
    return events


def _is_external_user(evt: dict) -> bool:
    """Check if an event is from an external/human user.

    Claude Code ``.jsonl`` events use ``userType: "external"``.
    Pipeline ``.ndjson`` events use ``type: "user"`` without ``userType``.
    """
    if evt.get("userType") == "external":
        return True
    if evt.get("type") == "user" and "userType" not in evt:
        return True
    return False


def safe_write_jsonl(
    output_path: str,
    examples: list[dict],
    *,
    force: bool = False,
) -> int:
    """Write examples to a JSONL file with backup and zero-example guard.

    Before overwriting an existing file with content:
    1. Creates a timestamped backup ({path}.bak.{YYYYMMDD}).
    2. If *examples* is empty and *force* is False, warns and skips
       the overwrite to prevent accidental data loss.

    Returns the number of examples written (0 if skipped).
    """
    path = output_path
    has_existing = os.path.isfile(path) and os.path.getsize(path) > 0

    if has_existing:
        date_stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        backup_path = f"{path}.bak.{date_stamp}"
        shutil.copy2(path, backup_path)

    if not examples and not force and has_existing:
        # Count existing lines for the warning message.
        with open(path) as f:
            existing_count = sum(1 for _ in f)
        print(
            f"WARNING: 0 examples exported but {path} has existing data "
            f"({existing_count} lines). Skipping overwrite. "
            f"Use --force to override.",
            file=sys.stderr,
        )
        return 0

    with open(path, "w") as out:
        for ex in examples:
            out.write(json.dumps(ex) + "\n")

    return len(examples)
