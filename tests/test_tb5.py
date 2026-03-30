"""Tests for TB-5: Cross-Repo Cascade — dependency detection, issue creation."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from devloop.feedback.pipeline import (
    _create_cascade_issue,
    _get_changed_files,
    _get_source_issue_details,
    _load_dependency_map,
    _match_watches,
    _report_cascade_outcome,
)
from devloop.feedback.types import TB5Result

# ---------------------------------------------------------------------------
# TB5Result type tests
# ---------------------------------------------------------------------------


class TestTB5Result:
    """Tests for TB5Result Pydantic model."""

    def test_defaults(self):
        """TB5Result has sensible defaults."""
        r = TB5Result(
            issue_id="dl-src",
            repo_path="/tmp/source",
            success=True,
            phase="complete",
        )
        assert r.target_repo_path == ""
        assert r.target_issue_id is None
        assert r.changed_files == []
        assert r.matched_watches == []
        assert r.dependency_type is None
        assert r.cascade_skipped is False
        assert r.tb1_result is None
        assert r.source_comment_added is False
        assert r.error is None

    def test_full_fields(self):
        """TB5Result stores all provided values."""
        r = TB5Result(
            issue_id="dl-src",
            repo_path="/tmp/source",
            success=True,
            phase="complete",
            target_repo_path="/tmp/target",
            target_issue_id="dl-tgt",
            changed_files=["src/api/routes.py", "src/types/user.py"],
            matched_watches=["src/api/**", "src/types/**"],
            dependency_type="api-contract",
            cascade_skipped=False,
            tb1_result={"success": True, "phase": "gates_passed"},
            source_comment_added=True,
        )
        assert r.target_issue_id == "dl-tgt"
        assert len(r.changed_files) == 2
        assert r.dependency_type == "api-contract"
        assert r.tb1_result["success"] is True

    def test_cascade_skipped(self):
        """TB5Result correctly represents a skipped cascade."""
        r = TB5Result(
            issue_id="dl-src",
            repo_path="/tmp/source",
            success=True,
            phase="match_dependencies",
            cascade_skipped=True,
        )
        assert r.cascade_skipped is True
        assert r.target_issue_id is None
        assert r.tb1_result is None

    def test_roundtrip(self):
        """TB5Result serializes and deserializes correctly."""
        r = TB5Result(
            issue_id="dl-src",
            repo_path="/tmp/source",
            success=False,
            phase="cascade_tb1",
            target_repo_path="/tmp/target",
            target_issue_id="dl-tgt",
            changed_files=["src/api/handler.py"],
            matched_watches=["src/api/**"],
            dependency_type="api-contract",
            tb1_result={"success": False, "error": "Gate 0 failed"},
            error="TB-1 failed on target repo",
        )
        d = r.model_dump()
        r2 = TB5Result(**d)
        assert r2.issue_id == r.issue_id
        assert r2.target_issue_id == r.target_issue_id
        assert r2.tb1_result == r.tb1_result


# ---------------------------------------------------------------------------
# _load_dependency_map tests
# ---------------------------------------------------------------------------


class TestLoadDependencyMap:
    """Tests for loading config/dependencies.yaml."""

    @patch("devloop.feedback.tb5_cascade._CONFIG_DIR", new_callable=lambda: MagicMock)
    def test_valid_load(self, mock_config_dir, tmp_path):
        """Loads and returns dependency list from valid YAML."""
        dep_file = tmp_path / "dependencies.yaml"
        dep_file.write_text(
            "dependencies:\n"
            "  - source: repo-a\n"
            "    target: repo-b\n"
            "    watches:\n"
            '      - "src/api/**"\n'
            "    type: api-contract\n"
        )
        with patch("devloop.feedback.tb5_cascade._CONFIG_DIR", tmp_path):
            result = _load_dependency_map()
        assert len(result) == 1
        assert result[0]["source"] == "repo-a"
        assert result[0]["target"] == "repo-b"
        assert result[0]["watches"] == ["src/api/**"]
        assert result[0]["type"] == "api-contract"

    def test_missing_file(self, tmp_path):
        """Returns empty list when file doesn't exist."""
        with patch("devloop.feedback.tb5_cascade._CONFIG_DIR", tmp_path):
            result = _load_dependency_map()
        assert result == []

    def test_malformed_yaml(self, tmp_path):
        """Returns empty list when YAML lacks 'dependencies' key."""
        dep_file = tmp_path / "dependencies.yaml"
        dep_file.write_text("something_else:\n  - foo\n")
        with patch("devloop.feedback.tb5_cascade._CONFIG_DIR", tmp_path):
            result = _load_dependency_map()
        assert result == []

    def test_multiple_entries(self, tmp_path):
        """Returns all dependency entries."""
        dep_file = tmp_path / "dependencies.yaml"
        dep_file.write_text(
            "dependencies:\n"
            "  - source: a\n"
            "    target: b\n"
            "    watches: ['src/**']\n"
            "    type: api\n"
            "  - source: b\n"
            "    target: c\n"
            "    watches: ['lib/**']\n"
            "    type: schema\n"
        )
        with patch("devloop.feedback.tb5_cascade._CONFIG_DIR", tmp_path):
            result = _load_dependency_map()
        assert len(result) == 2
        assert result[1]["source"] == "b"


# ---------------------------------------------------------------------------
# _get_changed_files tests
# ---------------------------------------------------------------------------


class TestGetChangedFiles:
    """Tests for git diff --name-only."""

    @patch("devloop.feedback.pipeline.subprocess.run")
    def test_success(self, mock_run):
        """Returns list of changed file paths."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="src/api/routes.py\nsrc/types/user.py\n",
        )
        result = _get_changed_files("/tmp/repo", "dl-abc")
        assert result == ["src/api/routes.py", "src/types/user.py"]
        # Verify correct git command
        args = mock_run.call_args[0][0]
        assert args == ["git", "diff", "main..dl/dl-abc", "--name-only"]
        assert mock_run.call_args[1]["cwd"] == "/tmp/repo"

    @patch("devloop.feedback.pipeline.subprocess.run")
    def test_missing_branch(self, mock_run):
        """Raises RuntimeError when branch doesn't exist."""
        mock_run.return_value = MagicMock(
            returncode=128,
            stderr="fatal: bad revision 'main..dl/dl-xxx'",
        )
        with pytest.raises(RuntimeError, match="bad revision"):
            _get_changed_files("/tmp/repo", "dl-xxx")

    @patch("devloop.feedback.pipeline.subprocess.run")
    def test_empty_diff(self, mock_run):
        """Returns empty list when no files changed."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="",
        )
        result = _get_changed_files("/tmp/repo", "dl-abc")
        assert result == []


# ---------------------------------------------------------------------------
# _match_watches tests
# ---------------------------------------------------------------------------


class TestMatchWatches:
    """Tests for glob pattern matching."""

    def test_glob_double_star(self):
        """Matches files under nested directories with ** pattern."""
        files = ["src/api/routes.py", "src/api/handlers/user.py"]
        watches = ["src/api/**"]
        assert _match_watches(files, watches) == ["src/api/**"]

    def test_exact_match(self):
        """Matches exact file paths."""
        files = ["prisma/schema.prisma"]
        watches = ["prisma/schema.prisma"]
        assert _match_watches(files, watches) == ["prisma/schema.prisma"]

    def test_no_match(self):
        """Returns empty when no patterns match."""
        files = ["README.md", "docs/guide.md"]
        watches = ["src/api/**", "src/types/**"]
        assert _match_watches(files, watches) == []

    def test_multiple_patterns_match(self):
        """Returns all matching patterns."""
        files = ["src/api/routes.py", "src/types/user.py"]
        watches = ["src/api/**", "src/types/**", "src/lib/**"]
        result = _match_watches(files, watches)
        assert "src/api/**" in result
        assert "src/types/**" in result
        assert "src/lib/**" not in result

    def test_nested_path(self):
        """Matches deeply nested paths."""
        files = ["src/api/v2/handlers/auth/login.py"]
        watches = ["src/api/**"]
        assert _match_watches(files, watches) == ["src/api/**"]

    def test_empty_files(self):
        """Returns empty when no files provided."""
        watches = ["src/api/**"]
        assert _match_watches([], watches) == []

    def test_empty_watches(self):
        """Returns empty when no watch patterns provided."""
        files = ["src/api/routes.py"]
        assert _match_watches(files, []) == []


# ---------------------------------------------------------------------------
# _create_cascade_issue tests
# ---------------------------------------------------------------------------


class TestCreateCascadeIssue:
    """Tests for br create --silent --parent."""

    @patch("devloop.feedback.pipeline.subprocess.run")
    def test_success_with_correct_args(self, mock_run):
        """Creates issue with correct title, labels, parent."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="dl-cascade-1\n",
        )
        result = _create_cascade_issue(
            source_issue_id="dl-src",
            source_title="Update API",
            target_repo_name="OOTestProject2",
            matched_watches=["src/oo_test_project/db/**"],
            dependency_type="data-model",
        )
        assert result == "dl-cascade-1"

        # Verify br create args
        args = mock_run.call_args[0][0]
        assert args[0] == "br"
        assert args[1] == "create"
        assert "[cascade]" in args[2]
        assert "--parent" in args
        parent_idx = args.index("--parent")
        assert args[parent_idx + 1] == "dl-src"
        assert "--labels" in args
        labels_idx = args.index("--labels")
        assert "cascade" in args[labels_idx + 1]
        assert "repo:OOTestProject2" in args[labels_idx + 1]
        assert "--silent" in args

    @patch("devloop.feedback.pipeline.subprocess.run")
    def test_failure(self, mock_run):
        """Raises RuntimeError when br create fails."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stderr="error: workspace not initialized",
        )
        with pytest.raises(RuntimeError, match="workspace not initialized"):
            _create_cascade_issue("dl-src", "Title", "target", ["src/**"], "api")

    @patch("devloop.feedback.pipeline.subprocess.run")
    def test_labels_format(self, mock_run):
        """Labels include 'cascade' and 'repo:<target>'."""
        mock_run.return_value = MagicMock(returncode=0, stdout="dl-c1\n")
        _create_cascade_issue("dl-src", "T", "my-repo", ["**"], "schema")
        args = mock_run.call_args[0][0]
        labels_idx = args.index("--labels")
        labels = args[labels_idx + 1]
        assert labels == "cascade,repo:my-repo"

    @patch("devloop.feedback.pipeline.subprocess.run")
    def test_parent_linked(self, mock_run):
        """Parent is set to source issue ID."""
        mock_run.return_value = MagicMock(returncode=0, stdout="dl-c2\n")
        _create_cascade_issue("dl-parent", "T", "repo", ["**"], "api")
        args = mock_run.call_args[0][0]
        parent_idx = args.index("--parent")
        assert args[parent_idx + 1] == "dl-parent"


# ---------------------------------------------------------------------------
# _report_cascade_outcome tests
# ---------------------------------------------------------------------------


class TestReportCascadeOutcome:
    """Tests for br comments add on source issue."""

    @patch("devloop.feedback.pipeline.subprocess.run")
    def test_success_message(self, mock_run):
        """Reports SUCCESS with target issue ID."""
        mock_run.return_value = MagicMock(returncode=0)
        result = _report_cascade_outcome(
            source_issue_id="dl-src",
            target_issue_id="dl-tgt",
            target_repo_name="backend",
            success=True,
            cascade_skipped=False,
        )
        assert result is True
        args = mock_run.call_args[0][0]
        # Verify --message flag is present (bug fix: was missing before)
        assert "--message" in args
        msg = args[-1]
        assert "SUCCESS" in msg
        assert "dl-tgt" in msg
        assert "backend" in msg

    @patch("devloop.feedback.pipeline.subprocess.run")
    def test_failure_message(self, mock_run):
        """Reports FAILED with error detail."""
        mock_run.return_value = MagicMock(returncode=0)
        result = _report_cascade_outcome(
            source_issue_id="dl-src",
            target_issue_id="dl-tgt",
            target_repo_name="backend",
            success=False,
            cascade_skipped=False,
            error="Gate 0 failed",
        )
        assert result is True
        msg = mock_run.call_args[0][0][-1]
        assert "FAILED" in msg
        assert "Gate 0 failed" in msg

    @patch("devloop.feedback.pipeline.subprocess.run")
    def test_skipped_message(self, mock_run):
        """Reports SKIPPED when cascade not needed."""
        mock_run.return_value = MagicMock(returncode=0)
        result = _report_cascade_outcome(
            source_issue_id="dl-src",
            target_issue_id=None,
            target_repo_name="backend",
            success=True,
            cascade_skipped=True,
        )
        assert result is True
        msg = mock_run.call_args[0][0][-1]
        assert "SKIPPED" in msg

    @patch("devloop.feedback.pipeline.subprocess.run")
    def test_br_error_returns_false(self, mock_run):
        """Returns False when br comments add fails."""
        mock_run.return_value = MagicMock(returncode=1, stderr="error")
        result = _report_cascade_outcome(
            source_issue_id="dl-src",
            target_issue_id="dl-tgt",
            target_repo_name="backend",
            success=True,
            cascade_skipped=False,
        )
        assert result is False


# ---------------------------------------------------------------------------
# _get_source_issue_details tests
# ---------------------------------------------------------------------------


class TestGetSourceIssueDetails:
    """Tests for br show --format json."""

    @patch("devloop.feedback.pipeline.subprocess.run")
    def test_success(self, mock_run):
        """Returns parsed title, description, labels."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({
                "title": "Update API signature",
                "description": "Change endpoint params",
                "labels": ["feature", "api"],
            }),
        )
        result = _get_source_issue_details("dl-abc")
        assert result["title"] == "Update API signature"
        assert result["description"] == "Change endpoint params"
        assert result["labels"] == ["feature", "api"]

    @patch("devloop.feedback.pipeline.subprocess.run")
    def test_list_response(self, mock_run):
        """Handles br show returning a JSON list (single element)."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([{
                "title": "List item title",
                "description": "From list response",
                "labels": ["feature"],
            }]),
        )
        result = _get_source_issue_details("dl-list")
        assert result["title"] == "List item title"
        assert result["labels"] == ["feature"]

    @patch("devloop.feedback.pipeline.subprocess.run")
    def test_empty_list_response(self, mock_run):
        """Raises RuntimeError when br show returns empty list."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="[]",
        )
        with pytest.raises(RuntimeError, match="not found"):
            _get_source_issue_details("dl-empty")

    @patch("devloop.feedback.pipeline.subprocess.run")
    def test_br_error(self, mock_run):
        """Raises RuntimeError on br failure."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stderr="error: issue not found",
        )
        with pytest.raises(RuntimeError, match="issue not found"):
            _get_source_issue_details("dl-xxx")

    @patch("devloop.feedback.pipeline.subprocess.run")
    def test_bad_json(self, mock_run):
        """Raises on malformed JSON output."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="not json at all",
        )
        with pytest.raises(json.JSONDecodeError):
            _get_source_issue_details("dl-abc")


# ---------------------------------------------------------------------------
# _resolve_repo_path tests
# ---------------------------------------------------------------------------


class TestResolveRepoPath:
    """Tests for _resolve_repo_path() — repo name to absolute path lookup."""

    def test_resolves_known_repo(self, tmp_path):
        """Returns absolute path for a known repo name."""
        dep_file = tmp_path / "dependencies.yaml"
        dep_file.write_text(
            "repo_paths:\n"
            "  OOTestProject1: /home/user/OOTestProject1\n"
            "  OOTestProject2: /home/user/OOTestProject2\n"
            "dependencies: []\n"
        )
        from devloop.feedback.tb5_cascade import _resolve_repo_path

        with patch("devloop.feedback.tb5_cascade._CONFIG_DIR", tmp_path):
            assert _resolve_repo_path("OOTestProject1") == "/home/user/OOTestProject1"

    def test_returns_none_for_unknown_repo(self, tmp_path):
        """Returns None for a repo name not in repo_paths."""
        dep_file = tmp_path / "dependencies.yaml"
        dep_file.write_text(
            "repo_paths:\n"
            "  OOTestProject1: /home/user/OOTestProject1\n"
            "  OOTestProject2: /home/user/OOTestProject2\n"
            "dependencies: []\n"
        )
        from devloop.feedback.tb5_cascade import _resolve_repo_path

        with patch("devloop.feedback.tb5_cascade._CONFIG_DIR", tmp_path):
            assert _resolve_repo_path("nonexistent") is None

    def test_returns_none_when_config_missing(self, tmp_path):
        """Returns None when dependencies.yaml doesn't exist."""
        from devloop.feedback.tb5_cascade import _resolve_repo_path

        with patch("devloop.feedback.tb5_cascade._CONFIG_DIR", tmp_path):
            assert _resolve_repo_path("anything") is None

    def test_returns_none_when_no_repo_paths_key(self, tmp_path):
        """Returns None when repo_paths key is absent."""
        dep_file = tmp_path / "dependencies.yaml"
        dep_file.write_text("dependencies: []\n")
        from devloop.feedback.tb5_cascade import _resolve_repo_path

        with patch("devloop.feedback.tb5_cascade._CONFIG_DIR", tmp_path):
            assert _resolve_repo_path("anything") is None


# ---------------------------------------------------------------------------
# find_cascade_targets tests
# ---------------------------------------------------------------------------


class TestFindCascadeTargets:
    """Tests for find_cascade_targets() — TB-1 integration helper."""

    @patch("devloop.feedback.tb5_cascade._resolve_repo_path")
    @patch("devloop.feedback.tb5_cascade._load_dependency_map")
    @patch("devloop.feedback.tb5_cascade._get_changed_files")
    def test_finds_matching_targets(self, mock_changed, mock_deps, mock_resolve):
        """Returns targets when changed files match watch patterns."""
        from devloop.feedback.tb5_cascade import find_cascade_targets

        mock_changed.return_value = ["src/oo_test_project/db/users.py"]
        mock_deps.return_value = [
            {"source": "OOTestProject1", "target": "OOTestProject2",
             "watches": ["src/oo_test_project/db/**"], "type": "data-model"},
        ]
        mock_resolve.return_value = "/home/user/OOTestProject2"

        targets = find_cascade_targets("/home/user/OOTestProject1", "dl-test")
        assert len(targets) == 1
        assert targets[0]["target_repo_name"] == "OOTestProject2"
        assert targets[0]["target_repo_path"] == "/home/user/OOTestProject2"
        assert targets[0]["matched_watches"] == ["src/oo_test_project/db/**"]

    @patch("devloop.feedback.tb5_cascade._get_changed_files")
    def test_no_changed_files_returns_empty(self, mock_changed):
        """Returns empty list when no files changed."""
        from devloop.feedback.tb5_cascade import find_cascade_targets

        mock_changed.return_value = []
        assert find_cascade_targets("/home/user/OOTestProject1", "dl-test") == []

    @patch("devloop.feedback.tb5_cascade._resolve_repo_path")
    @patch("devloop.feedback.tb5_cascade._load_dependency_map")
    @patch("devloop.feedback.tb5_cascade._get_changed_files")
    def test_no_watch_match_returns_empty(self, mock_changed, mock_deps, mock_resolve):
        """Returns empty when no watch patterns match changed files."""
        from devloop.feedback.tb5_cascade import find_cascade_targets

        mock_changed.return_value = ["docs/README.md"]
        mock_deps.return_value = [
            {"source": "OOTestProject1", "target": "OOTestProject2",
             "watches": ["src/oo_test_project/db/**"], "type": "data-model"},
        ]
        assert find_cascade_targets("/home/user/OOTestProject1", "dl-test") == []

    @patch("devloop.feedback.tb5_cascade._resolve_repo_path")
    @patch("devloop.feedback.tb5_cascade._load_dependency_map")
    @patch("devloop.feedback.tb5_cascade._get_changed_files")
    def test_missing_repo_path_skips_target(self, mock_changed, mock_deps, mock_resolve):
        """Skips target when repo_path can't be resolved."""
        from devloop.feedback.tb5_cascade import find_cascade_targets

        mock_changed.return_value = ["src/oo_test_project/db/users.py"]
        mock_deps.return_value = [
            {"source": "OOTestProject1", "target": "OOTestProject2",
             "watches": ["src/oo_test_project/db/**"], "type": "data-model"},
        ]
        mock_resolve.return_value = None  # path not configured

        assert find_cascade_targets("/home/user/OOTestProject1", "dl-test") == []

    @patch("devloop.feedback.tb5_cascade._get_changed_files")
    def test_git_diff_failure_returns_empty(self, mock_changed):
        """Returns empty list when git diff fails (fail-safe)."""
        from devloop.feedback.tb5_cascade import find_cascade_targets

        mock_changed.side_effect = RuntimeError("git diff failed")
        assert find_cascade_targets("/home/user/OOTestProject1", "dl-test") == []
