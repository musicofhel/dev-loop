#!/usr/bin/env python3
"""Suggest config tuning based on feedback data.

Reads all feedback YAML files from /tmp/dev-loop/feedback/,
analyzes false positives and false negatives, and suggests
.devloop.yaml changes.

Usage:
    python scripts/feedback/suggest_tuning.py             # human-readable suggestions
    python scripts/feedback/suggest_tuning.py --json      # JSON output
    python scripts/feedback/suggest_tuning.py --apply DIR # write suggested .devloop.yaml diff
"""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

import yaml

FEEDBACK_DIR = Path("/tmp/dev-loop/feedback")


def load_feedback() -> list[dict]:
    """Load all feedback YAML files."""
    feedbacks = []
    if not FEEDBACK_DIR.exists():
        return feedbacks
    for p in sorted(FEEDBACK_DIR.glob("*.yaml")):
        try:
            with open(p) as f:
                fb = yaml.safe_load(f)
                if fb and isinstance(fb, dict):
                    feedbacks.append(fb)
        except Exception:
            continue
    return feedbacks


def analyze(feedbacks: list[dict]) -> dict:
    """Analyze feedback for tuning suggestions."""
    suggestions = {"remove_patterns": [], "allow_patterns": [], "extra_patterns": [], "notes": []}

    # Group FPs by check type
    fps_by_check: dict[str, list[dict]] = defaultdict(list)
    fns_by_check: dict[str, list[dict]] = defaultdict(list)

    for fb in feedbacks:
        if fb.get("label") == "false-positive":
            fps_by_check[fb.get("check_type", "unknown")].append(fb)
        elif fb.get("label") == "missed":
            fns_by_check[fb.get("check_type", "unknown")].append(fb)

    # Analyze deny_list FPs — suggest remove_patterns or allow_patterns
    for fp in fps_by_check.get("deny_list", []):
        pattern = fp.get("pattern_matched") or _extract_pattern_from_reason(fp.get("reason", ""))
        if pattern:
            suggestions["remove_patterns"].append(
                {"section": "deny_list", "pattern": pattern, "reason": fp.get("notes", "false positive")}
            )

    # Analyze dangerous_ops FPs — suggest allow_patterns
    for fp in fps_by_check.get("dangerous_ops", []):
        pattern = fp.get("pattern_matched") or _extract_pattern_from_reason(fp.get("reason", ""))
        if pattern:
            suggestions["allow_patterns"].append(
                {"section": "dangerous_ops", "pattern": pattern, "reason": fp.get("notes", "false positive")}
            )

    # Analyze secrets FPs — suggest file_allowlist entries
    for fp in fps_by_check.get("secrets", []):
        reason = fp.get("reason", "")
        # Try to extract file path from reason
        file_path = _extract_file_from_reason(reason)
        if file_path:
            suggestions["allow_patterns"].append(
                {
                    "section": "secrets.file_allowlist",
                    "pattern": file_path,
                    "reason": fp.get("notes", "false positive"),
                }
            )

    # Analyze FNs — suggest extra_patterns
    for check_type, fns in fns_by_check.items():
        # Count common themes in FN notes
        note_counter = Counter()
        for fn in fns:
            notes = fn.get("notes", "")
            if notes:
                note_counter[notes] += 1

        for note, count in note_counter.most_common(5):
            suggestions["extra_patterns"].append(
                {"section": check_type, "description": note, "count": count}
            )

    # Deduplicate
    suggestions["remove_patterns"] = _dedup_suggestions(suggestions["remove_patterns"])
    suggestions["allow_patterns"] = _dedup_suggestions(suggestions["allow_patterns"])

    # Add summary notes
    total_fps = sum(len(fps) for fps in fps_by_check.values())
    total_fns = sum(len(fns) for fns in fns_by_check.values())
    if total_fps > 0:
        suggestions["notes"].append(f"{total_fps} false positive(s) found — review suggested removals/allows")
    if total_fns > 0:
        suggestions["notes"].append(f"{total_fns} missed detection(s) — consider adding patterns")
    if total_fps == 0 and total_fns == 0:
        suggestions["notes"].append("No false positives or missed detections — config looks good")

    return suggestions


def _extract_pattern_from_reason(reason: str) -> str | None:
    """Try to extract a pattern from a check reason string."""
    # Pattern: "Blocked: matches deny pattern '.env'"
    if "'" in reason:
        start = reason.index("'")
        end = reason.index("'", start + 1) if "'" in reason[start + 1 :] else -1
        if end > start:
            return reason[start + 1 : end]
    # Pattern: "matches pattern: <pattern>"
    if "pattern:" in reason.lower():
        return reason.split("pattern:")[-1].strip().strip("'\"")
    return None


def _extract_file_from_reason(reason: str) -> str | None:
    """Try to extract a file path from a reason string."""
    # Look for common file path patterns
    for token in reason.split():
        if "/" in token or token.startswith("."):
            cleaned = token.strip("'\"(),;:")
            if cleaned:
                return cleaned
    return None


def _dedup_suggestions(suggestions: list[dict]) -> list[dict]:
    """Deduplicate suggestions by pattern."""
    seen = set()
    result = []
    for s in suggestions:
        key = (s.get("section", ""), s.get("pattern", ""))
        if key not in seen:
            seen.add(key)
            result.append(s)
    return result


def generate_yaml_diff(suggestions: dict) -> str:
    """Generate a suggested .devloop.yaml snippet from suggestions."""
    config: dict = {}

    for s in suggestions.get("remove_patterns", []):
        section = s["section"]
        if section not in config:
            config[section] = {}
        if "remove_patterns" not in config[section]:
            config[section]["remove_patterns"] = []
        config[section]["remove_patterns"].append(s["pattern"])

    for s in suggestions.get("allow_patterns", []):
        section = s["section"]
        if "." in section:
            # Handle nested keys like "secrets.file_allowlist"
            parts = section.split(".")
            if parts[0] not in config:
                config[parts[0]] = {}
            if parts[1] not in config[parts[0]]:
                config[parts[0]][parts[1]] = []
            config[parts[0]][parts[1]].append(s["pattern"])
        else:
            if section not in config:
                config[section] = {}
            if "allow_patterns" not in config[section]:
                config[section]["allow_patterns"] = []
            config[section]["allow_patterns"].append(s["pattern"])

    if not config:
        return "# No config changes suggested"

    return yaml.dump(config, default_flow_style=False, sort_keys=False)


def print_suggestions(suggestions: dict):
    """Print human-readable tuning suggestions."""
    print("Config Tuning Suggestions")
    print("=" * 50)

    if suggestions["remove_patterns"]:
        print("\nSuggested deny_list.remove_patterns (reduce false positives):")
        for s in suggestions["remove_patterns"]:
            print(f"  - '{s['pattern']}' ({s['reason']})")

    if suggestions["allow_patterns"]:
        print("\nSuggested allow_patterns / file_allowlist (reduce false positives):")
        for s in suggestions["allow_patterns"]:
            print(f"  - [{s['section']}] '{s['pattern']}' ({s['reason']})")

    if suggestions["extra_patterns"]:
        print("\nMissed detections (consider adding patterns):")
        for s in suggestions["extra_patterns"]:
            print(f"  - [{s['section']}] {s['description']} ({s['count']}x)")

    for note in suggestions["notes"]:
        print(f"\n{note}")

    # Show suggested YAML diff
    yaml_diff = generate_yaml_diff(suggestions)
    if "No config changes" not in yaml_diff:
        print("\nSuggested .devloop.yaml additions:")
        print("---")
        print(yaml_diff)
        print("---")
        print("Review and apply manually to your repo's .devloop.yaml")


def main():
    parser = argparse.ArgumentParser(description="Suggest config tuning from feedback")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    feedbacks = load_feedback()
    if not feedbacks:
        if args.json:
            print(json.dumps({"suggestions": [], "notes": ["No feedback data"]}))
        else:
            print("No feedback data found at", FEEDBACK_DIR)
            print("Annotate events with: dl feedback <event-id> correct|false-positive|missed")
        return

    suggestions = analyze(feedbacks)

    if args.json:
        print(json.dumps(suggestions, indent=2))
    else:
        print_suggestions(suggestions)


if __name__ == "__main__":
    main()
