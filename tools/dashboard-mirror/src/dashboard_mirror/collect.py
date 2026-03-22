"""Phase 1: Collect mirror data from OpenObserve dashboards.

Launches Playwright, logs into OO, and for each dashboard captures:
  - Screenshots (full-page, viewport stops, per-panel)
  - DOM text content (titles, labels, legends, axes, errors)
  - Layout metrics (pixel dimensions, positions, visibility)
  - Chart data (series counts, data points, colors)
  - API responses (intercepted search queries + results)
  - Console errors and network failures
  - Per-panel load timing

Repeats screenshot capture at multiple time ranges (1h, 7d, 30d).

Usage:
    uv run dm-collect
    uv run dm-collect --dashboard agent-performance --dashboard loop-health
    uv run dm-collect --url http://oo.example.com:5080
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, Page, BrowserContext

from . import config as cfg


def slugify(title: str) -> str:
    """Convert dashboard title to filesystem-safe slug."""
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def api_get(context: BrowserContext, path: str) -> dict | list | str:
    """Make an authenticated GET request via Playwright's request context."""
    resp = context.request.get(
        f"{cfg.OO_URL}/api/{cfg.OO_ORG}/{path}",
        headers={"Content-Type": "application/json"},
    )
    if resp.ok:
        return resp.json()
    return f"HTTP {resp.status}: {resp.text()[:200]}"


def list_dashboards(context: BrowserContext) -> list[dict]:
    """Fetch all dashboards from OO, return list of {id, title, slug}."""
    result = api_get(context, "dashboards")
    if isinstance(result, str):
        print(f"  Failed to list dashboards: {result}")
        return []

    dashboards = []
    for d in result.get("dashboards", []):
        v8 = d.get("v8") or {}
        dash_id = v8.get("dashboardId", "")
        title = v8.get("title", "")
        if not dash_id or not title:
            continue
        dashboards.append({
            "id": dash_id,
            "title": title,
            "slug": slugify(title),
            "v8": v8,
        })
    return dashboards


def login(page: Page) -> None:
    """Log into OpenObserve."""
    page.goto(f"{cfg.OO_URL}/web/login")
    page.wait_for_load_state("networkidle")

    # Fill credentials
    page.fill('input[type="email"]', cfg.OO_USER)
    page.fill('input[type="password"]', cfg.OO_PASS)
    page.click('button[type="submit"]')

    # Wait for redirect to main app
    page.wait_for_url(re.compile(r"/web/"), timeout=15000)
    page.wait_for_load_state("networkidle")
    print("  Logged in.")


def set_time_range(page: Page, period: str) -> None:
    """Set the dashboard time picker to a relative period.

    Clicks the date-time picker and selects the appropriate preset.
    """
    # Map our period labels to OO's UI button text
    period_map = {
        "1h": "Past 1 Hours",
        "7d": "Past 7 Days",
        "30d": "Past 30 Days",
    }
    label = period_map.get(period, f"Past {period}")

    # Click the date-time picker to open it
    picker = page.locator('[data-test="date-time-btn"]')
    if picker.count() > 0:
        picker.first.click()
        page.wait_for_timeout(500)

        # Look for the preset button
        preset = page.locator(f'text="{label}"').first
        if preset.is_visible():
            preset.click()
            page.wait_for_timeout(1000)
        else:
            # Try without exact match
            preset = page.get_by_text(label, exact=False).first
            if preset.is_visible():
                preset.click()
                page.wait_for_timeout(1000)
            else:
                print(f"    Could not find time range preset: {label}")


def wait_for_panels_loaded(page: Page, timeout: int = 30000) -> None:
    """Wait until panels finish loading (spinners disappear)."""
    start = time.time()
    deadline = start + timeout / 1000

    while time.time() < deadline:
        spinners = page.locator(".q-spinner, .panel-loading, [data-test='panel-loading']").count()
        if spinners == 0:
            break
        page.wait_for_timeout(500)

    # Extra settle time for chart rendering
    page.wait_for_timeout(2000)


def capture_screenshots(page: Page, out_dir: Path, label: str = "") -> None:
    """Capture full-page and viewport screenshots.

    Args:
        page: Playwright page positioned on a dashboard.
        out_dir: Directory to save screenshots.
        label: Optional subfolder label (e.g., '1h', '7d').
    """
    ss_dir = out_dir / f"screenshots{'-' + label if label else ''}"
    ss_dir.mkdir(parents=True, exist_ok=True)

    # Scroll through entire page first to trigger lazy-loaded panels
    viewport_height = page.viewport_size["height"]
    scroll_height = page.evaluate("document.documentElement.scrollHeight")
    pos = 0
    while pos < scroll_height:
        page.evaluate(f"window.scrollTo(0, {pos})")
        page.wait_for_timeout(1000)  # let lazy panels load + render
        pos += viewport_height
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(1500)  # settle after scroll-back

    # Full-page screenshot (all panels now rendered)
    page.screenshot(path=str(ss_dir / "full-page.png"), full_page=True)

    # Viewport stops — scroll down capturing at each stop
    scroll_height = page.evaluate("document.documentElement.scrollHeight")
    stop = 0
    idx = 1

    while stop < scroll_height:
        page.evaluate(f"window.scrollTo(0, {stop})")
        page.wait_for_timeout(800)
        page.screenshot(path=str(ss_dir / f"viewport-{idx:02d}.png"))
        stop += viewport_height
        idx += 1

    # Scroll back to top
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(500)


def capture_panel_screenshots(page: Page, out_dir: Path) -> None:
    """Capture individual panel screenshots by locating panel elements.

    Targets the panel body (chart area) when possible, falling back to the
    full grid item. Skips drag handles, refresh icons, and other sub-element
    noise that inflates the screenshot count.
    """
    ss_dir = out_dir / "screenshots"
    ss_dir.mkdir(parents=True, exist_ok=True)

    panels = page.locator(".gs-stack-item, .grid-stack-item, [data-test*='panel']")
    count = panels.count()

    for i in range(count):
        panel = panels.nth(i)
        try:
            panel.scroll_into_view_if_needed(timeout=5000)
            page.wait_for_timeout(1000)  # extra time for canvas paint
            bbox = panel.bounding_box()
            if not bbox or bbox["width"] <= 10 or bbox["height"] <= 10:
                continue

            # Try to capture just the chart body (skip drag handles, icons)
            body = panel.locator(".panelBody, .panel-body, [data-test*='chart'], canvas, svg").first
            if body.count() > 0:
                body_box = body.bounding_box()
                if body_box and body_box["width"] > 10 and body_box["height"] > 10:
                    body.screenshot(path=str(ss_dir / f"panel-{i + 1:02d}.png"))
                    continue

            # Fallback: capture the full panel element
            panel.screenshot(path=str(ss_dir / f"panel-{i + 1:02d}.png"))
        except Exception as e:
            print(f"    Panel {i + 1} screenshot failed: {e}")


def extract_dom_text(page: Page) -> dict:
    """Extract all visible text content from dashboard panels.

    Returns a dict with per-panel text: titles, labels, legends, values, errors.
    """
    return page.evaluate("""() => {
        const panels = document.querySelectorAll('.gs-stack-item, .grid-stack-item, [data-gs-id]');
        const results = [];

        panels.forEach((panel, idx) => {
            const data = {
                index: idx + 1,
                panelId: panel.getAttribute('data-gs-id') || panel.getAttribute('gs-id') || null,
                title: '',
                noData: false,
                errorText: '',
                legendEntries: [],
                axisLabels: { x: [], y: [] },
                allText: '',
            };

            // Title
            const titleEl = panel.querySelector('.panelTitle, [data-test*="title"], h6, .panel-header');
            if (titleEl) data.title = titleEl.textContent.trim();

            // No data indicator
            const noDataEl = panel.querySelector('.no-data, [data-test*="no-data"], .q-table__no-data');
            if (noDataEl) {
                data.noData = true;
                data.errorText = noDataEl.textContent.trim();
            }

            // Error messages
            const errorEl = panel.querySelector('.error-message, .alert-danger, .text-negative');
            if (errorEl) data.errorText = errorEl.textContent.trim();

            // Legend entries
            const legends = panel.querySelectorAll('.legendLabel, .legend-item, [data-test*="legend"]');
            legends.forEach(l => {
                const color = l.querySelector('[style*="background"]');
                const colorVal = color ? getComputedStyle(color).backgroundColor : null;
                data.legendEntries.push({
                    text: l.textContent.trim(),
                    color: colorVal,
                });
            });

            // Axis labels (SVG text elements commonly used by charting libs)
            const svgTexts = panel.querySelectorAll('svg text');
            svgTexts.forEach(t => {
                const text = t.textContent.trim();
                if (text) {
                    // Heuristic: y-axis labels are on the left side
                    const bbox = t.getBBox ? t.getBBox() : null;
                    if (bbox && bbox.x < 60) {
                        data.axisLabels.y.push(text);
                    } else {
                        data.axisLabels.x.push(text);
                    }
                }
            });

            // All text content (for completeness)
            data.allText = panel.textContent.replace(/\\s+/g, ' ').trim().substring(0, 2000);

            results.push(data);
        });

        return results;
    }""")


def extract_layout_metrics(page: Page) -> dict:
    """Extract computed layout metrics for each panel."""
    return page.evaluate("""() => {
        const panels = document.querySelectorAll('.gs-stack-item, .grid-stack-item, [data-gs-id]');
        const results = [];
        const viewportHeight = window.innerHeight;

        panels.forEach((panel, idx) => {
            const rect = panel.getBoundingClientRect();
            const style = getComputedStyle(panel);

            // Check for content overflow
            const isOverflowing = panel.scrollWidth > panel.clientWidth ||
                                  panel.scrollHeight > panel.clientHeight;

            // GridStack attributes
            const gsX = panel.getAttribute('gs-x') || panel.getAttribute('data-gs-x');
            const gsY = panel.getAttribute('gs-y') || panel.getAttribute('data-gs-y');
            const gsW = panel.getAttribute('gs-w') || panel.getAttribute('data-gs-w');
            const gsH = panel.getAttribute('gs-h') || panel.getAttribute('data-gs-h');

            results.push({
                index: idx + 1,
                panelId: panel.getAttribute('data-gs-id') || panel.getAttribute('gs-id') || null,
                pixel: {
                    x: Math.round(rect.x),
                    y: Math.round(rect.y + window.scrollY),
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                },
                grid: { x: gsX, y: gsY, w: gsW, h: gsH },
                visible: rect.top < viewportHeight && rect.bottom > 0,
                display: style.display,
                overflow: isOverflowing,
                opacity: style.opacity,
            });
        });

        // Grid meta
        const gridEl = document.querySelector('.grid-stack');
        let gridMeta = null;
        if (gridEl && gridEl.gridstack) {
            gridMeta = {
                column: gridEl.gridstack.getColumn(),
                cellHeight: gridEl.gridstack.opts.cellHeight,
            };
        }

        return { panels: results, gridMeta };
    }""")


def extract_chart_data(page: Page) -> list[dict]:
    """Extract chart-level data from SVG/Canvas elements inside panels."""
    return page.evaluate("""() => {
        const panels = document.querySelectorAll('.gs-stack-item, .grid-stack-item, [data-gs-id]');
        const results = [];

        panels.forEach((panel, idx) => {
            const data = {
                index: idx + 1,
                chartType: null,
                svgPresent: false,
                canvasPresent: false,
                pathCount: 0,
                rectCount: 0,
                circleCount: 0,
                seriesColors: [],
                dataPointEstimate: 0,
            };

            const svg = panel.querySelector('svg');
            const canvas = panel.querySelector('canvas');

            if (svg) {
                data.svgPresent = true;
                data.pathCount = svg.querySelectorAll('path').length;
                data.rectCount = svg.querySelectorAll('rect').length;
                data.circleCount = svg.querySelectorAll('circle').length;

                // Estimate data points from paths/rects
                data.dataPointEstimate = Math.max(data.pathCount, data.rectCount, data.circleCount);

                // Extract unique fill/stroke colors
                const colorSet = new Set();
                svg.querySelectorAll('path[fill], rect[fill], circle[fill]').forEach(el => {
                    const fill = el.getAttribute('fill');
                    if (fill && fill !== 'none' && fill !== 'transparent' && !fill.startsWith('url(')) {
                        colorSet.add(fill);
                    }
                });
                svg.querySelectorAll('path[stroke]').forEach(el => {
                    const stroke = el.getAttribute('stroke');
                    if (stroke && stroke !== 'none' && stroke !== 'transparent') {
                        colorSet.add(stroke);
                    }
                });
                data.seriesColors = Array.from(colorSet);

                // Try to detect chart type
                if (data.rectCount > 5) data.chartType = 'bar';
                else if (data.pathCount > 0) data.chartType = 'line/area';
                else if (data.circleCount > 5) data.chartType = 'scatter';
            }

            if (canvas) {
                data.canvasPresent = true;
                data.chartType = data.chartType || 'canvas-based';
            }

            results.push(data);
        });

        return results;
    }""")


def capture_api_responses(page: Page, dashboard_url: str, out_dir: Path) -> list[dict]:
    """Navigate to dashboard while intercepting API search calls.

    Returns list of intercepted query/response pairs.
    """
    captured = []
    errors = []

    def handle_response(response):
        url = response.url
        if "/_search" in url or "/search" in url:
            try:
                status = response.status
                body = response.json() if response.ok else response.text()
                captured.append({
                    "url": url,
                    "status": status,
                    "row_count": body.get("total", 0) if isinstance(body, dict) else None,
                    "columns": list(body.get("hits", [{}])[0].keys()) if isinstance(body, dict) and body.get("hits") else [],
                    "sample_rows": body.get("hits", [])[:3] if isinstance(body, dict) else [],
                    "took_ms": body.get("took", None) if isinstance(body, dict) else None,
                })
            except Exception:
                captured.append({
                    "url": url,
                    "status": response.status,
                    "error": "Could not parse response body",
                })

    def handle_console(msg):
        if msg.type in ("error", "warning"):
            errors.append({
                "type": msg.type,
                "text": msg.text,
            })

    page.on("response", handle_response)
    page.on("console", handle_console)

    page.goto(dashboard_url, wait_until="networkidle")
    wait_for_panels_loaded(page)

    # Scroll to trigger lazy-loaded panels
    scroll_height = page.evaluate("document.documentElement.scrollHeight")
    viewport_height = page.viewport_size["height"]
    pos = 0
    while pos < scroll_height:
        pos += viewport_height
        page.evaluate(f"window.scrollTo(0, {pos})")
        page.wait_for_timeout(1500)

    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(1000)

    # Save errors
    api_dir = out_dir / "api"
    api_dir.mkdir(parents=True, exist_ok=True)
    with open(api_dir / "errors.json", "w") as f:
        json.dump(errors, f, indent=2)

    page.remove_listener("response", handle_response)
    page.remove_listener("console", handle_console)

    return captured


def capture_timing(page: Page) -> list[dict]:
    """Measure per-panel load timing using Performance API entries."""
    return page.evaluate("""() => {
        const entries = performance.getEntriesByType('resource')
            .filter(e => e.name.includes('_search') || e.name.includes('/search'))
            .map(e => ({
                url: e.name,
                duration_ms: Math.round(e.duration),
                start_ms: Math.round(e.startTime),
            }));
        return entries;
    }""")


def collect_dashboard(
    page: Page,
    context: BrowserContext,
    dash: dict,
    output_base: Path,
) -> None:
    """Collect full mirror data for a single dashboard."""
    slug = dash["slug"]
    dash_id = dash["id"]
    out_dir = output_base / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n  [{slug}] Collecting...")
    dashboard_url = f"{cfg.OO_URL}/web/dashboards/view?org_identifier={cfg.OO_ORG}&dashboard={dash_id}"

    # --- API interception + initial load ---
    print(f"    Capturing API responses...")
    api_responses = capture_api_responses(page, dashboard_url, out_dir)

    api_dir = out_dir / "api"
    api_dir.mkdir(parents=True, exist_ok=True)
    with open(api_dir / "queries-executed.json", "w") as f:
        json.dump(api_responses, f, indent=2, default=str)

    # --- Timing ---
    print(f"    Capturing timing...")
    timing = capture_timing(page)
    with open(out_dir / "timing.json", "w") as f:
        json.dump(timing, f, indent=2)

    # --- DOM extraction ---
    print(f"    Extracting DOM text...")
    dom_dir = out_dir / "dom"
    dom_dir.mkdir(parents=True, exist_ok=True)

    # Wait for canvas charts to finish painting before DOM scrape
    try:
        page.wait_for_selector("canvas", timeout=5000)
    except Exception:
        pass  # Not all dashboards have canvas charts
    page.wait_for_timeout(2000)

    text_content = extract_dom_text(page)
    with open(dom_dir / "text-content.json", "w") as f:
        json.dump(text_content, f, indent=2)

    layout_metrics = extract_layout_metrics(page)
    with open(dom_dir / "layout-metrics.json", "w") as f:
        json.dump(layout_metrics, f, indent=2)

    chart_data = extract_chart_data(page)
    with open(dom_dir / "chart-data.json", "w") as f:
        json.dump(chart_data, f, indent=2)

    # --- Screenshots at default time range (30d) ---
    print(f"    Capturing screenshots (default 30d)...")
    capture_screenshots(page, out_dir)
    capture_panel_screenshots(page, out_dir)

    # --- Screenshots at other time ranges ---
    for label, period in cfg.TIME_RANGES:
        if period == "30d":
            continue  # Already captured as default
        print(f"    Capturing screenshots ({label})...")
        set_time_range(page, period)
        wait_for_panels_loaded(page)
        capture_screenshots(page, out_dir, label=label)

    # Reset to 30d for consistency
    set_time_range(page, "30d")
    wait_for_panels_loaded(page)

    # --- Config: stored (what OO has) ---
    print(f"    Fetching stored config...")
    config_dir = out_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)

    stored = api_get(context, f"dashboards/{dash_id}")
    with open(config_dir / "stored.json", "w") as f:
        json.dump(stored, f, indent=2, default=str)

    # --- Meta ---
    tab_count = len(dash["v8"].get("tabs", []))
    panel_count = sum(len(t.get("panels", [])) for t in dash["v8"].get("tabs", []))

    meta = {
        "dashboard_id": dash_id,
        "title": dash["title"],
        "slug": slug,
        "url": dashboard_url,
        "panel_count": panel_count,
        "tab_count": tab_count,
        "time_ranges_captured": [tr[0] for tr in cfg.TIME_RANGES],
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    with open(out_dir / "meta.json", "w") as f:
        json.dump(meta, f, indent=2)

    print(f"    Done — {panel_count} panels, {len(api_responses)} API calls captured.")


def main():
    parser = argparse.ArgumentParser(description="Collect mirror data from OpenObserve dashboards")
    parser.add_argument("--url", default=cfg.OO_URL, help="OpenObserve base URL")
    parser.add_argument("--user", default=cfg.OO_USER, help="OO username")
    parser.add_argument("--pass", dest="password", default=cfg.OO_PASS, help="OO password")
    parser.add_argument("--dashboard", action="append", help="Only collect specific dashboard slug(s)")
    parser.add_argument("--output", type=Path, default=cfg.OUTPUT_DIR, help="Output directory")
    args = parser.parse_args()

    oo_url = args.url
    oo_user = args.user
    oo_pass = args.password

    output_base = args.output
    output_base.mkdir(parents=True, exist_ok=True)

    print(f"Dashboard Mirror — Collecting from {oo_url}")
    print(f"Output: {output_base.resolve()}")

    # Patch config so helper functions use CLI args
    cfg.OO_URL = oo_url
    cfg.OO_USER = oo_user
    cfg.OO_PASS = oo_pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            http_credentials={"username": oo_user, "password": oo_pass},
        )
        page = context.new_page()

        # Login
        print("\nLogging in...")
        login(page)

        # List dashboards
        print("\nFetching dashboard list...")
        dashboards = list_dashboards(context)
        print(f"  Found {len(dashboards)} dashboards.")

        if args.dashboard:
            slugs = set(args.dashboard)
            dashboards = [d for d in dashboards if d["slug"] in slugs]
            print(f"  Filtered to {len(dashboards)}: {[d['slug'] for d in dashboards]}")

        if not dashboards:
            print("  No dashboards to collect. Exiting.")
            browser.close()
            return

        # Collect each dashboard
        for dash in dashboards:
            collect_dashboard(page, context, dash, output_base)

        browser.close()

    print(f"\nCollection complete. Output at: {output_base.resolve()}")


if __name__ == "__main__":
    main()
