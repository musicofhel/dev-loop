"""Layer 7 MCP server: LLMOps — DSPy prompt optimization tools."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from fastmcp import FastMCP
from opentelemetry import trace

from devloop.llmops.types import LLMOpsConfig, OptimizationConfig, ProgramArtifact

mcp = FastMCP("llmops")
tracer = trace.get_tracer("llmops", "0.1.0")

VALID_PROGRAMS = {"code_review", "retry_prompt", "persona_select"}


def _load_llmops_config() -> LLMOpsConfig:
    """Load config/llmops.yaml and return typed config."""
    config_path = Path(__file__).resolve().parents[3] / "config" / "llmops.yaml"
    if not config_path.exists():
        return LLMOpsConfig()
    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}
    llmops = raw.get("llmops", {})
    programs = {}
    for name, pcfg in llmops.get("programs", {}).items():
        programs[name] = OptimizationConfig(**pcfg)
    return LLMOpsConfig(
        enabled=llmops.get("enabled", False),
        provider=llmops.get("provider", "anthropic"),
        api_key_env=llmops.get("api_key_env", "ANTHROPIC_API_KEY"),
        artifact_dir=llmops.get("artifact_dir", "~/.local/share/dev-loop/llmops/artifacts"),
        training_dir=llmops.get("training_dir", "~/.local/share/dev-loop/llmops/training"),
        programs=programs,
    )


def _artifact_dir() -> Path:
    """Resolved artifact directory, created on demand."""
    cfg = _load_llmops_config()
    d = Path(cfg.artifact_dir).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _latest_artifact(program_name: str) -> ProgramArtifact | None:
    """Find the latest artifact for a program, or None."""
    art_dir = _artifact_dir()
    latest = art_dir / f"{program_name}_latest.json"
    meta = art_dir / f"{program_name}_latest.meta.json"
    if not latest.exists() or not meta.exists():
        return None
    with open(meta) as f:
        return ProgramArtifact(**json.load(f))


@mcp.tool(
    description=(
        "Run GEPA (MIPROv2) optimization for a DSPy program. "
        "Programs: code_review, retry_prompt, persona_select."
    ),
    tags={"llmops", "optimize"},
)
def optimize_program(program_name: str, num_examples: int = 50) -> dict:
    """Trigger GEPA optimization for a named program."""
    with tracer.start_as_current_span(
        "llmops.optimize",
        attributes={"llmops.program": program_name, "llmops.num_examples": num_examples},
    ) as span:
        if program_name not in VALID_PROGRAMS:
            span.set_status(trace.StatusCode.ERROR, f"Unknown program: {program_name}")
            return {"error": f"Unknown program: {program_name}. Valid: {VALID_PROGRAMS}"}

        from devloop.llmops.optimize import run_optimization

        result = run_optimization(program_name, max_examples=num_examples)
        span.set_attribute("llmops.status", result.status)
        if result.metric_after is not None:
            span.set_attribute("llmops.metric_after", result.metric_after)
        return result.model_dump()


@mcp.tool(
    description="Get the optimization status and latest artifact for a DSPy program.",
    tags={"llmops", "status"},
)
def get_program_status(program_name: str) -> dict:
    """Return the latest artifact metadata for a program."""
    with tracer.start_as_current_span(
        "llmops.get_program_status",
        attributes={"llmops.program": program_name},
    ) as span:
        if program_name not in VALID_PROGRAMS:
            span.set_status(trace.StatusCode.ERROR, f"Unknown program: {program_name}")
            return {"error": f"Unknown program: {program_name}. Valid: {VALID_PROGRAMS}"}
        artifact = _latest_artifact(program_name)
        status = "optimized" if artifact else "not_optimized"
        span.set_attribute("llmops.status", status)
        if artifact is None:
            return {"program_name": program_name, "status": "not_optimized", "artifact": None}
        return {
            "program_name": program_name,
            "status": "optimized",
            "artifact": artifact.model_dump(),
        }


@mcp.tool(
    description=(
        "Run an optimized DSPy program with given inputs. "
        "Falls back to unoptimized if no artifact exists."
    ),
    tags={"llmops", "inference"},
)
def get_optimized_prompt(program_name: str, inputs: dict) -> dict:
    """Load and run the optimized DSPy program."""
    with tracer.start_as_current_span(
        "llmops.inference",
        attributes={"llmops.program": program_name},
    ) as span:
        if program_name not in VALID_PROGRAMS:
            span.set_status(trace.StatusCode.ERROR, f"Unknown program: {program_name}")
            return {"error": f"Unknown program: {program_name}"}

        from devloop.llmops.programs import load_program

        module = load_program(program_name)
        result = module(**inputs)
        span.set_attribute("llmops.artifact_loaded", _latest_artifact(program_name) is not None)
        return {k: getattr(result, k, None) for k in result.keys()}


@mcp.tool(
    description="List all DSPy programs and their optimization status.",
    tags={"llmops", "status"},
)
def list_programs() -> list[dict]:
    """Return status for all programs."""
    with tracer.start_as_current_span("llmops.list_programs") as span:
        results = []
        optimized_count = 0
        for name in sorted(VALID_PROGRAMS):
            artifact = _latest_artifact(name)
            if artifact:
                optimized_count += 1
            results.append({
                "program_name": name,
                "status": "optimized" if artifact else "not_optimized",
                "artifact": artifact.model_dump() if artifact else None,
            })
        span.set_attribute("llmops.program_count", len(results))
        span.set_attribute("llmops.optimized_count", optimized_count)
        return results
