"""GEPA (MIPROv2) optimization entrypoint for dev-loop DSPy programs.

Usage:
    python -m devloop.llmops.optimize code_review
    python -m devloop.llmops.optimize retry_prompt
    python -m devloop.llmops.optimize persona_select
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import dspy

from devloop.llmops.server import _load_llmops_config
from devloop.llmops.types import OptimizationConfig, OptimizationRun, ProgramArtifact


def _load_training_data(program_name: str, training_dir: Path, max_examples: int) -> list:
    """Load JSONL training data as dspy.Example list."""
    data_path = training_dir / f"{program_name}.jsonl"
    if not data_path.exists():
        return []

    examples = []
    with open(data_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            inputs = row.get("inputs", row)
            outputs = row.get("outputs", {})
            merged = {**inputs, **outputs}
            ex = dspy.Example(**merged)
            # Mark input fields based on program
            if program_name == "code_review":
                ex = ex.with_inputs("diff", "issue_context", "review_criteria")
            elif program_name == "retry_prompt":
                ex = ex.with_inputs("failure_log", "original_task", "gate_results")
            elif program_name == "persona_select":
                ex = ex.with_inputs("issue_labels", "issue_description", "repo_type")
            examples.append(ex)
            if len(examples) >= max_examples:
                break

    return examples


def _balance_code_review(examples: list) -> list:
    """Oversample clean-diff examples to reduce class imbalance.

    Code review training data typically has ~80% with-findings and ~20% clean.
    Oversamples clean diffs 3x so they represent ~40% of the dataset, reducing
    the chance the optimizer learns to always produce findings.
    """
    with_findings = []
    clean = []
    for ex in examples:
        try:
            findings = json.loads(ex.findings_json)
            if isinstance(findings, list) and len(findings) == 0:
                clean.append(ex)
            else:
                with_findings.append(ex)
        except (json.JSONDecodeError, AttributeError):
            with_findings.append(ex)

    if not clean or len(clean) / len(examples) > 0.35:
        return examples  # already balanced enough

    # Oversample clean diffs 2x
    balanced = with_findings + clean * 2
    print(f"  Balance: {len(with_findings)} findings + {len(clean)}x2 clean = {len(balanced)}")
    return balanced


def _get_metric(program_name: str):
    """Load the metric function for a program."""
    if program_name == "code_review":
        from devloop.llmops.programs.code_review import code_review_metric

        return code_review_metric
    elif program_name == "retry_prompt":
        from devloop.llmops.programs.retry_prompt import retry_prompt_metric

        return retry_prompt_metric
    elif program_name == "persona_select":
        from devloop.llmops.programs.persona_select import persona_select_metric

        return persona_select_metric
    else:
        raise ValueError(f"No metric for program: {program_name}")


def run_optimization(
    program_name: str,
    max_examples: int = 50,
) -> OptimizationRun:
    """Run GEPA (MIPROv2) optimization on a DSPy program.

    1. Load training data from JSONL
    2. Split into train/val (80/20)
    3. Configure DSPy LM
    4. Run MIPROv2 optimizer
    5. Save artifact
    6. Return run metadata
    """
    run_id = str(uuid.uuid4())[:8]
    started_at = datetime.now(UTC).isoformat()

    cfg = _load_llmops_config()
    pcfg = cfg.programs.get(program_name, OptimizationConfig())

    # Verify API key
    api_key = os.environ.get(cfg.api_key_env)
    if not api_key:
        return OptimizationRun(
            run_id=run_id,
            program_name=program_name,
            started_at=started_at,
            status="failed",
            error=f"Environment variable {cfg.api_key_env} not set. "
            f"DSPy requires direct API access (not Claude Code CLI auth).",
        )

    training_dir = Path(cfg.training_dir).expanduser()
    artifact_dir = Path(cfg.artifact_dir).expanduser()
    artifact_dir.mkdir(parents=True, exist_ok=True)

    # Load training data
    examples = _load_training_data(program_name, training_dir, max_examples)
    if len(examples) < 5:
        return OptimizationRun(
            run_id=run_id,
            program_name=program_name,
            started_at=started_at,
            status="failed",
            num_training_examples=len(examples),
            error=f"Need at least 5 training examples, found {len(examples)}. "
            f"Run 'just llmops-export' first.",
        )

    # Balance class distribution for code_review before splitting
    if program_name == "code_review":
        examples = _balance_code_review(examples)

    # Split train/val (80/20)
    split = int(len(examples) * 0.8)
    trainset = examples[:split]
    valset = examples[split:]
    if not valset:
        valset = trainset[-2:]
        trainset = trainset[:-2]

    # Configure DSPy — build LiteLLM model string from provider
    if cfg.provider == "openrouter":
        model_str = f"openrouter/anthropic/{pcfg.model}"
    else:
        model_str = f"anthropic/{pcfg.model}"
    task_lm = dspy.LM(model_str, max_tokens=2048)
    dspy.configure(lm=task_lm)

    # Load program and metric
    from devloop.llmops.programs import load_program

    module = load_program(program_name)
    metric = _get_metric(program_name)

    # Evaluate baseline (unoptimized)
    baseline_scores = []
    for ex in valset:
        try:
            pred = module(**{k: ex[k] for k in ex._input_keys})
            result = metric(ex, pred)
            baseline_scores.append(result.score)
        except Exception:
            baseline_scores.append(0.0)
    metric_before = sum(baseline_scores) / len(baseline_scores) if baseline_scores else 0.0

    # Run MIPROv2 (GEPA) optimization
    try:
        optimizer = dspy.MIPROv2(
            metric=metric,
            auto="medium",
            num_threads=4,
            max_bootstrapped_demos=pcfg.max_bootstrapped_demos,
            max_labeled_demos=pcfg.max_labeled_demos,
        )
        optimized = optimizer.compile(module, trainset=trainset, valset=valset)
    except Exception as exc:
        return OptimizationRun(
            run_id=run_id,
            program_name=program_name,
            started_at=started_at,
            completed_at=datetime.now(UTC).isoformat(),
            status="failed",
            metric_before=round(metric_before, 3),
            num_training_examples=len(trainset),
            num_val_examples=len(valset),
            num_trials=pcfg.num_trials,
            error=str(exc),
        )

    # Evaluate optimized
    opt_scores = []
    for ex in valset:
        try:
            pred = optimized(**{k: ex[k] for k in ex._input_keys})
            result = metric(ex, pred)
            opt_scores.append(result.score)
        except Exception:
            opt_scores.append(0.0)
    metric_after = sum(opt_scores) / len(opt_scores) if opt_scores else 0.0

    # Save artifact
    version = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    artifact_path = artifact_dir / f"{program_name}_{version}.json"
    latest_path = artifact_dir / f"{program_name}_latest.json"

    optimized.save(str(artifact_path))
    optimized.save(str(latest_path))

    artifact = ProgramArtifact(
        program_name=program_name,
        version=version,
        artifact_path=str(artifact_path),
        created_at=datetime.now(UTC).isoformat(),
        metric_score=round(metric_after, 3),
        num_training_examples=len(trainset),
        num_val_examples=len(valset),
    )

    # Save metadata
    meta_path = artifact_dir / f"{program_name}_latest.meta.json"
    with open(meta_path, "w") as f:
        json.dump(artifact.model_dump(), f, indent=2)

    completed_at = datetime.now(UTC).isoformat()
    return OptimizationRun(
        run_id=run_id,
        program_name=program_name,
        started_at=started_at,
        completed_at=completed_at,
        status="completed",
        metric_before=round(metric_before, 3),
        metric_after=round(metric_after, 3),
        num_training_examples=len(trainset),
        num_val_examples=len(valset),
        num_trials=pcfg.num_trials,
        artifact=artifact,
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m devloop.llmops.optimize <program_name>")
        print("Programs: code_review, retry_prompt, persona_select")
        sys.exit(1)

    program = sys.argv[1]
    max_ex = int(sys.argv[2]) if len(sys.argv) > 2 else 50

    # Optional: init Langfuse bridge
    try:
        from devloop.llmops.langfuse_bridge import init_langfuse_bridge

        init_langfuse_bridge()
    except Exception:
        pass  # Langfuse is optional

    result = run_optimization(program, max_examples=max_ex)
    print(json.dumps(result.model_dump(), indent=2))
