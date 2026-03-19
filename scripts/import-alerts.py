"""Import dev-loop alert rules into OpenObserve.

Reads config/alerts/rules.yaml, translates each rule into
OpenObserve v2 alert API format, and creates via POST.

Requires an alert destination to exist first. Creates a default
'dev-loop-log' webhook destination that logs alerts back to
OpenObserve as a stream if one doesn't exist.

Usage:
    uv run python scripts/import-alerts.py
    uv run python scripts/import-alerts.py --delete-existing
    uv run python scripts/import-alerts.py --dry-run
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import urllib.request
from pathlib import Path

import yaml

ALERTS_FILE = Path(__file__).parent.parent / "config" / "alerts" / "rules.yaml"
OO_URL = os.environ.get("OPENOBSERVE_URL", "http://localhost:5080")
OO_USER = os.environ.get("OPENOBSERVE_USER", "admin@dev-loop.local")
OO_PASS = os.environ.get("OPENOBSERVE_PASS", "devloop123")
OO_ORG = os.environ.get("OPENOBSERVE_ORG", "default")
DESTINATION_NAME = "dev-loop-log"

CREDS = base64.b64encode(f"{OO_USER}:{OO_PASS}".encode()).decode()
AUTH_HEADERS = {"Content-Type": "application/json", "Authorization": f"Basic {CREDS}"}

SEVERITY_MAP = {"warning": "P2", "critical": "P1", "info": "P3"}


def _request(method: str, url: str, data: dict | None = None) -> dict | str:
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(
        url, data=body, method=method, headers=AUTH_HEADERS,
    )
    try:
        resp = urllib.request.urlopen(req)  # nosemgrep: dynamic-urllib-use-detected
        raw = resp.read()
        return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read().decode()[:200]}"


def _api_v1(method: str, path: str, data: dict | None = None) -> dict | str:
    """Call /api/{org}/{path} (v1 style, used for destinations/templates)."""
    return _request(method, f"{OO_URL}/api/{OO_ORG}/{path}", data)


def _api_v2(method: str, path: str, data: dict | None = None) -> dict | str:
    """Call /api/v2/{org}/{path} (v2 style, used for alerts CRUD)."""
    return _request(method, f"{OO_URL}/api/v2/{OO_ORG}/{path}", data)


def ensure_destination() -> bool:
    """Ensure the dev-loop-log alert destination exists. Returns True if ready."""
    result = _api_v1("GET", "alerts/destinations")
    if isinstance(result, str):
        print(f"  Warning: could not list destinations: {result}")
        # Try creating anyway
    else:
        destinations = result if isinstance(result, list) else result.get("list", result)
        if isinstance(destinations, list):
            for d in destinations:
                if d.get("name") == DESTINATION_NAME:
                    return True

    # Create the destination: webhook that logs alerts back to an OO stream
    dest = {
        "name": DESTINATION_NAME,
        "url": f"{OO_URL}/api/{OO_ORG}/devloop_alerts/_json",
        "method": "post",
        "template": "prebuilt_webhook",
        "headers": {"Authorization": f"Basic {CREDS}"},
    }
    result = _api_v1("POST", "alerts/destinations", dest)
    if isinstance(result, str):
        print(f"  Failed to create destination: {result}")
        return False
    print(f"  Created alert destination '{DESTINATION_NAME}' (alerts → OO stream)")
    return True


def _translate_alert(rule: dict) -> dict:
    """Convert our YAML alert rule to OpenObserve v2 alert API format."""
    condition = rule["condition"]
    return {
        "name": rule["name"],
        "stream_type": "traces",
        "stream_name": "default",
        "is_real_time": False,
        "query_condition": {
            "sql": condition["sql"].strip(),
            "conditions": [],
            "type": "sql",
        },
        "trigger_condition": {
            "threshold": condition["threshold"],
            "operator": ">=",
            "period": rule.get("frequency_minutes", 5),
            "frequency_type": "minutes",
        },
        "duration": {
            "value": rule.get("frequency_minutes", 5),
            "unit": "Minutes",
        },
        "frequency": {
            "value": rule.get("frequency_minutes", 5),
            "unit": "Minutes",
        },
        "enabled": True,
        "description": rule.get("description", ""),
        "priority": SEVERITY_MAP.get(rule.get("severity", "warning"), "P2"),
        "destinations": [DESTINATION_NAME],
    }


def delete_existing():
    """Delete all existing dev-loop alerts."""
    result = _api_v2("GET", "alerts")
    if isinstance(result, str):
        print(f"  Failed to list alerts: {result}")
        return
    alerts = result if isinstance(result, list) else result.get("list", [])
    for alert in alerts:
        alert_id = alert.get("alert_id", "")
        name = alert.get("name", "?")
        if alert_id:
            _api_v2("DELETE", f"alerts/{alert_id}")
            print(f"  Deleted: {name} ({alert_id})")
        else:
            print(f"  Warning: alert '{name}' has no alert_id, cannot delete")


def import_alerts(dry_run: bool = False) -> int:
    """Import alert rules. Returns count of imported alerts."""
    if not ALERTS_FILE.exists():
        print(f"No alerts file found at {ALERTS_FILE}")
        return 0

    with open(ALERTS_FILE) as f:
        config = yaml.safe_load(f)

    rules = config.get("alerts", [])
    if not rules:
        print("No alert rules found in config.")
        return 0

    imported = 0
    for rule in rules:
        alert = _translate_alert(rule)
        if dry_run:
            print(f"  [DRY RUN] {rule['name']}:")
            print(f"    {json.dumps(alert, indent=2)}")
            imported += 1
            continue

        result = _api_v2("POST", "alerts", alert)
        if isinstance(result, str):
            print(f"  FAILED: {rule['name']} → {result}")
        else:
            print(f"  OK: {rule['name']} (severity: {rule.get('severity', '?')}, "
                  f"every {rule.get('frequency_minutes', '?')}min)")
            imported += 1

    return imported


def main():
    parser = argparse.ArgumentParser(description="Import alert rules into OpenObserve")
    parser.add_argument("--delete-existing", action="store_true", help="Delete existing alerts first")
    parser.add_argument("--dry-run", action="store_true", help="Print translated JSON without posting")
    args = parser.parse_args()

    print(f"OpenObserve: {OO_URL} (org: {OO_ORG})")

    if not args.dry_run:
        print("Ensuring alert destination exists...")
        if not ensure_destination():
            print("Cannot proceed without a destination. Aborting.")
            return

    if args.delete_existing and not args.dry_run:
        print("Deleting existing alerts...")
        delete_existing()

    print(f"Importing alerts from {ALERTS_FILE}...")
    count = import_alerts(dry_run=args.dry_run)
    print(f"Done. {count} alert(s) {'would be ' if args.dry_run else ''}imported.")


if __name__ == "__main__":
    main()
