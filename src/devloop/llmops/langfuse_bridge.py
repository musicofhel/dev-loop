"""OpenInference instrumentation bridge: DSPy → Langfuse.

Captures all DSPy LM calls as OpenInference spans and forwards
them to the self-hosted Langfuse instance via OTLP/HTTP.

Called once at optimization startup (not on every Gate 4 call).
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import yaml
from opentelemetry import trace

_initialized = False
tracer = trace.get_tracer("llmops.langfuse", "0.1.0")


def _load_langfuse_config() -> dict:
    """Load Langfuse config from config/llmops.yaml."""
    config_path = Path(__file__).resolve().parents[3] / "config" / "llmops.yaml"
    if not config_path.exists():
        return {}
    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}
    return raw.get("llmops", {}).get("langfuse", {})


def init_langfuse_bridge() -> bool:
    """Initialize OpenInference instrumentation for DSPy → Langfuse.

    Returns True if successfully initialized, False if skipped/failed.
    Safe to call multiple times (idempotent).
    """
    global _initialized
    if _initialized:
        return True

    with tracer.start_as_current_span("llmops.langfuse.init") as span:
        lf_cfg = _load_langfuse_config()
        enabled = lf_cfg.get("enabled", False)
        span.set_attribute("llmops.langfuse.enabled", enabled)
        if not enabled:
            span.set_attribute("llmops.langfuse.result", "disabled")
            return False

        url = lf_cfg.get("url", "http://localhost:3001")
        span.set_attribute("llmops.langfuse.url", url)
        public_key = os.environ.get(lf_cfg.get("public_key_env", "LANGFUSE_PUBLIC_KEY"), "")
        secret_key = os.environ.get(lf_cfg.get("secret_key_env", "LANGFUSE_SECRET_KEY"), "")

        if not public_key or not secret_key:
            span.set_attribute("llmops.langfuse.result", "no_keys")
            span.set_status(trace.StatusCode.ERROR, "Missing Langfuse API keys")
            return False

        # Health check — don't set OTEL vars if Langfuse is unreachable
        try:
            import urllib.request

            health_url = f"{url.rstrip('/')}/api/public/health"
            req = urllib.request.Request(health_url, method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status != 200:
                    span.set_attribute("llmops.langfuse.health_check_passed", False)
                    span.set_attribute("llmops.langfuse.result", "health_check_failed")
                    span.set_status(trace.StatusCode.ERROR, f"Health check returned {resp.status}")
                    return False
            span.set_attribute("llmops.langfuse.health_check_passed", True)
        except Exception as exc:
            span.set_attribute("llmops.langfuse.health_check_passed", False)
            span.set_attribute("llmops.langfuse.result", "health_check_failed")
            span.set_status(trace.StatusCode.ERROR, f"Health check failed: {exc}")
            return False

        try:
            from openinference.instrumentation.dspy import DSPyInstrumentor

            credentials = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
            os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"{url.rstrip('/')}/api/public/otel"
            os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
            os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = f"Authorization=Basic {credentials}"

            DSPyInstrumentor().instrument()
            _initialized = True
            span.set_attribute("llmops.langfuse.instrumentor_loaded", True)
            span.set_attribute("llmops.langfuse.result", "initialized")
            return True
        except ImportError:
            span.set_attribute("llmops.langfuse.instrumentor_loaded", False)
            span.set_attribute("llmops.langfuse.result", "import_error")
            span.set_status(trace.StatusCode.ERROR, "openinference.instrumentation.dspy not installed")
            return False
