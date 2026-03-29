"""Export retry prompt training data from Claude Code session JSONLs.

Scans sessions for retry-related patterns (TB-2 runs) where an agent
failed and was given a retry prompt. Extracts failure context and outcome.

Usage:
    python -m devloop.llmops.training.export_retries
"""

from __future__ import annotations

import glob
import json
import os
from pathlib import Path


def _is_retry_prompt(content: str) -> bool:
    """Detect if content is a retry prompt from build_retry_prompt().

    Matches the actual output format of feedback/server.py:build_retry_prompt():
    - Starts with ``## Issue:`` header
    - Contains ``### Failure`` sections (capital F, from line 169 of server.py)
    - Ends with ``fix the issues listed above`` instruction
    - Excludes Gate 4 code review prompts (contain "senior code reviewer")
    """
    lower = content.lower()
    # Must have the footer instruction (unique to retry prompts)
    if "fix the issues listed above" not in lower:
        return False
    # Must have structured failure sections — match the actual format:
    # "### Failure {i}: {gate_name} quality gate" (case-insensitive)
    if "### failure" not in lower and "## issue:" not in lower:
        return False
    # Exclude Gate 4 review prompts
    if "senior code reviewer" in lower:
        return False
    return len(content) > 100


def _extract_retry_data(events: list[dict]) -> list[dict]:
    """Extract retry training examples from a session's events.

    Looks for patterns:
    1. A message containing gate failure details
    2. A subsequent retry prompt to the agent
    3. The outcome (did the retry succeed?)
    """
    examples = []
    gate_failures: list[dict] = []
    original_task = ""

    for i, evt in enumerate(events):
        msg = evt.get("message", {})
        content = msg.get("content", "")

        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    content = block.get("text", "")
                    break
            else:
                continue

        if not isinstance(content, str):
            continue

        # Capture original task from first human message
        if not original_task and evt.get("userType") == "external":
            if not _is_retry_prompt(content) and len(content) > 20:
                original_task = content[:2000]

        # Detect gate failure reports (exclude Gate 4 review prompts)
        lower = content.lower()
        is_gate_failure = (
            # Original heuristic: explicit "gate" + "fail"/"did not pass"
            ("gate" in lower and ("fail" in lower or "did not pass" in lower))
            # Also match retry prompt format: "### Failure N: X quality gate"
            or ("### failure" in lower and "quality gate" in lower)
            # Also match structured failure sections from build_retry_prompt()
            or ("## issue:" in lower and "### failure" in lower)
        )
        if is_gate_failure and "senior code reviewer" not in lower:
            gate_failures.append({
                "content": content[:3000],
                "index": i,
            })

        # Detect retry prompts
        if _is_retry_prompt(content) and gate_failures:
            # Look ahead for outcome (pass/fail in subsequent events)
            retry_succeeded = False
            for j in range(i + 1, min(i + 20, len(events))):
                later_msg = events[j].get("message", {})
                later_content = later_msg.get("content", "")
                if isinstance(later_content, list):
                    for block in later_content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            later_content = block.get("text", "")
                            break
                if isinstance(later_content, str):
                    lc_lower = later_content.lower()
                    if "all gates passed" in lc_lower or "passed" in lc_lower:
                        retry_succeeded = True
                        break
                    if "gate" in later_content.lower() and "fail" in later_content.lower():
                        break

            # Build the failure log from accumulated gate failures
            failure_log = "\n---\n".join(gf["content"] for gf in gate_failures[-2:])

            examples.append({
                "inputs": {
                    "failure_log": failure_log,
                    "original_task": original_task,
                    "gate_results": json.dumps(
                        [{"failure": gf["content"][:500]} for gf in gate_failures[-2:]]
                    ),
                },
                "outputs": {
                    "retry_instructions": content[:3000],
                    "retry_succeeded": str(retry_succeeded),
                },
                "metadata": {
                    "event_index": i,
                    "num_failures": len(gate_failures),
                },
            })

    return examples


def export_retries(
    sessions_dir: str | None = None,
    output_path: str | None = None,
    max_sessions: int = 200,
    force: bool = False,
) -> int:
    """Export retry prompt data from session JSONLs.

    Returns the number of examples exported.
    """
    from devloop.llmops.training import safe_write_jsonl

    if sessions_dir is None:
        from devloop.llmops.training import _default_sessions_dir

        sessions_dir = _default_sessions_dir()

    if output_path is None:
        output_path = os.path.expanduser(
            "~/.local/share/dev-loop/llmops/training/retry_prompt.jsonl"
        )

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    files = sorted(glob.glob(os.path.join(sessions_dir, "*.jsonl")))[:max_sessions]
    examples: list[dict] = []

    for fpath in files:
        events = []
        with open(fpath) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        for example in _extract_retry_data(events):
            example["metadata"]["session_id"] = (
                os.path.basename(fpath).replace(".jsonl", "")
            )
            example["metadata"]["source"] = "retry_session"
            examples.append(example)

    return safe_write_jsonl(output_path, examples, force=force)


if __name__ == "__main__":
    import sys

    from devloop.llmops.training import _default_sessions_dir

    sessions_dir = _default_sessions_dir()
    if not os.path.isdir(sessions_dir):
        print(f"WARNING: Sessions dir not found: {sessions_dir}", file=sys.stderr)
    elif not glob.glob(os.path.join(sessions_dir, "*.jsonl")):
        print(f"WARNING: No .jsonl files in {sessions_dir}", file=sys.stderr)

    force = "--force" in sys.argv
    count = export_retries(force=force)
    print(f"Exported {count} retry prompt examples")
    print("Output: ~/.local/share/dev-loop/llmops/training/retry_prompt.jsonl")
