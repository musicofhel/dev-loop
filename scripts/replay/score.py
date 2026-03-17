#!/usr/bin/env python3
"""Score replay output — precision/recall for labeled data, frequency analysis for unlabeled.

Reads NDJSON from run_replay.py (each line has verdict, check_type, and optionally expected_verdict).

Usage:
    # Score unlabeled replay output (frequency analysis)
    cat replay_output.ndjson | uv run python scripts/replay/score.py

    # Score labeled data (precision/recall/F1)
    cat labeled_replay.ndjson | uv run python scripts/replay/score.py

    # Save JSON summary
    cat replay_output.ndjson | uv run python scripts/replay/score.py --json > score.json

    # Compare against baseline
    cat replay_output.ndjson | uv run python scripts/replay/score.py --baseline baselines/2026-03-baseline.json
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict


# File extensions that should never be allowed through without scrutiny
SENSITIVE_EXTENSIONS = {".env", ".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"}
# Patterns in filenames that suggest secrets
SENSITIVE_PATTERNS = [".env.", "credentials", "secret", "private_key", "id_rsa"]
# Known FP patterns (files that look sensitive but aren't)
KNOWN_FP_PATTERNS = [".env.example", ".env.template", ".env.sample", ".env.test"]


def is_sensitive_file(path: str) -> bool:
    """Check if a file path looks like it could contain secrets."""
    if not path:
        return False
    basename = os.path.basename(path).lower()
    # Check exact match (e.g. ".env" has no extension)
    if basename in SENSITIVE_EXTENSIONS:
        return True
    _, ext = os.path.splitext(basename)
    if ext in SENSITIVE_EXTENSIONS:
        return True
    return any(p in basename for p in SENSITIVE_PATTERNS)


def is_known_fp(path: str) -> bool:
    """Check if a file path is a known false positive pattern."""
    if not path:
        return False
    basename = os.path.basename(path).lower()
    return any(p in basename for p in KNOWN_FP_PATTERNS)


def compute_precision_recall(results: list[dict]) -> dict:
    """Compute precision/recall/F1 for labeled data.

    Labels: expected_verdict field on each record.
    A "positive" is a block or warn verdict.
    """
    per_check = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0, "tn": 0})
    overall = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}

    for r in results:
        expected = r.get("expected_verdict", "").lower()
        actual = r.get("verdict", "").lower()
        check_type = r.get("check_type", "unknown")

        expected_positive = expected in ("block", "warn")
        actual_positive = actual in ("block", "warn")

        if actual_positive and expected_positive:
            key = "tp"
        elif actual_positive and not expected_positive:
            key = "fp"
        elif not actual_positive and expected_positive:
            key = "fn"
        else:
            key = "tn"

        overall[key] += 1
        per_check[check_type][key] += 1

    def metrics(counts):
        tp, fp, fn = counts["tp"], counts["fp"], counts["fn"]
        precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 1.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
        return {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4), **counts}

    return {
        "overall": metrics(overall),
        "per_check": {ct: metrics(counts) for ct, counts in sorted(per_check.items())},
    }


def analyze_unlabeled(results: list[dict]) -> dict:
    """Frequency analysis for unlabeled replay data."""
    total = len(results)
    verdicts = Counter()
    by_check_type = Counter()
    by_tool = Counter()
    blocked_files = Counter()
    warned_files = Counter()
    reasons = Counter()
    allowed_sensitive = []
    likely_fps = []

    for r in results:
        verdict = r.get("verdict", "allow")
        check_type = r.get("check_type", "none")
        tool_name = r.get("tool_name", "")
        file_path = r.get("file_path", "")
        command = r.get("command", "")
        reason = r.get("reason", "")

        verdicts[verdict] += 1
        by_check_type[check_type] += 1
        by_tool[tool_name] += 1

        target = file_path or command

        if verdict == "block":
            blocked_files[target[:80]] += 1
            if reason:
                reasons[reason[:100]] += 1
            # Check for known FPs
            if is_known_fp(file_path):
                likely_fps.append({
                    "file": file_path,
                    "reason": reason,
                    "check_type": check_type,
                })
        elif verdict == "warn":
            warned_files[target[:80]] += 1
            if reason:
                reasons[reason[:100]] += 1
        elif verdict == "allow" and is_sensitive_file(file_path):
            # Potential false negative: sensitive-looking file that was allowed
            allowed_sensitive.append({
                "file": file_path,
                "tool": tool_name,
                "session": r.get("session_id", ""),
            })

    # Files blocked >5 times → likely FPs
    repeat_blocks = {f: c for f, c in blocked_files.items() if c > 5}

    return {
        "total": total,
        "verdicts": dict(verdicts),
        "by_check_type": dict(by_check_type),
        "by_tool": dict(by_tool),
        "top_blocked_files": [{"file": f, "count": c} for f, c in blocked_files.most_common(20)],
        "top_warned_files": [{"file": f, "count": c} for f, c in warned_files.most_common(20)],
        "top_reasons": [{"reason": r, "count": c} for r, c in reasons.most_common(20)],
        "likely_false_positives": likely_fps[:20],
        "repeat_blocked_files": repeat_blocks,
        "potential_false_negatives": allowed_sensitive[:20],
    }


def compare_baseline(current: dict, baseline: dict) -> dict:
    """Compare current results against a baseline for regressions."""
    regressions = []

    # Compare verdict distributions
    curr_verdicts = current.get("verdicts", {})
    base_verdicts = baseline.get("verdicts", {})

    curr_total = current.get("total", 0)
    base_total = baseline.get("total", 0)

    if base_total > 0 and curr_total > 0:
        curr_block_rate = curr_verdicts.get("block", 0) / curr_total
        base_block_rate = base_verdicts.get("block", 0) / base_total
        if curr_block_rate > base_block_rate * 1.5 and curr_block_rate - base_block_rate > 0.01:
            regressions.append({
                "type": "block_rate_increase",
                "baseline": round(base_block_rate, 4),
                "current": round(curr_block_rate, 4),
                "message": f"Block rate increased from {base_block_rate:.1%} to {curr_block_rate:.1%}",
            })

        curr_warn_rate = curr_verdicts.get("warn", 0) / curr_total
        base_warn_rate = base_verdicts.get("warn", 0) / base_total
        if curr_warn_rate > base_warn_rate * 1.5 and curr_warn_rate - base_warn_rate > 0.01:
            regressions.append({
                "type": "warn_rate_increase",
                "baseline": round(base_warn_rate, 4),
                "current": round(curr_warn_rate, 4),
                "message": f"Warn rate increased from {base_warn_rate:.1%} to {curr_warn_rate:.1%}",
            })

    # New repeat-blocked files (potential new FPs)
    curr_repeats = set(current.get("repeat_blocked_files", {}).keys())
    base_repeats = set(baseline.get("repeat_blocked_files", {}).keys())
    new_repeats = curr_repeats - base_repeats
    if new_repeats:
        regressions.append({
            "type": "new_repeat_blocks",
            "files": list(new_repeats),
            "message": f"{len(new_repeats)} new files blocked >5 times (potential new FPs)",
        })

    return {
        "regressions": regressions,
        "has_regressions": len(regressions) > 0,
    }


def print_report(analysis: dict, comparison: dict | None = None):
    """Print human-readable report to stderr."""
    total = analysis["total"]
    verdicts = analysis["verdicts"]

    print(f"\n{'='*60}", file=sys.stderr)
    print(f"Replay Scoring Report", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    print(f"Total tool calls analyzed: {total}", file=sys.stderr)

    if total:
        print(f"\nVerdict Distribution:", file=sys.stderr)
        for v in ["allow", "block", "warn", "error"]:
            count = verdicts.get(v, 0)
            if count:
                print(f"  {v:>5}: {count:>6} ({100*count/total:.1f}%)", file=sys.stderr)

    # Check type breakdown
    by_ct = analysis.get("by_check_type", {})
    if by_ct:
        print(f"\nBy Check Type:", file=sys.stderr)
        for ct, count in sorted(by_ct.items(), key=lambda x: -x[1]):
            print(f"  {ct}: {count}", file=sys.stderr)

    # Top blocked
    blocked = analysis.get("top_blocked_files", [])
    if blocked:
        print(f"\nTop Blocked Files:", file=sys.stderr)
        for entry in blocked[:10]:
            print(f"  [{entry['count']}x] {entry['file']}", file=sys.stderr)

    # Top reasons
    reasons = analysis.get("top_reasons", [])
    if reasons:
        print(f"\nTop Triggered Reasons:", file=sys.stderr)
        for entry in reasons[:10]:
            print(f"  [{entry['count']}x] {entry['reason']}", file=sys.stderr)

    # Likely FPs
    fps = analysis.get("likely_false_positives", [])
    repeats = analysis.get("repeat_blocked_files", {})
    if fps or repeats:
        print(f"\nLikely False Positives:", file=sys.stderr)
        for fp in fps:
            print(f"  {fp['file']} — {fp['reason']}", file=sys.stderr)
        for f, c in repeats.items():
            if not any(fp["file"] == f for fp in fps):
                print(f"  {f} (blocked {c} times)", file=sys.stderr)

    # Potential FNs
    fns = analysis.get("potential_false_negatives", [])
    if fns:
        print(f"\nPotential False Negatives (sensitive files allowed):", file=sys.stderr)
        for fn in fns[:10]:
            print(f"  {fn['file']} (tool={fn['tool']}, session={fn['session'][:8]}...)", file=sys.stderr)

    # Baseline comparison
    if comparison:
        print(f"\nBaseline Comparison:", file=sys.stderr)
        if comparison["has_regressions"]:
            print(f"  REGRESSIONS DETECTED:", file=sys.stderr)
            for reg in comparison["regressions"]:
                print(f"    - {reg['message']}", file=sys.stderr)
        else:
            print(f"  No regressions detected.", file=sys.stderr)

    print(file=sys.stderr)


def print_labeled_report(pr_data: dict):
    """Print precision/recall report for labeled data."""
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"Precision / Recall Report (Labeled Data)", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)

    overall = pr_data["overall"]
    print(f"\nOverall:", file=sys.stderr)
    print(f"  Precision: {overall['precision']:.4f}", file=sys.stderr)
    print(f"  Recall:    {overall['recall']:.4f}", file=sys.stderr)
    print(f"  F1:        {overall['f1']:.4f}", file=sys.stderr)
    print(f"  TP={overall['tp']}  FP={overall['fp']}  FN={overall['fn']}  TN={overall['tn']}", file=sys.stderr)

    per_check = pr_data.get("per_check", {})
    if per_check:
        print(f"\nPer Check Type:", file=sys.stderr)
        print(f"  {'Check':<20} {'Prec':>6} {'Rec':>6} {'F1':>6}  {'TP':>4} {'FP':>4} {'FN':>4}", file=sys.stderr)
        print(f"  {'-'*20} {'-'*6} {'-'*6} {'-'*6}  {'-'*4} {'-'*4} {'-'*4}", file=sys.stderr)
        for ct, m in per_check.items():
            print(f"  {ct:<20} {m['precision']:>6.4f} {m['recall']:>6.4f} {m['f1']:>6.4f}  {m['tp']:>4} {m['fp']:>4} {m['fn']:>4}", file=sys.stderr)

    print(file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Score replay output")
    parser.add_argument("--json", action="store_true", help="Output JSON summary to stdout")
    parser.add_argument("--baseline", type=str, help="Baseline JSON file for regression detection")
    args = parser.parse_args()

    # Read all results from stdin
    results = []
    summary_line = None
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        # Skip summary lines from run_replay.py --json output
        if record.get("_type") == "summary":
            summary_line = record
            continue

        results.append(record)

    if not results:
        print("No replay data to score.", file=sys.stderr)
        sys.exit(0)

    # Check if data is labeled
    has_labels = any("expected_verdict" in r for r in results)

    output = {}
    if has_labels:
        labeled = [r for r in results if "expected_verdict" in r]
        pr_data = compute_precision_recall(labeled)
        output["precision_recall"] = pr_data
        output["labeled_count"] = len(labeled)
        output["total_count"] = len(results)
        print_labeled_report(pr_data)

    # Always do frequency analysis
    analysis = analyze_unlabeled(results)
    output["analysis"] = analysis

    # Baseline comparison
    comparison = None
    if args.baseline:
        try:
            with open(args.baseline) as f:
                baseline = json.load(f)
            baseline_analysis = baseline.get("analysis", baseline)
            comparison = compare_baseline(analysis, baseline_analysis)
            output["comparison"] = comparison
        except (OSError, json.JSONDecodeError) as e:
            print(f"Warning: could not read baseline {args.baseline}: {e}", file=sys.stderr)

    print_report(analysis, comparison)

    if args.json:
        print(json.dumps(output, indent=2))

    # Exit non-zero if regressions found
    if comparison and comparison.get("has_regressions"):
        sys.exit(1)


if __name__ == "__main__":
    main()
