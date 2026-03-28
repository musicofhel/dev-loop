"""DSPy program: Retry Prompt — optimizes failure-to-retry prompt generation."""

from __future__ import annotations

import dspy


class RetryPrompt(dspy.Signature):
    """Generate a focused retry prompt from failure context that maximizes retry success.

    The retry prompt should tell the agent exactly what went wrong and the minimal
    changes needed to pass the gates. Do not repeat the full original task.
    """

    failure_log: str = dspy.InputField(desc="Structured gate failure details from the failed run")
    original_task: str = dspy.InputField(desc="Original issue title and description")
    gate_results: str = dspy.InputField(desc="JSON gate results from the failed attempt")
    retry_instructions: str = dspy.OutputField(
        desc="Concise retry prompt text for the agent to fix the specific failures"
    )


class RetryPromptModule(dspy.Module):
    """Chain-of-thought retry prompt generator."""

    def __init__(self):
        super().__init__()
        self.generator = dspy.ChainOfThought(RetryPrompt)

    def forward(
        self, failure_log: str, original_task: str, gate_results: str
    ) -> dspy.Prediction:
        return self.generator(
            failure_log=failure_log,
            original_task=original_task,
            gate_results=gate_results,
        )


def retry_prompt_metric(gold, pred, trace=None) -> dspy.Prediction:
    """GEPA-compatible metric for retry prompt quality.

    Binary: did the retry succeed? With heuristic feedback.
    """
    feedback_parts: list[str] = []

    # Primary signal: did the retry pass (from gold label)
    retry_succeeded = getattr(gold, "retry_succeeded", False)
    if isinstance(retry_succeeded, str):
        retry_succeeded = retry_succeeded.lower() in ("true", "1", "yes")

    instructions = pred.retry_instructions or ""

    if not instructions:
        return dspy.Prediction(score=0.0, feedback="Empty retry instructions.")

    score = 0.5 if retry_succeeded else 0.0

    # Heuristic quality checks
    if len(instructions) < 50:
        feedback_parts.append("Instructions too short to be actionable.")
        score = max(score - 0.1, 0.0)
    elif len(instructions) > 3000:
        feedback_parts.append("Instructions excessively long — agent may lose focus.")
        score = max(score - 0.1, 0.0)
    else:
        score += 0.1

    # Should reference specific gates/failures
    gate_keywords = ["gate", "test", "fail", "error", "fix", "pass"]
    if any(kw in instructions.lower() for kw in gate_keywords):
        score += 0.2
    else:
        feedback_parts.append("Instructions don't reference the specific gate failures.")

    # Should NOT repeat the full original task
    if len(instructions) > 200 and getattr(gold, "original_task", ""):
        original_words = set(gold.original_task.lower().split()[:20])
        instruction_words = set(instructions.lower().split()[:20])
        overlap = len(original_words & instruction_words) / max(len(original_words), 1)
        if overlap > 0.7:
            feedback_parts.append(
                "Instructions mostly repeat the original task instead of focusing on failures."
            )
            score = max(score - 0.2, 0.0)
        else:
            score += 0.1

    feedback = " | ".join(feedback_parts) if feedback_parts else "Good retry prompt."
    return dspy.Prediction(score=round(min(score, 1.0), 3), feedback=feedback)
