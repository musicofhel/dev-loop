"""Pydantic models for validating all YAML config files.

Usage::

    from devloop.config_schemas import validate_all
    validate_all()  # raises on first invalid config
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

VALID_MODELS = {"opus", "sonnet", "haiku"}


# ---------------------------------------------------------------------------
# agents.yaml
# ---------------------------------------------------------------------------


class PersonaConfig(BaseModel):
    labels: list[str]
    claude_md_overlay: str
    cost_ceiling_default: float = Field(ge=0)
    retry_max: int = Field(ge=0)
    model: str
    max_turns_default: int = Field(ge=1)
    max_context_pct: int = Field(ge=1, le=100)

    @field_validator("model")
    @classmethod
    def model_must_be_valid(cls, v: str) -> str:
        if v not in VALID_MODELS:
            msg = f"Invalid model '{v}', must be one of {sorted(VALID_MODELS)}"
            raise ValueError(msg)
        return v


class AgentsConfig(BaseModel):
    personas: dict[str, PersonaConfig]


# ---------------------------------------------------------------------------
# dependencies.yaml
# ---------------------------------------------------------------------------


class DependencyEntry(BaseModel):
    source: str
    target: str
    watches: list[str] = Field(default_factory=list)
    type: str = "unknown"


class DependenciesConfig(BaseModel):
    repo_paths: dict[str, str] = Field(default_factory=dict)
    dependencies: list[DependencyEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# llmops.yaml
# ---------------------------------------------------------------------------


class LangfuseConfig(BaseModel):
    enabled: bool = False
    url: str = ""
    public_key_env: str = ""
    secret_key_env: str = ""


class ProgramConfig(BaseModel):
    model: str
    max_bootstrapped_demos: int = Field(ge=0)
    max_labeled_demos: int = Field(ge=0)
    num_trials: int = Field(ge=1)
    metric_threshold: float = Field(ge=0, le=1)


class LLMOpsInner(BaseModel):
    enabled: bool = False
    provider: str = "anthropic"
    api_key_env: str = ""
    langfuse: LangfuseConfig = Field(default_factory=LangfuseConfig)
    artifact_dir: str = ""
    training_dir: str = ""
    programs: dict[str, ProgramConfig] = Field(default_factory=dict)


class LLMOpsConfig(BaseModel):
    llmops: LLMOpsInner


# ---------------------------------------------------------------------------
# review-gate.yaml
# ---------------------------------------------------------------------------


class ReviewSettings(BaseModel):
    model: str
    criteria: list[str]
    severity_levels: dict[str, Literal["fail", "pass"]]


class ReviewGateConfig(BaseModel):
    review: ReviewSettings


# ---------------------------------------------------------------------------
# scheduling.yaml
# ---------------------------------------------------------------------------


class BudgetThrottle(BaseModel):
    model_config = {"populate_by_name": True}

    eighty_percent: str = Field(alias="80_percent", default="")
    ninetyfive_percent: str = Field(alias="95_percent", default="")
    hundred_percent: str = Field(alias="100_percent", default="")


class WeeklyBudget(BaseModel):
    max_turns: int = Field(ge=1, default=1400)
    max_input_tokens: int = Field(ge=1, default=35_000_000)
    max_output_tokens: int = Field(ge=1, default=7_000_000)


class SchedulingConfig(BaseModel):
    max_concurrent_agents: int = Field(ge=1)
    priority_order: list[str]
    budget_throttle: BudgetThrottle = Field(default_factory=BudgetThrottle)
    weekly_budget: WeeklyBudget = Field(default_factory=WeeklyBudget)


# ---------------------------------------------------------------------------
# capabilities.yaml
# ---------------------------------------------------------------------------


class ProjectCapabilities(BaseModel):
    allowed_tools: list[str] = Field(default_factory=list)
    denied_paths: list[str] = Field(default_factory=list)
    bash_allowlist: list[str] = Field(default_factory=list)


class CapabilitiesConfig(BaseModel):
    """Top-level is a dict of project-name → ProjectCapabilities."""

    model_config = {"extra": "allow"}

    @classmethod
    def from_yaml(cls, data: dict) -> dict[str, ProjectCapabilities]:
        """Parse the capabilities YAML (project-name keys at root)."""
        result = {}
        for project_name, caps in data.items():
            result[project_name] = ProjectCapabilities(**caps)
        return result


# ---------------------------------------------------------------------------
# alerts/rules.yaml
# ---------------------------------------------------------------------------


class AlertCondition(BaseModel):
    sql: str
    threshold: int = Field(ge=0)
    operator: str


class AlertRule(BaseModel):
    name: str
    description: str
    stream: str
    condition: AlertCondition
    frequency_minutes: int = Field(ge=1)
    severity: str


class AlertsConfig(BaseModel):
    alerts: list[AlertRule]


# ---------------------------------------------------------------------------
# validate_all — entry point
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> dict:
    """Load a YAML file and return the parsed dict."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        msg = f"{path} did not parse to a dict"
        raise ValueError(msg)
    return raw


def validate_all(config_dir: Path | None = None) -> dict[str, str]:
    """Validate all config files. Returns {filename: 'ok'} on success.

    Raises ``ValueError`` or ``pydantic.ValidationError`` on first failure.
    """
    d = config_dir or CONFIG_DIR
    results: dict[str, str] = {}

    AgentsConfig(**_load_yaml(d / "agents.yaml"))
    results["agents.yaml"] = "ok"

    DependenciesConfig(**_load_yaml(d / "dependencies.yaml"))
    results["dependencies.yaml"] = "ok"

    LLMOpsConfig(**_load_yaml(d / "llmops.yaml"))
    results["llmops.yaml"] = "ok"

    ReviewGateConfig(**_load_yaml(d / "review-gate.yaml"))
    results["review-gate.yaml"] = "ok"

    SchedulingConfig(**_load_yaml(d / "scheduling.yaml"))
    results["scheduling.yaml"] = "ok"

    caps_data = _load_yaml(d / "capabilities.yaml")
    CapabilitiesConfig.from_yaml(caps_data)
    results["capabilities.yaml"] = "ok"

    AlertsConfig(**_load_yaml(d / "alerts" / "rules.yaml"))
    results["alerts/rules.yaml"] = "ok"

    return results
