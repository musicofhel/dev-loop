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

_initialized = False


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

    lf_cfg = _load_langfuse_config()
    if not lf_cfg.get("enabled", False):
        return False

    url = lf_cfg.get("url", "http://localhost:3001")
    public_key = os.environ.get(lf_cfg.get("public_key_env", "LANGFUSE_PUBLIC_KEY"), "")
    secret_key = os.environ.get(lf_cfg.get("secret_key_env", "LANGFUSE_SECRET_KEY"), "")

    if not public_key or not secret_key:
        return False

    # Health check — don't set OTEL vars if Langfuse is unreachable
    try:
        import urllib.request

        health_url = f"{url.rstrip('/')}/api/public/health"
        req = urllib.request.Request(health_url, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            if resp.status != 200:
                return False
    except Exception:
        return False

    try:
        from openinference.instrumentation.dspy import DSPyInstrumentor

        # Set OTEL env vars only after confirming the instrumentor is available
        credentials = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"{url.rstrip('/')}/api/public/otel"
        os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
        os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = f"Authorization=Basic {credentials}"

        DSPyInstrumentor().instrument()
        _initialized = True
        return True
    except ImportError:
        return False
