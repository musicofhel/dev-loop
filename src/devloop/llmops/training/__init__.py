"""Training data export scripts for GEPA optimization."""

from __future__ import annotations

import os


def _default_sessions_dir() -> str:
    """Derive Claude Code sessions dir from CWD.

    Claude Code stores project sessions at ~/.claude/projects/-<mangled-cwd>/
    where the mangled name is the absolute CWD with '/' replaced by '-'.
    """
    cwd = os.getcwd()
    mangled = cwd.replace("/", "-").lstrip("-")
    return os.path.expanduser(f"~/.claude/projects/-{mangled}/")
