"""Tests for devloop.runtime.deny_list — secrets deny list."""

from __future__ import annotations

import pytest

from devloop.runtime.deny_list import (
    DENIED_PATTERNS,
    generate_deny_rules,
    is_path_denied,
)

# ---------------------------------------------------------------------------
# is_path_denied — blocked paths
# ---------------------------------------------------------------------------


class TestIsPathDenied:
    """Tests for is_path_denied() function."""

    @pytest.mark.parametrize(
        "path",
        [
            ".env",
            ".env.local",
            ".env.production",
            "config/.env",
            "config/.env.staging",
        ],
    )
    def test_env_files_denied(self, path):
        """is_path_denied() returns True for .env files."""
        assert is_path_denied(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "server.key",
            "cert.pem",
            "ssl/private.key",
            "certs/ca-bundle.pem",
        ],
    )
    def test_crypto_files_denied(self, path):
        """is_path_denied() returns True for .pem and .key files."""
        assert is_path_denied(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            ".ssh/id_rsa",
            "home/user/.ssh/id_rsa",
            ".ssh/config",
        ],
    )
    def test_ssh_files_denied(self, path):
        """is_path_denied() returns True for .ssh/ directory files."""
        assert is_path_denied(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "credentials.json",
            "credentials.yaml",
            "config/credentials.toml",
        ],
    )
    def test_credentials_files_denied(self, path):
        """is_path_denied() returns True for credentials.* files."""
        assert is_path_denied(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "secrets.yaml",
            "my_secret_key.txt",
            "config/app_secret.json",
        ],
    )
    def test_secret_in_name_denied(self, path):
        """is_path_denied() returns True for paths containing 'secret'."""
        assert is_path_denied(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            ".aws/credentials",
            ".aws/config",
            "home/user/.aws/credentials",
        ],
    )
    def test_aws_files_denied(self, path):
        """is_path_denied() returns True for .aws/ directory files."""
        assert is_path_denied(path) is True

    # ---------------------------------------------------------------------------
    # is_path_denied — allowed paths
    # ---------------------------------------------------------------------------

    @pytest.mark.parametrize(
        "path",
        [
            "main.py",
            "src/app.ts",
            "README.md",
            "docs/guide.md",
            "setup.cfg",
            "pyproject.toml",
            "package.json",
            "Dockerfile",
            "src/utils/helpers.py",
        ],
    )
    def test_normal_source_files_allowed(self, path):
        """is_path_denied() returns False for normal source files."""
        assert is_path_denied(path) is False

    @pytest.mark.parametrize(
        "path",
        [
            "release.keystore",
            "debug.jks",
            ".netrc",
            ".npmrc",
            ".pypirc",
        ],
    )
    def test_auth_files_denied(self, path):
        """is_path_denied() returns True for keystore, jks, netrc, npmrc, pypirc files."""
        assert is_path_denied(path) is True


# ---------------------------------------------------------------------------
# generate_deny_rules
# ---------------------------------------------------------------------------


class TestGenerateDenyRules:
    """Tests for generate_deny_rules() function."""

    def test_returns_non_empty_string(self):
        """generate_deny_rules() returns a non-empty string."""
        rules = generate_deny_rules()
        assert isinstance(rules, str)
        assert len(rules) > 0

    def test_contains_header(self):
        """generate_deny_rules() includes the 'NEVER read' header."""
        rules = generate_deny_rules()
        assert "NEVER read these files" in rules

    def test_contains_patterns(self):
        """generate_deny_rules() includes key patterns from the deny list."""
        rules = generate_deny_rules()
        assert ".env" in rules
        assert ".key" in rules or "*.key" in rules

    def test_denied_patterns_not_empty(self):
        """DENIED_PATTERNS list contains entries."""
        assert len(DENIED_PATTERNS) > 0
