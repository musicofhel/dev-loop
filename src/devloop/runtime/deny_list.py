"""Secrets deny list — prevents agents from reading sensitive files.

Edge case #11 (Task #41): Agents must never read secrets, keys, or credentials.
This module provides pattern matching and CLAUDE.md rule generation for the
deny list that gets injected into agent overlays at spawn time.

The patterns here extend the per-project denied_paths in config/capabilities.yaml
with a comprehensive baseline that covers common secret file conventions.
"""

from __future__ import annotations

import fnmatch
from pathlib import PurePosixPath

from opentelemetry import trace

# ---------------------------------------------------------------------------
# OTel tracer for deny-list operations
# ---------------------------------------------------------------------------

tracer = trace.get_tracer("runtime.deny_list", "0.1.0")

# ---------------------------------------------------------------------------
# Denied patterns — glob patterns that agents must never read
# ---------------------------------------------------------------------------

DENIED_PATTERNS: list[str] = [
    # Environment / dotenv
    ".env",
    ".env.*",
    # Cryptographic keys and certificates
    "*.key",
    "*.pem",
    "*.p12",
    "*.pfx",
    # Credentials files (any extension)
    "credentials.*",
    # Anything with "secret" in the name
    "*secret*",
    # Cloud provider credential directories
    ".aws/*",
    ".ssh/*",
    # Token / auth files
    "*.keystore",
    "*.jks",
    ".netrc",
    ".npmrc",
    ".pypirc",
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_path_denied(path: str) -> bool:
    """Check if a file path matches any denied pattern.

    Matches are checked against the full path and also against just the
    filename, so both ``credentials.json`` and ``config/credentials.json``
    are caught by the ``credentials.*`` pattern.

    Args:
        path: File path to check (relative or absolute).

    Returns:
        True if the path matches any denied pattern and should be blocked.
    """
    with tracer.start_as_current_span(
        "runtime.deny_list.is_path_denied",
        attributes={"deny_list.path": path},
    ) as span:
        posix = PurePosixPath(path)

        for pattern in DENIED_PATTERNS:
            # Match against the full relative/absolute path
            if fnmatch.fnmatch(str(posix), pattern):
                span.set_attribute("deny_list.matched_pattern", pattern)
                span.set_attribute("deny_list.denied", True)
                return True

            # Match against just the filename (basename)
            if fnmatch.fnmatch(posix.name, pattern):
                span.set_attribute("deny_list.matched_pattern", pattern)
                span.set_attribute("deny_list.denied", True)
                return True

            # Match against each suffix of the path parts so that
            # directory-scoped patterns like ".aws/*" work on paths
            # like "home/user/.aws/credentials"
            parts = posix.parts
            for i in range(len(parts)):
                sub = str(PurePosixPath(*parts[i:]))
                if fnmatch.fnmatch(sub, pattern):
                    span.set_attribute("deny_list.matched_pattern", pattern)
                    span.set_attribute("deny_list.denied", True)
                    return True

        span.set_attribute("deny_list.denied", False)
        return False


def generate_deny_rules() -> str:
    """Generate the CLAUDE.md deny-list text block for agent overlays.

    This text is injected into the CLAUDE.md that agents see at spawn time,
    ensuring they refuse to read any sensitive files even if a task prompt
    asks them to.

    Returns:
        A Markdown block suitable for inclusion in a CLAUDE.md overlay.
    """
    with tracer.start_as_current_span(
        "runtime.deny_list.generate_deny_rules",
        attributes={"deny_list.pattern_count": len(DENIED_PATTERNS)},
    ):
        # Group patterns into logical rows for readability
        env_patterns = [p for p in DENIED_PATTERNS if p.startswith(".env")]
        crypto_patterns = [
            p for p in DENIED_PATTERNS
            if any(p.endswith(ext) for ext in (".key", ".pem", ".p12", ".pfx"))
        ]
        cred_patterns = [
            p for p in DENIED_PATTERNS
            if p.startswith("credentials") or "secret" in p
        ]
        dir_patterns = [
            p for p in DENIED_PATTERNS
            if p.startswith(".aws") or p.startswith(".ssh")
        ]
        auth_patterns = [
            p for p in DENIED_PATTERNS
            if p in ("*.keystore", "*.jks", ".netrc", ".npmrc", ".pypirc")
        ]

        lines = [
            "## NEVER read these files",
            f"- {', '.join(env_patterns + crypto_patterns + cred_patterns)}",
            f"- {', '.join(dir_patterns + auth_patterns)}",
            "If you need data from these files, ask the human.",
        ]

        return "\n".join(lines)
