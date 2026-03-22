# Rules

## Always

- Trace every factual claim to a source file (screenshot path, JSON key, DOM text)
- Resolve analyst contradictions using screenshot evidence, not majority vote
- Distinguish expected "No Data" (missing telemetry) from unexpected (broken query)
- Include schema coverage for every panel query
- Run baseline analysis before per-dashboard analysis
- Spawn the 3 per-dashboard analysts in parallel (they are independent)
- Wait for all 3 analysts to complete before running the synthesizer

## Never

- State subjective opinions without backing evidence
- Skip the baseline step — cross-dashboard consistency errors are invisible to single-dashboard analysts
- Modify mirror data files in `output/` — they are read-only inputs to analysis
- Assume a "No Data" panel is broken — check the schema first
- Run collection against production OO instances without explicit confirmation
