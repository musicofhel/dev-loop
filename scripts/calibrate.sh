#!/usr/bin/env bash
# =============================================================================
# Continuous Calibration Pipeline
# Runs all calibration stages and produces a dated report.
# Exit 1 if any regression detected.
# Usage: just calibrate [--skip-rust]
# =============================================================================
set -euo pipefail

SKIP_RUST=false
for arg in "$@"; do
    case "$arg" in
        --skip-rust) SKIP_RUST=true ;;
    esac
done

DATE=$(date +%Y-%m-%d)
TIMESTAMP=$(date -Iseconds)
REPORT_DIR="$(cd "$(dirname "$0")/.." && pwd)/docs/calibration"
REPORT="$REPORT_DIR/$DATE.md"
BASELINES_DIR="$(cd "$(dirname "$0")/.." && pwd)/scripts/replay/baselines"
FEEDBACK_BASELINES_DIR="$(cd "$(dirname "$0")/.." && pwd)/scripts/feedback/baselines"
SESSIONS_GLOB="$HOME/.claude/projects/-home-musicofhel/*.jsonl"
TMPDIR="${TMPDIR:-/tmp}"
WORK="$TMPDIR/dev-loop-calibrate-$$"
mkdir -p "$WORK" "$REPORT_DIR" "$BASELINES_DIR" "$FEEDBACK_BASELINES_DIR"

# Track overall result
EXIT_CODE=0
REGRESSIONS=()

cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

header() {
    echo ""
    echo "══════════════════════════════════════════════════════════"
    echo "  $1"
    echo "══════════════════════════════════════════════════════════"
}

# Find the latest baseline file in a directory (by filename sort)
latest_baseline() {
    local dir="$1"
    local files
    files=$(find "$dir" -maxdepth 1 -name '*.json' 2>/dev/null | sort | tail -1)
    echo "$files"
}

# ── Stage 1: Shadow Report ──────────────────────────────────────

header "Stage 1/5: Shadow Report (last 7 days)"

SHADOW_OK=true
SHADOW_SUMMARY="skipped"
if command -v dl &>/dev/null; then
    if dl shadow-report --last 168 --csv > "$WORK/shadow.csv" 2>"$WORK/shadow.err"; then
        SHADOW_LINES=$(wc -l < "$WORK/shadow.csv")
        SHADOW_SUMMARY="$((SHADOW_LINES - 1)) shadow verdicts collected"
        echo "  $SHADOW_SUMMARY"
    else
        # shadow-report may fail if no shadow events exist — that's OK
        SHADOW_SUMMARY="no shadow data ($(head -1 "$WORK/shadow.err" 2>/dev/null || echo 'unknown error'))"
        echo "  $SHADOW_SUMMARY"
    fi
else
    SHADOW_SUMMARY="dl binary not found"
    echo "  WARNING: dl not found in PATH"
fi

# ── Stage 2: Replay Harness ─────────────────────────────────────

header "Stage 2/5: Replay Harness"

REPLAY_OK=true
REPLAY_SUMMARY="skipped"
REPLAY_BASELINE_CMP=""

# Check for session files
SESSION_COUNT=$(ls -1 $SESSIONS_GLOB 2>/dev/null | wc -l || echo 0)
if [ "$SESSION_COUNT" -eq 0 ]; then
    REPLAY_SUMMARY="no session JSONL files found"
    echo "  $REPLAY_SUMMARY"
else
    echo "  Parsing $SESSION_COUNT session files..."
    # Parse sessions to a temp file first (avoids pipefail + head SIGPIPE issues)
    uv run python scripts/replay/parse_sessions.py $SESSIONS_GLOB > "$WORK/parsed.ndjson" 2>/dev/null || true
    PARSED_LINES=$(wc -l < "$WORK/parsed.ndjson")
    echo "  Parsed $PARSED_LINES tool calls, replaying (max 2000)..."
    if head -2000 "$WORK/parsed.ndjson" | \
       uv run python scripts/replay/run_replay.py --raw --json --workers 4 2>/dev/null | \
       uv run python scripts/replay/score.py --json > "$WORK/replay-score.json" 2>"$WORK/replay.err"; then

        REPLAY_TOTAL=$(python3 -c "import json; d=json.load(open('$WORK/replay-score.json')); print(d.get('analysis',{}).get('total',0))" 2>/dev/null || echo "?")
        REPLAY_BLOCK=$(python3 -c "import json; d=json.load(open('$WORK/replay-score.json')); print(d.get('analysis',{}).get('verdicts',{}).get('block',0))" 2>/dev/null || echo "?")
        REPLAY_WARN=$(python3 -c "import json; d=json.load(open('$WORK/replay-score.json')); print(d.get('analysis',{}).get('verdicts',{}).get('warn',0))" 2>/dev/null || echo "?")
        REPLAY_SUMMARY="$REPLAY_TOTAL tool calls — $REPLAY_BLOCK blocked, $REPLAY_WARN warned"
        echo "  $REPLAY_SUMMARY"

        # Compare against latest baseline if one exists
        LATEST_REPLAY_BL=$(latest_baseline "$BASELINES_DIR")
        if [ -n "$LATEST_REPLAY_BL" ]; then
            echo "  Comparing against baseline: $(basename "$LATEST_REPLAY_BL")"
            if python3 -c "
import json, sys
cur = json.load(open('$WORK/replay-score.json'))
base = json.load(open('$LATEST_REPLAY_BL'))
ca = cur.get('analysis', {})
ba = base.get('analysis', base)
ct = ca.get('total', 0)
bt = ba.get('total', 0)
if bt > 0 and ct > 0:
    cbr = ca.get('verdicts', {}).get('block', 0) / ct
    bbr = ba.get('verdicts', {}).get('block', 0) / bt
    if cbr > bbr * 1.5 and cbr - bbr > 0.01:
        print(f'REGRESSION: block rate {bbr:.1%} -> {cbr:.1%}')
        sys.exit(1)
print('No regressions vs baseline')
" 2>/dev/null; then
                REPLAY_BASELINE_CMP="no regressions vs $(basename "$LATEST_REPLAY_BL")"
            else
                REPLAY_BASELINE_CMP="REGRESSION detected vs $(basename "$LATEST_REPLAY_BL")"
                REGRESSIONS+=("replay: block rate regression")
                EXIT_CODE=1
            fi
        else
            REPLAY_BASELINE_CMP="no baseline found (first run)"
        fi
    else
        REPLAY_SUMMARY="replay failed (see $WORK/replay.err)"
        REPLAY_OK=false
        echo "  WARNING: $REPLAY_SUMMARY"
    fi
fi

# ── Stage 3: Tier 2 Planted-Defect Suite ────────────────────────

header "Stage 3/5: Tier 2 Planted-Defect Suite"

TIER2_OK=true
TIER2_SUMMARY="skipped"
if [ -f tests/tier2/test_checkpoint_gates.py ]; then
    if uv run pytest tests/tier2/test_checkpoint_gates.py --tb=short -q > "$WORK/tier2.out" 2>&1; then
        TIER2_PASS=$(grep -oP '\d+ passed' "$WORK/tier2.out" | head -1 || echo "? passed")
        TIER2_SUMMARY="$TIER2_PASS"
        echo "  $TIER2_SUMMARY"
    else
        TIER2_PASS=$(grep -oP '\d+ passed' "$WORK/tier2.out" | head -1 || echo "0 passed")
        TIER2_FAIL=$(grep -oP '\d+ failed' "$WORK/tier2.out" | head -1 || echo "? failed")
        TIER2_SUMMARY="$TIER2_PASS, $TIER2_FAIL"
        TIER2_OK=false
        REGRESSIONS+=("tier2: $TIER2_FAIL")
        EXIT_CODE=1
        echo "  FAIL: $TIER2_SUMMARY"
    fi
else
    TIER2_SUMMARY="test file not found"
    echo "  $TIER2_SUMMARY"
fi

# ── Stage 4: Feedback Scoring ───────────────────────────────────

header "Stage 4/5: Feedback Scoring"

FEEDBACK_OK=true
FEEDBACK_SUMMARY="skipped"
FEEDBACK_BASELINE_CMP=""

if uv run python scripts/feedback/score.py --json > "$WORK/feedback-score.json" 2>"$WORK/feedback.err"; then
    FB_COUNT=$(python3 -c "import json; d=json.load(open('$WORK/feedback-score.json')); print(d.get('total',{}).get('labeled_count',0))" 2>/dev/null || echo "?")
    FB_F1=$(python3 -c "import json; d=json.load(open('$WORK/feedback-score.json')); print(d.get('total',{}).get('f1','?'))" 2>/dev/null || echo "?")

    if [ "$FB_COUNT" = "0" ] || python3 -c "import json; d=json.load(open('$WORK/feedback-score.json')); assert 'error' not in d" 2>/dev/null; then
        FEEDBACK_SUMMARY="$FB_COUNT labels, F1=$FB_F1"
    else
        FEEDBACK_SUMMARY="no feedback data"
    fi
    echo "  $FEEDBACK_SUMMARY"

    # Compare against latest feedback baseline
    LATEST_FB_BL=$(latest_baseline "$FEEDBACK_BASELINES_DIR")
    if [ -n "$LATEST_FB_BL" ] && [ "$FB_COUNT" != "0" ]; then
        echo "  Comparing against baseline: $(basename "$LATEST_FB_BL")"
        if uv run python scripts/feedback/score.py --baseline "$LATEST_FB_BL" > /dev/null 2>&1; then
            FEEDBACK_BASELINE_CMP="no regressions vs $(basename "$LATEST_FB_BL")"
        else
            FEEDBACK_BASELINE_CMP="REGRESSION detected vs $(basename "$LATEST_FB_BL")"
            REGRESSIONS+=("feedback: F1 regression")
            EXIT_CODE=1
        fi
    else
        FEEDBACK_BASELINE_CMP="no baseline found or no data"
    fi
else
    FEEDBACK_SUMMARY="scoring failed"
    echo "  $FEEDBACK_SUMMARY"
fi

# ── Stage 5: Rust Tests ─────────────────────────────────────────

header "Stage 5/5: Daemon Rust Tests"

RUST_OK=true
RUST_SUMMARY="skipped"
if [ "$SKIP_RUST" = true ]; then
    echo "  Skipped (--skip-rust)"
elif [ -f daemon/Cargo.toml ]; then
    echo "  Running cargo test (this may take a minute if recompiling)..."
    if (cd daemon && cargo test 2>&1) > "$WORK/rust.out" 2>&1; then
        # Count all "test result" lines (lib + bin + integration tests)
        RUST_TOTAL=0
        while IFS= read -r line; do
            count=$(echo "$line" | grep -oP '\d+ passed' | grep -oP '\d+')
            RUST_TOTAL=$((RUST_TOTAL + count))
        done < <(grep 'test result: ok' "$WORK/rust.out")
        RUST_SUMMARY="$RUST_TOTAL tests passed"
        echo "  $RUST_SUMMARY"
    else
        RUST_SUMMARY="FAILED (see output below)"
        RUST_OK=false
        REGRESSIONS+=("rust: test failures")
        EXIT_CODE=1
        echo "  FAIL: Rust tests failed"
        tail -20 "$WORK/rust.out"
    fi
else
    RUST_SUMMARY="daemon/Cargo.toml not found"
    echo "  $RUST_SUMMARY"
fi

# ── Generate Report ─────────────────────────────────────────────

header "Generating Report"

cat > "$REPORT" <<REPORT_EOF
# Calibration Report — $DATE

**Generated**: $TIMESTAMP
**Status**: $([ $EXIT_CODE -eq 0 ] && echo "PASS" || echo "FAIL")

---

## Summary

| Stage | Status | Details |
|-------|--------|---------|
| Shadow Report | $([ "$SHADOW_OK" = true ] && echo "OK" || echo "WARN") | $SHADOW_SUMMARY |
| Replay Harness | $([ "$REPLAY_OK" = true ] && echo "OK" || echo "FAIL") | $REPLAY_SUMMARY |
| Tier 2 Suite | $([ "$TIER2_OK" = true ] && echo "OK" || echo "FAIL") | $TIER2_SUMMARY |
| Feedback Scoring | $([ "$FEEDBACK_OK" = true ] && echo "OK" || echo "WARN") | $FEEDBACK_SUMMARY |
| Rust Tests | $([ "$RUST_OK" = true ] && echo "OK" || echo "FAIL") | $RUST_SUMMARY |

REPORT_EOF

# Replay details
if [ -f "$WORK/replay-score.json" ]; then
    cat >> "$REPORT" <<REPLAY_EOF

## Replay Details

$(python3 -c "
import json
d = json.load(open('$WORK/replay-score.json'))
a = d.get('analysis', {})
v = a.get('verdicts', {})
total = a.get('total', 0)
if total:
    print(f'- **Total tool calls**: {total}')
    for k in ['allow', 'block', 'warn']:
        c = v.get(k, 0)
        if c: print(f'- **{k.title()}**: {c} ({100*c/total:.1f}%)')
    blocked = a.get('top_blocked_files', [])[:5]
    if blocked:
        print()
        print('**Top Blocked Files**:')
        for b in blocked:
            print(f'- \`{b[\"file\"]}\` ({b[\"count\"]}x)')
" 2>/dev/null || echo "_(could not parse replay data)_")

Baseline: $REPLAY_BASELINE_CMP
REPLAY_EOF
fi

# Feedback details
if [ -f "$WORK/feedback-score.json" ]; then
    cat >> "$REPORT" <<FB_EOF

## Feedback Details

$(python3 -c "
import json
d = json.load(open('$WORK/feedback-score.json'))
if 'error' in d:
    print('No feedback data available.')
else:
    t = d.get('total', {})
    print(f'- **Labels**: {t.get(\"labeled_count\", 0)}')
    print(f'- **Overall F1**: {t.get(\"f1\", \"?\"):.4f}' if isinstance(t.get('f1'), float) else f'- **Overall F1**: {t.get(\"f1\", \"?\")}')
    pc = d.get('per_check', {})
    if pc:
        print()
        print('| Check Type | TP | FP | FN | Precision | Recall | F1 |')
        print('|------------|----|----|----|-----------|---------|----|')
        for ct, m in pc.items():
            print(f'| {ct} | {m[\"tp\"]} | {m[\"fp\"]} | {m[\"fn\"]} | {m[\"precision\"]:.1%} | {m[\"recall\"]:.1%} | {m[\"f1\"]:.3f} |')
" 2>/dev/null || echo "_(could not parse feedback data)_")

Baseline: $FEEDBACK_BASELINE_CMP
FB_EOF
fi

# Regressions section
REG_COUNT=${#REGRESSIONS[@]}
if [ "$REG_COUNT" -gt 0 ]; then
    cat >> "$REPORT" <<REG_EOF

## Regressions Detected

$(for r in "${REGRESSIONS[@]}"; do echo "- **$r**"; done)
REG_EOF
fi

echo "  Report written to: $REPORT"

# ── Final Summary ───────────────────────────────────────────────

header "Calibration Complete"

if [ $EXIT_CODE -eq 0 ]; then
    echo "  RESULT: PASS — no regressions detected"
else
    echo "  RESULT: FAIL — $REG_COUNT regression(s):"
    for r in "${REGRESSIONS[@]}"; do
        echo "    - $r"
    done
fi

echo ""
echo "  Report: $REPORT"
echo ""

exit $EXIT_CODE
