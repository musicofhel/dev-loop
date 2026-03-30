"""Shared path constants for dev-loop.

All ephemeral temp directories derive from a single base so they can be
overridden together via ``DEVLOOP_TMP_DIR`` for parallel/isolated runs.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_BASE = Path(
    os.environ.get(
        "DEVLOOP_TMP_DIR",
        os.path.join(tempfile.gettempdir(), "dev-loop"),
    )
)

WORKTREE_BASE = Path(
    os.environ.get("DEVLOOP_WORKTREE_DIR", str(_BASE / "worktrees"))
)
LOCK_DIR = _BASE / "locks"
CACHE_DIR = _BASE / "cache"
HANDOFF_DIR = _BASE / "handoffs"
SESSIONS_DIR = _BASE / "sessions"
RESULTS_DIR = _BASE / "stress-test"
