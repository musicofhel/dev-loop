"""Tests for devloop.gates.server — gitleaks discovery and project type detection."""

from __future__ import annotations

from unittest.mock import patch

from devloop.gates.server import _detect_project_type, _find_gitleaks

# ---------------------------------------------------------------------------
# _find_gitleaks tests
# ---------------------------------------------------------------------------


class TestFindGitleaks:
    """Tests for _find_gitleaks() function."""

    @patch("devloop.gates.server.shutil.which")
    def test_found_on_path(self, mock_which):
        """_find_gitleaks() returns path when shutil.which finds gitleaks."""
        mock_which.return_value = "/usr/local/bin/gitleaks"

        result = _find_gitleaks()

        assert result == "/usr/local/bin/gitleaks"
        mock_which.assert_called_once_with("gitleaks")

    @patch("devloop.gates.server._GITLEAKS_FALLBACK")
    @patch("devloop.gates.server.shutil.which")
    def test_fallback_path_exists(self, mock_which, mock_fallback):
        """_find_gitleaks() returns fallback path when which returns None but fallback exists."""
        mock_which.return_value = None
        mock_fallback.exists.return_value = True
        mock_fallback.__str__ = lambda self: "/home/user/.local/bin/gitleaks"

        result = _find_gitleaks()

        assert result == "/home/user/.local/bin/gitleaks"

    @patch("devloop.gates.server._GITLEAKS_FALLBACK")
    @patch("devloop.gates.server.shutil.which")
    def test_not_found_anywhere(self, mock_which, mock_fallback):
        """_find_gitleaks() returns None when neither PATH nor fallback has gitleaks."""
        mock_which.return_value = None
        mock_fallback.exists.return_value = False

        result = _find_gitleaks()

        assert result is None


# ---------------------------------------------------------------------------
# _detect_project_type tests
# ---------------------------------------------------------------------------


class TestDetectProjectType:
    """Tests for _detect_project_type() function."""

    def test_python_project(self, tmp_path):
        """_detect_project_type() returns 'python' when pyproject.toml exists."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")

        result = _detect_project_type(tmp_path)

        assert result == "python"

    def test_node_project(self, tmp_path):
        """_detect_project_type() returns 'node' when package.json exists."""
        (tmp_path / "package.json").write_text('{"name": "test"}')

        result = _detect_project_type(tmp_path)

        assert result == "node"

    def test_rust_project(self, tmp_path):
        """_detect_project_type() returns 'rust' when Cargo.toml exists."""
        (tmp_path / "Cargo.toml").write_text("[package]\nname = 'test'\n")

        result = _detect_project_type(tmp_path)

        assert result == "rust"

    def test_unknown_project(self, tmp_path):
        """_detect_project_type() returns 'unknown' when no known config file exists."""
        result = _detect_project_type(tmp_path)

        assert result == "unknown"

    def test_node_takes_precedence_over_python(self, tmp_path):
        """When both package.json and pyproject.toml exist, node wins (checked first)."""
        (tmp_path / "package.json").write_text('{"name": "test"}')
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")

        result = _detect_project_type(tmp_path)

        assert result == "node"
