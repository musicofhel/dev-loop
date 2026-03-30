"""CLI entry point — delegates to justfile for now, will grow as TBs land."""

import subprocess
import sys
from pathlib import Path

import yaml


def _run_tb1_mock(fixture_path: str) -> None:
    """Run TB-1 with a mock ticket loaded from a YAML fixture."""
    fixture = Path(fixture_path)
    if not fixture.exists():
        print(f"Error: fixture not found: {fixture}", file=sys.stderr)
        sys.exit(1)

    with open(fixture) as f:
        ticket = yaml.safe_load(f)

    issue_id = ticket.get("id", "MOCK-001")
    repo = ticket.get("repo", "OOTestProject1")
    repo_path = Path.home() / repo
    if not repo_path.exists():
        print(f"Error: repo not found: {repo_path}", file=sys.stderr)
        sys.exit(1)

    print(f"TB-1 Mock: {ticket.get('title', issue_id)}")
    print(f"  Issue:  {issue_id}")
    print(f"  Repo:   {repo_path}")
    print(f"  Labels: {ticket.get('labels', [])}")
    print()

    # Run TB-1 with the fixture data
    subprocess.run(
        [
            "uv", "run", "python", "-c",
            f"from devloop.feedback.pipeline import run_tb1; "
            f"import json; print(json.dumps(run_tb1('{issue_id}', '{repo_path}'), indent=2))",
        ],
        check=False,
    )


def main() -> None:
    """Run devloop commands. Thin wrapper over just until MCP servers are wired."""
    args = sys.argv[1:]
    if not args:
        subprocess.run(["just", "--list"], check=False)
        return

    if args[0] == "tb1-mock":
        fixture = args[1] if len(args) > 1 else "test-fixtures/tickets/tb1-sample.yaml"
        _run_tb1_mock(fixture)
        return

    subprocess.run(["just", *args], check=False)


if __name__ == "__main__":
    main()
