#!/usr/bin/env python3
"""Parse Claude Code session JSONLs and extract tool calls for replay.

Reads JSONL files from ~/.claude/projects/ and extracts Write, Edit, and Bash
tool invocations. Outputs NDJSON with one line per tool call.

Usage:
    uv run python scripts/replay/parse_sessions.py ~/.claude/projects/-home-musicofhel/*.jsonl
    uv run python scripts/replay/parse_sessions.py ~/.claude/projects/-home-musicofhel/*.jsonl --stats
"""

import argparse
import json
import sys
from pathlib import Path

CHECKABLE_TOOLS = {"Write", "Edit", "Bash"}


def parse_session(jsonl_path: str) -> list[dict]:
    """Extract checkable tool calls from a session JSONL."""
    session_id = Path(jsonl_path).stem
    tool_calls = []

    with open(jsonl_path) as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Look for assistant messages with tool_use blocks
            if entry.get("type") != "assistant":
                continue

            timestamp = entry.get("timestamp", "")
            message = entry.get("message", {})
            content = message.get("content", [])
            if not isinstance(content, list):
                continue

            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_use":
                    continue

                tool_name = block.get("name", "")
                if tool_name not in CHECKABLE_TOOLS:
                    continue

                tool_input = block.get("input", {})

                # Extract key fields for summary
                record = {
                    "session_id": session_id,
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "timestamp": timestamp,
                    "line": line_num,
                }

                # Add convenience fields
                if tool_name in ("Write", "Edit"):
                    record["file_path"] = tool_input.get("file_path", "")
                elif tool_name == "Bash":
                    record["command"] = tool_input.get("command", "")

                tool_calls.append(record)

    return tool_calls


def main():
    parser = argparse.ArgumentParser(description="Parse session JSONLs for replay")
    parser.add_argument("files", nargs="+", help="JSONL session files")
    parser.add_argument("--stats", action="store_true", help="Print stats instead of NDJSON")
    args = parser.parse_args()

    total = 0
    by_tool: dict[str, int] = {}
    sessions_parsed = 0

    for f in args.files:
        try:
            tool_calls = parse_session(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"Error reading {f}: {e}", file=sys.stderr)
            continue

        sessions_parsed += 1
        for tc in tool_calls:
            if args.stats:
                total += 1
                by_tool[tc["tool_name"]] = by_tool.get(tc["tool_name"], 0) + 1
            else:
                print(json.dumps(tc))

    if args.stats:
        print(f"Sessions: {sessions_parsed}")
        print(f"Tool calls: {total}")
        for tool, count in sorted(by_tool.items()):
            print(f"  {tool}: {count}")


if __name__ == "__main__":
    main()
