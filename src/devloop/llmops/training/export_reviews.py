"""Export Gate 4 code review training data from Claude Code session JSONLs.

Parses session NDJSON files to find Gate 4 review invocations and extract
(diff, issue_context, review_criteria, findings_json) tuples for GEPA training.

Usage:
    python -m devloop.llmops.training.export_reviews
"""

from __future__ import annotations

import glob
import json
import os
import re
from pathlib import Path


def _parse_review_prompt(content: str) -> dict | None:
    """Extract structured fields from a Gate 4 review prompt.

    The prompt follows the pattern:
        You are a senior code reviewer...
        ## Issue Context
        **Title:** ...
        **Description:** ...
        ## Review Criteria
        ...
        ## Diff
        ```diff
        ...
        ```
    """
    if "senior code reviewer" not in content.lower():
        return None

    result: dict = {}

    # Extract issue title
    title_match = re.search(r"\*\*Title:\*\*\s*(.+?)(?:\n|$)", content)
    result["issue_title"] = title_match.group(1).strip() if title_match else ""

    # Extract issue description: text between **Description:** and next ## header
    desc_start = content.find("**Description:**")
    if desc_start != -1:
        desc_text_start = desc_start + len("**Description:**")
        desc_end = content.find("\n##", desc_text_start)
        if desc_end == -1:
            desc_end = len(content)
        result["issue_description"] = content[desc_text_start:desc_end].strip()
    else:
        result["issue_description"] = ""

    result["issue_context"] = (
        f"{result['issue_title']}\n{result['issue_description']}".strip()
    )

    # Extract review criteria
    criteria_match = re.search(
        r"## Review Criteria\s*\n(.+?)(?=\n##|```|$)", content, re.DOTALL
    )
    result["review_criteria"] = criteria_match.group(1).strip() if criteria_match else ""

    # Extract diff — format is: ## Diff to Review\n```\n...\n```
    diff_match = re.search(r"```\s*\n(diff --git .+?)```", content, re.DOTALL)
    if not diff_match:
        # Try ```diff marker
        diff_match = re.search(r"```diff\s*\n(.+?)```", content, re.DOTALL)
    if not diff_match:
        # Try raw ## Diff section
        diff_match = re.search(r"## Diff[^\n]*\n(.+?)(?=\n##|$)", content, re.DOTALL)
    result["diff"] = diff_match.group(1).strip() if diff_match else ""

    if not result["diff"]:
        return None

    return result


def _find_review_response(events: list[dict], review_idx: int) -> str | None:
    """Find the assistant response following a review prompt.

    The Gate 4 response comes as an assistant message with content as a
    list of blocks. The findings are in a tool_use block named
    'StructuredOutput' with 'findings' in its 'input' dict.
    """
    for i in range(review_idx + 1, min(review_idx + 10, len(events))):
        evt = events[i]
        msg = evt.get("message", {})
        content = msg.get("content", "")

        # Handle list-of-blocks content (Claude Code format)
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                # StructuredOutput tool_use block
                if block.get("type") == "tool_use" and "findings" in str(
                    block.get("input", {})
                ):
                    inp = block.get("input", {})
                    findings = inp.get("findings", [])
                    return json.dumps(findings)
                # Text block with JSON
                if block.get("type") == "text":
                    text = block.get("text", "")
                    if "findings" in text:
                        try:
                            parsed = json.loads(text)
                            if "findings" in parsed:
                                return json.dumps(parsed["findings"])
                        except (json.JSONDecodeError, TypeError):
                            brace_start = text.find("{")
                            brace_end = text.rfind("}")
                            if brace_start != -1 and brace_end > brace_start:
                                try:
                                    parsed = json.loads(
                                        text[brace_start : brace_end + 1]
                                    )
                                    if "findings" in parsed:
                                        return json.dumps(parsed["findings"])
                                except json.JSONDecodeError:
                                    pass
            continue

        if not isinstance(content, str):
            continue

        # Plain string content with JSON
        if "findings" in content:
            try:
                parsed = json.loads(content)
                if "findings" in parsed:
                    return json.dumps(parsed.get("findings", []))
            except (json.JSONDecodeError, TypeError):
                pass

            brace_start = content.find("{")
            brace_end = content.rfind("}")
            if brace_start != -1 and brace_end > brace_start:
                try:
                    parsed = json.loads(content[brace_start : brace_end + 1])
                    if "findings" in parsed:
                        return json.dumps(parsed["findings"])
                except json.JSONDecodeError:
                    pass

    return None


def export_reviews(
    sessions_dir: str | None = None,
    output_path: str | None = None,
    max_sessions: int = 200,
) -> int:
    """Export Gate 4 review data from session JSONLs.

    Returns the number of examples exported.
    """
    if sessions_dir is None:
        from devloop.llmops.training import _default_sessions_dir

        sessions_dir = _default_sessions_dir()

    if output_path is None:
        output_path = os.path.expanduser(
            "~/.local/share/dev-loop/llmops/training/code_review.jsonl"
        )

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    files = sorted(glob.glob(os.path.join(sessions_dir, "*.jsonl")))[:max_sessions]
    exported = 0

    with open(output_path, "w") as out:
        for fpath in files:
            # Parse all events from the session
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

            # Find review prompts and their responses
            for i, evt in enumerate(events):
                msg = evt.get("message", {})
                content = msg.get("content", "")
                if not isinstance(content, str):
                    continue

                parsed = _parse_review_prompt(content)
                if parsed is None:
                    continue

                # Find the corresponding response
                findings_json = _find_review_response(events, i)
                if findings_json is None:
                    # No parseable response — skip to avoid false-negative training data
                    continue

                example = {
                    "inputs": {
                        "diff": parsed["diff"][:50000],  # cap large diffs
                        "issue_context": parsed["issue_context"],
                        "review_criteria": parsed["review_criteria"],
                    },
                    "outputs": {
                        "findings_json": findings_json,
                    },
                    "metadata": {
                        "session_id": os.path.basename(fpath).replace(".jsonl", ""),
                        "source": "gate4_session",
                    },
                }
                out.write(json.dumps(example) + "\n")
                exported += 1

    return exported


if __name__ == "__main__":
    import sys

    from devloop.llmops.training import _default_sessions_dir

    sessions_dir = _default_sessions_dir()
    if not os.path.isdir(sessions_dir):
        print(f"WARNING: Sessions dir not found: {sessions_dir}", file=sys.stderr)
    elif not glob.glob(os.path.join(sessions_dir, "*.jsonl")):
        print(f"WARNING: No .jsonl files in {sessions_dir}", file=sys.stderr)

    count = export_reviews()
    print(f"Exported {count} code review examples")
    print("Output: ~/.local/share/dev-loop/llmops/training/code_review.jsonl")
