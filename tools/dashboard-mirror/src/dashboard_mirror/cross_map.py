"""Build a cross-dashboard metric map.

Scans all dashboard source configs to find metrics/queries that appear
in multiple dashboards, enabling consistency analysis.

Runs as part of the baseline collection, not as a standalone command.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .config import CONFIG_DIR, OUTPUT_DIR


def _extract_columns_from_sql(sql: str) -> set[str]:
    """Extract column names referenced in a SQL query."""
    columns = set()

    # Columns in SELECT (aliases)
    m = re.search(r"SELECT\s+(.*?)\s+FROM\s", sql, re.I | re.S)
    if m:
        select_body = m.group(1)
        # Find all word tokens that look like column names
        for token in re.findall(r'\b([a-z_][a-z0-9_]*)\b', select_body, re.I):
            if token.upper() not in {
                "SELECT", "AS", "FROM", "AND", "OR", "CASE", "WHEN", "THEN",
                "ELSE", "END", "NULL", "TRUE", "FALSE", "COUNT", "SUM", "AVG",
                "MIN", "MAX", "ROUND", "CAST", "DATE_TRUNC", "TO_TIMESTAMP",
                "INTERVAL", "NOW", "BIGINT", "DOUBLE", "VARCHAR", "INT",
                "LIMIT", "OFFSET", "ASC", "DESC", "BETWEEN",
            }:
                columns.add(token)

    # Columns in WHERE
    where_match = re.search(r"WHERE\s+(.*?)(?:GROUP|ORDER|LIMIT|$)", sql, re.I | re.S)
    if where_match:
        for token in re.findall(r'\b([a-z_][a-z0-9_]*)\b', where_match.group(1), re.I):
            if token.upper() not in {
                "AND", "OR", "NOT", "IN", "LIKE", "IS", "NULL", "BETWEEN",
                "TRUE", "FALSE", "CAST", "BIGINT", "NOW", "INTERVAL",
            }:
                columns.add(token)

    # Columns in GROUP BY
    group_match = re.search(r"GROUP\s+BY\s+(.*?)(?:ORDER|LIMIT|HAVING|$)", sql, re.I | re.S)
    if group_match:
        for token in re.findall(r'\b([a-z_][a-z0-9_]*)\b', group_match.group(1), re.I):
            columns.add(token)

    return columns


def build_cross_map(config_dir: Path | None = None) -> dict:
    """Build a map of which metrics/columns appear in which dashboards."""
    config_dir = config_dir or CONFIG_DIR
    configs = sorted(config_dir.glob("*.json"))

    # column → list of {dashboard, panel_title, panel_index, usage}
    column_map: dict[str, list[dict]] = {}
    # panel_title → list of {dashboard, query}
    title_map: dict[str, list[dict]] = {}
    # query_pattern → list of {dashboard, panel_title}
    query_patterns: dict[str, list[dict]] = {}

    for config_path in configs:
        with open(config_path) as f:
            config = json.load(f)

        dashboard_name = config["title"]
        dashboard_slug = config_path.stem

        for i, panel in enumerate(config.get("panels", [])):
            sql = panel.get("query", "")
            panel_title = panel.get("title", f"panel_{i}")

            # Column extraction
            columns = _extract_columns_from_sql(sql)
            for col in columns:
                if col not in column_map:
                    column_map[col] = []
                column_map[col].append({
                    "dashboard": dashboard_name,
                    "dashboard_slug": dashboard_slug,
                    "panel_title": panel_title,
                    "panel_index": i,
                })

            # Title tracking
            if panel_title not in title_map:
                title_map[panel_title] = []
            title_map[panel_title].append({
                "dashboard": dashboard_name,
                "query": sql[:200],
            })

            # Normalize query for pattern matching (strip whitespace, lowercase)
            normalized = re.sub(r'\s+', ' ', sql.strip().upper())
            # Extract the core pattern (SELECT ... FROM ... GROUP BY ...)
            pattern_match = re.match(
                r"SELECT\s+.*?\s+FROM\s+(\S+).*?(GROUP\s+BY\s+\S+)?",
                normalized,
            )
            if pattern_match:
                pattern = f"FROM {pattern_match.group(1)}"
                if pattern_match.group(2):
                    pattern += f" {pattern_match.group(2)}"
                if pattern not in query_patterns:
                    query_patterns[pattern] = []
                query_patterns[pattern].append({
                    "dashboard": dashboard_name,
                    "panel_title": panel_title,
                })

    # Filter to only columns that appear in multiple dashboards
    shared_columns = {
        col: refs for col, refs in column_map.items()
        if len({r["dashboard"] for r in refs}) > 1
    }

    # Filter to duplicate panel titles
    duplicate_titles = {
        title: refs for title, refs in title_map.items()
        if len(refs) > 1
    }

    return {
        "shared_columns": shared_columns,
        "duplicate_titles": duplicate_titles,
        "query_patterns": query_patterns,
        "all_columns": {col: len(refs) for col, refs in column_map.items()},
    }


def save_cross_map(output_dir: Path | None = None, config_dir: Path | None = None) -> Path:
    """Build and save the cross-dashboard map."""
    output_dir = output_dir or OUTPUT_DIR
    out_path = output_dir / "_baseline" / "cross-dashboard-map.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cross_map = build_cross_map(config_dir)

    with open(out_path, "w") as f:
        json.dump(cross_map, f, indent=2)

    shared = len(cross_map["shared_columns"])
    dupes = len(cross_map["duplicate_titles"])
    print(f"  Cross-dashboard map: {shared} shared columns, {dupes} duplicate titles.")
    return out_path
