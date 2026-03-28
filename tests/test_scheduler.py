"""Tests for priority scheduler (#36)."""

from pathlib import Path
from unittest.mock import patch

from devloop.orchestration.scheduler import (
    SchedulerConfig,
    WorkItemLike,
    compute_min_priority,
    count_active_agents,
    load_scheduler_config,
    select_next_issues,
    sort_by_priority,
)


def _item(
    id: str,
    priority: int = 4,
    title: str = "Fix bug in auth.py",
    description: str = "The function should return 401",
) -> WorkItemLike:
    return WorkItemLike(id=id, priority=priority, title=title, description=description)


class TestSortByPriority:
    def test_sorts_ascending(self):
        items = [_item("c", 2), _item("a", 0), _item("b", 1)]
        result = sort_by_priority(items)
        assert [r.id for r in result] == ["a", "b", "c"]

    def test_fifo_within_same(self):
        items = [_item("c", 1), _item("a", 1), _item("b", 1)]
        result = sort_by_priority(items)
        assert [r.id for r in result] == ["a", "b", "c"]

    def test_empty_list(self):
        assert sort_by_priority([]) == []

    def test_single_item(self):
        items = [_item("a", 0)]
        assert sort_by_priority(items) == items

    def test_all_same_priority(self):
        items = [_item("z", 2), _item("a", 2), _item("m", 2)]
        result = sort_by_priority(items)
        assert [r.id for r in result] == ["a", "m", "z"]


class TestComputeMinPriority:
    def test_under_80_allows_all(self):
        assert compute_min_priority(50.0) == 4

    def test_at_80_p1_and_above(self):
        assert compute_min_priority(80.0) == 1

    def test_between_80_and_95(self):
        assert compute_min_priority(90.0) == 1

    def test_at_95_p0_only(self):
        assert compute_min_priority(95.0) == 0

    def test_at_100_pauses(self):
        assert compute_min_priority(100.0) == -1

    def test_zero_allows_all(self):
        assert compute_min_priority(0.0) == 4

    def test_just_under_80(self):
        assert compute_min_priority(79.9) == 4

    def test_just_under_95(self):
        assert compute_min_priority(94.9) == 1


class TestSelectNextIssues:
    def test_selects_highest_priority(self):
        items = [_item("low", 3), _item("high", 0), _item("med", 1)]
        config = SchedulerConfig(max_concurrent_agents=1)
        result = select_next_issues(items, config, active_count=0, budget_pct=0, check_ambiguity=False)
        assert len(result) == 1
        assert result[0].id == "high"

    def test_respects_max_concurrent(self):
        items = [_item(f"i{i}", 0) for i in range(5)]
        config = SchedulerConfig(max_concurrent_agents=3)
        result = select_next_issues(items, config, active_count=0, budget_pct=0, check_ambiguity=False)
        assert len(result) == 3

    def test_no_dispatch_when_full(self):
        items = [_item("a", 0)]
        config = SchedulerConfig(max_concurrent_agents=3)
        result = select_next_issues(items, config, active_count=3, budget_pct=0, check_ambiguity=False)
        assert len(result) == 0

    def test_budget_throttle_filters(self):
        items = [_item("p0", 0), _item("p2", 2), _item("p4", 4)]
        config = SchedulerConfig(max_concurrent_agents=5)
        # At 85%, min_pri=1, so only P0 (pri=0) is allowed (P0 <= 1)
        result = select_next_issues(items, config, active_count=0, budget_pct=85, check_ambiguity=False)
        assert len(result) == 1
        assert result[0].id == "p0"

    def test_pause_all_returns_empty(self):
        items = [_item("a", 0)]
        config = SchedulerConfig(max_concurrent_agents=5)
        result = select_next_issues(items, config, active_count=0, budget_pct=100, check_ambiguity=False)
        assert len(result) == 0

    def test_mixed_priorities_partial_slots(self):
        items = [_item("p0", 0), _item("p1", 1), _item("p2", 2)]
        config = SchedulerConfig(max_concurrent_agents=2)
        result = select_next_issues(items, config, active_count=0, budget_pct=0, check_ambiguity=False)
        assert len(result) == 2
        assert result[0].id == "p0"
        assert result[1].id == "p1"

    def test_empty_ready_list(self):
        config = SchedulerConfig()
        result = select_next_issues([], config, active_count=0, budget_pct=0, check_ambiguity=False)
        assert len(result) == 0

    @patch("devloop.orchestration.scheduler.defer_ambiguous_issue")
    def test_ambiguity_check_defers(self, mock_defer):
        mock_defer.return_value = True
        # Vague title, no description -> ambiguous
        items = [_item("vague", 0, title="Improve performance", description="")]
        config = SchedulerConfig(max_concurrent_agents=5)
        result = select_next_issues(items, config, active_count=0, budget_pct=0, check_ambiguity=True)
        assert len(result) == 0
        assert mock_defer.call_count == 1


class TestCountActiveAgents:
    def test_no_locks(self, tmp_path):
        assert count_active_agents(tmp_path) == 0

    def test_no_dir(self, tmp_path):
        assert count_active_agents(tmp_path / "nonexistent") == 0

    def test_stale_lock_not_counted(self, tmp_path):
        # Create a lock file but don't hold the lock
        (tmp_path / "test.lock").touch()
        assert count_active_agents(tmp_path) == 0


class TestLoadSchedulerConfig:
    def test_loads_real_config(self):
        config = load_scheduler_config()
        assert config.max_concurrent_agents == 3
        assert config.priority_order == ["P0", "P1", "P2", "P3", "P4"]

    def test_defaults_on_missing(self, tmp_path):
        config = load_scheduler_config(tmp_path / "nonexistent.yaml")
        assert config.max_concurrent_agents == 3

    def test_loads_weekly_budget(self):
        config = load_scheduler_config()
        assert config.weekly_budget_turns == 1400
        assert config.weekly_budget_input_tokens == 35_000_000
        assert config.weekly_budget_output_tokens == 7_000_000
