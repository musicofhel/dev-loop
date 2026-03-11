"""CLI entry point — delegates to justfile for now, will grow as TBs land."""

import subprocess
import sys


def main() -> None:
    """Run devloop commands. Thin wrapper over just until MCP servers are wired."""
    args = sys.argv[1:]
    if not args:
        subprocess.run(["just", "--list"], check=False)
        return
    subprocess.run(["just", *args], check=False)


if __name__ == "__main__":
    main()
