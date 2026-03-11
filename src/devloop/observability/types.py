"""Pydantic models for the observability layer MCP server."""

from __future__ import annotations

from pydantic import BaseModel, Field


class TraceInfo(BaseModel):
    """Summary of a single trace from OpenObserve."""

    trace_id: str
    service_name: str = ""
    operation_name: str = ""
    duration_ms: float | None = None
    start_time: str | None = None
    status: str = "unknown"
    span_count: int = 0


class HealthStatus(BaseModel):
    """Health check result for OpenObserve."""

    healthy: bool
    message: str
    url: str = ""
    status_code: int | None = None


class TraceUrlResult(BaseModel):
    """Result of resolving a trace URL."""

    trace_id: str
    url: str
    message: str = ""


class RecentTracesResult(BaseModel):
    """Result of querying recent traces."""

    traces: list[TraceInfo] = Field(default_factory=list)
    total: int = 0
    success: bool
    message: str = ""
