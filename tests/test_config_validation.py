"""Tests for config schema validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from devloop.config_schemas import (
    AgentsConfig,
    AlertsConfig,
    CapabilitiesConfig,
    DependenciesConfig,
    LLMOpsConfig,
    ReviewGateConfig,
    SchedulingConfig,
    validate_all,
)

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


# ---------------------------------------------------------------------------
# Positive: each real config file loads and validates
# ---------------------------------------------------------------------------


class TestRealConfigFiles:
    """Validate all real config files in config/."""

    def test_agents_yaml_valid(self):
        raw = yaml.safe_load((CONFIG_DIR / "agents.yaml").read_text())
        config = AgentsConfig(**raw)
        assert len(config.personas) == 9
        for name, persona in config.personas.items():
            assert persona.labels, f"{name} has no labels"
            assert persona.model in {"opus", "sonnet", "haiku"}

    def test_dependencies_yaml_valid(self):
        raw = yaml.safe_load((CONFIG_DIR / "dependencies.yaml").read_text())
        config = DependenciesConfig(**raw)
        assert len(config.repo_paths) >= 1
        # dependencies may be empty (TB-5 dormant — needs second repo)
        for dep in config.dependencies:
            assert dep.source
            assert dep.target

    def test_llmops_yaml_valid(self):
        raw = yaml.safe_load((CONFIG_DIR / "llmops.yaml").read_text())
        config = LLMOpsConfig(**raw)
        assert len(config.llmops.programs) >= 1
        for name, prog in config.llmops.programs.items():
            assert prog.num_trials >= 1
            assert 0 <= prog.metric_threshold <= 1

    def test_review_gate_yaml_valid(self):
        raw = yaml.safe_load((CONFIG_DIR / "review-gate.yaml").read_text())
        config = ReviewGateConfig(**raw)
        assert len(config.review.criteria) >= 1
        assert "critical" in config.review.severity_levels

    def test_scheduling_yaml_valid(self):
        raw = yaml.safe_load((CONFIG_DIR / "scheduling.yaml").read_text())
        config = SchedulingConfig(**raw)
        assert config.max_concurrent_agents >= 1
        assert len(config.priority_order) >= 1

    def test_capabilities_yaml_valid(self):
        raw = yaml.safe_load((CONFIG_DIR / "capabilities.yaml").read_text())
        caps = CapabilitiesConfig.from_yaml(raw)
        assert len(caps) >= 1
        for project_name, cap in caps.items():
            assert isinstance(cap.allowed_tools, list)

    def test_alerts_yaml_valid(self):
        raw = yaml.safe_load((CONFIG_DIR / "alerts" / "rules.yaml").read_text())
        config = AlertsConfig(**raw)
        assert len(config.alerts) == 7
        for alert in config.alerts:
            assert alert.name
            assert alert.condition.sql.strip()

    def test_validate_all_succeeds(self):
        results = validate_all()
        assert len(results) == 7
        assert all(v == "ok" for v in results.values())


# ---------------------------------------------------------------------------
# Negative: invalid data is rejected
# ---------------------------------------------------------------------------


class TestConfigRejection:
    """Invalid config data is properly rejected."""

    def test_invalid_persona_model_rejected(self):
        raw = {
            "personas": {
                "bad": {
                    "labels": ["test"],
                    "claude_md_overlay": "test",
                    "cost_ceiling_default": 1.0,
                    "retry_max": 1,
                    "model": "gpt-4",
                    "max_turns_default": 10,
                    "max_context_pct": 75,
                },
            },
        }
        with pytest.raises(Exception, match="Invalid model"):
            AgentsConfig(**raw)

    def test_missing_required_field_rejected(self):
        raw = {
            "personas": {
                "bad": {
                    # missing labels
                    "claude_md_overlay": "test",
                    "cost_ceiling_default": 1.0,
                    "retry_max": 1,
                    "model": "sonnet",
                    "max_turns_default": 10,
                    "max_context_pct": 75,
                },
            },
        }
        with pytest.raises(Exception):
            AgentsConfig(**raw)

    def test_negative_cost_ceiling_rejected(self):
        raw = {
            "personas": {
                "bad": {
                    "labels": ["test"],
                    "claude_md_overlay": "test",
                    "cost_ceiling_default": -1.0,
                    "retry_max": 1,
                    "model": "sonnet",
                    "max_turns_default": 10,
                    "max_context_pct": 75,
                },
            },
        }
        with pytest.raises(Exception):
            AgentsConfig(**raw)

    def test_empty_watches_accepted(self):
        """Dependencies with no watches are valid (matches all files)."""
        raw = {
            "repo_paths": {},
            "dependencies": [
                {"source": "a", "target": "b", "watches": [], "type": "all"},
            ],
        }
        config = DependenciesConfig(**raw)
        assert len(config.dependencies) == 1

    def test_invalid_severity_level_rejected(self):
        raw = {
            "review": {
                "model": "claude-sonnet-4-6",
                "criteria": ["race_conditions"],
                "severity_levels": {"critical": "maybe"},
            },
        }
        with pytest.raises(Exception):
            ReviewGateConfig(**raw)

    def test_zero_max_turns_rejected(self):
        raw = {
            "personas": {
                "bad": {
                    "labels": ["test"],
                    "claude_md_overlay": "test",
                    "cost_ceiling_default": 1.0,
                    "retry_max": 1,
                    "model": "sonnet",
                    "max_turns_default": 0,
                    "max_context_pct": 75,
                },
            },
        }
        with pytest.raises(Exception):
            AgentsConfig(**raw)

    def test_metric_threshold_above_one_rejected(self):
        from devloop.config_schemas import ProgramConfig

        with pytest.raises(Exception):
            ProgramConfig(
                model="claude-opus-4-6",
                max_bootstrapped_demos=4,
                max_labeled_demos=8,
                num_trials=10,
                metric_threshold=1.5,
            )
