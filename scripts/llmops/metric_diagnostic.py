"""Metric diagnostic pass: analyze code_review_metric on all val examples.

Categorizes failures as: jaccard_miss, severity_mismatch, false_positive,
false_negative, parse_error. Prints per-example breakdown and summary.

Usage:
    uv run python scripts/llmops/metric_diagnostic.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import dspy

from devloop.llmops.programs.code_review import (
    _MATCH_THRESHOLD,
    _word_overlap,
    code_review_metric,
)


def load_val_examples(training_path: str, val_fraction: float = 0.2) -> list:
    """Load the validation split matching optimize.py's 80/20 split."""
    examples = []
    with open(training_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            inputs = row.get("inputs", row)
            outputs = row.get("outputs", {})
            merged = {**inputs, **outputs}
            ex = dspy.Example(**merged).with_inputs(
                "diff", "issue_context", "review_criteria"
            )
            examples.append(ex)

    split = int(len(examples) * (1 - val_fraction))
    return examples[split:]


def diagnose_example(gold, pred_findings_json: str) -> dict:
    """Run metric and categorize the failure mode."""
    pred = dspy.Example(findings_json=pred_findings_json)
    result = code_review_metric(gold, pred)

    # Parse findings
    try:
        gold_findings = json.loads(gold.findings_json)
        if not isinstance(gold_findings, list):
            gold_findings = []
    except (json.JSONDecodeError, TypeError):
        gold_findings = []

    try:
        pred_findings = json.loads(pred_findings_json)
        if not isinstance(pred_findings, list):
            pred_findings = []
    except (json.JSONDecodeError, TypeError):
        return {
            "score": result.score,
            "feedback": result.feedback,
            "category": "parse_error",
            "gold_count": len(gold_findings),
            "pred_count": 0,
        }

    # Categorize
    if not gold_findings and not pred_findings:
        category = "both_empty_correct"
    elif not gold_findings and pred_findings:
        category = "false_positive"
    elif gold_findings and not pred_findings:
        category = "false_negative"
    else:
        # Both have findings — check matching details
        gold_msgs = [f.get("message", "") for f in gold_findings]
        pred_msgs = [f.get("message", "") for f in pred_findings]

        matched = 0
        severity_mismatches = 0
        for pm in pred_msgs:
            best = max((_word_overlap(pm, gm) for gm in gold_msgs), default=0.0)
            if best >= _MATCH_THRESHOLD:
                matched += 1
                # Check severity
                best_gi = max(
                    range(len(gold_msgs)),
                    key=lambda gi: _word_overlap(pm, gold_msgs[gi]),
                )
                if pred_findings[pred_msgs.index(pm)].get("severity") != gold_findings[
                    best_gi
                ].get("severity"):
                    severity_mismatches += 1

        unmatched_pred = len(pred_findings) - matched
        unmatched_gold = len(gold_findings) - matched

        if unmatched_pred > 0 and unmatched_gold > 0:
            category = "jaccard_miss"  # findings exist but don't match
        elif severity_mismatches > 0 and matched > 0:
            category = "severity_mismatch"
        elif unmatched_pred > 0:
            category = "false_positive"
        elif unmatched_gold > 0:
            category = "jaccard_miss"
        else:
            category = "full_match" if result.score >= 0.9 else "severity_mismatch"

    return {
        "score": result.score,
        "feedback": result.feedback,
        "category": category,
        "gold_count": len(gold_findings),
        "pred_count": len(pred_findings),
    }


def main():
    training_path = Path("~/.local/share/dev-loop/llmops/training/code_review.jsonl").expanduser()
    if not training_path.exists():
        print(f"ERROR: {training_path} not found. Run 'just llmops-export' first.", file=sys.stderr)
        sys.exit(1)

    val_examples = load_val_examples(str(training_path))
    print(f"Loaded {len(val_examples)} validation examples\n")

    category_counts: Counter = Counter()
    score_by_category: dict[str, list[float]] = {}
    all_scores: list[float] = []

    print(f"{'#':>3} {'Score':>6} {'Gold':>5} {'Pred':>5} {'Category':<20} Feedback")
    print("-" * 90)

    for i, ex in enumerate(val_examples):
        # Use the gold findings as "predicted" to see self-consistency metric
        diag = diagnose_example(ex, ex.findings_json)
        score = diag["score"]
        cat = diag["category"]

        all_scores.append(score)
        category_counts[cat] += 1
        score_by_category.setdefault(cat, []).append(score)

        feedback_short = diag["feedback"][:60] if diag["feedback"] else ""
        print(
            f"{i+1:>3} {score:>6.3f} {diag['gold_count']:>5} {diag['pred_count']:>5} {cat:<20} {feedback_short}"
        )

    # Summary
    print("\n" + "=" * 90)
    print("SUMMARY")
    print("=" * 90)

    avg_score = sum(all_scores) / len(all_scores) if all_scores else 0
    print(f"\nOverall: {len(val_examples)} examples, avg score: {avg_score:.3f}")
    print(f"Match threshold: {_MATCH_THRESHOLD}")

    print(f"\n{'Category':<25} {'Count':>6} {'Avg Score':>10} {'% of Total':>10}")
    print("-" * 55)
    for cat in sorted(category_counts, key=lambda c: -category_counts[c]):
        cnt = category_counts[cat]
        avg = sum(score_by_category[cat]) / len(score_by_category[cat])
        pct = cnt / len(val_examples) * 100
        print(f"{cat:<25} {cnt:>6} {avg:>10.3f} {pct:>9.1f}%")

    # Score distribution
    print("\nScore distribution:")
    for bucket_lo in [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]:
        bucket_hi = bucket_lo + 0.2
        if bucket_lo == 1.0:
            count = sum(1 for s in all_scores if s == 1.0)
            print(f"  [1.000]        : {count:>3} examples {'#' * count}")
        else:
            count = sum(1 for s in all_scores if bucket_lo <= s < bucket_hi)
            print(f"  [{bucket_lo:.1f}, {bucket_hi:.1f})    : {count:>3} examples {'#' * count}")

    # Recommendations
    print("\nDiagnostic recommendations:")
    if category_counts.get("jaccard_miss", 0) > len(val_examples) * 0.2:
        print("  - HIGH Jaccard miss rate: consider lowering _MATCH_THRESHOLD or adding SequenceMatcher")
    if category_counts.get("severity_mismatch", 0) > len(val_examples) * 0.15:
        print("  - HIGH severity mismatch rate: severity accuracy weight (0.3) may be too high")
    if category_counts.get("false_positive", 0) > len(val_examples) * 0.1:
        print("  - Significant false positive rate: FP penalty (0.2) may need adjustment")
    if category_counts.get("false_negative", 0) > len(val_examples) * 0.1:
        print("  - Significant false negative rate: check training data quality")
    if avg_score >= 0.95:
        print("  - Self-consistency is high — metric formula itself is sound")
        print("  - The gap vs optimization is likely in how the LLM reproduces gold output")


if __name__ == "__main__":
    main()
