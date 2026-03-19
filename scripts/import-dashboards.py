"""Import dev-loop dashboards into OpenObserve.

Reads config/dashboards/*.json (custom format), translates each panel
into OpenObserve v8 dashboard format, and creates via POST API.

Usage:
    uv run python scripts/import-dashboards.py
    uv run python scripts/import-dashboards.py --delete-existing
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import urllib.request
from pathlib import Path

DASHBOARDS_DIR = Path(__file__).parent.parent / "config" / "dashboards"
OO_URL = os.environ.get("OPENOBSERVE_URL", "http://localhost:5080")
OO_USER = os.environ.get("OPENOBSERVE_USER", "admin@dev-loop.local")
OO_PASS = os.environ.get("OPENOBSERVE_PASS", "devloop123")
OO_ORG = os.environ.get("OPENOBSERVE_ORG", "default")

CREDS = base64.b64encode(f"{OO_USER}:{OO_PASS}".encode()).decode()

EMPTY_FILTER = {"filterType": "group", "logicalOperator": "AND", "conditions": []}

# Panel type mapping: our custom types → OpenObserve chart types
PANEL_TYPE_MAP = {
    "metric": "metric",
    "bar": "bar",
    "line": "line",
    "pie": "pie",
    "table": "table",
}


def _api(method: str, path: str, data: dict | None = None) -> dict | str:
    url = f"{OO_URL}/api/{OO_ORG}/{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(
        url, data=body, method=method,
        headers={"Content-Type": "application/json", "Authorization": f"Basic {CREDS}"},
    )
    try:
        resp = urllib.request.urlopen(req)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read().decode()[:200]}"


import re


# Colours for y-axis series
_COLORS = ["#5960b2", "#e8854a", "#54a24b", "#b279a2", "#4c78a8", "#f58518"]


def _parse_select_aliases(sql: str) -> list[str]:
    """Extract column aliases from a SELECT clause.

    Returns a list of alias names in order.  Handles ``expr as alias``
    and bare ``column`` forms.
    """
    # Grab text between SELECT and FROM
    m = re.search(r"SELECT\s+(.*?)\s+FROM\s", sql, re.I | re.S)
    if not m:
        return []
    select_body = m.group(1)

    aliases: list[str] = []
    depth = 0
    current = ""
    for ch in select_body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            aliases.append(current.strip())
            current = ""
            continue
        current += ch
    if current.strip():
        aliases.append(current.strip())

    result = []
    for part in aliases:
        # "expr as alias" — take alias
        m2 = re.search(r"\bas\s+(\w+)\s*$", part, re.I)
        if m2:
            result.append(m2.group(1))
        else:
            # bare column or "table.column"
            token = part.strip().split(".")[-1].strip()
            result.append(token)
    return result


def _detect_agg(expr_upper: str) -> str | None:
    """Return the aggregate function name if the expression uses one."""
    for fn in ("COUNT", "SUM", "AVG", "MIN", "MAX", "ROUND"):
        if fn + "(" in expr_upper:
            return fn.lower()
    return None


def _make_fields(sql: str, panel_type: str) -> dict:
    """Build the ``fields`` dict with proper x/y axis definitions.

    OpenObserve requires non-empty ``x`` and ``y`` arrays for panels to
    render, even when ``customQuery: true``.
    """
    aliases = _parse_select_aliases(sql)
    upper = sql.upper()
    has_group_by = "GROUP BY" in upper

    x_fields: list[dict] = []
    y_fields: list[dict] = []

    for alias in aliases:
        is_ts = alias == "_timestamp" or alias in ("hour", "day", "week", "month")
        # Check if this alias is an aggregate by looking at its expression
        # in the original SQL
        m = re.search(
            rf"([\w()./*+\- ]+)\s+as\s+{re.escape(alias)}\b",
            sql, re.I,
        )
        expr = m.group(1).upper().strip() if m else ""
        is_agg = _detect_agg(expr) is not None

        if is_ts or (not is_agg and has_group_by and alias not in ("_timestamp",)):
            # x-axis: time columns or GROUP BY dimensions
            x_fields.append({
                "label": alias.replace("_", " ").title(),
                "alias": alias,
                "column": alias,
                "color": None,
                "aggregationFunction": "min" if is_ts else None,
            })
        else:
            # y-axis: aggregate / value columns
            agg_fn = _detect_agg(expr) or "count"
            y_fields.append({
                "label": alias.replace("_", " ").title(),
                "alias": alias,
                "column": alias,
                "color": _COLORS[len(y_fields) % len(_COLORS)],
                "aggregationFunction": agg_fn,
            })

    # Ensure at least one x (timestamp) and one y
    if not x_fields:
        x_fields.append({
            "label": "Timestamp",
            "alias": "_timestamp",
            "column": "_timestamp",
            "color": None,
            "aggregationFunction": "min",
        })
    if not y_fields and aliases:
        # Use last alias as y
        y_fields.append({
            "label": aliases[-1].replace("_", " ").title(),
            "alias": aliases[-1],
            "column": aliases[-1],
            "color": _COLORS[0],
            "aggregationFunction": "count",
        })

    return {
        "stream": "default",
        "stream_type": "traces",
        "x": x_fields,
        "y": y_fields,
        "z": [],
        "filter": EMPTY_FILTER,
    }


def _fix_aggregate_timestamp(sql: str) -> str:
    """Fix OpenObserve aggregate query quirk.

    OpenObserve injects ``_timestamp`` into the result set via wildcard
    expansion.  When a query uses only aggregate functions (no GROUP BY),
    the bare ``_timestamp`` column violates the SQL rule that every
    selected column must be aggregated or grouped.

    Fix: prepend ``MIN(_timestamp) as _timestamp,`` to the SELECT list
    of pure-aggregate queries (those with an aggregate function but no
    GROUP BY clause).
    """
    upper = sql.upper()
    has_agg = any(fn in upper for fn in ("COUNT(", "SUM(", "AVG(", "MIN(", "MAX(", "ROUND("))
    has_group_by = "GROUP BY" in upper
    already_has_ts_agg = "MIN(_TIMESTAMP)" in upper or "MAX(_TIMESTAMP)" in upper
    if has_agg and not has_group_by and not already_has_ts_agg:
        # Insert MIN(_timestamp) right after SELECT
        idx = upper.index("SELECT") + len("SELECT")
        sql = sql[:idx] + " MIN(_timestamp) as _timestamp," + sql[idx:]
    return sql


def _translate_panel(panel: dict, idx: int) -> dict:
    """Convert our custom panel format to OpenObserve v8 panel."""
    query = _fix_aggregate_timestamp(panel["query"])
    ptype = PANEL_TYPE_MAP.get(panel["type"], "table")
    fields = _make_fields(query, ptype)
    return {
        "id": f"panel_{panel['id']}",
        "type": ptype,
        "title": panel["title"],
        "description": "",
        "queryType": "sql",
        "fields": fields,
        "queries": [{
            "query": query,
            "customQuery": True,
            "fields": fields,
            "config": {
                "promql_legend": "",
                "layer_type": "default",
                "limit": 0,
                "time_shift": [],
            },
        }],
        "config": {
            "show_legends": True,
            "legends_position": "bottom",
        },
        "layout": {
            "x": (idx % 2) * 6,
            "y": (idx // 2) * 5,
            "w": 6 if panel["type"] != "line" else 12,
            "h": 4 if panel["type"] == "metric" else 6,
            "i": idx + 1,
        },
    }


def delete_existing():
    """Delete all existing dev-loop dashboards."""
    result = _api("GET", "dashboards")
    if isinstance(result, str):
        print(f"  Failed to list dashboards: {result}")
        return
    for d in result.get("dashboards", []):
        v8 = d.get("v8") or {}
        dash_id = v8.get("dashboardId", "")
        title = v8.get("title", "")
        if not dash_id:
            continue
        _api("DELETE", f"dashboards/{dash_id}")
        print(f"  Deleted: {title} ({dash_id})")


def import_dashboard(path: Path) -> str | None:
    """Import a single dashboard JSON file. Returns dashboard ID or None."""
    with open(path) as f:
        config = json.load(f)

    panels = [_translate_panel(p, i) for i, p in enumerate(config["panels"])]

    dashboard = {
        "title": config["title"],
        "description": config.get("description", ""),
        "tabs": [{
            "tabId": "default",
            "name": "Overview",
            "panels": panels,
        }],
    }

    result = _api("POST", "dashboards", dashboard)
    if isinstance(result, str):
        print(f"  FAILED: {path.name} → {result}")
        return None

    dash_id = result.get("v8", {}).get("dashboardId", "?")
    tab_count = len(result.get("v8", {}).get("tabs", []))
    panel_count = len(result["v8"]["tabs"][0]["panels"]) if tab_count else 0
    print(f"  OK: {config['title']} → {dash_id} ({panel_count} panels)")
    return dash_id


def main():
    parser = argparse.ArgumentParser(description="Import dashboards into OpenObserve")
    parser.add_argument("--delete-existing", action="store_true", help="Delete existing dashboards first")
    args = parser.parse_args()

    print(f"OpenObserve: {OO_URL}/api/{OO_ORG}")

    if args.delete_existing:
        print("Deleting existing dashboards...")
        delete_existing()

    print(f"Importing dashboards from {DASHBOARDS_DIR}...")
    for path in sorted(DASHBOARDS_DIR.glob("*.json")):
        import_dashboard(path)

    print("Done.")


if __name__ == "__main__":
    main()
