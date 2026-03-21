"""Tests for scripts/import-dashboards.py parsing functions."""

import importlib
import sys
from pathlib import Path

# Hyphenated filename requires importlib
_script = Path(__file__).parent.parent / "scripts" / "import-dashboards.py"
_spec = importlib.util.spec_from_file_location("import_dashboards", _script)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


class TestParseSelectColumns:
    def test_simple_columns(self):
        sql = "SELECT a, b FROM t"
        cols = mod._parse_select_columns(sql)
        assert cols == [("a", "a"), ("b", "b")]

    def test_aliased_aggregate(self):
        sql = "SELECT COUNT(*) as total FROM t"
        cols = mod._parse_select_columns(sql)
        assert cols == [("COUNT(*)", "total")]

    def test_round_wrapped_aggregate(self):
        sql = "SELECT ROUND(AVG(x), 2) as avg_x FROM t"
        cols = mod._parse_select_columns(sql)
        assert len(cols) == 1
        assert cols[0][1] == "avg_x"
        assert "ROUND(AVG(x), 2)" in cols[0][0]

    def test_case_when_expression(self):
        sql = "SELECT SUM(CASE WHEN x = 1 THEN 1 ELSE 0 END) as hits FROM t"
        cols = mod._parse_select_columns(sql)
        assert len(cols) == 1
        assert cols[0][1] == "hits"
        assert "SUM(CASE WHEN" in cols[0][0]

    def test_multi_column_with_date_trunc(self):
        sql = "SELECT DATE_TRUNC('day', TO_TIMESTAMP(_timestamp / 1000000)) as day, COUNT(*) as total FROM t"
        cols = mod._parse_select_columns(sql)
        assert len(cols) == 2
        assert cols[0][1] == "day"
        assert cols[1] == ("COUNT(*)", "total")

    def test_no_from_returns_empty(self):
        sql = "SELECT a, b"
        assert mod._parse_select_columns(sql) == []


class TestParseSelectAliases:
    def test_simple_columns(self):
        sql = "SELECT a, b FROM t"
        assert mod._parse_select_aliases(sql) == ["a", "b"]

    def test_aliased_aggregate(self):
        sql = "SELECT COUNT(*) as total FROM t"
        assert mod._parse_select_aliases(sql) == ["total"]

    def test_nested_parens(self):
        sql = "SELECT ROUND(AVG(x), 2) as avg_x FROM t"
        assert mod._parse_select_aliases(sql) == ["avg_x"]

    def test_date_trunc(self):
        sql = "SELECT DATE_TRUNC('day', TO_TIMESTAMP(_timestamp / 1000000)) as day, COUNT(*) as total FROM t"
        assert mod._parse_select_aliases(sql) == ["day", "total"]

    def test_no_from_clause(self):
        sql = "SELECT a, b"
        assert mod._parse_select_aliases(sql) == []

    def test_multiple_aggregates(self):
        sql = "SELECT SUM(a) as s, AVG(b) as a, MAX(c) as m FROM t"
        assert mod._parse_select_aliases(sql) == ["s", "a", "m"]

    def test_bare_column_with_table_prefix(self):
        sql = "SELECT t.column_name FROM t"
        assert mod._parse_select_aliases(sql) == ["column_name"]

    def test_case_expression(self):
        sql = "SELECT SUM(CASE WHEN x = 1 THEN 1 ELSE 0 END) as hits FROM t"
        assert mod._parse_select_aliases(sql) == ["hits"]


class TestMakeFields:
    def test_time_column_goes_to_x_axis(self):
        sql = "SELECT DATE_TRUNC('day', TO_TIMESTAMP(_timestamp / 1000000)) as day, COUNT(*) as total FROM t GROUP BY day"
        fields = mod._make_fields(sql, "bar")
        x_aliases = [f["alias"] for f in fields["x"]]
        y_aliases = [f["alias"] for f in fields["y"]]
        assert "day" in x_aliases
        assert "total" in y_aliases

    def test_aggregate_goes_to_y_axis(self):
        sql = "SELECT SUM(x) as total FROM t"
        fields = mod._make_fields(sql, "area")
        y_aliases = [f["alias"] for f in fields["y"]]
        assert "total" in y_aliases

    def test_fallback_timestamp_when_no_x(self):
        sql = "SELECT COUNT(*) as total FROM t"
        fields = mod._make_fields(sql, "bar")
        x_aliases = [f["alias"] for f in fields["x"]]
        assert "_timestamp" in x_aliases

    def test_empty_aliases_still_produce_fields(self):
        # No FROM clause → empty aliases
        sql = "SELECT a, b"
        fields = mod._make_fields(sql, "line")
        # Should have fallback x at minimum
        assert len(fields["x"]) >= 1

    def test_group_by_non_time_without_time_goes_to_x(self):
        """Non-time GROUP BY without time dimension → x-axis (categorical bar/pie)."""
        sql = "SELECT persona, COUNT(*) as runs FROM t GROUP BY persona"
        fields = mod._make_fields(sql, "bar")
        x_aliases = [f["alias"] for f in fields["x"]]
        y_aliases = [f["alias"] for f in fields["y"]]
        assert "persona" in x_aliases
        assert "runs" in y_aliases
        assert fields["z"] == []

    def test_colors_assigned_to_y(self):
        sql = "SELECT COUNT(*) as total FROM t"
        fields = mod._make_fields(sql, "bar")
        for y in fields["y"]:
            assert y["color"] is not None

    def test_case_when_aggregate_goes_to_y(self):
        """CASE WHEN wrapped in SUM should be classified as y-axis, not x."""
        sql = "SELECT DATE_TRUNC('day', TO_TIMESTAMP(_timestamp / 1000000)) as day, ROUND(100.0 * SUM(CASE WHEN gate_status = 'fail' THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 1) as failure_pct FROM t GROUP BY day ORDER BY day"
        fields = mod._make_fields(sql, "line")
        x_aliases = [f["alias"] for f in fields["x"]]
        y_aliases = [f["alias"] for f in fields["y"]]
        assert "day" in x_aliases
        assert "failure_pct" in y_aliases
        assert "failure_pct" not in x_aliases

    def test_multi_group_by_produces_z_axis(self):
        """Multi-GROUP BY: time goes to x, non-time dimension goes to z, aggregate to y."""
        sql = "SELECT DATE_TRUNC('day', TO_TIMESTAMP(_timestamp / 1000000)) as day, operation_name as gate, COUNT(*) as failures FROM t GROUP BY day, gate ORDER BY day"
        fields = mod._make_fields(sql, "line")
        x_aliases = [f["alias"] for f in fields["x"]]
        y_aliases = [f["alias"] for f in fields["y"]]
        z_aliases = [f["alias"] for f in fields["z"]]
        assert "day" in x_aliases
        assert "gate" in z_aliases
        assert "failures" in y_aliases

    def test_round_wrapped_aggregate_goes_to_y(self):
        """ROUND(AVG(x), 2) should go to y-axis since AVG is detected inside."""
        sql = "SELECT DATE_TRUNC('day', TO_TIMESTAMP(_timestamp / 1000000)) as day, ROUND(AVG(duration / 1000000), 1) as avg_lead_time_s FROM t GROUP BY day ORDER BY day"
        fields = mod._make_fields(sql, "area")
        x_aliases = [f["alias"] for f in fields["x"]]
        y_aliases = [f["alias"] for f in fields["y"]]
        assert "day" in x_aliases
        assert "avg_lead_time_s" in y_aliases
        assert "avg_lead_time_s" not in x_aliases

    def test_quality_gates_panel1_pattern(self):
        """Quality Gates Panel 1: gate to x (categorical, no time), passed+failed to y."""
        sql = "SELECT operation_name as gate, SUM(CASE WHEN gate_status = 'pass' THEN 1 ELSE 0 END) as passed, SUM(CASE WHEN gate_status = 'fail' THEN 1 ELSE 0 END) as failed FROM t GROUP BY gate"
        fields = mod._make_fields(sql, "bar")
        x_aliases = [f["alias"] for f in fields["x"]]
        y_aliases = [f["alias"] for f in fields["y"]]
        assert "gate" in x_aliases
        assert "passed" in y_aliases
        assert "failed" in y_aliases
        assert fields["z"] == []

    def test_z_field_present_in_result(self):
        """The z key should always be present in the result dict."""
        sql = "SELECT COUNT(*) as total FROM t"
        fields = mod._make_fields(sql, "bar")
        assert "z" in fields


class TestFixAggregateTimestamp:
    def test_pure_aggregate_gets_min_timestamp(self):
        sql = "SELECT COUNT(*) as total FROM t"
        fixed = mod._fix_aggregate_timestamp(sql)
        assert "MIN(_timestamp) as _timestamp" in fixed

    def test_group_by_left_unchanged(self):
        sql = "SELECT day, COUNT(*) as total FROM t GROUP BY day"
        fixed = mod._fix_aggregate_timestamp(sql)
        assert fixed == sql

    def test_already_has_min_timestamp(self):
        sql = "SELECT MIN(_timestamp) as _timestamp, COUNT(*) as total FROM t"
        fixed = mod._fix_aggregate_timestamp(sql)
        # Should not double up
        assert fixed.count("MIN(_timestamp)") == 1

    def test_no_aggregate_left_unchanged(self):
        sql = "SELECT a, b FROM t"
        fixed = mod._fix_aggregate_timestamp(sql)
        assert fixed == sql

    def test_round_avg_is_aggregate(self):
        """ROUND(AVG(x), 2) still triggers because AVG( is in the detection list."""
        sql = "SELECT ROUND(AVG(x), 2) as avg_x FROM t"
        fixed = mod._fix_aggregate_timestamp(sql)
        assert "MIN(_timestamp)" in fixed

    def test_limit_query_skipped(self):
        """Queries with LIMIT are flat detail listings — no MIN(_timestamp) injection."""
        sql = "SELECT trace_id, ROUND(cost, 4) as spent FROM t ORDER BY spent DESC LIMIT 20"
        fixed = mod._fix_aggregate_timestamp(sql)
        assert fixed == sql
        assert "MIN(_timestamp)" not in fixed

    def test_limit_with_aggregate_still_skipped(self):
        """Even if there's an aggregate function, LIMIT queries should be skipped."""
        sql = "SELECT name, COUNT(*) as cnt FROM t GROUP BY name ORDER BY cnt DESC LIMIT 10"
        fixed = mod._fix_aggregate_timestamp(sql)
        assert fixed == sql


class TestLabelOverrides:
    def test_known_alias_gets_override(self):
        """Aliases in _LABEL_OVERRIDES get human-readable labels."""
        sql = "SELECT DATE_TRUNC('day', TO_TIMESTAMP(_timestamp / 1000000)) as day, ROUND(AVG(CAST(duration AS DOUBLE) / 1000000), 1) as avg_lead_time_s FROM t GROUP BY day"
        fields = mod._make_fields(sql, "area")
        y_labels = [f["label"] for f in fields["y"]]
        assert "Avg Lead Time (s)" in y_labels

    def test_unknown_alias_gets_title_case(self):
        """Aliases not in _LABEL_OVERRIDES fall back to title case."""
        sql = "SELECT some_custom_metric as custom_thing, COUNT(*) as total FROM t GROUP BY custom_thing"
        fields = mod._make_fields(sql, "bar")
        x_labels = [f["label"] for f in fields["x"]]
        assert "Custom Thing" in x_labels

    def test_multiple_overrides_in_one_query(self):
        """Multiple y-axis fields each get their own label override."""
        sql = "SELECT DATE_TRUNC('day', TO_TIMESTAMP(_timestamp / 1000000)) as day, ROUND(100.0 * SUM(CASE WHEN x = 1 THEN 1 ELSE 0 END) / COUNT(*), 1) as success_pct FROM t GROUP BY day"
        fields = mod._make_fields(sql, "line")
        y_labels = [f["label"] for f in fields["y"]]
        assert "Success Rate (%)" in y_labels


class TestTranslatePanel:
    def test_color_overrides_applied(self):
        """Per-panel color overrides from config are applied to y-axis fields."""
        panel = {
            "id": 1,
            "title": "Test",
            "type": "bar",
            "colors": {"passed": "#4caf50", "failed": "#d62728"},
            "query": "SELECT gate, SUM(CASE WHEN s = 'pass' THEN 1 ELSE 0 END) as passed, SUM(CASE WHEN s = 'fail' THEN 1 ELSE 0 END) as failed FROM t GROUP BY gate",
        }
        result = mod._translate_panel(panel, 0)
        y_fields = result["fields"]["y"]
        color_map = {f["alias"]: f["color"] for f in y_fields}
        assert color_map["passed"] == "#4caf50"
        assert color_map["failed"] == "#d62728"

    def test_no_color_overrides_uses_defaults(self):
        """Without color overrides, default palette is used."""
        panel = {
            "id": 1,
            "title": "Test",
            "type": "bar",
            "query": "SELECT COUNT(*) as total FROM t",
        }
        result = mod._translate_panel(panel, 0)
        y_fields = result["fields"]["y"]
        assert y_fields[0]["color"] == mod._COLORS[0]

    def test_categorical_x_axis_has_count_aggregation(self):
        """Categorical x-axis fields get aggregationFunction='count' to signal OO."""
        panel = {
            "id": 1,
            "title": "Test",
            "type": "bar",
            "query": "SELECT persona, COUNT(*) as runs FROM t GROUP BY persona",
        }
        result = mod._translate_panel(panel, 0)
        x_fields = result["fields"]["x"]
        assert x_fields[0]["alias"] == "persona"
        assert x_fields[0]["aggregationFunction"] == "count"
