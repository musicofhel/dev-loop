"""Priority scheduler — sits between intake polling and TB-1 execution.

Implements #36: priority-ordered issue selection with concurrency limits
and budget-aware throttling.
"""

from __future__ import annotations

import fcntl
import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from devloop.intake.ambiguity import defer_ambiguous_issue, detect_ambiguity
from devloop.paths import LOCK_DIR

logger = logging.getLogger(__name__)
CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "scheduling.yaml"


@dataclass
class SchedulerConfig:
    max_concurrent_agents: int = 3
    priority_order: list[str] = field(default_factory=lambda: ["P0", "P1", "P2", "P3", "P4"])
    weekly_budget_turns: int = 1400
    weekly_budget_input_tokens: int = 35_000_000
    weekly_budget_output_tokens: int = 7_000_000


def load_scheduler_config(config_path: Path | None = None) -> SchedulerConfig:
    path = config_path or CONFIG_PATH
    if not path.exists():
        return SchedulerConfig()
    try:
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        budget = raw.get("weekly_budget", {})
        return SchedulerConfig(
            max_concurrent_agents=raw.get("max_concurrent_agents", 3),
            priority_order=raw.get("priority_order", ["P0", "P1", "P2", "P3", "P4"]),
            weekly_budget_turns=budget.get("max_turns", 1400),
            weekly_budget_input_tokens=budget.get("max_input_tokens", 35_000_000),
            weekly_budget_output_tokens=budget.get("max_output_tokens", 7_000_000),
        )
    except Exception:
        logger.exception("Failed to load scheduler config from %s", path)
        return SchedulerConfig()


def count_active_agents(lock_dir: Path | None = None) -> int:
    """Count currently held lock files. Stale locks (not held) are excluded."""
    d = lock_dir or LOCK_DIR
    if not d.exists():
        return 0
    count = 0
    for lock_file in d.glob("*.lock"):
        try:
            fd = open(lock_file, "r")
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Lock acquired = stale (no process holds it)
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            except OSError:
                # Cannot acquire = someone holds it = active agent
                count += 1
            finally:
                fd.close()
        except Exception:
            pass
    return count


def get_budget_usage_pct(config: SchedulerConfig) -> float:
    """Get highest usage percentage across turns/tokens for the week."""
    try:
        from devloop.feedback.cost_monitor import get_usage_summary

        summary = get_usage_summary(hours=168)
        if not summary:
            return 0.0

        turns = summary.get("total_turns", 0)
        input_tokens = summary.get("total_input_tokens", 0)
        output_tokens = summary.get("total_output_tokens", 0)

        pcts: list[float] = []
        if config.weekly_budget_turns > 0:
            pcts.append(turns / config.weekly_budget_turns * 100)
        if config.weekly_budget_input_tokens > 0:
            pcts.append(input_tokens / config.weekly_budget_input_tokens * 100)
        if config.weekly_budget_output_tokens > 0:
            pcts.append(output_tokens / config.weekly_budget_output_tokens * 100)

        return max(pcts) if pcts else 0.0
    except Exception:
        logger.exception("Failed to get budget usage")
        return 0.0


def compute_min_priority(budget_pct: float) -> int:
    """Given budget usage %, return minimum priority level allowed.

    Returns: 4 (all), 1 (P1+), 0 (P0 only), -1 (paused)
    """
    if budget_pct >= 100:
        return -1  # pause all
    if budget_pct >= 95:
        return 0   # P0 only
    if budget_pct >= 80:
        return 1   # P1 and above
    return 4       # all priorities


@dataclass
class WorkItemLike:
    """Minimal interface for scheduling — matches WorkItem from beads_poller."""

    id: str
    title: str
    priority: int = 4
    description: str = ""
    target_repo: str = ""


def sort_by_priority(items: list) -> list:
    """Sort by priority (0=highest), then by ID as FIFO proxy."""
    return sorted(items, key=lambda x: (getattr(x, "priority", 4), getattr(x, "id", "")))


def select_next_issues(
    ready_items: list,
    config: SchedulerConfig,
    active_count: int | None = None,
    budget_pct: float | None = None,
    check_ambiguity: bool = True,
) -> list:
    """Select the next batch of issues to dispatch.

    Pure function when active_count and budget_pct are provided explicitly.
    """
    if active_count is None:
        active_count = count_active_agents()
    if budget_pct is None:
        budget_pct = get_budget_usage_pct(config)

    # Budget check
    min_pri = compute_min_priority(budget_pct)
    if min_pri == -1:
        logger.info("Budget exhausted (%.1f%%) — pausing all dispatches", budget_pct)
        return []

    # Filter ambiguous issues
    eligible = []
    for item in ready_items:
        if check_ambiguity:
            result = detect_ambiguity(
                getattr(item, "title", ""),
                getattr(item, "description", ""),
            )
            if result.is_ambiguous:
                logger.info("Deferring ambiguous issue %s: %s", getattr(item, "id", "?"), result.summary)
                defer_ambiguous_issue(getattr(item, "id", ""), result)
                continue
        eligible.append(item)

    # Sort by priority
    sorted_items = sort_by_priority(eligible)

    # Filter by budget-allowed priority
    allowed = [item for item in sorted_items if getattr(item, "priority", 4) <= min_pri]

    # Limit by available slots
    available_slots = max(0, config.max_concurrent_agents - active_count)

    return allowed[:available_slots]


def dispatch_issue(issue_id: str, repo_path: str) -> int | None:
    """Spawn a TB-1 pipeline subprocess. Returns PID or None on failure."""
    try:
        cmd = [
            "uv", "run", "python", "-c",
            f"from devloop.feedback.tb1_golden_path import run_tb1; run_tb1('{issue_id}', '{repo_path}')",
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        logger.info("Dispatched issue %s as PID %d", issue_id, proc.pid)
        return proc.pid
    except Exception:
        logger.exception("Failed to dispatch issue %s", issue_id)
        return None


def run_scheduler_loop(
    repo_path: str,
    config: SchedulerConfig | None = None,
    poll_interval: float = 30.0,
    max_cycles: int | None = None,
) -> list[str]:
    """Main scheduler loop. Returns list of dispatched issue IDs."""
    if config is None:
        config = load_scheduler_config()

    dispatched: list[str] = []
    cycle = 0

    while max_cycles is None or cycle < max_cycles:
        cycle += 1
        try:
            from devloop.intake.beads_poller import poll_ready

            ready = poll_ready()
        except Exception:
            logger.exception("Failed to poll ready issues")
            ready = []

        # Filter already-dispatched
        ready = [item for item in ready if item.id not in dispatched]

        selected = select_next_issues(ready, config)

        for item in selected:
            target = getattr(item, "target_repo", "") or repo_path
            pid = dispatch_issue(item.id, target)
            if pid is not None:
                dispatched.append(item.id)

        if max_cycles is not None:
            continue
        time.sleep(poll_interval)

    return dispatched


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if len(sys.argv) < 2:
        print("Usage: python -m devloop.orchestration.scheduler <repo_path>", file=sys.stderr)
        sys.exit(1)

    repo = sys.argv[1]
    logger.info("Starting scheduler for %s", repo)
    run_scheduler_loop(repo)
