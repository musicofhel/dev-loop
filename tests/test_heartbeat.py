"""Tests for devloop.observability.heartbeat — heartbeat thread and stale run detection."""

from __future__ import annotations

import threading
from unittest.mock import patch

from devloop.observability.heartbeat import find_stale_runs, start_heartbeat, stop_heartbeat

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

    def test_start_returns_event(self):
        """start_heartbeat() returns a threading.Event."""
        # Use a very long interval so the heartbeat thread doesn't
        # do much before we stop it.
        stop_event = start_heartbeat("TEST-001", interval_seconds=3600)
        try:
            assert isinstance(stop_event, threading.Event)
            assert not stop_event.is_set()
        finally:
            stop_event.set()

    def test_stop_sets_event(self):
        """stop_heartbeat() sets the event, signalling the thread to stop."""
        stop_event = start_heartbeat("TEST-002", interval_seconds=3600)
        try:
            assert not stop_event.is_set()
            stop_heartbeat(stop_event)
            assert stop_event.is_set()
        finally:
            # Ensure cleanup even if assertion fails
            stop_event.set()

    def test_heartbeat_thread_is_daemon(self):
        """The heartbeat thread is daemonic so it won't block process exit."""
        stop_event = start_heartbeat("TEST-003", interval_seconds=3600)
        try:
            # Find our heartbeat thread
            heartbeat_threads = [
                t for t in threading.enumerate()
                if t.name == "heartbeat-TEST-003"
            ]
            assert len(heartbeat_threads) == 1
            assert heartbeat_threads[0].daemon is True
        finally:
            stop_event.set()
