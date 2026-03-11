"""Tests for devloop.paths — shared path constants."""

from __future__ import annotations

import os
import tempfile
from unittest.mock import patch

# ---------------------------------------------------------------------------
# WORKTREE_BASE tests
# ---------------------------------------------------------------------------


class TestWorktreeBase:
    """Tests for the WORKTREE_BASE path constant."""

    def test_default_path(self):
        """WORKTREE_BASE defaults to <tempdir>/dev-loop/worktrees when env var is not set."""
        # Ensure the env var is not set, then re-import the module
        env = os.environ.copy()
        env.pop("DEVLOOP_WORKTREE_DIR", None)

        with patch.dict(os.environ, env, clear=True):
            # Re-import to pick up the env change
            import importlib

            import devloop.paths

            importlib.reload(devloop.paths)

            expected = os.path.join(tempfile.gettempdir(), "dev-loop", "worktrees")
            assert str(devloop.paths.WORKTREE_BASE) == expected

    def test_env_var_override(self):
        """DEVLOOP_WORKTREE_DIR env var overrides the default WORKTREE_BASE path."""
        custom_path = "/custom/worktree/dir"

        with patch.dict(os.environ, {"DEVLOOP_WORKTREE_DIR": custom_path}):
            import importlib

            import devloop.paths

            importlib.reload(devloop.paths)

            assert str(devloop.paths.WORKTREE_BASE) == custom_path

    def test_worktree_base_is_a_path(self):
        """WORKTREE_BASE is a pathlib.Path instance."""
        from pathlib import Path

        from devloop.paths import WORKTREE_BASE

        assert isinstance(WORKTREE_BASE, Path)
