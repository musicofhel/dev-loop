"""DSPy program: Code Review — optimizes Gate 4 LLM review prompts."""

from __future__ import annotations

import json

import dspy


class CodeReview(dspy.Signature):
    """Review a code diff for quality issues given issue context and review criteria.

    Return findings as a JSON array. Each finding has: severity (critical/warning/suggestion),
    message, file (optional), line (optional), rule (optional).
    """

    diff: str = dspy.InputField(desc="Git diff to review")
    issue_context: str = dspy.InputField(desc="Issue title and description")
    review_criteria: str = dspy.InputField(
        desc="Comma-separated criteria: race_conditions, memory_leaks, logic_errors, etc."
    )
    findings_json: str = dspy.OutputField(
        desc='JSON array of {"severity","message","file","line","rule"} objects'
    )


class CodeReviewModule(dspy.Module):
    """Chain-of-thought code reviewer that produces structured findings."""

    def __init__(self):
        super().__init__()
        self.reviewer = dspy.ChainOfThought(CodeReview)

    def forward(self, diff: str, issue_context: str, review_criteria: str) -> dspy.Prediction:
        return self.reviewer(
            diff=diff,
            issue_context=issue_context,
            review_criteria=review_criteria,
        )


def _normalize_msg(text: str) -> str:
    """Lowercase, strip backticks/quotes, collapse whitespace."""
    import re

    text = text.lower().replace("`", "").replace("'", "").replace('"', "")
    return re.sub(r"\s+", " ", text).strip()


def _word_overlap(a: str, b: str) -> float:
    """Jaccard similarity between word sets of two normalized strings."""
    a_words = set(_normalize_msg(a).split())
    b_words = set(_normalize_msg(b).split())
    if not a_words or not b_words:
        return 0.0
    return len(a_words & b_words) / len(a_words | b_words)


_MATCH_THRESHOLD = 0.25


def code_review_metric(gold, pred, trace=None) -> dspy.Prediction:
    """GEPA-compatible metric for code review quality.

    Compares predicted findings against gold-standard findings using F1
    with word-overlap matching (Jaccard >= 0.25). Returns score (0-1)
    and textual feedback explaining failures.
    """
    feedback_parts: list[str] = []
    score = 0.0

    # Parse predicted findings
    try:
        pred_findings = json.loads(pred.findings_json)
        if not isinstance(pred_findings, list):
            return dspy.Prediction(
                score=0.0, feedback="findings_json is not a JSON array."
            )
    except (json.JSONDecodeError, AttributeError, TypeError):
        return dspy.Prediction(
            score=0.0, feedback="findings_json is not valid JSON."
        )

    # Parse gold findings
    try:
        gold_findings = json.loads(gold.findings_json)
        if not isinstance(gold_findings, list):
            gold_findings = []
    except (json.JSONDecodeError, AttributeError, TypeError):
        gold_findings = []

    if not gold_findings and not pred_findings:
        return dspy.Prediction(score=1.0, feedback="Both empty — correct no-finding result.")

    if not gold_findings and pred_findings:
        feedback_parts.append(
            f"Reported {len(pred_findings)} findings but none expected (false positives)."
        )
        return dspy.Prediction(score=0.2, feedback=" | ".join(feedback_parts))

    if gold_findings and not pred_findings:
        feedback_parts.append(
            f"Missed all {len(gold_findings)} expected findings (false negatives)."
        )
        return dspy.Prediction(score=0.0, feedback=" | ".join(feedback_parts))

    # Match findings by word overlap (Jaccard similarity)
    gold_msgs = [f.get("message", "") for f in gold_findings]
    pred_msgs = [f.get("message", "") for f in pred_findings]
    gold_sevs = [f.get("severity", "") for f in gold_findings]
    pred_sevs = [f.get("severity", "") for f in pred_findings]

    matched_gold: set[int] = set()
    tp = 0
    severity_matches = 0
    severity_total = 0

    for pi, pm in enumerate(pred_msgs):
        best_score = 0.0
        best_gi = -1
        for gi, gm in enumerate(gold_msgs):
            if gi in matched_gold:
                continue
            sim = _word_overlap(pm, gm)
            if sim > best_score:
                best_score = sim
                best_gi = gi
        if best_score >= _MATCH_THRESHOLD and best_gi >= 0:
            matched_gold.add(best_gi)
            tp += 1
            severity_total += 1
            if pred_sevs[pi] == gold_sevs[best_gi]:
                severity_matches += 1
            else:
                feedback_parts.append(
                    f"Severity mismatch: predicted '{pred_sevs[pi]}' but expected "
                    f"'{gold_sevs[best_gi]}' for '{pm[:60]}...'."
                )

    precision = tp / len(pred_findings) if pred_findings else 0.0
    recall = tp / len(gold_findings) if gold_findings else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    score = f1

    if severity_total > 0:
        severity_acc = severity_matches / severity_total
        score = 0.7 * f1 + 0.3 * severity_acc

    # Feedback for missed findings
    missed = len(gold_findings) - tp
    if missed > 0:
        feedback_parts.append(f"Missed {missed} of {len(gold_findings)} expected findings.")

    # Feedback for false positives
    fps = len(pred_findings) - tp
    if fps > 0:
        feedback_parts.append(f"{fps} false positive(s) reported.")

    feedback = " | ".join(feedback_parts) if feedback_parts else "Good review."
    return dspy.Prediction(score=round(min(score, 1.0), 3), feedback=feedback)
