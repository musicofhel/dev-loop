"""Tests for devloop.orchestration.server — persona matching and config loading."""

from __future__ import annotations

import pytest

from devloop.orchestration.server import (
    AGENTS_CONFIG,
    _load_agents_config,
    _match_persona,
    select_persona,
)

# ---------------------------------------------------------------------------
# _load_agents_config tests
# ---------------------------------------------------------------------------


class TestLoadAgentsConfig:
    """Tests for _load_agents_config()."""

    def test_loads_real_config(self):
        """_load_agents_config() successfully loads config/agents.yaml."""
        config = _load_agents_config()
        assert isinstance(config, dict)
        assert "personas" in config

    def test_config_has_expected_personas(self):
        """The loaded config contains all expected persona names."""
        config = _load_agents_config()
        personas = config["personas"]
        expected = {"bug-fix", "feature", "refactor", "security-fix", "docs"}
        assert set(personas.keys()) == expected

    def test_each_persona_has_labels(self):
        """Each persona in the config has a labels list."""
        config = _load_agents_config()
        for name, data in config["personas"].items():
            assert "labels" in data, f"Persona '{name}' missing 'labels'"
            assert isinstance(data["labels"], list)

    def test_config_file_path(self):
        """AGENTS_CONFIG points to an existing file."""
        assert AGENTS_CONFIG.exists(), f"Config not found at {AGENTS_CONFIG}"


# ---------------------------------------------------------------------------
# _match_persona tests
# ---------------------------------------------------------------------------


class TestMatchPersona:
    """Tests for _match_persona()."""

    @pytest.fixture()
    def config(self):
        """Load the real agents.yaml config."""
        return _load_agents_config()

    def test_match_bug_fix(self, config):
        """Labels containing 'bug' match the bug-fix persona."""
        result = _match_persona(["bug", "repo:prompt-bench"], config)
        assert result is not None
        name, data = result
        assert name == "bug-fix"
        assert "labels" in data

    def test_match_feature(self, config):
        """Labels containing 'feature' match the feature persona."""
        result = _match_persona(["feature"], config)
        assert result is not None
        name, data = result
        assert name == "feature"

    def test_match_refactor(self, config):
        """Labels containing 'refactor' match the refactor persona."""
        result = _match_persona(["refactor"], config)
        assert result is not None
        name, _ = result
        assert name == "refactor"

    def test_match_security(self, config):
        """Labels containing 'security' match the security-fix persona."""
        result = _match_persona(["security"], config)
        assert result is not None
        name, _ = result
        assert name == "security-fix"

    def test_match_docs(self, config):
        """Labels containing 'docs' match the docs persona."""
        result = _match_persona(["docs"], config)
        assert result is not None
        name, _ = result
        assert name == "docs"

    def test_no_matching_labels_returns_none(self, config):
        """Labels with no persona match return None (default fallback)."""
        result = _match_persona(["urgent", "repo:prompt-bench"], config)
        assert result is None

    def test_empty_labels_returns_none(self, config):
        """Empty labels list returns None."""
        result = _match_persona([], config)
        assert result is None

    def test_case_insensitive_matching(self, config):
        """Label matching is case-insensitive."""
        result = _match_persona(["BUG"], config)
        assert result is not None
        name, _ = result
        assert name == "bug-fix"

    def test_multiple_matching_labels_picks_best(self, config):
        """When multiple labels match, the persona with most overlap wins."""
        # 'bug' matches bug-fix, 'feature' matches feature
        # Each has 1 overlap — the first found with max overlap wins
        result = _match_persona(["bug", "feature"], config)
        assert result is not None
        name, _ = result
        # Both have overlap=1, so whichever is first in dict iteration wins
        assert name in ("bug-fix", "feature")


# ---------------------------------------------------------------------------
# select_persona max_turns_default tests (TB-4)
# ---------------------------------------------------------------------------


class TestSelectPersonaMaxTurns:
    """Tests that select_persona extracts max_turns_default from config."""

    def test_bug_fix_returns_10_turns(self):
        """Bug-fix persona returns max_turns_default=10 from agents.yaml."""
        result = select_persona(["bug"])
        assert result["max_turns_default"] == 10

    def test_feature_returns_25_turns(self):
        """Feature persona returns max_turns_default=25 from agents.yaml."""
        result = select_persona(["feature"])
        assert result["max_turns_default"] == 25

    def test_docs_returns_10_turns(self):
        """Docs persona returns max_turns_default=10 from agents.yaml."""
        result = select_persona(["docs"])
        assert result["max_turns_default"] == 10

    def test_fallback_returns_default(self):
        """Unmatched labels fallback to feature persona's max_turns_default."""
        result = select_persona(["unknown-label"])
        assert result["max_turns_default"] == 25  # feature default from yaml

    def test_security_fix_returns_15_turns(self):
        """Security-fix persona returns max_turns_default=15."""
        result = select_persona(["security"])
        assert result["max_turns_default"] == 15
