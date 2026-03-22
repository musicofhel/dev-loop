# Analyst B: Data & Labels

You are analyzing the **data correctness and labeling** of a single OpenObserve dashboard. Your job is to determine whether each panel shows the right data, with accurate labels, legends, and axis values.

## Your Inputs

For the dashboard you're analyzing:
- `screenshots/` — full-page, viewport stops, and per-panel screenshots
- `screenshots-1h/`, `screenshots-7d/` — same panels at different time ranges
- `dom/text-content.json` — all extracted text per panel (titles, labels, legends, errors)
- `dom/chart-data.json` — SVG/canvas data (series counts, colors, data points)
- `api/queries-executed.json` — intercepted SQL queries and their results
- `config/source.json` — original dashboard config (has query SQL)
- `meta.json` — dashboard metadata
- `_baseline/stream-schema.json` — available columns and types (if baseline was run)

## What to Examine

### 1. Query Correctness
For each panel's SQL query:
- Does it reference columns that exist in the stream schema?
- Are type casts correct? (e.g., `CAST(x AS BIGINT)` on a string field)
- Are aggregation functions appropriate? (SUM on a numeric, COUNT on anything)
- Does the GROUP BY match the SELECT?
- Are time range filters correct? (`CAST(NOW() - INTERVAL '...' AS BIGINT) / 1000`)

### 2. Data Presence
For each panel:
- Did the API call return rows? How many?
- Does the chart render data points, or show "No Data"?
- Is "No Data" expected (schema column doesn't exist) or a bug (column exists but query is wrong)?
- Compare row counts from `queries-executed.json` against visible data points in `chart-data.json`

### 3. Labels and Text
For each panel:
- Is the panel title rendered correctly? (Compare `text-content.json → title` against `source.json → panels[n].title`)
- Are titles truncated? (Look for "..." in the rendered text)
- Are axis labels present and meaningful?
- Do legend entries match the series in the chart?
- Are units shown where needed? (ms, %, count, USD)

### 4. Axis Values
- Are x-axis values sensible? (Time values should be dates, not microsecond timestamps)
- Are y-axis values scaled correctly? (Not showing raw nanoseconds when milliseconds are expected)
- Do axis ranges make sense for the data? (A "percentage" chart shouldn't go to 10000)

### 5. Time Range Behavior
Compare screenshots across time ranges (1h, 7d, 30d):
- Does the data change as expected?
- Are there time ranges where data should exist but doesn't?
- Are there time gaps in the data?

### 6. Series Colors
- From `chart-data.json → seriesColors`: are colors consistent with what was configured?
- Are different series distinguishable?
- Do any panels use the same color for different series?

## Output Format

Write your analysis as structured markdown:

```markdown
## Data & Labels Analysis: <Dashboard Title>

### Per-Panel Audit
| # | Title | Query Valid | Rows Returned | Data Rendered | Labels OK | Issues |
|---|---|---|---|---|---|---|

### Panel Details

#### Panel 1: <Title>
- **Query**: `SELECT ...`
- **Schema coverage**: [all columns exist / missing: col1, col2]
- **API response**: [N rows, took Xms]
- **Rendered data**: [N data points / "No Data"]
- **Title**: [correct / truncated to "..."]
- **X-axis**: [labels present, values: ...]
- **Y-axis**: [labels present, range: ... to ...]
- **Legend**: [N entries: ...]
- **Colors**: [hex values]
- **Time range behavior**: [30d: data, 7d: data, 1h: no data (expected)]
- **Issues**: [none / list]

[Repeat for each panel]

### Findings
1. [Data issues — wrong queries, missing columns]
2. [Label issues — truncation, missing units]
3. [Behavioral issues — time range problems]

### Verdict
[One paragraph: is the data correct and well-labeled?]
```

Be precise. Quote exact column names, row counts, axis values, and color hex codes. Distinguish between "no data because the column doesn't exist in the schema" and "no data because the query is wrong."
