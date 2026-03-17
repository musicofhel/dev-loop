#!/usr/bin/env python3
"""Validate corpus YAML schema and file structure."""

import sys
from pathlib import Path

import yaml

CORPUS_DIR = Path(__file__).parent.parent.parent / "tests" / "tier2" / "corpus"
VALID_GATES = {"sanity", "semgrep", "secrets", "atdd", "review"}


def validate():
    errors = []
    scenarios = sorted(d for d in CORPUS_DIR.iterdir() if d.is_dir())

    if not scenarios:
        print(f"ERROR: No scenarios found in {CORPUS_DIR}")
        return 1

    for scenario_dir in scenarios:
        name = scenario_dir.name
        expected_path = scenario_dir / "expected.yaml"
        files_dir = scenario_dir / "files"

        # Check expected.yaml exists
        if not expected_path.exists():
            errors.append(f"{name}: missing expected.yaml")
            continue

        # Parse YAML
        try:
            with open(expected_path) as f:
                expected = yaml.safe_load(f)
        except yaml.YAMLError as e:
            errors.append(f"{name}: invalid YAML: {e}")
            continue

        # Check required fields
        if not isinstance(expected, dict):
            errors.append(f"{name}: expected.yaml must be a dict")
            continue

        if "description" not in expected:
            errors.append(f"{name}: missing 'description' field")

        if "expected_gates" not in expected:
            errors.append(f"{name}: missing 'expected_gates' field")
            continue

        gates = expected["expected_gates"]
        if not isinstance(gates, dict):
            errors.append(f"{name}: 'expected_gates' must be a dict")
            continue

        for gate_name, gate_expect in gates.items():
            if gate_name not in VALID_GATES:
                errors.append(f"{name}: unknown gate '{gate_name}' (valid: {VALID_GATES})")

            if not isinstance(gate_expect, dict):
                errors.append(f"{name}: gate '{gate_name}' must be a dict")
                continue

            if "passed" not in gate_expect:
                errors.append(f"{name}: gate '{gate_name}' missing 'passed' field")

        # Check files directory
        if not files_dir.exists():
            errors.append(f"{name}: missing files/ directory")
        elif not any(files_dir.iterdir()):
            errors.append(f"{name}: files/ directory is empty")

        # Print OK
        if not any(name in e for e in errors):
            n_files = sum(1 for _ in files_dir.rglob("*") if _.is_file()) if files_dir.exists() else 0
            gate_summary = ", ".join(
                f"{g}={'PASS' if e.get('passed') else 'FAIL'}"
                for g, e in gates.items()
            )
            print(f"  OK  {name} ({n_files} files) — {gate_summary}")

    print(f"\n{len(scenarios)} scenarios, {len(errors)} errors")

    if errors:
        print("\nErrors:")
        for e in errors:
            print(f"  - {e}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(validate())
