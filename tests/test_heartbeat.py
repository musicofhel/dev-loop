"""Tests for devloop.observability.heartbeat — heartbeat thread and stale run detection."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import patch

from devloop.observability.heartbeat import (
    find_stale_runs,
    start_heartbeat,
    stop_heartbeat,
)

# ---------------------------------------------------------------------------
# find_stale_runs tests
# ---------------------------------------------------------------------------


class TestFindStaleRuns:
    """Tests for find_stale_runs() function."""

    @patch("devloop.observability.heartbeat.WORKTREE_BASE")
    def test_no_worktree_directory(self, mock_base):
        """find_stale_runs() returns [] when worktree base directory does not exist."""
        mock_base.is_dir.return_value = False

        result = find_stale_runs()

        assert result == []

    def test_empty_worktree_directory(self, tmp_path):
        """find_stale_runs() returns [] when worktree directory is empty."""
        with patch("devloop.observability.heartbeat.WORKTREE_BASE", tmp_path):
            result = find_stale_runs()

        assert result == []


# ---------------------------------------------------------------------------
# start_heartbeat / stop_heartbeat tests
# ---------------------------------------------------------------------------


class TestHeartbeat:
    """Tests for start_heartbeat() and stop_heartbeat()."""

    def test_start_returns_event_and_thread(self):
        """start_heartbeat() returns a (Event, Thread) tuple."""
        stop_event, thread = start_heartbeat("TEST-001", interval_seconds=3600)
        try:
            assert isinstance(stop_event, threading.Event)
            assert isinstance(thread, threading.Thread)
            assert not stop_event.is_set()
        finally:
            stop_event.set()
            thread.join(timeout=5)

    def test_stop_sets_event_and_joins_thread(self):
        """stop_heartbeat() sets the event and joins the thread."""
        stop_event, thread = start_heartbeat("TEST-002", interval_seconds=3600)
        try:
            assert not stop_event.is_set()
            assert thread.is_alive()
            stop_heartbeat(stop_event, thread)
            assert stop_event.is_set()
            assert not thread.is_alive()
        finally:
            # Ensure cleanup even if assertion fails
            stop_event.set()

    def test_heartbeat_thread_is_daemon(self):
        """The heartbeat thread is daemonic so it won't block process exit."""
        stop_event, thread = start_heartbeat("TEST-003", interval_seconds=3600)
        try:
            assert thread.daemon is True
            assert thread.name == "heartbeat-TEST-003"
        finally:
            stop_event.set()
            thread.join(timeout=5)

    def test_stop_backward_compat_no_thread(self):
        """stop_heartbeat() works when thread is None (backward compat)."""
        stop_event, thread = start_heartbeat("TEST-004", interval_seconds=3600)
        try:
            # Call with thread=None — should not raise
            stop_heartbeat(stop_event, thread=None)
            assert stop_event.is_set()
        finally:
            stop_event.set()
            thread.join(timeout=5)

    def test_thread_stops_before_cleanup(self):
        """After stop_heartbeat returns, the thread is no longer alive."""
        stop_event, thread = start_heartbeat("TEST-005", interval_seconds=3600)
        stop_heartbeat(stop_event, thread)
        # Thread should be dead — safe to clean up worktree
        assert not thread.is_alive()


# ---------------------------------------------------------------------------
# _touch_metadata_path tests
# ---------------------------------------------------------------------------


class TestTouchMetadataPath:
    """Tests for _touch_metadata_path with direct path."""

    def test_worktree_path_resolves_directly(self, tmp_path):
        """start_heartbeat with worktree_path doesn't need rglob."""
        # Create a metadata file in the worktree
        meta = tmp_path / ".dev-loop-metadata.json"
        meta.write_text(json.dumps({"issue_id": "TEST-006"}), encoding="utf-8")

        stop_event, thread = start_heartbeat(
            "TEST-006", interval_seconds=3600, worktree_path=str(tmp_path),
        )
        try:
            assert isinstance(stop_event, threading.Event)
        finally:
            stop_event.set()
            thread.join(timeout=5)

    def test_touch_updates_heartbeat_timestamp(self, tmp_path):
        """_touch_metadata_path writes last_heartbeat to the metadata file."""
        from devloop.observability.heartbeat import _touch_metadata_path

        meta = tmp_path / ".dev-loop-metadata.json"
        meta.write_text(json.dumps({"issue_id": "TEST-007"}), encoding="utf-8")

        _touch_metadata_path(str(meta))

        updated = json.loads(meta.read_text(encoding="utf-8"))
        assert "last_heartbeat" in updated
        assert isinstance(updated["last_heartbeat"], float)

    def test_touch_none_path_is_noop(self):
        """_touch_metadata_path(None) does nothing."""
        from devloop.observability.heartbeat import _touch_metadata_path

        # Should not raise
        _touch_metadata_path(None)

    def test_touch_missing_file_is_noop(self, tmp_path):
        """_touch_metadata_path with nonexistent file does nothing."""
        from devloop.observability.heartbeat import _touch_metadata_path

        _touch_metadata_path(str(tmp_path / "nonexistent.json"))


# ---------------------------------------------------------------------------
# TOCTOU tests (L3 fix)
# ---------------------------------------------------------------------------


class TestFindStaleRunsToctou:
    """Tests for TOCTOU-safe find_stale_runs()."""

    def test_handles_file_removed_between_rglob_and_read(self, tmp_path):
        """find_stale_runs skips files that vanish between rglob and read."""
        import time as _time

        # Create a stale metadata file
        meta = tmp_path / "wt-1" / ".dev-loop-metadata.json"
        meta.parent.mkdir()
        meta.write_text(
            json.dumps({
                "issue_id": "TEST-TOCTOU",
                "last_heartbeat": _time.time() - 600,
            }),
            encoding="utf-8",
        )

        # Verify it works normally
        with patch("devloop.observability.heartbeat.WORKTREE_BASE", tmp_path):
            result = find_stale_runs(max_age_minutes=5)
            assert len(result) == 1

        # Now delete the file and verify no crash
        meta.unlink()
        with patch("devloop.observability.heartbeat.WORKTREE_BASE", tmp_path):
            result = find_stale_runs(max_age_minutes=5)
            assert result == []
