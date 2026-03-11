"""Observability MCP server — trace lookup, health checks, recent-trace queries.

This is Layer 5 of the dev-loop harness. It exposes OpenObserve trace data
via MCP tools so that orchestration agents and humans can inspect what
happened in any run.

Run standalone:  uv run python -m devloop.observability.server
"""

from __future__ import annotations

import base64
import os

import httpx
from fastmcp import FastMCP
from opentelemetry import trace

from devloop.observability.types import (
    HealthStatus,
    RecentTracesResult,
    TraceInfo,
    TraceUrlResult,
)

# ---------------------------------------------------------------------------
# OTel tracer for observability layer
# ---------------------------------------------------------------------------

tracer = trace.get_tracer("observability", "0.1.0")

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _o2_url() -> str:
    return os.getenv("OPENOBSERVE_URL", "http://localhost:5080")


def _o2_org() -> str:
    return os.getenv("OPENOBSERVE_ORG", "default")


def _o2_auth_header() -> str:
    user = os.getenv("OPENOBSERVE_USER", "admin@dev-loop.local")
    password = os.getenv("OPENOBSERVE_PASSWORD", "devloop123")
    credentials = base64.b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {credentials}"


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="observability",
    instructions=(
        "Observability layer for dev-loop. "
        "Use these tools to look up traces in OpenObserve, "
        "query recent trace activity, and check OpenObserve health."
    ),
)

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool(
    description=(
        "Return the OpenObserve web UI URL for viewing a specific trace. "
        "Use this after a run completes to inspect the full span waterfall."
    ),
    tags={"observability", "traces"},
)
def get_trace_url(trace_id: str) -> dict:
    """Build the OpenObserve URL for a given trace ID."""
    with tracer.start_as_current_span(
        "observability.get_trace_url",
        attributes={
            "observability.operation": "get_trace_url",
            "trace.id": trace_id,
        },
    ) as span:
        if not trace_id or not trace_id.strip():
            error_msg = "trace_id must be a non-empty string"
            span.set_status(trace.StatusCode.ERROR, error_msg)
            return TraceUrlResult(
                trace_id=trace_id or "",
                url="",
                message=error_msg,
            ).model_dump()

        url = f"{_o2_url()}/web/traces?trace_id={trace_id.strip()}"

        span.set_attribute("trace.url", url)
        span.set_status(trace.StatusCode.OK)

        return TraceUrlResult(
            trace_id=trace_id.strip(),
            url=url,
            message="Open this URL to view the trace in OpenObserve",
        ).model_dump()


@mcp.tool(
    description=(
        "Query OpenObserve for recent traces. Returns up to `limit` traces "
        "ordered by start time descending. Useful for checking what the "
        "system has been doing recently."
    ),
    tags={"observability", "traces"},
)
def query_recent_traces(limit: int = 10) -> dict:
    """Query OpenObserve search API for recent traces."""
    with tracer.start_as_current_span(
        "observability.query_recent_traces",
        attributes={
            "observability.operation": "query_recent_traces",
            "query.limit": limit,
        },
    ) as span:
        search_url = f"{_o2_url()}/api/{_o2_org()}/_search"

        # OpenObserve SQL query against the default traces stream.
        # The stream name for OTLP traces is typically "default".
        query_payload = {
            "query": {
                "sql": (
                    "SELECT trace_id, service_name, operation_name, "
                    "duration, start_time, status_code, span_id "
                    "FROM default "
                    "ORDER BY start_time DESC "
                    f"LIMIT {max(1, min(limit, 100))}"
                ),
                "start_time": 0,
                "end_time": 0,
                "from": 0,
                "size": max(1, min(limit, 100)),
            },
        }

        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(
                    search_url,
                    json=query_payload,
                    headers={
                        "Authorization": _o2_auth_header(),
                        "Content-Type": "application/json",
                    },
                )
                resp.raise_for_status()
                data = resp.json()

        except httpx.HTTPStatusError as exc:
            error_msg = f"OpenObserve query failed: HTTP {exc.response.status_code}"
            span.set_status(trace.StatusCode.ERROR, error_msg)
            return RecentTracesResult(
                success=False,
                message=error_msg,
            ).model_dump()

        except httpx.ConnectError:
            error_msg = f"Cannot connect to OpenObserve at {_o2_url()}"
            span.set_status(trace.StatusCode.ERROR, error_msg)
            return RecentTracesResult(
                success=False,
                message=error_msg,
            ).model_dump()

        except Exception as exc:
            error_msg = f"OpenObserve query error: {exc}"
            span.set_status(trace.StatusCode.ERROR, error_msg)
            return RecentTracesResult(
                success=False,
                message=error_msg,
            ).model_dump()

        # Parse hits from the response
        hits = data.get("hits", [])
        traces: list[TraceInfo] = []

        for hit in hits:
            traces.append(
                TraceInfo(
                    trace_id=hit.get("trace_id", ""),
                    service_name=hit.get("service_name", ""),
                    operation_name=hit.get("operation_name", ""),
                    duration_ms=hit.get("duration"),
                    start_time=str(hit.get("start_time", "")),
                    status=str(hit.get("status_code", "unknown")),
                    span_count=1,  # grouping by trace_id is a future enhancement
                )
            )

        span.set_attribute("query.results_count", len(traces))
        span.set_status(trace.StatusCode.OK)

        return RecentTracesResult(
            traces=traces,
            total=len(traces),
            success=True,
            message=f"Found {len(traces)} recent trace spans",
        ).model_dump()


@mcp.tool(
    description=(
        "Check whether OpenObserve is running and reachable. "
        "Hits the /healthz endpoint and reports the status."
    ),
    tags={"observability", "health"},
)
def health_check() -> dict:
    """Hit OpenObserve /healthz and return the status."""
    with tracer.start_as_current_span(
        "observability.health_check",
        attributes={"observability.operation": "health_check"},
    ) as span:
        url = f"{_o2_url()}/healthz"

        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(url)

            healthy = resp.status_code == 200
            message = "OpenObserve is healthy" if healthy else f"Unhealthy: HTTP {resp.status_code}"

            span.set_attribute("health.status_code", resp.status_code)
            span.set_attribute("health.healthy", healthy)
            span.set_status(trace.StatusCode.OK if healthy else trace.StatusCode.ERROR, message)

            return HealthStatus(
                healthy=healthy,
                message=message,
                url=url,
                status_code=resp.status_code,
            ).model_dump()

        except httpx.ConnectError:
            error_msg = f"Cannot connect to OpenObserve at {url}"
            span.set_status(trace.StatusCode.ERROR, error_msg)
            return HealthStatus(
                healthy=False,
                message=error_msg,
                url=url,
            ).model_dump()

        except Exception as exc:
            error_msg = f"Health check error: {exc}"
            span.set_status(trace.StatusCode.ERROR, error_msg)
            return HealthStatus(
                healthy=False,
                message=error_msg,
                url=url,
            ).model_dump()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
