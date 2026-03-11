"""Tests for devloop.intake.beads_poller — subprocess calls are mocked."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from devloop.intake.beads_poller import WorkItem, claim_issue, poll_ready

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_ISSUES = [
    {
        "id": "ISSUE-1",
        "title": "Fix login bug",
        "type": "bug",
        "priority": 1,
        "labels": ["bug", "repo:prompt-bench"],
        "description": "Login form crashes on empty password",
        "parent": None,
    },
    {
        "id": "ISSUE-2",
        "title": "Add search feature",
        "type": "feature",
        "priority": 2,
        "labels": ["feature"],
        "description": None,
        "parent": "EPIC-1",
    },
]


@pytest.fixture()
def sample_issues():
    return SAMPLE_ISSUES


# ---------------------------------------------------------------------------
# poll_ready tests
# ---------------------------------------------------------------------------


class TestPollReady:
    """Tests for poll_ready() function."""

    @patch("devloop.intake.beads_poller.subprocess.run")
    def test_valid_json(self, mock_run, sample_issues):
        """poll_ready() with valid JSON from br ready --json returns WorkItems."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["br", "ready", "--json"],
            returncode=0,
            stdout=json.dumps(sample_issues),
            stderr="",
        )

        items = poll_ready()

        assert len(items) == 2
        assert isinstance(items[0], WorkItem)
        assert items[0].id == "ISSUE-1"
        assert items[0].title == "Fix login bug"
        assert items[0].type == "bug"
        assert items[0].priority == 1
        assert items[0].labels == ["bug", "repo:prompt-bench"]
        assert items[0].description == "Login form crashes on empty password"
        assert items[1].id == "ISSUE-2"
        assert items[1].parent == "EPIC-1"

    @patch("devloop.intake.beads_poller.subprocess.run")
    def test_empty_json_array(self, mock_run):
        """poll_ready() with empty JSON array returns empty list."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["br", "ready", "--json"],
            returncode=0,
            stdout="[]",
            stderr="",
        )

        items = poll_ready()

        assert items == []

    @patch("devloop.intake.beads_poller.subprocess.run")
    def test_nonzero_exit_code(self, mock_run):
        """poll_ready() with br returning non-zero exit code returns []."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["br", "ready", "--json"],
            returncode=1,
            stdout="",
            stderr="br: command failed",
        )

        items = poll_ready()

        assert items == []

    @patch("devloop.intake.beads_poller.subprocess.run")
    def test_timeout_expired(self, mock_run):
        """poll_ready() with subprocess.TimeoutExpired returns []."""
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=["br", "ready", "--json"],
            timeout=30,
        )

        items = poll_ready()

        assert items == []


# ---------------------------------------------------------------------------
# claim_issue tests
# ---------------------------------------------------------------------------


class TestClaimIssue:
    """Tests for claim_issue() function."""

    @patch("devloop.intake.beads_poller.subprocess.run")
    def test_claim_success(self, mock_run):
        """claim_issue() success when status transitions to in_progress."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["br", "update", "ISSUE-1", "--claim"],
            returncode=0,
            stdout="status: open \u2192 in_progress\nassignee: agent",
            stderr="",
        )

        assert claim_issue("ISSUE-1") is True

    @patch("devloop.intake.beads_poller.subprocess.run")
    def test_claim_failure_nonzero(self, mock_run):
        """claim_issue() failure when br returns non-zero."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["br", "update", "ISSUE-1", "--claim"],
            returncode=1,
            stdout="",
            stderr="issue not found",
        )

        assert claim_issue("ISSUE-1") is False

    @patch("devloop.intake.beads_poller.subprocess.run")
    def test_claim_already_claimed(self, mock_run):
        """claim_issue() returns False when issue is already claimed (no status transition)."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["br", "update", "ISSUE-1", "--claim"],
            returncode=0,
            stdout="assignee: agent\n",
            stderr="",
        )

        assert claim_issue("ISSUE-1") is False

    @patch("devloop.intake.beads_poller.subprocess.run")
    def test_claim_timeout(self, mock_run):
        """claim_issue() returns False on timeout."""
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=["br", "update", "ISSUE-1", "--claim"],
            timeout=30,
        )

        assert claim_issue("ISSUE-1") is False


# ---------------------------------------------------------------------------
# WorkItem property tests
# ---------------------------------------------------------------------------


class TestWorkItemProperties:
    """Tests for WorkItem dataclass properties."""

    def test_target_repo_with_repo_label(self):
        """WorkItem.target_repo extracts repo name from 'repo:xxx' label."""
        item = WorkItem(
            id="ISSUE-1",
            title="Test",
            type="task",
            priority=2,
            labels=["bug", "repo:prompt-bench"],
        )
        assert item.target_repo == "prompt-bench"

    def test_target_repo_without_repo_label(self):
        """WorkItem.target_repo returns None when no repo: label exists."""
        item = WorkItem(
            id="ISSUE-1",
            title="Test",
            type="task",
            priority=2,
            labels=["bug", "feature"],
        )
        assert item.target_repo is None

    def test_persona_bug(self):
        """WorkItem.persona maps 'bug' label to 'bug-fix' persona."""
        item = WorkItem(
            id="ISSUE-1",
            title="Test",
            type="task",
            priority=2,
            labels=["bug"],
        )
        assert item.persona == "bug-fix"

    def test_persona_feature(self):
        """WorkItem.persona maps 'feature' label to 'feature' persona."""
        item = WorkItem(
            id="ISSUE-1",
            title="Test",
            type="task",
            priority=2,
            labels=["feature"],
        )
        assert item.persona == "feature"

    def test_persona_security(self):
        """WorkItem.persona maps 'security' label to 'security-fix' persona."""
        item = WorkItem(
            id="ISSUE-1",
            title="Test",
            type="task",
            priority=2,
            labels=["security"],
        )
        assert item.persona == "security-fix"

    def test_persona_docs(self):
        """WorkItem.persona maps 'docs' label to 'docs' persona."""
        item = WorkItem(
            id="ISSUE-1",
            title="Test",
            type="task",
            priority=2,
            labels=["docs"],
        )
        assert item.persona == "docs"

    def test_persona_refactor(self):
        """WorkItem.persona maps 'refactor' label to 'refactor' persona."""
        item = WorkItem(
            id="ISSUE-1",
            title="Test",
            type="task",
            priority=2,
            labels=["refactor"],
        )
        assert item.persona == "refactor"

    def test_persona_no_match(self):
        """WorkItem.persona returns None when no label matches a persona."""
        item = WorkItem(
            id="ISSUE-1",
            title="Test",
            type="task",
            priority=2,
            labels=["repo:prompt-bench", "urgent"],
        )
        assert item.persona is None

    def test_persona_first_match_wins(self):
        """WorkItem.persona returns the first matching persona."""
        item = WorkItem(
            id="ISSUE-1",
            title="Test",
            type="task",
            priority=2,
            labels=["bug", "feature"],
        )
        assert item.persona == "bug-fix"
