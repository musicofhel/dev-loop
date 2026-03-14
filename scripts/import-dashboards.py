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


def _make_fields() -> dict:
    return {
        "stream": "default",
        "stream_type": "traces",
        "x": [], "y": [], "z": [],
        "filter": EMPTY_FILTER,
    }


def _translate_panel(panel: dict, idx: int) -> dict:
    """Convert our custom panel format to OpenObserve v8 panel."""
    fields = _make_fields()
    return {
        "id": f"panel_{panel['id']}",
        "type": PANEL_TYPE_MAP.get(panel["type"], "table"),
        "title": panel["title"],
        "description": "",
        "queryType": "sql",
        "fields": fields,
        "queries": [{
            "query": panel["query"],
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
        v8 = d.get("v8", {})
        dash_id = v8.get("dashboardId", "")
        title = v8.get("title", "")
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
