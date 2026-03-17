#!/usr/bin/env python3
"""Replay tool calls through the check engine and report verdicts.

Two modes:
1. Raw replay: read NDJSON from parse_sessions.py, run through dl check
2. YAML test cases: read declarative tests, assert verdicts

Usage:
    # Raw replay from session data
    uv run python scripts/replay/parse_sessions.py ~/.claude/projects/-home-musicofhel/*.jsonl | \
        uv run python scripts/replay/run_replay.py --raw

    # With summary
    uv run python scripts/replay/parse_sessions.py ~/.claude/projects/-home-musicofhel/*.jsonl | \
        uv run python scripts/replay/run_replay.py --raw --summarize

    # Parallel replay (4 workers)
    uv run python scripts/replay/parse_sessions.py ~/.claude/projects/-home-musicofhel/*.jsonl | \
        uv run python scripts/replay/run_replay.py --raw --workers 4

    # JSON summary output (for scoring pipeline)
    uv run python scripts/replay/parse_sessions.py ~/.claude/projects/-home-musicofhel/*.jsonl | \
        uv run python scripts/replay/run_replay.py --raw --json > replay_output.ndjson

    # YAML test cases
    uv run python scripts/replay/run_replay.py --yaml tests/replay/test_cases.yaml
"""

import argparse
import json
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml


def dl_check(tool_name: str, tool_input: dict, phase: str = "pre") -> dict:
    """Run dl check and return result."""
    check_input = json.dumps({
        "tool_name": tool_name,
        "tool_input": tool_input,
        "phase": phase,
    })
    try:
        result = subprocess.run(
            ["dl", "check", check_input],
            capture_output=True, text=True, timeout=5,
        )
        if result.stdout.strip():
            return json.loads(result.stdout)
        return {"action": "allow", "exit_code": result.returncode}
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return {"action": "error"}


def check_one(tc: dict) -> dict:
    """Check a single tool call and return the enriched record."""
    tool_name = tc.get("tool_name", "")
    tool_input = tc.get("tool_input", {})

    # Pre-check (deny list + dangerous ops)
    pre_result = dl_check(tool_name, tool_input, "pre")
    pre_action = pre_result.get("action", "allow")

    # Post-check for Write/Edit (secrets)
    post_action = "allow"
    post_result = {}
    if tool_name in ("Write", "Edit"):
        post_result = dl_check(tool_name, tool_input, "post")
        post_action = post_result.get("action", "allow")

    # Use the most restrictive verdict
    if pre_action == "block":
        verdict = "block"
        check_type = pre_result.get("check_type", "unknown")
        reason = pre_result.get("reason", "")
    elif post_action == "warn":
        verdict = "warn"
        check_type = "secrets"
        reason = post_result.get("reason", "")
    elif pre_action == "warn":
        verdict = "warn"
        check_type = pre_result.get("check_type", "unknown")
        reason = pre_result.get("reason", "")
    else:
        verdict = "allow"
        check_type = pre_result.get("check_type", "none")
        reason = ""

    output = {**tc, "verdict": verdict, "check_type": check_type}
    if reason:
        output["reason"] = reason
    return output


def run_raw_replay(summarize: bool = False, json_output: bool = False, workers: int = 1):
    """Read NDJSON from stdin, run each through dl check, output results."""
    # Read all input first (needed for parallel mode)
    lines = []
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            lines.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    results = []
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(check_one, tc): i for i, tc in enumerate(lines)}
            indexed_results = [None] * len(lines)
            for future in as_completed(futures):
                idx = futures[future]
                indexed_results[idx] = future.result()
            results = indexed_results
    else:
        results = [check_one(tc) for tc in lines]

    # Aggregate stats
    verdicts = Counter()
    by_check_type = Counter()
    by_pattern = Counter()
    blocked_files = Counter()

    for output in results:
        verdict = output["verdict"]
        check_type = output["check_type"]
        verdicts[verdict] += 1
        by_check_type[check_type] += 1

        if verdict in ("block", "warn"):
            reason = output.get("reason", "")
            if reason:
                by_pattern[reason[:80]] += 1
            fp = output.get("file_path", output.get("command", ""))
            if fp and verdict == "block":
                blocked_files[str(fp)[:60]] += 1

        if not summarize and not json_output:
            print(json.dumps(output))

    total = len(results)

    if json_output:
        # Output NDJSON (one line per result) to stdout
        for output in results:
            print(json.dumps(output))

        # Also write summary as final line with _type marker
        summary = {
            "_type": "summary",
            "total": total,
            "allow": verdicts["allow"],
            "block": verdicts["block"],
            "warn": verdicts["warn"],
            "error": verdicts["error"],
            "by_check_type": dict(by_check_type),
            "top_patterns": [{"pattern": p, "count": c} for p, c in by_pattern.most_common(20)],
            "top_blocked_files": [{"file": f, "count": c} for f, c in blocked_files.most_common(20)],
        }
        print(json.dumps(summary))

    if summarize or (total > 0 and not json_output):
        _print_summary(total, verdicts, by_check_type, by_pattern, blocked_files)


def _print_summary(total, verdicts, by_check_type, by_pattern, blocked_files):
    """Print human-readable summary to stderr."""
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"Replay Summary", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    print(f"Total tool calls: {total}", file=sys.stderr)
    if total:
        print(f"  Allow: {verdicts['allow']} ({100*verdicts['allow']/total:.1f}%)", file=sys.stderr)
        print(f"  Block: {verdicts['block']} ({100*verdicts['block']/total:.1f}%)", file=sys.stderr)
        print(f"  Warn:  {verdicts['warn']} ({100*verdicts['warn']/total:.1f}%)", file=sys.stderr)
        if verdicts['error']:
            print(f"  Error: {verdicts['error']} ({100*verdicts['error']/total:.1f}%)", file=sys.stderr)
    print(file=sys.stderr)

    if by_check_type:
        print("By check type:", file=sys.stderr)
        for ct, count in by_check_type.most_common():
            print(f"  {ct}: {count}", file=sys.stderr)
        print(file=sys.stderr)

    if by_pattern:
        print("Top triggered patterns:", file=sys.stderr)
        for pat, count in by_pattern.most_common(10):
            print(f"  [{count}x] {pat}", file=sys.stderr)
        print(file=sys.stderr)

    if blocked_files:
        print("Most blocked files/commands:", file=sys.stderr)
        for fp, count in blocked_files.most_common(10):
            print(f"  [{count}x] {fp}", file=sys.stderr)
        if any(c > 3 for c in blocked_files.values()):
            print("  ^^^ Files blocked >3 times may be false positives", file=sys.stderr)
        print(file=sys.stderr)


def run_yaml_tests(yaml_path: str):
    """Run declarative YAML test cases."""
    with open(yaml_path) as f:
        data = yaml.safe_load(f)

    tests = data.get("tests", data) if isinstance(data, dict) else data
    passed = 0
    failed = 0

    for test in tests:
        name = test.get("name", "unnamed")
        inp = test["input"]
        expected = test["assert"]["verdict"]
        phase = "post" if expected == "warn" and test["assert"].get("check_type") == "secrets" else "pre"

        result = dl_check(inp["tool"], inp, phase)
        actual = result.get("action", "allow")

        if actual == expected:
            passed += 1
        else:
            failed += 1
            print(f"[FAIL] {name}: expected={expected}, got={actual}")

    total = passed + failed
    print(f"\n{passed}/{total} passed ({100*passed/total:.0f}%)" if total else "No tests")
    return failed == 0


def main():
    parser = argparse.ArgumentParser(description="Replay tool calls through check engine")
    parser.add_argument("--raw", action="store_true", help="Raw NDJSON replay from stdin")
    parser.add_argument("--yaml", type=str, help="YAML test case file")
    parser.add_argument("--summarize", action="store_true", help="Print summary to stderr")
    parser.add_argument("--json", action="store_true", help="JSON output (NDJSON + summary line)")
    parser.add_argument("--workers", type=int, default=1, help="Parallel workers (default: 1)")
    args = parser.parse_args()

    if args.yaml:
        ok = run_yaml_tests(args.yaml)
        sys.exit(0 if ok else 1)
    elif args.raw:
        run_raw_replay(summarize=args.summarize, json_output=args.json, workers=args.workers)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
