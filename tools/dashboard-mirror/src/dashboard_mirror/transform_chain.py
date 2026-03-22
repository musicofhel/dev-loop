"""Capture the full transformation chain for each dashboard.

Traces how each panel config transforms through 4 stages:
  1. source.json    — raw config/dashboards/*.json (our custom format)
  2. transformed.json — after _translate_panel() in import script
  3. sent.json      — the full POST payload sent to OO
  4. stored.json    — what OO GET returns (already captured by collect.py)

Produces a chain-diff.txt showing mutations at each stage.

Usage:
    uv run dm-chain
    uv run dm-chain --config-dir ~/dev-loop/config/dashboards
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from difflib import unified_diff
from pathlib import Path

from .config import CONFIG_DIR, OUTPUT_DIR


def _load_import_script() -> object:
    """Dynamically load the import-dashboards.py script to access its transform functions."""
    script_path = CONFIG_DIR.parent.parent / "scripts" / "import-dashboards.py"
    if not script_path.exists():
        print(f"  Import script not found: {script_path}")
        return None

    spec = importlib.util.spec_from_file_location("import_dashboards", script_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _json_diff(a: dict, b: dict, label_a: str, label_b: str) -> str:
    """Produce a unified diff between two JSON structures."""
    a_lines = json.dumps(a, indent=2, sort_keys=True, default=str).splitlines(keepends=True)
    b_lines = json.dumps(b, indent=2, sort_keys=True, default=str).splitlines(keepends=True)
    diff = unified_diff(a_lines, b_lines, fromfile=label_a, tofile=label_b)
    return "".join(diff)


def process_dashboard(config_path: Path, output_dir: Path, mod: object) -> None:
    """Process one dashboard config file through the transformation chain."""
    with open(config_path) as f:
        source_config = json.load(f)

    slug = config_path.stem
    dash_output = output_dir / slug / "config"
    dash_output.mkdir(parents=True, exist_ok=True)

    # Stage 1: Source
    with open(dash_output / "source.json", "w") as f:
        json.dump(source_config, f, indent=2)

    if mod is None:
        print(f"  [{slug}] Skipping transform chain — import script not loaded.")
        return

    # Stage 2: Transformed panels (what _translate_panel produces)
    transformed_panels = []
    for i, panel in enumerate(source_config.get("panels", [])):
        try:
            translated = mod._translate_panel(panel, i)
            transformed_panels.append(translated)
        except Exception as e:
            transformed_panels.append({"error": str(e), "source_panel": panel})

    with open(dash_output / "transformed.json", "w") as f:
        json.dump(transformed_panels, f, indent=2)

    # Stage 3: Sent payload (what import_dashboard() would POST)
    sent_payload = {
        "title": source_config["title"],
        "description": source_config.get("description", ""),
        "defaultDatetimeDuration": {
            "type": "relative",
            "relativeTimePeriod": "30d",
        },
        "tabs": [{
            "tabId": "default",
            "name": "Overview",
            "panels": transformed_panels,
        }],
    }

    with open(dash_output / "sent.json", "w") as f:
        json.dump(sent_payload, f, indent=2)

    # Stage 4: stored.json is already captured by collect.py — read it if present
    stored_path = dash_output / "stored.json"
    stored = None
    if stored_path.exists():
        with open(stored_path) as f:
            stored = json.load(f)

    # Generate chain diff
    diff_parts = []
    diff_parts.append("=" * 72)
    diff_parts.append(f"TRANSFORMATION CHAIN: {source_config['title']}")
    diff_parts.append(f"Source: {config_path}")
    diff_parts.append("=" * 72)

    # Source → Transformed (per panel)
    diff_parts.append("\n--- Stage 1→2: Source panels → Translated panels ---\n")
    for i, (src_panel, trans_panel) in enumerate(zip(source_config.get("panels", []), transformed_panels)):
        panel_diff = _json_diff(src_panel, trans_panel, f"source/panel[{i}]", f"transformed/panel[{i}]")
        if panel_diff:
            diff_parts.append(f"Panel {i} ({src_panel.get('title', '?')}):")
            diff_parts.append(panel_diff)
        else:
            diff_parts.append(f"Panel {i}: no diff (identical)")

    # Transformed → Stored (if available)
    if stored:
        diff_parts.append("\n--- Stage 3→4: Sent payload → OO stored ---\n")
        stored_v8 = stored.get("v8", stored)
        sent_diff = _json_diff(sent_payload, stored_v8, "sent", "stored")
        if sent_diff:
            diff_parts.append(sent_diff)
        else:
            diff_parts.append("No diff — OO stored exactly what was sent.")
    else:
        diff_parts.append("\n--- Stage 3→4: stored.json not yet captured (run dm-collect first) ---\n")

    chain_diff = "\n".join(diff_parts)
    with open(dash_output / "chain-diff.txt", "w") as f:
        f.write(chain_diff)

    panel_count = len(source_config.get("panels", []))
    print(f"  [{slug}] {panel_count} panels — chain diff written.")


def main():
    parser = argparse.ArgumentParser(description="Capture dashboard transformation chain diffs")
    parser.add_argument("--config-dir", type=Path, default=CONFIG_DIR, help="Dashboard config directory")
    parser.add_argument("--output", type=Path, default=OUTPUT_DIR, help="Output directory")
    args = parser.parse_args()

    config_dir = args.config_dir
    output_dir = args.output

    if not config_dir.exists():
        print(f"Config directory not found: {config_dir}")
        sys.exit(1)

    configs = sorted(config_dir.glob("*.json"))
    if not configs:
        print(f"No dashboard configs found in {config_dir}")
        sys.exit(1)

    print(f"Transform chain analysis — {len(configs)} dashboards from {config_dir}")

    # Load the import script for transformation functions
    mod = _load_import_script()
    if mod:
        print("  Import script loaded for transformation replay.")

    for config_path in configs:
        process_dashboard(config_path, output_dir, mod)

    print(f"\nDone. Chain diffs in {output_dir}/*/config/chain-diff.txt")


if __name__ == "__main__":
    main()
