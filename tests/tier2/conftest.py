"""Fixtures for Tier 2 planted-defect regression suite.

Creates temporary git repos from corpus scenarios, stages files,
and provides helpers to run `dl checkpoint` against them.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CORPUS_DIR = Path(__file__).parent / "corpus"


def discover_scenarios():
    """Find all corpus scenario directories."""
    scenarios = []
    for d in sorted(CORPUS_DIR.iterdir()):
        if d.is_dir() and (d / "expected.yaml").exists():
            scenarios.append(d.name)
    return scenarios


SCENARIOS = discover_scenarios()


@pytest.fixture
def dl_binary():
    """Path to the dl binary."""
    path = shutil.which("dl")
    if not path:
        path = os.path.expanduser("~/.local/bin/dl")
    if not os.path.exists(path):
        pytest.skip("dl binary not found")
    return path


def create_git_repo(tmp_path: Path, scenario_name: str, config_overrides: dict | None = None):
    """Create a temporary git repo from a corpus scenario.

    Returns (repo_path, expected_gates dict).
    """
    scenario_dir = CORPUS_DIR / scenario_name
    expected_path = scenario_dir / "expected.yaml"
    files_dir = scenario_dir / "files"

    with open(expected_path) as f:
        expected = yaml.safe_load(f)

    repo_path = tmp_path / scenario_name
    repo_path.mkdir()

    # git init
    subprocess.run(["git", "init", str(repo_path)], capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(repo_path), capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(repo_path), capture_output=True, check=True,
    )

    # Initial empty commit so we have a HEAD
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=str(repo_path), capture_output=True, check=True,
    )

    # Copy corpus files into repo
    if files_dir.exists():
        for src in files_dir.rglob("*"):
            if src.is_file():
                rel = src.relative_to(files_dir)
                dst = repo_path / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

    # Write .devloop.yaml if config overrides provided
    if config_overrides:
        devloop_config = repo_path / ".devloop.yaml"
        with open(devloop_config, "w") as f:
            yaml.dump(config_overrides, f)

    # Stage all files
    subprocess.run(
        ["git", "add", "-A"],
        cwd=str(repo_path), capture_output=True, check=True,
    )

    return repo_path, expected


def run_checkpoint(dl_binary: str, repo_path: Path) -> dict:
    """Run `dl checkpoint --json --dir <repo>` and parse the result."""
    result = subprocess.run(
        [dl_binary, "checkpoint", "--json", "--dir", str(repo_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    # dl checkpoint exits non-zero on failure, but still outputs JSON
    stdout = result.stdout.strip()
    if not stdout:
        return {
            "passed": True if result.returncode == 0 else False,
            "gates_run": 0,
            "gates_passed": 0,
            "gates_failed": 0,
            "first_failure": None,
            "trailer": None,
            "gate_results": [],
            "duration_ms": 0,
            "error": result.stderr.strip(),
        }

    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {
            "passed": result.returncode == 0,
            "gates_run": 0,
            "gate_results": [],
            "raw_stdout": stdout,
            "raw_stderr": result.stderr.strip(),
        }
