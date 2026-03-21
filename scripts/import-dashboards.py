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
    "area": "area-stacked",  # OO lacks plain 'area' type; area-stacked is visually identical for single-series
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

# Human-readable label overrides for common aliases
_LABEL_OVERRIDES = {
    "avg_lead_time_s": "Avg Lead Time (s)",
    "failure_pct": "Failure Rate (%)",
    "avg_recovery_s": "Avg Recovery Time (s)",
    "success_pct": "Success Rate (%)",
    "retry_pct": "Retry Rate (%)",
    "avg_ms": "Avg Duration (ms)",
    "avg_seconds": "Avg Duration (s)",
    "avg_us": "Avg Duration (µs)",
    "block_pct": "Block Rate (%)",
    "utilization_pct": "Budget Utilization (%)",
}


def _parse_select_columns(sql: str) -> list[tuple[str, str]]:
    """Extract (expression, alias) pairs from a SELECT clause.

    Returns a list of ``(expression_text, alias)`` tuples in column order.
    Handles ``expr as alias`` and bare ``column`` forms.
    Uses parenthesis-depth tracking to correctly split on commas.
    """
    m = re.search(r"SELECT\s+(.*?)\s+FROM\s", sql, re.I | re.S)
    if not m:
        return []
    select_body = m.group(1)

    parts: list[str] = []
    depth = 0
    current = ""
    for ch in select_body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(current.strip())
            current = ""
            continue
        current += ch
    if current.strip():
        parts.append(current.strip())

    result: list[tuple[str, str]] = []
    for part in parts:
        m2 = re.search(r"\bas\s+(\w+)\s*$", part, re.I)
        if m2:
            expr = part[: m2.start()].strip()
            result.append((expr, m2.group(1)))
        else:
            token = part.strip().split(".")[-1].strip()
            result.append((part.strip(), token))
    return result


def _parse_select_aliases(sql: str) -> list[str]:
    """Extract column aliases from a SELECT clause.

    Delegates to ``_parse_select_columns`` and returns only the alias names.
    """
    return [alias for _, alias in _parse_select_columns(sql)]


def _detect_agg(expr_upper: str) -> str | None:
    """Return the aggregate function name if the expression uses one."""
    for fn in ("COUNT", "SUM", "AVG", "MIN", "MAX"):
        if fn + "(" in expr_upper:
            return fn.lower()
    return None


def _extract_group_by_columns(sql: str) -> list[str]:
    """Extract column names/aliases from the GROUP BY clause."""
    m = re.search(r"GROUP\s+BY\s+(.*?)(?:\s+ORDER\s+|\s+LIMIT\s+|\s+HAVING\s+|$)", sql, re.I | re.S)
    if not m:
        return []
    return [col.strip() for col in m.group(1).split(",")]


def _make_fields(sql: str, panel_type: str) -> dict:
    """Build the ``fields`` dict with proper x/y/z axis definitions.

    Classification logic:
    - **x-axis**: aliases matching time dimensions (_timestamp, day, hour, week, month)
    - **z-axis (breakdown)**: non-time, non-aggregate aliases that appear in GROUP BY
    - **y-axis**: everything else (aggregates, computed metrics)

    OpenObserve requires non-empty ``x`` and ``y`` arrays for panels to
    render, even when ``customQuery: true``.
    """
    columns = _parse_select_columns(sql)
    group_by_cols = _extract_group_by_columns(sql)

    _TIME_ALIASES = {"_timestamp", "day", "hour", "week", "month"}

    # Determine if query has a time dimension — controls GROUP BY routing
    has_time = any(alias in _TIME_ALIASES for _, alias in columns)

    x_fields: list[dict] = []
    y_fields: list[dict] = []
    z_fields: list[dict] = []

    def _label(alias: str) -> str:
        return _LABEL_OVERRIDES.get(alias, alias.replace("_", " ").title())

    for expr, alias in columns:
        is_time = alias in _TIME_ALIASES
        is_agg = _detect_agg(expr.upper()) is not None
        in_group_by = alias in group_by_cols

        if is_time:
            # x-axis: time dimensions
            x_fields.append({
                "label": _label(alias),
                "alias": alias,
                "column": alias,
                "color": None,
                "aggregationFunction": "min" if alias == "_timestamp" else None,
            })
        elif not is_agg and in_group_by:
            if has_time:
                # z-axis: breakdown dimension alongside a time x-axis (multi-series)
                z_fields.append({
                    "label": _label(alias),
                    "alias": alias,
                    "column": alias,
                    "color": None,
                    "aggregationFunction": None,
                })
            else:
                # x-axis: categorical dimension (no time → bar/pie by category)
                # aggregationFunction="count" signals OO this is a categorical dimension
                x_fields.append({
                    "label": _label(alias),
                    "alias": alias,
                    "column": alias,
                    "color": None,
                    "aggregationFunction": "count",
                })
        else:
            # y-axis: aggregates and computed metrics
            agg_fn = _detect_agg(expr.upper()) or "count"
            y_fields.append({
                "label": _label(alias),
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
    if not y_fields and columns:
        last_alias = columns[-1][1]
        y_fields.append({
            "label": last_alias.replace("_", " ").title(),
            "alias": last_alias,
            "column": last_alias,
            "color": _COLORS[0],
            "aggregationFunction": "count",
        })

    return {
        "stream": "default",
        "stream_type": "traces",
        "x": x_fields,
        "y": y_fields,
        "z": z_fields,
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
    # Skip flat detail listings (LIMIT implies top-N, not aggregate)
    if "LIMIT" in upper:
        return sql
    has_agg = any(fn in upper for fn in ("COUNT(", "SUM(", "AVG(", "MIN(", "MAX("))
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

    # Apply per-panel color overrides from config
    color_overrides = panel.get("colors", {})
    if color_overrides:
        for y_field in fields["y"]:
            if y_field["alias"] in color_overrides:
                y_field["color"] = color_overrides[y_field["alias"]]

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
            "x": 0,
            "y": idx * 18,
            "w": 192,
            "h": 18,
            "i": idx + 1,
        },
    }


def _get_query_fields(panel: dict) -> dict:
    """Get field definitions from the query level (where OO stores them).

    OO empties panel-level ``fields`` for ``customQuery`` panels but preserves
    ``queries[0].fields``.
    """
    queries = panel.get("queries") or [{}]
    return queries[0].get("fields", {})


def _detect_drift(sent_panels: list[dict], stored_panels: list[dict]) -> list[dict]:
    """Compare sent vs stored panels for OO config drift.

    Compares query-level field definitions (where OO stores them) and query text.
    """
    drifts: list[dict] = []
    sent_by_id = {p["id"]: p for p in sent_panels}
    stored_by_id = {p["id"]: p for p in stored_panels}

    for panel_id, sent in sent_by_id.items():
        stored = stored_by_id.get(panel_id)
        if not stored:
            drifts.append({"panel": panel_id, "type": "missing"})
            continue

        # Compare query-level fields (where OO stores them)
        sent_fields = _get_query_fields(sent)
        stored_fields = _get_query_fields(stored)

        for axis in ("x", "y", "z"):
            sent_aliases = sorted(f["alias"] for f in sent_fields.get(axis, []))
            stored_aliases = sorted(f["alias"] for f in stored_fields.get(axis, []))
            if sent_aliases != stored_aliases:
                drifts.append({
                    "panel": panel_id, "type": "fields", "axis": axis,
                    "sent": sent_aliases, "stored": stored_aliases,
                })

        # Compare query text
        sent_q = (sent.get("queries") or [{}])[0].get("query", "")
        stored_q = (stored.get("queries") or [{}])[0].get("query", "")
        if sent_q.strip() != stored_q.strip():
            drifts.append({
                "panel": panel_id, "type": "query",
                "sent_preview": sent_q[:80], "stored_preview": stored_q[:80],
            })

    return drifts


def _patch_dashboard(dash_id: str, sent_panels: list[dict]) -> None:
    """Verify stored dashboard matches what we sent. Patch if drifted.

    OO stores field definitions in ``queries[0].fields`` (not panel-level).
    If drift is detected, overlays our definitions and PUTs back with the
    required ``hash`` for optimistic concurrency.
    """
    stored = _api("GET", f"dashboards/{dash_id}")
    if isinstance(stored, str):
        print(f"    WARN: Could not GET for verification: {stored}")
        return

    v8 = stored.get("v8", {})
    stored_tabs = v8.get("tabs", [])
    if not stored_tabs:
        return
    stored_panels = stored_tabs[0].get("panels", [])

    drifts = _detect_drift(sent_panels, stored_panels)
    if not drifts:
        print(f"    VERIFIED: no drift")
        return

    # Report drift
    for d in drifts:
        if d["type"] == "fields":
            print(f"    DRIFT: {d['panel']} {d['axis']}-axis: sent={d['sent']} stored={d['stored']}")
        elif d["type"] == "query":
            print(f"    DRIFT: {d['panel']} query rewritten")
        else:
            print(f"    DRIFT: {d['panel']} {d['type']}")

    # Overlay our field definitions and queries onto stored panels
    sent_by_id = {p["id"]: p for p in sent_panels}
    for sp in stored_panels:
        original = sent_by_id.get(sp["id"])
        if not original:
            continue
        # Patch query-level fields and query text
        if original.get("queries") and sp.get("queries"):
            for sq, oq in zip(sp["queries"], original["queries"]):
                sq["fields"] = oq["fields"]
                sq["query"] = oq["query"]

    # PUT back with hash for optimistic concurrency
    put_body = v8.copy()
    put_body["hash"] = stored.get("hash", "")
    result = _api("PUT", f"dashboards/{dash_id}", put_body)
    if isinstance(result, str):
        print(f"    WARN: PATCH PUT failed: {result}")
        return
    print(f"    PATCHED: corrected {len(drifts)} drift(s)")

    # Verify patch stuck
    verify = _api("GET", f"dashboards/{dash_id}")
    if isinstance(verify, str):
        return
    verify_panels = verify.get("v8", {}).get("tabs", [{}])[0].get("panels", [])
    re_drifts = _detect_drift(sent_panels, verify_panels)
    if re_drifts:
        print(f"    WARN: {len(re_drifts)} drift(s) persist after PATCH — OO rewrites on save")
        for d in re_drifts:
            if d["type"] == "fields":
                print(f"      {d['panel']} {d['axis']}-axis: wanted={d['sent']} got={d['stored']}")


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
        "defaultDatetimeDuration": {
            "type": "relative",
            "relativeTimePeriod": "30d",
        },
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

    # Verify and patch OO config drift
    _patch_dashboard(dash_id, panels)

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
