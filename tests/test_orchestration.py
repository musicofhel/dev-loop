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
        expected = {"bug-fix", "feature", "refactor", "security-fix", "docs",
                    "chore", "performance", "infrastructure", "test"}
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

    def test_chore_returns_haiku_model(self):
        """Chore persona returns model=haiku, max_turns_default=5."""
        result = select_persona(["chore"])
        assert result["model"] == "haiku"
        assert result["max_turns_default"] == 5

    def test_infrastructure_returns_sonnet(self):
        """Infrastructure persona returns model=sonnet."""
        result = select_persona(["infrastructure"])
        assert result["model"] == "sonnet"
        assert result["max_turns_default"] == 10

    def test_test_persona_returns_sonnet(self):
        """Test persona returns model=sonnet, max_turns_default=15."""
        result = select_persona(["test"])
        assert result["model"] == "sonnet"
        assert result["max_turns_default"] == 15

    def test_performance_persona_returns_sonnet(self):
        """Performance persona returns model=sonnet."""
        result = select_persona(["performance"])
        assert result["model"] == "sonnet"


# ---------------------------------------------------------------------------
# Model validation tests (E-5)
# ---------------------------------------------------------------------------


class TestModelValidation:
    """Tests for persona model validation — invalid models fall back to sonnet."""

    def test_invalid_model_falls_back_to_sonnet(self, tmp_path):
        """Persona with invalid model gets corrected to sonnet."""
        import yaml

        # Load real config and add a persona with invalid model
        config = _load_agents_config()
        config["personas"]["test-invalid"] = {
            "labels": ["test-invalid-label"],
            "claude_md_overlay": "Test",
            "cost_ceiling_default": 1.00,
            "retry_max": 1,
            "model": "gpt-4o",  # Invalid
            "max_turns_default": 10,
        }

        # Write modified config to temp file and patch
        temp_config = tmp_path / "agents.yaml"
        with open(temp_config, "w") as f:
            yaml.dump(config, f)

        from unittest.mock import patch
        with patch("devloop.orchestration.server.AGENTS_CONFIG", temp_config):
            result = select_persona(["test-invalid-label"])
            assert result["model"] == "sonnet"  # Fell back from gpt-4o


# ---------------------------------------------------------------------------
# build_claude_md_overlay tests (T-4)
# ---------------------------------------------------------------------------


class TestBuildClaudeMdOverlay:
    """Tests for build_claude_md_overlay() — overlay text generation."""

    def test_overlay_contains_persona_instructions(self):
        """Overlay text includes persona overlay from config."""
        from devloop.orchestration.server import build_claude_md_overlay

        result = build_claude_md_overlay(
            persona="bug-fix",
            issue_title="Fix login crash",
            issue_description="Login crashes on empty password",
        )
        overlay = result["overlay_text"]
        assert "Focus on minimal fix" in overlay

    def test_overlay_contains_issue_context(self):
        """Overlay text includes issue title and description."""
        from devloop.orchestration.server import build_claude_md_overlay

        result = build_claude_md_overlay(
            persona="feature",
            issue_title="Add search feature",
            issue_description="Implement full-text search",
        )
        overlay = result["overlay_text"]
        assert "Add search feature" in overlay
        assert "full-text search" in overlay

    def test_overlay_structure(self):
        """Overlay has expected sections: heading, issue, persona, rules."""
        from devloop.orchestration.server import build_claude_md_overlay

        result = build_claude_md_overlay(
            persona="refactor",
            issue_title="Refactor auth module",
            issue_description="Split auth into separate files",
        )
        overlay = result["overlay_text"]
        assert "# Dev-Loop Agent Instructions" in overlay
        assert "## Issue:" in overlay
        assert "## Rules" in overlay

    def test_overlay_includes_deny_rules(self):
        """Overlay includes deny list rules section."""
        from devloop.orchestration.server import build_claude_md_overlay

        result = build_claude_md_overlay(
            persona="bug-fix",
            issue_title="Test",
            issue_description="Test description",
        )
        # The overlay should reference denied file patterns
        overlay = result["overlay_text"]
        assert "NEVER" in overlay or "deny" in overlay.lower() or "Do not" in overlay

    def test_overlay_has_backpressure_rules_for_node(self, tmp_path):
        """Node project overlay includes tsc --noEmit backpressure rule."""
        from devloop.orchestration.server import build_claude_md_overlay

        repo = tmp_path / "node-project"
        repo.mkdir()
        (repo / "package.json").write_text("{}")

        result = build_claude_md_overlay(
            persona="feature",
            issue_title="Add search",
            issue_description="Implement search",
            repo_path=str(repo),
        )
        overlay = result["overlay_text"]
        assert "tsc --noEmit" in overlay
        assert "In-Process Feedback" in overlay

    def test_overlay_has_backpressure_rules_for_python(self, tmp_path):
        """Python project overlay includes pytest backpressure rule."""
        from devloop.orchestration.server import build_claude_md_overlay

        repo = tmp_path / "python-project"
        repo.mkdir()
        (repo / "pyproject.toml").write_text("[project]\nname='test'")

        result = build_claude_md_overlay(
            persona="bug-fix",
            issue_title="Fix crash",
            issue_description="Fix null crash",
            repo_path=str(repo),
        )
        overlay = result["overlay_text"]
        assert "pytest" in overlay
        assert "In-Process Feedback" in overlay

    def test_overlay_has_backpressure_rules_for_rust(self, tmp_path):
        """Rust project overlay includes cargo check backpressure rule."""
        from devloop.orchestration.server import build_claude_md_overlay

        repo = tmp_path / "rust-project"
        repo.mkdir()
        (repo / "Cargo.toml").write_text("[package]\nname='test'")

        result = build_claude_md_overlay(
            persona="feature",
            issue_title="Add feature",
            issue_description="New feature",
            repo_path=str(repo),
        )
        overlay = result["overlay_text"]
        assert "cargo check" in overlay

    def test_overlay_has_anti_hallucination_rules(self):
        """Overlay includes anti-hallucination 'read before call' rules."""
        from devloop.orchestration.server import build_claude_md_overlay

        result = build_claude_md_overlay(
            persona="feature",
            issue_title="Add feature",
            issue_description="New feature",
        )
        overlay = result["overlay_text"]
        assert "read a function" in overlay.lower() or "Code Verification" in overlay

    def test_overlay_has_lock_file_rules_for_node(self, tmp_path):
        """Node project overlay includes npm install lock file rule."""
        from devloop.orchestration.server import build_claude_md_overlay

        repo = tmp_path / "node-project"
        repo.mkdir()
        (repo / "package.json").write_text("{}")

        result = build_claude_md_overlay(
            persona="feature",
            issue_title="Add dep",
            issue_description="Add dependency",
            repo_path=str(repo),
        )
        overlay = result["overlay_text"]
        assert "npm install" in overlay
        assert "Lock File" in overlay

    def test_overlay_no_lock_rules_for_unknown_project(self, tmp_path):
        """Unknown project type → no lock file rules section."""
        from devloop.orchestration.server import build_claude_md_overlay

        repo = tmp_path / "unknown-project"
        repo.mkdir()

        result = build_claude_md_overlay(
            persona="feature",
            issue_title="Add feature",
            issue_description="New feature",
            repo_path=str(repo),
        )
        overlay = result["overlay_text"]
        assert "Lock File Rules" not in overlay

    def test_overlay_without_repo_path_still_has_backpressure(self):
        """Without repo_path, overlay still has generic backpressure rules."""
        from devloop.orchestration.server import build_claude_md_overlay

        result = build_claude_md_overlay(
            persona="feature",
            issue_title="Add feature",
            issue_description="New feature",
        )
        overlay = result["overlay_text"]
        assert "In-Process Feedback" in overlay
        assert "run the project's test command" in overlay
