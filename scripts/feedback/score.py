#!/usr/bin/env python3
"""Feedback aggregation and scoring — computes precision/recall/F1 from labeled events.

Reads all feedback YAML files from /tmp/dev-loop/feedback/, groups by check_type,
computes per-type and overall metrics. Optionally tracks over time via history.jsonl.

Usage:
    python scripts/feedback/score.py                # human-readable table
    python scripts/feedback/score.py --json          # JSON summary
    python scripts/feedback/score.py --history       # append to history.jsonl
    python scripts/feedback/score.py --baseline FILE # compare against baseline
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

FEEDBACK_DIR = Path("/tmp/dev-loop/feedback")
SEED_LABELS_DIR = Path(__file__).parent / "seed_labels"
HISTORY_FILE = Path(__file__).parent / "history.jsonl"


def _load_from_dir(directory: Path) -> list[dict]:
    """Load all feedback YAML files from a directory."""
    feedbacks = []
    if not directory.exists():
        return feedbacks
    for p in sorted(directory.glob("*.yaml")):
        try:
            with open(p) as f:
                fb = yaml.safe_load(f)
                if fb and isinstance(fb, dict):
                    feedbacks.append(fb)
        except Exception:
            continue
    return feedbacks


def load_feedback() -> list[dict]:
    """Load all feedback YAML files from runtime and seed directories."""
    return _load_from_dir(SEED_LABELS_DIR) + _load_from_dir(FEEDBACK_DIR)


def compute_metrics(feedbacks: list[dict]) -> dict:
    """Compute per-check-type and overall precision/recall/F1."""
    by_check: dict[str, dict[str, int]] = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    total = {"tp": 0, "fp": 0, "fn": 0}

    for fb in feedbacks:
        check = fb.get("check_type", "unknown")
        label = fb.get("label", "")
        if label == "correct":
            by_check[check]["tp"] += 1
            total["tp"] += 1
        elif label == "false-positive":
            by_check[check]["fp"] += 1
            total["fp"] += 1
        elif label == "missed":
            by_check[check]["fn"] += 1
            total["fn"] += 1

    result = {"per_check": {}, "total": {}, "timestamp": datetime.now(timezone.utc).isoformat()}

    for check, counts in sorted(by_check.items()):
        result["per_check"][check] = _prf(counts)

    result["total"] = _prf(total)
    result["total"]["labeled_count"] = len(feedbacks)

    return result


def _prf(counts: dict) -> dict:
    """Compute precision, recall, F1 from TP/FP/FN counts."""
    tp, fp, fn = counts["tp"], counts["fp"], counts["fn"]
    precision = tp / (tp + fp) if tp + fp > 0 else 0.0
    recall = tp / (tp + fn) if tp + fn > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall > 0 else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def print_table(metrics: dict):
    """Print a human-readable metrics table."""
    print("Feedback Score Report")
    print("=" * 50)
    print()

    header = f"{'CHECK TYPE':<16} {'TP':>4} {'FP':>4} {'FN':>4} {'PREC':>8} {'RECALL':>8} {'F1':>8}"
    print(header)
    print("-" * len(header))

    for check, m in metrics["per_check"].items():
        print(
            f"{check:<16} {m['tp']:>4} {m['fp']:>4} {m['fn']:>4} "
            f"{m['precision']:>7.1%} {m['recall']:>7.1%} {m['f1']:>7.3f}"
        )

    print("-" * len(header))
    t = metrics["total"]
    print(
        f"{'TOTAL':<16} {t['tp']:>4} {t['fp']:>4} {t['fn']:>4} "
        f"{t['precision']:>7.1%} {t['recall']:>7.1%} {t['f1']:>7.3f}"
    )

    print(f"\nLabeled events: {t.get('labeled_count', 0)}")

    if t.get("labeled_count", 0) < 20:
        print(f"\nNote: {t.get('labeled_count', 0)} labels is too few for reliable metrics. Aim for 200+.")


def compare_baseline(current: dict, baseline_path: str):
    """Compare current metrics against a baseline file."""
    try:
        with open(baseline_path) as f:
            baseline = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Error reading baseline: {e}", file=sys.stderr)
        return

    print("\nBaseline Comparison")
    print("=" * 50)

    regressions = []
    for check in sorted(set(list(current["per_check"].keys()) + list(baseline.get("per_check", {}).keys()))):
        cur = current["per_check"].get(check, {"f1": 0.0})
        base = baseline.get("per_check", {}).get(check, {"f1": 0.0})
        delta = cur["f1"] - base["f1"]
        marker = "  " if abs(delta) < 0.05 else ("!!" if delta < -0.05 else "++")
        print(f"  {marker} {check:<16} F1: {base['f1']:.3f} -> {cur['f1']:.3f} ({delta:+.3f})")
        if delta < -0.05:
            regressions.append(check)

    # Overall
    cur_total = current["total"]
    base_total = baseline.get("total", {"f1": 0.0})
    delta = cur_total["f1"] - base_total["f1"]
    marker = "  " if abs(delta) < 0.05 else ("!!" if delta < -0.05 else "++")
    print(f"  {marker} {'TOTAL':<16} F1: {base_total['f1']:.3f} -> {cur_total['f1']:.3f} ({delta:+.3f})")

    if regressions:
        print(f"\nREGRESSIONS DETECTED in: {', '.join(regressions)}")
        sys.exit(1)
    else:
        print("\nNo regressions detected.")


def main():
    parser = argparse.ArgumentParser(description="Score feedback data")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--history", action="store_true", help="Append to history.jsonl")
    parser.add_argument("--baseline", help="Compare against baseline JSON file")
    args = parser.parse_args()

    feedbacks = load_feedback()
    if not feedbacks:
        if args.json:
            print(json.dumps({"error": "no feedback data", "labeled_count": 0}))
        else:
            print("No feedback data found at", FEEDBACK_DIR)
            print("Annotate events with: dl feedback <event-id> correct|false-positive|missed")
        return

    metrics = compute_metrics(feedbacks)

    if args.json:
        print(json.dumps(metrics, indent=2))
    else:
        print_table(metrics)

    if args.baseline:
        compare_baseline(metrics, args.baseline)

    if args.history:
        with open(HISTORY_FILE, "a") as f:
            f.write(json.dumps(metrics) + "\n")
        print(f"\nAppended to {HISTORY_FILE}")


if __name__ == "__main__":
    main()
