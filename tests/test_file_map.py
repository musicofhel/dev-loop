"""Tests for file map generation and scope hint extraction (#26)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from devloop.orchestration.file_map import (
    _list_files,
    extract_scope_hints,
    generate_directory_summary,
    generate_file_map,
)


def _get_paths(repo: Path) -> list[str]:
    """Get file paths for scope hint tests."""
    return _list_files(str(repo))


def _init_git_repo(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    """Create a minimal git repo with given files."""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path, capture_output=True, check=True,
    )
    if files is None:
        files = {
            "src/main.py": "print('hello')",
            "src/utils/helpers.py": "def helper(): pass",
            "src/utils/config.py": "CONFIG = {}",
            "tests/test_main.py": "def test(): pass",
            "README.md": "# Test",
            "pyproject.toml": "[project]\nname = 'test'",
        }
    for path, content in files.items():
        full = tmp_path / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path, capture_output=True, check=True,
    )
    return tmp_path


# ---------------------------------------------------------------------------
# _list_files
# ---------------------------------------------------------------------------


class TestListFiles:
    def test_lists_tracked_files(self, tmp_path):
        repo = _init_git_repo(tmp_path)
        files = _list_files(str(repo))
        assert "src/main.py" in files
        assert "tests/test_main.py" in files

    def test_respects_max_files(self, tmp_path):
        repo = _init_git_repo(tmp_path)
        files = _list_files(str(repo), max_files=2)
        assert len(files) == 2

    def test_nonexistent_repo(self, tmp_path):
        files = _list_files(str(tmp_path / "nonexistent"))
        assert files == []


# ---------------------------------------------------------------------------
# generate_directory_summary
# ---------------------------------------------------------------------------


class TestGenerateDirectorySummary:
    def test_includes_file_count(self, tmp_path):
        repo = _init_git_repo(tmp_path)
        summary = generate_directory_summary(str(repo))
        assert "6 tracked files" in summary

    def test_includes_languages(self, tmp_path):
        repo = _init_git_repo(tmp_path)
        summary = generate_directory_summary(str(repo))
        assert "Python" in summary

    def test_includes_directories(self, tmp_path):
        repo = _init_git_repo(tmp_path)
        summary = generate_directory_summary(str(repo))
        assert "src" in summary
        assert "tests" in summary

    def test_empty_repo(self, tmp_path):
        summary = generate_directory_summary(str(tmp_path / "nonexistent"))
        assert summary == ""


# ---------------------------------------------------------------------------
# generate_file_map
# ---------------------------------------------------------------------------


class TestGenerateFileMap:
    def test_contains_files(self, tmp_path):
        repo = _init_git_repo(tmp_path)
        file_map = generate_file_map(str(repo))
        assert "main.py" in file_map
        assert "helpers.py" in file_map

    def test_contains_directory_structure(self, tmp_path):
        repo = _init_git_repo(tmp_path)
        file_map = generate_file_map(str(repo))
        assert "src/" in file_map
        assert "tests/" in file_map

    def test_cache_hit(self, tmp_path):
        repo = _init_git_repo(tmp_path)
        # First call generates
        map1 = generate_file_map(str(repo))
        # Second call should hit cache
        map2 = generate_file_map(str(repo))
        assert map1 == map2

    def test_empty_repo(self, tmp_path):
        file_map = generate_file_map(str(tmp_path / "nonexistent"))
        assert file_map == ""


# ---------------------------------------------------------------------------
# extract_scope_hints
# ---------------------------------------------------------------------------


class TestExtractScopeHints:
    def test_detects_file_paths(self, tmp_path):
        repo = _init_git_repo(tmp_path)
        paths = _get_paths(repo)
        hints = extract_scope_hints(
            "Fix bug in src/main.py",
            "The function in src/utils/helpers.py is broken",
            paths,
        )
        assert "src/main.py" in hints
        assert "src/utils/helpers.py" in hints

    def test_detects_bare_filenames(self, tmp_path):
        repo = _init_git_repo(tmp_path)
        paths = _get_paths(repo)
        hints = extract_scope_hints(
            "Fix helpers.py",
            "helpers.py has a bug",
            paths,
        )
        assert any("helpers.py" in h for h in hints)

    def test_generic_text_returns_empty(self):
        hints = extract_scope_hints(
            "Improve performance",
            "Make everything faster",
            ["src/main.py", "tests/test.py"],
        )
        assert len(hints) == 0

    def test_module_references(self, tmp_path):
        repo = _init_git_repo(tmp_path)
        paths = _get_paths(repo)
        hints = extract_scope_hints(
            "Fix bug",
            "The issue is in `src.utils.helpers` module",
            paths,
        )
        assert any("src/utils" in h for h in hints)

    def test_deduplicates(self, tmp_path):
        repo = _init_git_repo(tmp_path)
        paths = _get_paths(repo)
        hints = extract_scope_hints(
            "Fix src/main.py",
            "Check src/main.py again and src/main.py once more",
            paths,
        )
        assert hints.count("src/main.py") == 1

    def test_no_false_positives_on_prose(self):
        hints = extract_scope_hints(
            "Update documentation",
            "We need to update the README with new instructions for the team.",
            [],
        )
        assert len(hints) == 0
