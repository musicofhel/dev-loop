"""Pydantic models for the quality gates layer."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Finding(BaseModel):
    """A single finding from a gate check."""

    severity: str = "info"  # critical, warning, suggestion, info
    message: str
    file: str | None = None
    line: int | None = None
    rule: str | None = None


class GateResult(BaseModel):
    """Result of running a single quality gate."""

    gate_name: str
    passed: bool
    findings: list[Finding] = Field(default_factory=list)
    duration_seconds: float = 0.0
    skipped: bool = False
    error: str | None = None


class GateSuiteResult(BaseModel):
    """Aggregate result of running all gates in sequence."""

    overall_passed: bool
    gate_results: list[GateResult] = Field(default_factory=list)
    first_failure: str | None = None
    total_duration_seconds: float = 0.0
