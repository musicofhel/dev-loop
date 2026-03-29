"""Training data export scripts for GEPA optimization."""

from __future__ import annotations

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
