"""Tiered context loading — file map generation and scope hint extraction.

Implements #26: Large Repo Context. Generates a repository structure summary
and extracts scope hints from issue text to focus agent attention.
"""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
import time
from collections import Counter
from pathlib import Path

from opentelemetry import trace

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# Language detection by extension
_EXT_TO_LANG: dict[str, str] = {
    ".py": "Python",
    ".js": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".jsx": "JavaScript",
    ".rs": "Rust",
    ".go": "Go",
    ".java": "Java",
    ".rb": "Ruby",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".toml": "TOML",
    ".json": "JSON",
    ".md": "Markdown",
    ".sh": "Shell",
    ".sql": "SQL",
    ".html": "HTML",
    ".css": "CSS",
}

CACHE_DIR = Path("/tmp/dev-loop/cache")
CACHE_TTL_SECONDS = 3600  # 1 hour


def _repo_hash(repo_path: str) -> str:
    """Short hash of repo path for cache key."""
    return hashlib.sha256(repo_path.encode()).hexdigest()[:12]


def _get_head_sha(repo_path: str) -> str:
    """Get current HEAD SHA for cache invalidation."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _list_files(repo_path: str, max_files: int = 500) -> list[str]:
    """List tracked files using git ls-files."""
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []
        files = result.stdout.strip().split("\n")
        return [f for f in files if f][:max_files]
    except Exception:
        logger.exception("Failed to list files in %s", repo_path)
        return []


def generate_directory_summary(repo_path: str, max_files: int = 500) -> str:
    """Generate a top-level directory summary with file counts and languages.

    Returns a compact text block suitable for embedding in CLAUDE.md overlays.
    """
    with tracer.start_as_current_span(
        "file_map.directory_summary",
        attributes={"repo_path": repo_path, "max_files": max_files},
    ):
        files = _list_files(repo_path, max_files)
        if not files:
            return ""

        # Count files per top-level directory
        dir_counts: Counter[str] = Counter()
        lang_counts: Counter[str] = Counter()
        for f in files:
            parts = f.split("/")
            top_dir = parts[0] if len(parts) > 1 else "."
            dir_counts[top_dir] += 1
            ext = Path(f).suffix.lower()
            if ext in _EXT_TO_LANG:
                lang_counts[_EXT_TO_LANG[ext]] += 1

        lines = [f"**{len(files)} tracked files**"]
        if lang_counts:
            top_langs = lang_counts.most_common(5)
            langs_str = ", ".join(f"{lang} ({n})" for lang, n in top_langs)
            lines.append(f"Languages: {langs_str}")
        lines.append("")

        # Top directories sorted by file count
        lines.append("```")
        for d, count in dir_counts.most_common(20):
            lines.append(f"{d:30s} {count:>4} files")
        lines.append("```")

        return "\n".join(lines)


def generate_file_map(repo_path: str, max_files: int = 500) -> str:
    """Generate a tree-like file map of the repository.

    Uses caching: invalidates on HEAD change or TTL expiry.
    """
    with tracer.start_as_current_span(
        "file_map.generate",
        attributes={"repo_path": repo_path, "max_files": max_files},
    ) as span:
        # Check cache
        rhash = _repo_hash(repo_path)
        cache_dir = CACHE_DIR / rhash
        cache_file = cache_dir / "file_map.txt"
        head_file = cache_dir / "head_sha.txt"
        current_head = _get_head_sha(repo_path)

        if cache_file.exists() and head_file.exists():
            cached_head = head_file.read_text().strip()
            age = time.time() - cache_file.stat().st_mtime
            if cached_head == current_head and age < CACHE_TTL_SECONDS:
                span.set_attribute("file_map.cache_hit", True)
                return cache_file.read_text()

        span.set_attribute("file_map.cache_hit", False)

        files = _list_files(repo_path, max_files)
        if not files:
            return ""

        # Build tree structure
        tree: dict = {}
        for f in files:
            parts = f.split("/")
            node = tree
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = None  # leaf

        def _render(node: dict, prefix: str = "", depth: int = 0) -> list[str]:
            if depth > 3:
                remaining = sum(1 for _ in _count_leaves(node))
                if remaining > 0:
                    return [f"{prefix}... ({remaining} more files)"]
                return []
            lines = []
            items = sorted(node.items(), key=lambda x: (x[1] is not None, x[0]))
            for i, (name, subtree) in enumerate(items):
                is_last = i == len(items) - 1
                connector = "└── " if is_last else "├── "
                if subtree is None:
                    lines.append(f"{prefix}{connector}{name}")
                else:
                    lines.append(f"{prefix}{connector}{name}/")
                    extension = "    " if is_last else "│   "
                    lines.extend(_render(subtree, prefix + extension, depth + 1))
            return lines

        def _count_leaves(node: dict):
            for _name, subtree in node.items():
                if subtree is None:
                    yield 1
                else:
                    yield from _count_leaves(subtree)

        rendered = "\n".join(_render(tree))

        # Write cache
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(rendered)
            if current_head:
                head_file.write_text(current_head)
        except Exception:
            logger.debug("Failed to write file map cache", exc_info=True)

        span.set_attribute("file_map.file_count", len(files))
        return rendered


# ---------------------------------------------------------------------------
# Scope hint extraction
# ---------------------------------------------------------------------------

# Patterns that look like file/directory references in issue text
_FILE_PATH_RE = re.compile(
    r"""
    (?:^|[\s`"'(])               # preceded by whitespace, backtick, quote, paren
    (
        (?:[\w.-]+/)+[\w.-]+     # dir/dir/file.ext pattern
        |
        [\w.-]+\.(?:py|js|ts|tsx|jsx|rs|go|java|rb|yaml|yml|toml|json|sql|sh|css|html)
                                  # bare filename with code extension
    )
    (?:[\s`"'),.:;]|$)           # followed by whitespace, backtick, quote, punctuation
    """,
    re.VERBOSE | re.MULTILINE,
)

# Module-style references (e.g. "devloop.orchestration.server")
_MODULE_RE = re.compile(
    r"(?:^|[\s`\"'(])((?:[a-zA-Z_]\w*\.){2,}[a-zA-Z_]\w*)(?:[\s`\"'),.:;]|$)",
    re.MULTILINE,
)


def extract_scope_hints(
    issue_title: str,
    issue_description: str,
    known_paths: list[str] | str,
) -> list[str]:
    """Extract file/directory references from issue text and cross-reference against known paths.

    known_paths: list of file paths from git ls-files, or a newline-joined string.
    Returns a deduplicated list of paths that appear both in the issue text and in the repo.
    """
    with tracer.start_as_current_span("file_map.extract_scope_hints"):
        if isinstance(known_paths, str):
            path_set = set(known_paths.strip().split("\n")) if known_paths.strip() else set()
        else:
            path_set = set(known_paths)

        # Build directory prefixes and basename → full path index
        dir_set: set[str] = set()
        basename_map: dict[str, str] = {}  # basename -> first full path
        for p in path_set:
            parts = p.split("/")
            for i in range(1, len(parts)):
                dir_set.add("/".join(parts[:i]))
            basename = parts[-1]
            if basename not in basename_map:
                basename_map[basename] = p

        text = f"{issue_title}\n{issue_description}"
        candidates: list[str] = []

        # Extract file path patterns
        for m in _FILE_PATH_RE.finditer(text):
            candidates.append(m.group(1))

        # Extract module references and convert to paths
        for m in _MODULE_RE.finditer(text):
            module = m.group(1)
            as_path = module.replace(".", "/")
            candidates.append(as_path)
            candidates.append(f"{as_path}.py")
            candidates.append(f"src/{as_path}.py")

        # Cross-reference against known paths
        hints: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate in path_set:
                hints.append(candidate)
            elif candidate in dir_set:
                hints.append(candidate)
            elif candidate in basename_map:
                # Bare filename matches a known file
                full_path = basename_map[candidate]
                if full_path not in seen:
                    hints.append(full_path)
                    seen.add(full_path)
            else:
                # Check if any directory prefix of the candidate is known
                parts = candidate.split("/")
                for i in range(len(parts), 0, -1):
                    prefix = "/".join(parts[:i])
                    if prefix in path_set or prefix in dir_set:
                        if prefix not in seen:
                            hints.append(prefix)
                            seen.add(prefix)
                        break

        return hints
