"""Central OpenTelemetry setup for dev-loop.

Every layer server calls ``trace.get_tracer("layer_name")`` but never
configures an exporter, so spans vanish into the void. This module wires
the OTel SDK to export spans to **OpenObserve** via the OTLP/HTTP protocol.

Usage (at process startup, before any spans are created)::

    from devloop.observability.tracing import init_tracing

    init_tracing()  # call once

    # Now all tracers across all layers will export to OpenObserve.

Or, to grab a tracer directly::

    from devloop.observability.tracing import get_tracer

    tracer = get_tracer("my-module")
    with tracer.start_as_current_span("my-operation"):
        ...

Environment variables (all optional, sensible defaults provided):

    OTEL_SERVICE_NAME          — override the service name   (default: dev-loop)
    OTEL_SERVICE_VERSION       — override the service version (default: 0.1.0)
    OTEL_DEPLOYMENT_ENV        — deployment.environment       (default: local)
    OPENOBSERVE_URL            — base URL for OpenObserve     (default: http://localhost:5080)
    OPENOBSERVE_ORG            — OpenObserve organization     (default: default)
    OPENOBSERVE_USER           — basic-auth username          (default: admin@dev-loop.local)
    OPENOBSERVE_PASSWORD       — basic-auth password          (default: devloop123)
"""

from __future__ import annotations

import base64
import os
import threading

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_provider: TracerProvider | None = None
_provider_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def init_tracing(
    service_name: str = "dev-loop",
    *,
    service_version: str | None = None,
    deployment_env: str | None = None,
    openobserve_url: str | None = None,
    openobserve_org: str | None = None,
    openobserve_user: str | None = None,
    openobserve_password: str | None = None,
) -> TracerProvider:
    """Initialise the OTel SDK and wire it to OpenObserve via OTLP/HTTP.

    Safe to call multiple times — subsequent calls return the already-
    configured provider without reinitialising.

    Args:
        service_name: ``service.name`` resource attribute.
        service_version: ``service.version`` resource attribute.
        deployment_env: ``deployment.environment`` resource attribute.
        openobserve_url: Base URL of the OpenObserve instance.
        openobserve_org: OpenObserve organization slug.
        openobserve_user: HTTP basic-auth username.
        openobserve_password: HTTP basic-auth password.

    Returns:
        The configured :class:`TracerProvider`.
    """
    global _provider

    # Double-checked locking — fast path without lock, lock only on init
    if _provider is not None:
        return _provider

    with _provider_lock:
        # Re-check after acquiring lock (another thread may have init'd)
        if _provider is not None:
            return _provider

        # ---- resolve config from args / env / defaults ----
        svc_name = service_name or os.getenv("OTEL_SERVICE_NAME", "dev-loop")
        svc_version = service_version or os.getenv("OTEL_SERVICE_VERSION", "0.1.0")
        env = deployment_env or os.getenv("OTEL_DEPLOYMENT_ENV", "local")

        o2_url = openobserve_url or os.getenv("OPENOBSERVE_URL", "http://localhost:5080")
        o2_org = openobserve_org or os.getenv("OPENOBSERVE_ORG", "default")
        o2_user = openobserve_user or os.getenv("OPENOBSERVE_USER", "admin@dev-loop.local")
        o2_pass = openobserve_password or os.getenv("OPENOBSERVE_PASSWORD", "devloop123")

        # ---- resource ----
        resource = Resource.create(
            {
                "service.name": svc_name,
                "service.version": svc_version,
                "deployment.environment": env,
            }
        )

        # ---- exporter ----
        # OpenObserve accepts OTLP traces at /api/{org}/v1/traces with HTTP
        # basic auth passed as a header.
        endpoint = f"{o2_url.rstrip('/')}/api/{o2_org}/v1/traces"

        credentials = base64.b64encode(f"{o2_user}:{o2_pass}".encode()).decode()
        auth_header = f"Basic {credentials}"

        exporter = OTLPSpanExporter(
            endpoint=endpoint,
            headers={"Authorization": auth_header},
        )

        # ---- provider + processor ----
        _provider = TracerProvider(resource=resource)
        _provider.add_span_processor(BatchSpanProcessor(exporter))

        # Register as the global provider so that every call to
        # ``trace.get_tracer(...)`` across the process uses it.
        trace.set_tracer_provider(_provider)

        return _provider


def get_tracer(name: str, version: str = "0.1.0") -> trace.Tracer:
    """Return a tracer from the configured provider.

    If :func:`init_tracing` has not been called yet, it is called with
    defaults so that spans always have somewhere to go.
    """
    if _provider is None:
        init_tracing()

    return trace.get_tracer(name, version)
