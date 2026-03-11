"""Crash-recovery heartbeat — detects stale agent runs.

Edge case #7 (Task #40): When an agent process crashes without cleaning up
its worktree, we need a way to detect the stale run and recover. This module
provides:

1. A background heartbeat thread that emits OTel spans at a fixed interval,
   proving the agent is still alive.
2. A scanner that checks for stale worktree metadata files whose heartbeat
   has stopped.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from opentelemetry import trace

# ---------------------------------------------------------------------------
# OTel tracer for heartbeat operations
# ---------------------------------------------------------------------------

tracer = trace.get_tracer("observability.heartbeat", "0.1.0")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WORKTREE_BASE = Path("/tmp/dev-loop/worktrees")
METADATA_FILENAME = ".dev-loop-metadata.json"

# ---------------------------------------------------------------------------
# Heartbeat thread
# ---------------------------------------------------------------------------


def start_heartbeat(issue_id: str, interval_seconds: int = 30) -> threading.Event:
    """Start a background heartbeat thread that emits OTel spans.

    The thread emits a ``runtime.heartbeat`` span every *interval_seconds*,
    carrying the ``issue.id`` attribute. This proves the agent process is
    still alive. If spans stop appearing, the run is presumed crashed.

    Also writes a timestamp to the worktree metadata file so that
    ``find_stale_runs`` can detect crashed agents even when the OTel
    collector is unavailable.

    Args:
        issue_id: The beads issue ID this agent run is working on.
        interval_seconds: Seconds between heartbeat emissions.

    Returns:
        A ``threading.Event`` that, when set, stops the heartbeat thread.
    """
    stop_event = threading.Event()

    def _heartbeat_loop() -> None:
        while not stop_event.is_set():
            with tracer.start_as_current_span(
                "runtime.heartbeat",
                attributes={
                    "issue.id": issue_id,
                    "heartbeat.interval_seconds": interval_seconds,
                    "heartbeat.timestamp": time.time(),
                },
            ):
                # Write heartbeat timestamp to metadata file if it exists
                _touch_metadata(issue_id)

            stop_event.wait(timeout=interval_seconds)

    thread = threading.Thread(
        target=_heartbeat_loop,
        name=f"heartbeat-{issue_id}",
        daemon=True,
    )
    thread.start()
    return stop_event


def stop_heartbeat(stop_event: threading.Event) -> None:
    """Signal the heartbeat thread to stop.

    This sets the event, causing the heartbeat loop to exit on its next
    iteration. The thread is daemonic, so it will not prevent process exit
    even if ``stop_heartbeat`` is never called.

    Args:
        stop_event: The event returned by ``start_heartbeat``.
    """
    with tracer.start_as_current_span("runtime.heartbeat.stop"):
        stop_event.set()


# ---------------------------------------------------------------------------
# Stale-run scanner
# ---------------------------------------------------------------------------


def find_stale_runs(max_age_minutes: int = 5) -> list[dict]:
    """Scan for worktree metadata files whose heartbeat has gone stale.

    Looks in ``/tmp/dev-loop/worktrees/`` for ``.dev-loop-metadata.json``
    files. A run is considered stale if the file's ``last_heartbeat``
    timestamp (or ``mtime`` as fallback) is older than *max_age_minutes*.

    Args:
        max_age_minutes: How many minutes without a heartbeat before a run
            is considered stale.

    Returns:
        A list of dicts, each containing the metadata from a stale run plus
        a ``stale_minutes`` field showing how long since the last heartbeat.
    """
    with tracer.start_as_current_span(
        "runtime.heartbeat.find_stale_runs",
        attributes={
            "heartbeat.max_age_minutes": max_age_minutes,
            "heartbeat.worktree_base": str(WORKTREE_BASE),
        },
    ) as span:
        stale: list[dict] = []
        now = time.time()
        cutoff = now - (max_age_minutes * 60)

        if not WORKTREE_BASE.is_dir():
            span.set_attribute("heartbeat.worktree_base_exists", False)
            span.set_attribute("heartbeat.stale_count", 0)
            return stale

        span.set_attribute("heartbeat.worktree_base_exists", True)

        for meta_path in WORKTREE_BASE.rglob(METADATA_FILENAME):
            try:
                raw = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                # Corrupted or unreadable metadata — treat mtime as heartbeat
                raw = {}

            # Prefer the explicit heartbeat timestamp, fall back to mtime
            last_beat = raw.get("last_heartbeat")
            if last_beat is None:
                try:
                    last_beat = meta_path.stat().st_mtime
                except OSError:
                    continue

            if last_beat < cutoff:
                stale_minutes = round((now - last_beat) / 60, 1)
                entry = {
                    **raw,
                    "metadata_path": str(meta_path),
                    "worktree_path": str(meta_path.parent),
                    "last_heartbeat": last_beat,
                    "stale_minutes": stale_minutes,
                }
                stale.append(entry)

        span.set_attribute("heartbeat.stale_count", len(stale))
        if stale:
            span.set_attribute(
                "heartbeat.stale_issue_ids",
                [s.get("issue_id", "unknown") for s in stale],
            )

        return stale


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _touch_metadata(issue_id: str) -> None:
    """Update the heartbeat timestamp in the worktree metadata file.

    Searches for the metadata file associated with the given issue_id
    and updates its ``last_heartbeat`` field. If no file exists yet, this
    is a no-op (the metadata file is created at worktree setup time, not
    by the heartbeat).
    """
    if not WORKTREE_BASE.is_dir():
        return

    for meta_path in WORKTREE_BASE.rglob(METADATA_FILENAME):
        try:
            raw = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        if raw.get("issue_id") == issue_id:
            raw["last_heartbeat"] = time.time()
            try:
                meta_path.write_text(
                    json.dumps(raw, indent=2),
                    encoding="utf-8",
                )
            except OSError:
                pass
            return
