"""DSPy programs for dev-loop prompt optimization."""

from __future__ import annotations

from pathlib import Path

import dspy


def load_program(program_name: str) -> dspy.Module:
    """Load a DSPy program module, with optimized artifact if available.

    Returns the module ready to call. If an artifact exists at
    ``{artifact_dir}/{program_name}_latest.json`` (artifact_dir from
    config/llmops.yaml), it is loaded into the module.
    """
    if program_name == "code_review":
        from devloop.llmops.programs.code_review import CodeReviewModule

        module = CodeReviewModule()
    elif program_name == "retry_prompt":
        from devloop.llmops.programs.retry_prompt import RetryPromptModule

        module = RetryPromptModule()
    elif program_name == "persona_select":
        from devloop.llmops.programs.persona_select import PersonaSelectModule

        module = PersonaSelectModule()
    else:
        raise ValueError(f"Unknown program: {program_name}")

    from devloop.llmops.server import _load_llmops_config

    cfg = _load_llmops_config()
    artifact = Path(cfg.artifact_dir).expanduser() / f"{program_name}_latest.json"
    if artifact.exists():
        module.load(str(artifact))

    return module
