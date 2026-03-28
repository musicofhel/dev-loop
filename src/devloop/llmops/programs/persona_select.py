"""DSPy program: Persona Selection — optimizes agent persona assignment."""

from __future__ import annotations

import dspy


class PersonaSelect(dspy.Signature):
    """Select the optimal agent persona for an issue.

    Choose from: bug-fix, feature, refactor, security-fix, docs, chore,
    performance, infrastructure, test. Also suggest custom guidelines
    tailored to the specific issue beyond the persona's defaults.
    """

    issue_labels: str = dspy.InputField(desc="Comma-separated issue labels")
    issue_description: str = dspy.InputField(desc="Issue title and description")
    repo_type: str = dspy.InputField(desc="Repository type: python, typescript, rust, etc.")
    persona_id: str = dspy.OutputField(
        desc="Selected persona name from agents.yaml"
    )
    custom_guidelines: str = dspy.OutputField(
        desc="Additional task-specific guidelines for the agent CLAUDE.md overlay"
    )


VALID_PERSONAS = {
    "bug-fix", "feature", "refactor", "security-fix", "docs",
    "chore", "performance", "infrastructure", "test",
}


class PersonaSelectModule(dspy.Module):
    """Chain-of-thought persona selector."""

    def __init__(self):
        super().__init__()
        self.selector = dspy.ChainOfThought(PersonaSelect)

    def forward(
        self, issue_labels: str, issue_description: str, repo_type: str
    ) -> dspy.Prediction:
        return self.selector(
            issue_labels=issue_labels,
            issue_description=issue_description,
            repo_type=repo_type,
        )


def persona_select_metric(gold, pred, trace=None) -> dspy.Prediction:
    """GEPA-compatible metric for persona selection quality.

    Primary: correct persona match. Secondary: task completion correlation.
    """
    feedback_parts: list[str] = []
    score = 0.0

    predicted_persona = (pred.persona_id or "").strip().lower()
    expected_persona = (getattr(gold, "persona_id", "") or "").strip().lower()

    # Validate persona is in the valid set
    if predicted_persona not in VALID_PERSONAS:
        feedback_parts.append(
            f"'{predicted_persona}' is not a valid persona. "
            f"Valid: {', '.join(sorted(VALID_PERSONAS))}."
        )
        return dspy.Prediction(score=0.0, feedback=" | ".join(feedback_parts))

    # Exact match
    if predicted_persona == expected_persona:
        score += 0.6
    else:
        feedback_parts.append(
            f"Predicted '{predicted_persona}' but expected '{expected_persona}'."
        )

    # Task outcome signal (if available)
    task_succeeded = getattr(gold, "task_succeeded", None)
    if task_succeeded is not None:
        if isinstance(task_succeeded, str):
            task_succeeded = task_succeeded.lower() in ("true", "1", "yes")
        if task_succeeded:
            score += 0.2
        else:
            feedback_parts.append("Task did not succeed with this persona assignment.")

    # Custom guidelines quality
    guidelines = pred.custom_guidelines or ""
    if guidelines and len(guidelines) > 20:
        score += 0.2
    elif not guidelines:
        feedback_parts.append("No custom guidelines provided.")

    feedback = " | ".join(feedback_parts) if feedback_parts else "Good persona selection."
    return dspy.Prediction(score=round(min(score, 1.0), 3), feedback=feedback)
