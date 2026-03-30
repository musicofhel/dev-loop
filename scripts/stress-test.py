"""Stress test: run all 6 TBs N times, log every result, fix nothing.

Collects evidence of race conditions, edge cases, and intermittent failures.
Results logged to /tmp/dev-loop/stress-test/<timestamp>/

Usage:
    uv run python scripts/stress-test.py              # 30 iterations (default)
    uv run python scripts/stress-test.py --runs 10    # 10 iterations
    uv run python scripts/stress-test.py --skip tb3   # skip TB-3 (slow)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).parent.parent

# Add project src to path so we can import devloop.paths
import sys
sys.path.insert(0, str(REPO / "src"))
from devloop.paths import RESULTS_DIR, SESSIONS_DIR, WORKTREE_BASE

OOTESTPROJECT1 = Path.home() / "OOTestProject1"
OOTESTPROJECT2 = Path.home() / "OOTestProject2"
RESULTS_BASE = RESULTS_DIR
SESSION_BASE = SESSIONS_DIR


def ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def log(msg: str, file=None):
    line = f"[{ts()}] {msg}"
    print(line, flush=True)
    if file:
        file.write(line + "\n")
        file.flush()


def br_create(title: str, labels: str, description: str = "", cwd: str | Path | None = None) -> str | None:
    """Create a beads issue, return ID or None."""
    cmd = ["br", "create", title, "--labels", labels, "--silent"]
    if description:
        cmd.extend(["--description", description])
    result = subprocess.run(
        cmd,
        capture_output=True, text=True, check=False, timeout=30, cwd=str(cwd or OOTESTPROJECT1),
    )
    if result.returncode != 0:
        return None
    # First line of stdout is the issue ID
    issue_id = result.stdout.strip().split("\n")[0].strip()
    return issue_id if issue_id else None


def br_reset(issue_id: str, cwd: str | Path | None = None):
    """Reset issue to open status."""
    subprocess.run(
        ["br", "update", issue_id, "--status", "open"],
        capture_output=True, text=True, check=False, timeout=30, cwd=str(cwd or OOTESTPROJECT1),
    )


def cleanup_worktree(issue_id: str):
    """Remove worktree and prune."""
    wt = WORKTREE_BASE / issue_id
    if wt.exists():
        subprocess.run(["rm", "-rf", str(wt)], check=False)
    for repo in [OOTESTPROJECT1, OOTESTPROJECT2]:
        subprocess.run(
            ["git", "worktree", "prune"],
            capture_output=True, check=False, cwd=str(repo),
        )


def setup_tb5_branch(issue_id: str) -> bool:
    """Create a dl/<id> branch in OOTestProject1 with a non-matching change."""
    branch = f"dl/{issue_id}"
    subprocess.run(
        ["git", "branch", "-D", branch],
        capture_output=True, check=False, cwd=str(OOTESTPROJECT1),
    )
    r = subprocess.run(
        ["git", "checkout", "-b", branch],
        capture_output=True, text=True, check=False, cwd=str(OOTESTPROJECT1),
    )
    if r.returncode != 0:
        return False

    # Change a file OUTSIDE the watched db/ path to trigger cascade_skipped
    readme = OOTESTPROJECT1 / "README.md"
    with open(readme, "a") as f:
        f.write(f"\n# Stress test marker: {issue_id}\n")

    subprocess.run(["git", "add", str(readme)], check=False, cwd=str(OOTESTPROJECT1))
    subprocess.run(
        ["git", "commit", "-m", f"Stress test marker for {issue_id}"],
        capture_output=True, check=False, cwd=str(OOTESTPROJECT1),
    )
    subprocess.run(
        ["git", "checkout", "main"],
        capture_output=True, check=False, cwd=str(OOTESTPROJECT1),
    )
    return True


def cleanup_tb5_branch(issue_id: str):
    """Delete the test branch."""
    branch = f"dl/{issue_id}"
    subprocess.run(
        ["git", "branch", "-D", branch],
        capture_output=True, check=False, cwd=str(OOTESTPROJECT1),
    )


def run_pipeline(func_name: str, *args) -> dict:
    """Run a pipeline function and return the result dict."""
    args_str = ", ".join(repr(a) for a in args)
    code = (
        f"from devloop.feedback.pipeline import {func_name}; "
        f"import json; print(json.dumps({func_name}({args_str})))"
    )
    result = subprocess.run(
        ["uv", "run", "python", "-c", code],
        capture_output=True, text=True, check=False,
        timeout=1320, cwd=str(REPO),  # 1200s pipeline cap + 120s grace
        env={**os.environ, "CLAUDECODE": ""},  # unset CLAUDECODE
    )
    # Find the JSON line in stdout (skip log noise)
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {
        "success": False,
        "error": f"No JSON in output. stderr: {result.stderr[:500]}",
        "stdout_tail": result.stdout[-500:] if result.stdout else "",
        "returncode": result.returncode,
    }


def run_tb1(issue_id: str, run_log) -> dict:
    log(f"  TB-1 (golden path) issue={issue_id}", run_log)
    return run_pipeline("run_tb1", issue_id, str(OOTESTPROJECT1))


def run_tb2(issue_id: str, run_log) -> dict:
    log(f"  TB-2 (forced retry) issue={issue_id}", run_log)
    return run_pipeline("run_tb2", issue_id, str(OOTESTPROJECT1), True)


def run_tb3(issue_id: str, run_log) -> dict:
    log(f"  TB-3 (security gate) issue={issue_id}", run_log)
    return run_pipeline("run_tb3", issue_id, str(OOTESTPROJECT1))


def run_tb4(issue_id: str, run_log) -> dict:
    log(f"  TB-4 (turn limit=3) issue={issue_id}", run_log)
    return run_pipeline("run_tb4", issue_id, str(OOTESTPROJECT1), 3)


def run_tb5(issue_id: str, run_log) -> dict:
    log(f"  TB-5 (cascade) issue={issue_id}", run_log)
    # OOTestProject1 (source) → OOTestProject2 (target) via db/** watch
    return run_pipeline("run_tb5", issue_id, str(OOTESTPROJECT1), str(OOTESTPROJECT2))


def run_tb6(issue_id: str, run_log) -> dict:
    log(f"  TB-6 (session replay) issue={issue_id}", run_log)
    return run_pipeline("run_tb6", issue_id, str(OOTESTPROJECT1))


# TB definitions: (name, issue_title, description, labels, runner, needs_branch, cleanup_fn)
TB_DEFS = [
    ("tb1", "Stress: add factorial function",
     "Add a factorial(n) function to calculator.py that returns n! for non-negative integers. "
     "Should raise ValueError for negative inputs. Add tests in test_calculator.py.",
     "feature", run_tb1, False, None),
    ("tb2", "Stress: fix modulo edge case",
     "The modulo function in calculator.py returns incorrect results when the divisor is negative. "
     "Fix the edge case and add a regression test.",
     "bug", run_tb2, False, None),
    ("tb3", "Stress: add input sanitizer",
     "Add an input sanitizer that validates calculator inputs before processing. "
     "Should reject non-numeric strings and prevent injection via eval().",
     "security", run_tb3, False, None),
    ("tb4", "Stress: add log function",
     "Add a natural logarithm function log(x) to calculator.py. "
     "Should raise ValueError for x <= 0. Add corresponding tests.",
     "bug", run_tb4, False, None),
    ("tb5", "Stress: add helper function",
     "Add a helper utility function to the project. "
     "Should integrate with the existing module structure.",
     "feature", run_tb5, True, cleanup_tb5_branch),
    ("tb6", "Stress: add ceil function",
     "Add a ceil(x) function to calculator.py that returns the ceiling of x. "
     "Should handle float and integer inputs. Add tests.",
     "bug", run_tb6, False, None),
]


def run_one_iteration(iteration: int, results_dir: Path, run_log, skip: set[str], cooldown: int = 0) -> dict:
    """Run all 6 TBs once. Returns summary dict."""
    log(f"=== Iteration {iteration} ===", run_log)
    summary: dict[str, dict] = {}

    for tb_name, title, description, labels, runner, needs_branch, cleanup_fn in TB_DEFS:
        if tb_name in skip:
            log(f"  {tb_name.upper()} SKIPPED (--skip)", run_log)
            summary[tb_name] = {"skipped": True}
            continue

        issue_title = f"{title} (iter {iteration})"
        issue_id = br_create(issue_title, labels, description)
        if not issue_id:
            log(f"  {tb_name.upper()} FAILED: could not create issue", run_log)
            summary[tb_name] = {"success": False, "error": "br create failed"}
            continue

        # Setup branch for TB-5
        if needs_branch:
            if not setup_tb5_branch(issue_id):
                log(f"  {tb_name.upper()} FAILED: branch setup failed", run_log)
                summary[tb_name] = {"success": False, "error": "branch setup failed"}
                continue

        start = time.monotonic()
        try:
            result = runner(issue_id, run_log)
        except Exception as e:
            result = {"success": False, "error": f"Exception: {e}", "traceback": traceback.format_exc()}

        elapsed = round(time.monotonic() - start, 1)
        result["_elapsed"] = elapsed
        result["_issue_id"] = issue_id
        result["_iteration"] = iteration

        success = result.get("success", False)
        phase = result.get("phase", "?")
        persona = result.get("persona", "?")
        error = result.get("error", "")

        # TB-4 escalation is expected success
        if tb_name == "tb4" and result.get("escalated"):
            status = "ESCALATED (expected)"
        # TB-5 cascade_skipped is expected success
        elif tb_name == "tb5" and result.get("cascade_skipped"):
            status = "CASCADE_SKIPPED (expected)"
        elif success:
            status = "PASS"
        else:
            status = f"FAIL ({phase})"

        log(f"  {tb_name.upper()} {status} [{elapsed}s] persona={persona} err={error[:80] if error else 'none'}", run_log)

        summary[tb_name] = result

        # Save individual result
        result_path = results_dir / f"iter{iteration:03d}_{tb_name}.json"
        with open(result_path, "w") as f:
            json.dump(result, f, indent=2, default=str)

        # Cleanup
        cleanup_worktree(issue_id)
        if cleanup_fn:
            cleanup_fn(issue_id)
        # Also clean cascade child if TB-5 created one
        target_id = result.get("target_issue_id")
        if target_id:
            cleanup_worktree(target_id)

        # Cooldown between TBs to avoid concurrency-induced timeouts
        if cooldown > 0:
            log(f"  Cooldown: {cooldown}s", run_log)
            time.sleep(cooldown)

    return summary


def generate_report(all_results: list[dict], results_dir: Path, run_log):
    """Generate a summary report."""
    log("\n" + "=" * 60, run_log)
    log("STRESS TEST REPORT", run_log)
    log("=" * 60, run_log)

    total_runs = len(all_results)
    tb_names = ["tb1", "tb2", "tb3", "tb4", "tb5", "tb6"]

    for tb in tb_names:
        runs = [r.get(tb, {}) for r in all_results if tb in r]
        if not runs or all(r.get("skipped") for r in runs):
            log(f"  {tb.upper()}: SKIPPED", run_log)
            continue

        active_runs = [r for r in runs if not r.get("skipped")]
        passes = sum(1 for r in active_runs if r.get("success") or r.get("escalated") or r.get("cascade_skipped"))
        fails = len(active_runs) - passes
        errors = [r.get("error", "?") for r in active_runs if not (r.get("success") or r.get("escalated") or r.get("cascade_skipped"))]
        durations = [r.get("_elapsed", 0) for r in active_runs]
        avg_dur = round(sum(durations) / len(durations), 1) if durations else 0

        log(f"  {tb.upper()}: {passes}/{len(active_runs)} pass, {fails} fail, avg={avg_dur}s", run_log)
        if errors:
            # Deduplicate errors
            unique_errors: dict[str, int] = {}
            for e in errors:
                key = (e or "unknown")[:100]
                unique_errors[key] = unique_errors.get(key, 0) + 1
            for err, count in sorted(unique_errors.items(), key=lambda x: -x[1]):
                log(f"    [{count}x] {err}", run_log)

    # Timing
    all_durations = []
    for r in all_results:
        for tb in tb_names:
            if tb in r and not r[tb].get("skipped"):
                all_durations.append(r[tb].get("_elapsed", 0))
    total_time = sum(all_durations)
    log(f"\n  Total time: {total_time:.0f}s ({total_time/60:.1f} min)", run_log)
    log(f"  Iterations: {total_runs}", run_log)
    log(f"  Results dir: {results_dir}", run_log)

    # Save full report
    report_path = results_dir / "report.json"
    with open(report_path, "w") as f:
        json.dump({
            "total_iterations": total_runs,
            "summary": {
                tb: {
                    "runs": sum(1 for r in all_results if tb in r and not r.get(tb, {}).get("skipped")),
                    "passes": sum(1 for r in all_results if tb in r and (r[tb].get("success") or r[tb].get("escalated") or r[tb].get("cascade_skipped"))),
                }
                for tb in tb_names
            },
            "results": all_results,
        }, f, indent=2, default=str)
    log(f"  Full report: {report_path}", run_log)


def main():
    parser = argparse.ArgumentParser(description="Stress test all 6 TBs")
    parser.add_argument("--runs", type=int, default=30, help="Number of iterations (default: 30)")
    parser.add_argument("--skip", nargs="*", default=[], help="TBs to skip (e.g. --skip tb3 tb5)")
    parser.add_argument("--cooldown", type=int, default=0, help="Seconds to wait between TBs within each iteration (default: 0)")
    args = parser.parse_args()

    skip = set(args.skip)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    results_dir = RESULTS_BASE / run_id
    results_dir.mkdir(parents=True, exist_ok=True)

    log_path = results_dir / "stress.log"
    run_log = open(log_path, "w")

    cooldown = args.cooldown
    log(f"Stress test: {args.runs} iterations, skip={skip or 'none'}, cooldown={cooldown}s", run_log)
    log(f"Results: {results_dir}", run_log)
    log(f"Log: {log_path}", run_log)

    # Verify test repos are accessible
    if not OOTESTPROJECT1.exists():
        log(f"ERROR: OOTestProject1 not found at {OOTESTPROJECT1}", run_log)
        sys.exit(1)
    if not OOTESTPROJECT2.exists():
        log(f"ERROR: OOTestProject2 not found at {OOTESTPROJECT2}", run_log)
        sys.exit(1)

    # Pre-flight: pytest
    log("Pre-flight: running pytest...", run_log)
    pytest_result = subprocess.run(
        ["uv", "run", "pytest", "tests/", "-q", "--tb=no"],
        capture_output=True, text=True, check=False, timeout=300, cwd=str(REPO),
    )
    log(f"  pytest: {pytest_result.stdout.strip().splitlines()[-1] if pytest_result.stdout.strip() else 'FAILED'}", run_log)
    if pytest_result.returncode != 0:
        log("  WARNING: pytest failed, continuing anyway", run_log)

    # Pre-flight: OpenObserve
    log("Pre-flight: OpenObserve health...", run_log)
    oo = subprocess.run(
        ["curl", "-s", "http://localhost:5080/healthz"],
        capture_output=True, text=True, check=False, timeout=10,
    )
    log(f"  OpenObserve: {oo.stdout.strip()}", run_log)

    all_results: list[dict] = []
    try:
        for i in range(1, args.runs + 1):
            summary = run_one_iteration(i, results_dir, run_log, skip, cooldown)
            all_results.append(summary)
    except KeyboardInterrupt:
        log("\nInterrupted by user", run_log)
    finally:
        generate_report(all_results, results_dir, run_log)
        run_log.close()

    # Exit with failure if any TB had >20% failure rate
    for tb in ["tb1", "tb2", "tb3", "tb4", "tb5", "tb6"]:
        runs = [r.get(tb, {}) for r in all_results if tb in r and not r.get(tb, {}).get("skipped")]
        if not runs:
            continue
        passes = sum(1 for r in runs if r.get("success") or r.get("escalated") or r.get("cascade_skipped"))
        if passes / len(runs) < 0.8:
            sys.exit(1)


if __name__ == "__main__":
    main()
