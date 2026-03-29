"""Pydantic models for the LLMOps layer."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ProgramArtifact(BaseModel):
    """A saved, optimized DSPy program artifact."""

    program_name: str
    version: str = Field(description="Timestamp-based version string (YYYYMMDD-HHMMSS).")
    artifact_path: str = Field(description="Absolute path to the saved JSON artifact.")
    created_at: str = Field(description="ISO 8601 creation timestamp.")
    metric_score: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Best metric score achieved during optimization.",
    )
    num_training_examples: int = Field(
        default=0,
        ge=0,
        description="Number of training examples used.",
    )
    num_val_examples: int = Field(
        default=0,
        ge=0,
        description="Number of validation examples used.",
    )


class OptimizationRun(BaseModel):
    """Result of a GEPA optimization run."""

    run_id: str
    program_name: str
    started_at: str = Field(description="ISO 8601 start timestamp.")
    completed_at: str | None = Field(
        default=None,
        description="ISO 8601 completion timestamp (None if still running or failed).",
    )
    status: str = Field(
        default="pending",
        description="One of: pending, running, completed, failed.",
    )
    metric_before: float | None = Field(
        default=None,
        description="Metric score of the unoptimized program on val set.",
    )
    metric_after: float | None = Field(
        default=None,
        description="Metric score of the optimized program on val set.",
    )
    num_training_examples: int = 0
    num_val_examples: int = 0
    num_trials: int = 0
    error: str | None = None
    artifact: ProgramArtifact | None = None


class OptimizationConfig(BaseModel):
    """Per-program optimization hyperparameters from config/llmops.yaml."""

    model: str = "claude-opus-4-6"
    max_bootstrapped_demos: int = 4
    max_labeled_demos: int = 8
    num_trials: int = 20
    metric_threshold: float = 0.7


class LLMOpsConfig(BaseModel):
    """Top-level LLMOps configuration."""

    enabled: bool = False
    provider: str = Field(
        default="anthropic",
        description='LLM provider: "anthropic" (direct API) or "openrouter".',
    )
    api_key_env: str = "ANTHROPIC_API_KEY"
    artifact_dir: str = "~/.local/share/dev-loop/llmops/artifacts"
    training_dir: str = "~/.local/share/dev-loop/llmops/training"
    programs: dict[str, OptimizationConfig] = Field(default_factory=dict)


class TrainingExample(BaseModel):
    """A single training example for any DSPy program."""

    inputs: dict = Field(description="Input fields matching the DSPy Signature.")
    outputs: dict = Field(description="Expected output fields.")
    metadata: dict = Field(
        default_factory=dict,
        description="Source session ID, timestamp, etc.",
    )
