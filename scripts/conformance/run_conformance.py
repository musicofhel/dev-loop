#!/usr/bin/env python3
"""Hook conformance test runner.

Reads a YAML test corpus, runs each test case through `dl check`, and compares
the verdict to the expected result.

Usage:
    uv run python scripts/conformance/run_conformance.py tests/conformance/pre_tool_use.yaml
    uv run python scripts/conformance/run_conformance.py tests/conformance/post_tool_use.yaml --phase post
    uv run python scripts/conformance/run_conformance.py tests/conformance/*.yaml --json
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml


def run_dl_check(input_json: dict, phase: str = "pre") -> dict:
    """Run dl check and return parsed result."""
    # Add phase to input
    check_input = {**input_json, "phase": phase}
    json_str = json.dumps(check_input)

    try:
        result = subprocess.run(
            ["dl", "check", json_str],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        return {"action": "error", "reason": "timeout", "exit_code": -1}
    except FileNotFoundError:
        print("ERROR: dl binary not found in PATH", file=sys.stderr)
        sys.exit(1)

    # Parse stdout JSON
    try:
        output = json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError:
        output = {}

    output["exit_code"] = result.returncode
    return output


def verdict_from_result(result: dict) -> str:
    """Extract verdict string from dl check result."""
    action = result.get("action", "allow").lower()
    if action == "block":
        return "block"
    elif action == "warn":
        return "warn"
    else:
        return "allow"


def run_test(test: dict, phase: str) -> dict:
    """Run a single conformance test. Returns result dict."""
    name = test["name"]
    input_json = test["input"]
    expected = test["expect"]
    expected_check_type = test.get("check_type")
    expected_is_commit = test.get("is_commit")
    skip_reason = test.get("skip_reason")

    if skip_reason:
        return {
            "name": name,
            "status": "skip",
            "reason": skip_reason,
        }

    result = run_dl_check(input_json, phase)
    actual = verdict_from_result(result)
    actual_check_type = result.get("check_type")
    actual_is_commit = result.get("is_commit", False)

    passed = actual == expected

    # Also check check_type if specified
    type_mismatch = None
    if expected_check_type and actual_check_type and actual_check_type != expected_check_type:
        type_mismatch = f"expected check_type={expected_check_type}, got {actual_check_type}"
        passed = False

    # Check is_commit if specified
    commit_mismatch = None
    if expected_is_commit is not None and actual_is_commit != expected_is_commit:
        commit_mismatch = f"expected is_commit={expected_is_commit}, got {actual_is_commit}"
        passed = False

    out = {
        "name": name,
        "status": "pass" if passed else "fail",
        "expected": expected,
        "actual": actual,
    }

    if not passed:
        out["input"] = input_json
        if type_mismatch:
            out["type_mismatch"] = type_mismatch
        if commit_mismatch:
            out["commit_mismatch"] = commit_mismatch
        if result.get("reason"):
            out["reason"] = result["reason"]

    return out


def run_corpus(corpus_path: str, phase: str) -> list[dict]:
    """Run all tests in a corpus file."""
    with open(corpus_path) as f:
        tests = yaml.safe_load(f)

    results = []
    for test in tests:
        result = run_test(test, phase)
        results.append(result)

    return results


def print_report(results: list[dict], corpus_path: str):
    """Print a human-readable conformance report."""
    total = len(results)
    passed = sum(1 for r in results if r["status"] == "pass")
    failed = sum(1 for r in results if r["status"] == "fail")
    skipped = sum(1 for r in results if r["status"] == "skip")

    print(f"\nHook Conformance Report: {corpus_path}")
    print("=" * 60)
    print(f"Total:   {total} tests")
    print(f"Pass:    {passed} ({100*passed/total:.1f}%)" if total else "Pass:    0")
    print(f"Fail:    {failed} ({100*failed/total:.1f}%)" if total else "Fail:    0")
    if skipped:
        print(f"Skip:    {skipped}")
    print()

    # Group by category (prefix of name before colon)
    categories: dict[str, dict] = {}
    for r in results:
        cat = r["name"].split(":")[0].strip() if ":" in r["name"] else "other"
        if cat not in categories:
            categories[cat] = {"total": 0, "pass": 0, "fail": 0, "skip": 0}
        categories[cat]["total"] += 1
        categories[cat][r["status"]] += 1

    print("By Category:")
    for cat, counts in sorted(categories.items()):
        t, p = counts["total"], counts["pass"]
        status = "OK" if p == t else f"{counts['fail']} FAIL"
        s = f" ({counts['skip']} skip)" if counts["skip"] else ""
        print(f"  {cat:40s} {p}/{t:3d} ({100*p/t:.0f}%) {status}{s}")
    print()

    # Show failures
    failures = [r for r in results if r["status"] == "fail"]
    if failures:
        print("Failures:")
        for f in failures:
            print(f"  [FAIL] {f['name']}: expected={f['expected']}, got={f['actual']}")
            if f.get("type_mismatch"):
                print(f"         {f['type_mismatch']}")
            if f.get("commit_mismatch"):
                print(f"         {f['commit_mismatch']}")
            if f.get("reason"):
                reason = f["reason"][:100]
                print(f"         reason: {reason}")
            if f.get("input"):
                tool = f["input"].get("tool_name", "?")
                ti = f["input"].get("tool_input", {})
                fp = ti.get("file_path", ti.get("command", "?"))[:60]
                print(f"         input: {tool} → {fp}")
        print()

    # Show skips
    skips = [r for r in results if r["status"] == "skip"]
    if skips:
        print("Skipped:")
        for s in skips:
            print(f"  [SKIP] {s['name']}: {s['reason']}")
        print()

    return failed == 0


def main():
    parser = argparse.ArgumentParser(description="Hook conformance test runner")
    parser.add_argument("corpus", nargs="+", help="YAML test corpus file(s)")
    parser.add_argument("--phase", default="pre", choices=["pre", "post"],
                        help="Check phase (default: pre)")
    parser.add_argument("--json", action="store_true", help="Output JSON results")
    args = parser.parse_args()

    all_passed = True
    all_results = []

    for corpus_path in args.corpus:
        # Auto-detect phase from filename
        phase = args.phase
        if "post_tool_use" in corpus_path:
            phase = "post"

        results = run_corpus(corpus_path, phase)
        all_results.extend(results)

        if args.json:
            continue

        if not print_report(results, corpus_path):
            all_passed = False

    if args.json:
        json.dump(all_results, sys.stdout, indent=2)
        print()
        all_passed = all(r["status"] in ("pass", "skip") for r in all_results)

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
