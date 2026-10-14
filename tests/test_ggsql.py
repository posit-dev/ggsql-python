"""Tests for ggsql Python bindings.

These tests focus on Python-specific logic:
- DataFrame conversion via narwhals
- Return type handling
- Two-stage API (reader.execute() -> render)

Rust logic (parsing, Vega-Lite generation) is tested in the Rust test suite.
"""

import json
import re
import time
import warnings
from datetime import datetime
from pathlib import Path

import duckdb
import pyarrow as pa
import pytest
import altair

import ggsql

# Optional dependencies
try:
    import polars as pl

    HAS_POLARS = True
except ImportError:
    HAS_POLARS = False

try:
    import ibis

    HAS_IBIS = hasattr(ibis, "duckdb")
except ImportError:
    HAS_IBIS = False


class TestValidate:
    """Tests for validate() function."""

    def test_valid_query_with_visualise(self):
        validated = ggsql.validate(
            "SELECT 1 AS x, 2 AS y VISUALISE DRAW point MAPPING x AS x, y AS y"
        )
        assert validated.has_visual()
        assert validated.valid()
        assert "SELECT" in validated.sql()
        assert "VISUALISE" in validated.visual()
        assert len(validated.errors()) == 0

    def test_valid_query_without_visualise(self):
        validated = ggsql.validate("SELECT 1 AS x, 2 AS y")
        assert not validated.has_visual()
        assert validated.valid()
        assert validated.sql() == "SELECT 1 AS x, 2 AS y"
        assert validated.visual() == ""

    def test_invalid_query_has_errors(self):
        validated = ggsql.validate("SELECT 1 VISUALISE DRAW invalid_geom")
        assert not validated.valid()
        assert len(validated.errors()) > 0

    def test_missing_required_aesthetic(self):
        # Text requires label, not providing it
        # (point's position aesthetics became optional in ggsql 0.4.0)
        validated = ggsql.validate(
            "SELECT 1 AS x, 2 AS y VISUALISE DRAW text MAPPING x AS x"
        )
        assert not validated.valid()
        errors = validated.errors()
        assert len(errors) > 0
        assert any("label" in e["message"] for e in errors)


class TestDuckDBReader:
    """Tests for DuckDBReader class."""

    def test_create_in_memory(self):
        reader = ggsql.DuckDBReader("duckdb://memory")
        assert reader is not None

    def test_execute_simple_query(self):
        reader = ggsql.DuckDBReader("duckdb://memory")
        table = reader.execute_sql("SELECT 1 AS x, 2 AS y")
        assert isinstance(table, pa.Table)
        assert table.shape == (1, 2)
        assert table.column_names == ["x", "y"]

    def test_register_and_query(self):
        reader = ggsql.DuckDBReader("duckdb://memory")
        table = pa.table({"x": [1, 2, 3], "y": [10, 20, 30]})
        reader.register("my_data", table)

        result = reader.execute_sql("SELECT * FROM my_data WHERE x > 1")
        assert isinstance(result, pa.Table)
        assert result.shape == (2, 2)

    @pytest.mark.skipif(not HAS_POLARS, reason="polars not installed")
    def test_register_polars_dataframe(self):
        """register() accepts polars DataFrames via automatic conversion."""
        reader = ggsql.DuckDBReader("duckdb://memory")
        df = pl.DataFrame({"x": [1, 2, 3], "y": [10, 20, 30]})
        reader.register("my_data", df)

        result = reader.execute_sql("SELECT * FROM my_data WHERE x > 1")
        assert isinstance(result, pa.Table)
        assert result.shape == (2, 2)

    def test_invalid_connection_string(self):
        with pytest.raises(ValueError):
            ggsql.DuckDBReader("invalid://connection")


class TestVegaLiteWriter:
    """Tests for VegaLiteWriter class."""

    def test_create_writer(self):
        writer = ggsql.VegaLiteWriter()
        assert writer is not None


class TestExecute:
    """Tests for reader.execute() method."""

    def test_execute_simple_query(self):
        reader = ggsql.DuckDBReader("duckdb://memory")
        spec = reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")
        assert spec is not None
        assert spec.layer_count() == 1

    def test_execute_with_registered_data(self):
        reader = ggsql.DuckDBReader("duckdb://memory")
        table = pa.table({"x": [1, 2, 3], "y": [10, 20, 30]})
        reader.register("data", table)

        spec = reader.execute("SELECT * FROM data VISUALISE x, y DRAW point")
        assert spec.metadata()["rows"] == 3

    def test_execute_metadata(self):
        reader = ggsql.DuckDBReader("duckdb://memory")
        spec = reader.execute(
            "SELECT * FROM (VALUES (1, 10), (2, 20), (3, 30)) AS t(x, y) "
            "VISUALISE x, y DRAW point",
        )

        metadata = spec.metadata()
        assert metadata["rows"] == 3
        assert isinstance(metadata["columns"], list)
        assert len(metadata["columns"]) > 0
        assert metadata["layer_count"] == 1

    def test_execute_sql_accessor(self):
        reader = ggsql.DuckDBReader("duckdb://memory")
        spec = reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")
        assert "SELECT" in spec.sql()

    def test_execute_visual_accessor(self):
        reader = ggsql.DuckDBReader("duckdb://memory")
        spec = reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")
        assert "VISUALISE" in spec.visual()

    def test_execute_data_accessor(self):
        reader = ggsql.DuckDBReader("duckdb://memory")
        spec = reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")
        data = spec.data()
        assert isinstance(data, pa.Table)
        assert data.shape == (1, 2)

    def test_execute_without_visualise_fails(self):
        reader = ggsql.DuckDBReader("duckdb://memory")
        with pytest.raises(ValueError):
            reader.execute("SELECT 1 AS x, 2 AS y")


class TestWriterRender:
    """Tests for VegaLiteWriter.render() method."""

    def test_render_to_vegalite(self):
        reader = ggsql.DuckDBReader("duckdb://memory")
        spec = reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")
        writer = ggsql.VegaLiteWriter()

        result = writer.render(spec)
        assert isinstance(result, str)

        spec_dict = json.loads(result)
        assert "$schema" in spec_dict
        assert "vega-lite" in spec_dict["$schema"]

    def test_render_contains_data(self):
        reader = ggsql.DuckDBReader("duckdb://memory")
        table = pa.table({"x": [1, 2, 3], "y": [10, 20, 30]})
        reader.register("data", table)

        spec = reader.execute("SELECT * FROM data VISUALISE x, y DRAW point")
        writer = ggsql.VegaLiteWriter()

        result = writer.render(spec)
        spec_dict = json.loads(result)
        # Data should be in the spec (either inline or in datasets)
        assert "data" in spec_dict or "datasets" in spec_dict

    def test_render_multi_layer(self):
        reader = ggsql.DuckDBReader("duckdb://memory")
        spec = reader.execute(
            "SELECT * FROM (VALUES (1, 10), (2, 20)) AS t(x, y) "
            "VISUALISE "
            "DRAW point MAPPING x AS x, y AS y "
            "DRAW line MAPPING x AS x, y AS y",
        )
        writer = ggsql.VegaLiteWriter()

        result = writer.render(spec)
        spec_dict = json.loads(result)
        assert "layer" in spec_dict


class TestSpatial:
    """Tests for spatial (map) layers via the VegaLiteWriter."""

    @pytest.fixture(scope="class", autouse=True)
    def install_spatial_extension(self):
        """Install DuckDB's spatial extension (the ggsql crate only LOADs it).

        The binary is cached per DuckDB version/platform under
        ~/.duckdb/extensions, so this downloads at most once per machine.
        """
        reader = ggsql.DuckDBReader("duckdb://memory")
        try:
            reader.execute_sql("INSTALL spatial")
        except Exception as e:
            pytest.skip(f"DuckDB spatial extension unavailable (offline?): {e}")

    def test_render_spatial_builtin_world(self):
        reader = ggsql.DuckDBReader("duckdb://memory")
        spec = reader.execute("VISUALISE FROM ggsql:world DRAW spatial")
        writer = ggsql.VegaLiteWriter()

        result = writer.render(spec)
        spec_dict = json.loads(result)
        marks = [
            layer["mark"]["type"] if isinstance(layer["mark"], dict) else layer["mark"]
            for layer in spec_dict["layer"]
        ]
        assert "geoshape" in marks

    def test_render_spatial_choropleth(self):
        reader = ggsql.DuckDBReader("duckdb://memory")
        spec = reader.execute(
            "VISUALISE FROM ggsql:world DRAW spatial MAPPING population AS fill"
        )
        writer = ggsql.VegaLiteWriter()

        result = writer.render(spec)
        assert "geoshape" in result

    def test_render_chart_spatial(self):
        reader = ggsql.DuckDBReader("duckdb://memory")
        spec = reader.execute("VISUALISE FROM ggsql:world DRAW spatial")
        writer = ggsql.VegaLiteWriter()

        chart = writer.render_chart(spec)
        assert isinstance(chart, altair.TopLevelMixin)
        # The chart survives Altair-level round-tripping (this is how the
        # spec reaches the browser widget)
        assert "projection" in chart.to_dict()


class TestRenderAltairDataFrameConversion:
    """Tests for DataFrame handling in render_altair()."""

    def test_accepts_pyarrow_table(self):
        table = pa.table({"x": [1, 2, 3], "y": [10, 20, 30]})
        chart = ggsql.render_altair(table, "VISUALISE x, y DRAW point")
        assert isinstance(chart, altair.TopLevelMixin)

    @pytest.mark.skipif(not HAS_POLARS, reason="polars not installed")
    def test_accepts_polars_dataframe(self):
        df = pl.DataFrame({"x": [1, 2, 3], "y": [10, 20, 30]})
        chart = ggsql.render_altair(df, "VISUALISE x, y DRAW point")
        assert isinstance(chart, altair.TopLevelMixin)

    @pytest.mark.skipif(not HAS_POLARS, reason="polars not installed")
    def test_accepts_polars_lazyframe(self):
        lf = pl.LazyFrame({"x": [1, 2, 3], "y": [10, 20, 30]})
        chart = ggsql.render_altair(lf, "VISUALISE x, y DRAW point")
        assert isinstance(chart, altair.TopLevelMixin)

    def test_accepts_narwhals_dataframe(self):
        import narwhals as nw

        table = pa.table({"x": [1, 2, 3], "y": [10, 20, 30]})
        nw_df = nw.from_native(table)

        chart = ggsql.render_altair(nw_df, "VISUALISE x, y DRAW point")
        assert isinstance(chart, altair.TopLevelMixin)

    def test_accepts_pandas_dataframe(self):
        pd = pytest.importorskip("pandas")

        pd_df = pd.DataFrame({"x": [1, 2, 3], "y": [10, 20, 30]})
        chart = ggsql.render_altair(pd_df, "VISUALISE x, y DRAW point")
        assert isinstance(chart, altair.TopLevelMixin)

    def test_rejects_invalid_dataframe_type(self):
        with pytest.raises(TypeError, match="must be a narwhals DataFrame"):
            ggsql.render_altair({"x": [1, 2, 3]}, "VISUALISE x, y DRAW point")


class TestRenderAltairReturnType:
    """Tests for render_altair() return type."""

    def test_returns_altair_chart(self):
        table = pa.table({"x": [1, 2, 3], "y": [10, 20, 30]})
        chart = ggsql.render_altair(table, "VISUALISE x, y DRAW point")
        assert isinstance(chart, altair.TopLevelMixin)

    def test_chart_has_data(self):
        table = pa.table({"x": [1, 2, 3], "y": [10, 20, 30]})
        chart = ggsql.render_altair(table, "VISUALISE x, y DRAW point")
        spec = chart.to_dict()
        # Data should be embedded in datasets
        assert "datasets" in spec

    def test_chart_can_be_serialized(self):
        table = pa.table({"x": [1, 2, 3], "y": [10, 20, 30]})
        chart = ggsql.render_altair(table, "VISUALISE x, y DRAW point")
        # Should not raise
        json_str = chart.to_json()
        assert len(json_str) > 0

    def test_height_width(self):
        table = pa.table({"x": [1, 2, 3], "y": [10, 20, 30]})
        chart = ggsql.render_altair(
            table, "VISUALISE x, y DRAW point", height=300, width=500
        )
        chart_dict = chart.to_dict()
        assert chart_dict["height"] == 300
        assert chart_dict["width"] == 500

    def test_height_only(self):
        table = pa.table({"x": [1, 2, 3], "y": [10, 20, 30]})
        chart = ggsql.render_altair(table, "VISUALISE x, y DRAW point", height=400)
        chart_dict = chart.to_dict()
        assert chart_dict["height"] == 400

    def test_default_preserves_ggsql_dimensions(self):
        table = pa.table({"x": [1, 2, 3], "y": [10, 20, 30]})

        reader = ggsql.DuckDBReader("duckdb://memory")
        reader.register("__data__", table)
        spec = reader.execute("SELECT * FROM __data__ VISUALISE x, y DRAW point")
        raw_spec = json.loads(ggsql.VegaLiteWriter().render(spec))

        chart = ggsql.render_altair(table, "VISUALISE x, y DRAW point")
        chart_dict = chart.to_dict()

        if "height" in raw_spec:
            assert chart_dict["height"] == raw_spec["height"]
        if "width" in raw_spec:
            assert chart_dict["width"] == raw_spec["width"]


class TestRenderAltairChartTypeDetection:
    """Tests for correct Altair chart type detection based on spec structure."""

    def test_simple_chart_returns_layer_chart(self):
        """Simple DRAW specs produce LayerChart (ggsql always wraps in layer)."""
        table = pa.table({"x": [1, 2, 3], "y": [10, 20, 30]})
        chart = ggsql.render_altair(table, "VISUALISE x, y DRAW point")
        # ggsql wraps all charts in a layer
        assert isinstance(chart, altair.LayerChart)

    def test_layered_chart_can_round_trip(self):
        """LayerChart can be converted to dict and back."""
        table = pa.table({"x": [1, 2, 3], "y": [10, 20, 30]})
        chart = ggsql.render_altair(table, "VISUALISE x, y DRAW point")

        # Convert to dict and back
        spec = chart.to_dict()
        assert "layer" in spec

        # Should be able to recreate from dict
        recreated = altair.LayerChart.from_dict(spec)
        assert isinstance(recreated, altair.LayerChart)

    def test_faceted_chart_returns_facet_chart(self):
        """FACET specs produce FacetChart."""
        table = pa.table(
            {
                "x": [1, 2, 3, 4, 5, 6],
                "y": [10, 20, 30, 40, 50, 60],
                "group": ["A", "A", "A", "B", "B", "B"],
            }
        )
        # Need validate=False because ggsql produces v6 specs
        chart = ggsql.render_altair(
            table, "VISUALISE x, y FACET group DRAW point", validate=False
        )
        assert isinstance(chart, altair.FacetChart)

    def test_faceted_chart_can_round_trip(self):
        """FacetChart can be converted to dict and back."""
        table = pa.table(
            {
                "x": [1, 2, 3, 4, 5, 6],
                "y": [10, 20, 30, 40, 50, 60],
                "group": ["A", "A", "A", "B", "B", "B"],
            }
        )
        chart = ggsql.render_altair(
            table, "VISUALISE x, y FACET group DRAW point", validate=False
        )

        # Convert to dict (skip validation for ggsql specs)
        spec = chart.to_dict(validate=False)
        assert "facet" in spec or "spec" in spec

        # Should be able to recreate from dict (with validation disabled)
        recreated = altair.FacetChart.from_dict(spec, validate=False)
        assert isinstance(recreated, altair.FacetChart)

    def test_chart_with_color_encoding(self):
        """Charts with color encoding still return correct type."""
        table = pa.table(
            {
                "x": [1, 2, 3, 4],
                "y": [10, 20, 30, 40],
                "category": ["A", "B", "A", "B"],
            }
        )
        chart = ggsql.render_altair(
            table, "VISUALISE x, y, category AS color DRAW point"
        )
        # Should still be a LayerChart (ggsql wraps in layer)
        assert isinstance(chart, altair.LayerChart)


class TestAltairChartFallback:
    """Tests for fallback to Chart when a specific Altair type can't parse the spec."""

    def test_layer_fallback_to_chart(self):
        """Specs with 'layer' fall back to Chart when LayerChart.from_json fails."""
        from ggsql import _json_to_altair_chart

        spec = json.dumps(
            {
                "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
                "data": {"values": [{"x": 1, "y": 2}]},
                "layer": [
                    {
                        "mark": "point",
                        "encoding": {"x": {"field": "x", "type": "quantitative"}},
                        "not_a_real_property": True,
                    }
                ],
            }
        )
        chart = _json_to_altair_chart(spec)
        assert isinstance(chart, altair.Chart)

    def test_layer_uses_specific_type_when_possible(self):
        """Specs with 'layer' use LayerChart when parsing succeeds."""
        from ggsql import _json_to_altair_chart

        spec = json.dumps(
            {
                "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
                "data": {"values": [{"x": 1, "y": 2}]},
                "layer": [
                    {
                        "mark": "point",
                        "encoding": {"x": {"field": "x", "type": "quantitative"}},
                    }
                ],
            }
        )
        chart = _json_to_altair_chart(spec)
        assert isinstance(chart, altair.LayerChart)


class TestRenderAltairErrorHandling:
    """Tests for error handling in render_altair()."""

    def test_invalid_viz_raises(self):
        table = pa.table({"x": [1, 2, 3], "y": [10, 20, 30]})
        with pytest.raises(ValueError):
            ggsql.render_altair(table, "NOT VALID SYNTAX")


class TestTwoStageAPIIntegration:
    """Integration tests for the two-stage reader.execute() -> render API."""

    def test_end_to_end_workflow(self):
        """Complete workflow: create reader, register data, execute, render."""
        # Create reader
        reader = ggsql.DuckDBReader("duckdb://memory")

        # Register data
        table = pa.table(
            {
                "date": ["2024-01-01", "2024-01-02", "2024-01-03"],
                "value": [10, 20, 30],
                "region": ["North", "South", "North"],
            }
        )
        reader.register("sales", table)

        # Execute visualization
        spec = reader.execute(
            "SELECT * FROM sales VISUALISE date AS x, value AS y, region AS color DRAW line",
        )

        # Verify metadata
        assert spec.metadata()["rows"] == 3
        assert spec.layer_count() == 1

        # Render to Vega-Lite
        writer = ggsql.VegaLiteWriter()
        result = writer.render(spec)

        # Verify output
        spec_dict = json.loads(result)
        assert "$schema" in spec_dict
        assert "line" in json.dumps(spec_dict)

    def test_can_introspect_spec(self):
        """Test all introspection methods on Spec."""
        reader = ggsql.DuckDBReader("duckdb://memory")
        spec = reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")

        # All these should work without error
        assert spec.sql() is not None
        assert spec.visual() is not None
        assert spec.layer_count() >= 1
        assert spec.metadata() is not None
        assert spec.data() is not None
        assert spec.warnings() is not None

        # Layer-specific accessors (may return None)
        _ = spec.layer_data(0)
        _ = spec.stat_data(0)
        _ = spec.layer_sql(0)
        _ = spec.stat_sql(0)


class TestCustomReader:
    """Tests for custom Python reader support."""

    def test_custom_reader_with_register(self):
        """Custom reader with register() support."""

        class RegisterReader:
            def __init__(self):
                self.conn = duckdb.connect()

            def execute_sql(self, sql: str) -> pa.Table:
                return self.conn.execute(sql).arrow()

            def register(self, name: str, table: pa.Table, _replace: bool) -> None:
                self.conn.register(name, table)

        reader = RegisterReader()
        spec = ggsql.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point", reader)
        assert spec is not None

    def test_custom_reader_error_handling(self):
        """Custom reader errors are propagated."""

        class ErrorReader:
            def execute_sql(self, sql: str) -> pa.Table:
                raise ValueError("Custom reader error")

        reader = ErrorReader()
        with pytest.raises(ValueError, match="Custom reader error"):
            ggsql.execute("SELECT 1 VISUALISE x, y DRAW point", reader)

    def test_custom_reader_wrong_return_type(self):
        """Custom reader returning wrong type raises TypeError."""

        class WrongTypeReader:
            def execute_sql(self, sql: str):
                return {"x": [1, 2, 3]}  # dict, not Table

        reader = WrongTypeReader()
        with pytest.raises((ValueError, TypeError)):
            ggsql.execute("SELECT 1 VISUALISE x, y DRAW point", reader)

    def test_native_reader_fast_path(self):
        """Native DuckDBReader still works (fast path)."""
        reader = ggsql.DuckDBReader("duckdb://memory")
        spec = reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")
        assert spec.metadata()["rows"] == 1

    def test_custom_reader_can_render(self):
        """Custom reader result can be rendered to Vega-Lite."""

        class DuckDBBackedReader:
            def __init__(self):
                self.conn = duckdb.connect()
                self.conn.execute(
                    "CREATE TABLE data AS SELECT * FROM ("
                    "VALUES (1, 10, 'A'), (2, 40, 'B'), (3, 20, 'A'), "
                    "(4, 50, 'B'), (5, 30, 'A')"
                    ") AS t(x, y, category)"
                )

            def execute_sql(self, sql: str) -> pa.Table:
                return self.conn.execute(sql).arrow()

            def register(self, name: str, table: pa.Table, _replace: bool) -> None:
                self.conn.register(name, table)

        reader = DuckDBBackedReader()
        spec = ggsql.execute(
            "SELECT * FROM data VISUALISE x, y, category AS color DRAW point",
            reader,
        )

        writer = ggsql.VegaLiteWriter()
        result = writer.render(spec)

        spec_dict = json.loads(result)
        assert "$schema" in spec_dict
        assert "vega-lite" in spec_dict["$schema"]

    def test_custom_reader_execute_sql_called(self):
        """Verify execute_sql() is called on the custom reader."""

        class RecordingReader:
            def __init__(self):
                self.conn = duckdb.connect()
                self.conn.execute(
                    "CREATE TABLE data AS SELECT * FROM (VALUES (1, 2)) AS t(x, y)"
                )
                self.execute_calls = []

            def execute_sql(self, sql: str) -> pa.Table:
                self.execute_calls.append(sql)
                return self.conn.execute(sql).arrow()

            def register(self, name: str, table: pa.Table, _replace: bool) -> None:
                self.conn.register(name, table)

        reader = RecordingReader()
        ggsql.execute(
            "SELECT * FROM data VISUALISE x, y DRAW point",
            reader,
        )

        # execute_sql() should have been called at least once
        assert len(reader.execute_calls) > 0
        # All calls should be valid SQL strings
        assert all(isinstance(sql, str) for sql in reader.execute_calls)

    @pytest.mark.skipif(not HAS_IBIS, reason="ibis not installed")
    def test_custom_reader_ibis(self):
        """Test custom reader using ibis as backend."""

        class IbisReader:
            def __init__(self):
                self.con = ibis.duckdb.connect()

            def execute_sql(self, sql: str) -> pa.Table:
                return self.con.con.execute(sql).arrow()

            def register(
                self, name: str, table: pa.Table, replace: bool = True
            ) -> None:
                self.con.create_table(name, table, overwrite=replace)

            def unregister(self, name: str) -> None:
                self.con.drop_table(name)

        reader = IbisReader()
        table = pa.table({"x": [1, 2, 3], "y": [10, 20, 30]})
        reader.register("mydata", table)

        spec = ggsql.execute(
            "SELECT * FROM mydata VISUALISE x, y DRAW point",
            reader,
        )

        assert spec.metadata()["rows"] == 3
        writer = ggsql.VegaLiteWriter()
        json_output = writer.render(spec)
        assert "point" in json_output


class TestVegaLiteWriterRenderChart:
    """Tests for VegaLiteWriter.render_chart() method."""

    def test_render_chart_returns_altair_chart(self):
        """render_chart() returns an Altair chart object."""
        reader = ggsql.DuckDBReader("duckdb://memory")
        spec = reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")
        writer = ggsql.VegaLiteWriter()
        chart = writer.render_chart(spec)
        assert isinstance(chart, altair.TopLevelMixin)

    def test_render_chart_layer(self):
        """render_chart() returns LayerChart for layered specs."""
        reader = ggsql.DuckDBReader("duckdb://memory")
        spec = reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")
        writer = ggsql.VegaLiteWriter()
        chart = writer.render_chart(spec)
        assert isinstance(chart, altair.LayerChart)

    def test_render_chart_facet(self):
        """render_chart() returns FacetChart for faceted specs."""
        reader = ggsql.DuckDBReader("duckdb://memory")
        table = pa.table(
            {
                "x": [1, 2, 3, 4, 5, 6],
                "y": [10, 20, 30, 40, 50, 60],
                "group": ["A", "A", "A", "B", "B", "B"],
            }
        )
        reader.register("data", table)
        spec = reader.execute(
            "SELECT * FROM data VISUALISE x, y FACET group DRAW point"
        )
        writer = ggsql.VegaLiteWriter()
        chart = writer.render_chart(spec, validate=False)
        assert isinstance(chart, altair.FacetChart)

    def test_render_chart_height_width(self):
        """render_chart() injects height/width into the spec when provided."""
        reader = ggsql.DuckDBReader("duckdb://memory")
        spec = reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")
        writer = ggsql.VegaLiteWriter()
        chart = writer.render_chart(spec, height=300, width=500)
        chart_dict = chart.to_dict()
        assert chart_dict["height"] == 300
        assert chart_dict["width"] == 500

    def test_render_chart_height_only(self):
        """render_chart() can set height without width."""
        reader = ggsql.DuckDBReader("duckdb://memory")
        spec = reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")
        writer = ggsql.VegaLiteWriter()
        chart = writer.render_chart(spec, height=400)
        chart_dict = chart.to_dict()
        assert chart_dict["height"] == 400

    def test_render_chart_default_preserves_ggsql_dimensions(self):
        """When height/width are not specified, ggsql's defaults are preserved."""
        reader = ggsql.DuckDBReader("duckdb://memory")
        spec = reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")
        writer = ggsql.VegaLiteWriter()

        raw_spec = json.loads(writer.render(spec))
        chart = writer.render_chart(spec)
        chart_dict = chart.to_dict()

        if "height" in raw_spec:
            assert chart_dict["height"] == raw_spec["height"]
        if "width" in raw_spec:
            assert chart_dict["width"] == raw_spec["width"]


class TestSVGWriter:
    def test_create_writer(self):
        writer = ggsql.SVGWriter()
        assert writer is not None

    def test_render_to_svg(self):
        reader = ggsql.DuckDBReader("duckdb://memory")
        spec = reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")
        svg = ggsql.SVGWriter().render(spec)
        assert isinstance(svg, str)
        assert "<svg" in svg

    def test_render_with_dimensions(self):
        reader = ggsql.DuckDBReader("duckdb://memory")
        spec = reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")
        svg = ggsql.SVGWriter(width=400, height=300, dpi=96).render(spec)
        assert isinstance(svg, str)
        assert "<svg" in svg


class TestPDFWriter:
    def test_create_writer(self):
        writer = ggsql.PDFWriter()
        assert writer is not None

    def test_render_to_pdf(self):
        reader = ggsql.DuckDBReader("duckdb://memory")
        spec = reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")
        pdf = ggsql.PDFWriter().render(spec)
        assert isinstance(pdf, bytes)
        assert pdf.startswith(b"%PDF")

    def test_render_with_dimensions(self):
        reader = ggsql.DuckDBReader("duckdb://memory")
        spec = reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")
        pdf = ggsql.PDFWriter(width=400, height=300, dpi=96).render(spec)
        assert isinstance(pdf, bytes)
        assert pdf.startswith(b"%PDF")


class TestHepWriter:
    def test_create_writer(self):
        writer = ggsql.HepWriter()
        assert writer is not None

    def test_render_to_hep(self):
        reader = ggsql.DuckDBReader("duckdb://memory")
        spec = reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")
        doc = ggsql.HepWriter().render(spec)
        assert isinstance(doc, bytes)
        assert len(doc) > 0


class TestWriterRepr:
    def test_svg_writer_repr(self):
        assert repr(ggsql.SVGWriter()) == "SVGWriter(width=600, height=400, dpi=96.0)"
        assert (
            repr(ggsql.SVGWriter(width=800, height=600, dpi=150))
            == "SVGWriter(width=800, height=600, dpi=150)"
        )

    def test_pdf_writer_repr(self):
        assert repr(ggsql.PDFWriter()) == "PDFWriter(width=600, height=400, dpi=96.0)"
        assert (
            repr(ggsql.PDFWriter(width=800, height=600, dpi=150.5))
            == "PDFWriter(width=800, height=600, dpi=150.5)"
        )

    def test_hep_writer_repr(self):
        assert repr(ggsql.HepWriter()) == "HepWriter()"

    def test_vegalite_writer_repr(self):
        assert repr(ggsql.VegaLiteWriter()) == "VegaLiteWriter()"


class TestSave:
    @pytest.fixture
    def spec(self):
        reader = ggsql.DuckDBReader("duckdb://memory")
        return reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")

    def test_save_svg(self, spec, tmp_path):
        out = tmp_path / "plot.svg"
        ggsql.save(spec, out)
        assert "<svg" in out.read_text()

    def test_save_pdf(self, spec, tmp_path):
        out = tmp_path / "plot.pdf"
        ggsql.save(spec, out)
        assert out.read_bytes().startswith(b"%PDF")

    def test_save_hep(self, spec, tmp_path):
        out = tmp_path / "plot.hep"
        ggsql.save(spec, out)
        assert len(out.read_bytes()) > 0

    def test_save_json(self, spec, tmp_path):
        out = tmp_path / "plot.json"
        ggsql.save(spec, out)
        spec_dict = json.loads(out.read_text())
        assert "$schema" in spec_dict or "mark" in spec_dict

    def test_save_with_dimensions(self, spec, tmp_path):
        out = tmp_path / "plot.svg"
        ggsql.save(spec, out, width=400, height=300, dpi=96)
        assert "<svg" in out.read_text()

    def test_save_unknown_extension_raises(self, spec, tmp_path):
        with pytest.raises(ValueError, match="Unsupported file extension"):
            ggsql.save(spec, tmp_path / "plot.xyz")

    @pytest.mark.parametrize(
        "ext", [".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"]
    )
    def test_save_raster_extension_suggests_svg(self, spec, tmp_path, ext):
        with pytest.raises(ValueError, match=r"Save as \.svg"):
            ggsql.save(spec, tmp_path / f"plot{ext}")

    def test_save_invalid_dpi_raises(self, spec, tmp_path):
        with pytest.raises(ValueError, match="dpi must be a positive"):
            ggsql.save(spec, tmp_path / "plot.svg", dpi=0)


class TestWriterWarnings:
    """Writer render issues (from Rust's render_reporting) are surfaced as
    UserWarnings while the rendered output is returned unchanged."""

    class _StubInner:
        """Stand-in for the Rust writer returning a fixed (output, issues)."""

        def __init__(self, output, issues):
            self._output = output
            self._issues = issues

        def render(self, _spec):
            return self._output, self._issues

    @pytest.fixture
    def spec(self):
        reader = ggsql.DuckDBReader("duckdb://memory")
        return reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")

    @pytest.mark.parametrize(
        "writer_cls,output",
        [
            (ggsql.SVGWriter, "<svg/>"),
            (ggsql.PDFWriter, b"%PDF-stub"),
            (ggsql.HepWriter, b"hep-stub"),
        ],
    )
    def test_render_issues_emit_userwarning(self, spec, writer_cls, output):
        writer = writer_cls()
        issues = ["font 'Foo' not found", "missing glyph for '×'"]
        writer._inner = self._StubInner(output, issues)

        with pytest.warns(UserWarning, match="2 issue") as record:
            result = writer.render(spec)

        assert result == output
        message = str(record[0].message)
        assert all(issue in message for issue in issues)

    def test_empty_issues_emit_no_warning(self, spec):
        writer = ggsql.SVGWriter()
        writer._inner = self._StubInner("<svg/>", [])

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            result = writer.render(spec)

        assert result == "<svg/>"


class TestVersion:
    def test_version_matches_pyproject(self):
        pyproject = Path(__file__).parent.parent / "pyproject.toml"
        match = re.search(r'^version = "([^"]+)"', pyproject.read_text(), re.M)
        assert match is not None
        assert ggsql.__version__ == match.group(1)


class TestWriterDpiValidation:
    @pytest.mark.parametrize("bad_dpi", [0, -1, -96.0, float("nan"), float("inf")])
    def test_svg_writer_rejects_invalid_dpi(self, bad_dpi):
        with pytest.raises(ValueError, match="dpi must be a positive"):
            ggsql.SVGWriter(dpi=bad_dpi)

    @pytest.mark.parametrize("bad_dpi", [0, -1, -96.0, float("nan"), float("inf")])
    def test_pdf_writer_rejects_invalid_dpi(self, bad_dpi):
        with pytest.raises(ValueError, match="dpi must be a positive"):
            ggsql.PDFWriter(dpi=bad_dpi)


class TestWriterDimensionValidation:
    @pytest.mark.parametrize("writer", [ggsql.SVGWriter, ggsql.PDFWriter])
    @pytest.mark.parametrize("bad_width", [0, -1, -600])
    def test_rejects_nonpositive_width(self, writer, bad_width):
        with pytest.raises(ValueError, match="width must be a positive"):
            writer(width=bad_width)

    @pytest.mark.parametrize("writer", [ggsql.SVGWriter, ggsql.PDFWriter])
    @pytest.mark.parametrize("bad_height", [0, -1, -400])
    def test_rejects_nonpositive_height(self, writer, bad_height):
        with pytest.raises(ValueError, match="height must be a positive"):
            writer(height=bad_height)

    def test_save_rejects_nonpositive_dimensions(self, tmp_path):
        reader = ggsql.DuckDBReader("duckdb://memory")
        spec = reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")
        with pytest.raises(ValueError, match="width must be a positive"):
            ggsql.save(spec, tmp_path / "plot.svg", width=0)


class TestWriterConcurrency:
    """Native writers release the GIL while rendering; concurrent renders
    of a shared writer and spec must produce identical, correct output."""

    def test_concurrent_svg_renders(self):
        from concurrent.futures import ThreadPoolExecutor

        reader = ggsql.DuckDBReader("duckdb://memory")
        spec = reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")
        writer = ggsql.SVGWriter()
        expected = writer.render(spec)

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: writer.render(spec), range(8)))

        assert all(svg == expected for svg in results)


class TestBuiltinData:
    """Integration coverage for builtin datasets (ggsql:... relations)."""

    def test_execute_builtin_dataset(self):
        reader = ggsql.DuckDBReader("duckdb://memory")
        spec = reader.execute(
            "SELECT * FROM ggsql:penguins "
            "VISUALISE DRAW point MAPPING bill_len AS x, bill_dep AS y"
        )
        assert spec is not None

    def test_builtin_dataset_renders_svg(self):
        reader = ggsql.DuckDBReader("duckdb://memory")
        spec = reader.execute(
            "VISUALISE FROM ggsql:airquality DRAW point MAPPING Temp AS x, Ozone AS y"
        )
        svg = ggsql.SVGWriter().render(spec)
        assert "<svg" in svg


class _CountingReader:
    """Bridged primary that counts source reads, backed by duckdb."""

    def __init__(self):
        self.calls = 0
        self.conn = duckdb.connect()
        self.conn.execute(
            "CREATE TABLE t AS SELECT * FROM (VALUES (1), (2), (3)) AS t(v)"
        )

    def execute_sql(self, sql: str) -> pa.Table:
        self.calls += 1
        return self.conn.execute(sql).arrow()

    def register(self, name: str, table: pa.Table, _replace: bool) -> None:
        self.conn.register(name, table)

    def unregister(self, name: str) -> None:
        self.conn.unregister(name)


class TestCachingReader:
    def test_create_with_connection_string(self):
        reader = ggsql.CachingReader("duckdb://memory")
        assert reader is not None

    def test_exported_at_package_level(self):
        assert "CachingReader" in ggsql.__all__
        assert ggsql.CachingReader.__module__ == "ggsql._ggsql"

    def test_rejects_non_duckdb_scheme(self):
        with pytest.raises(ValueError, match="duckdb://"):
            ggsql.CachingReader("sqlite://memory")

    def test_rejects_duckdb_reader_instance(self):
        inner = ggsql.DuckDBReader("duckdb://memory")
        with pytest.raises(ValueError, match="connection string"):
            ggsql.CachingReader(inner)

    def test_execute(self):
        reader = ggsql.CachingReader("duckdb://memory")
        spec = reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")
        assert spec.metadata()["rows"] == 1

    def test_execute_sql(self):
        reader = ggsql.CachingReader("duckdb://memory")
        table = reader.execute_sql("SELECT 1 AS x")
        assert table.num_rows == 1

    def test_execute_sql_cached(self):
        """execute_sql_cached runs on the cache, never touching the primary.

        This is the surface for per-connection setup that ggsql's derived
        SQL depends on (e.g. INSTALL/LOAD of DuckDB extensions), which
        would fail or take no effect on a locked-down primary.
        """
        primary = _CountingReader()
        reader = ggsql.CachingReader(primary)
        table = reader.execute_sql_cached("SELECT 42 AS x")
        assert table.column("x").to_pylist() == [42]
        assert primary.calls == 0

    def test_execute_sql_cached_error(self):
        reader = ggsql.CachingReader("duckdb://memory")
        with pytest.raises(ValueError):
            reader.execute_sql_cached("SELECT * FROM __ggsql_missing__")

    def test_register_and_query(self):
        reader = ggsql.CachingReader("duckdb://memory")
        reader.register("mytab", pa.table({"x": [1, 2], "y": [3, 4]}))
        spec = reader.execute("SELECT * FROM mytab VISUALISE x, y DRAW point")
        assert spec.metadata()["rows"] == 2

    def test_memoizes_identical_reads(self):
        primary = _CountingReader()
        reader = ggsql.CachingReader(primary)
        reader.execute_sql("SELECT * FROM t")
        reader.execute_sql("SELECT * FROM t")
        assert primary.calls == 1

    def test_distinct_reads_each_hit_primary(self):
        primary = _CountingReader()
        reader = ggsql.CachingReader(primary)
        reader.execute_sql("SELECT * FROM t")
        reader.execute_sql("SELECT v + 1 AS v FROM t")
        assert primary.calls == 2

    def test_ttl_expiry(self):
        primary = _CountingReader()
        reader = ggsql.CachingReader(primary, ttl_secs=1)
        reader.execute_sql("SELECT * FROM t")
        time.sleep(1.1)
        reader.execute_sql("SELECT * FROM t")
        assert primary.calls == 2

    def test_disabled_kwarg(self):
        primary = _CountingReader()
        reader = ggsql.CachingReader(primary, enabled=False)
        reader.execute_sql("SELECT * FROM t")
        reader.execute_sql("SELECT * FROM t")
        assert primary.calls == 2

    def test_disabled_env_var(self, monkeypatch):
        monkeypatch.setenv("GGSQL_CACHE_DISABLED", "1")
        primary = _CountingReader()
        reader = ggsql.CachingReader(primary)
        reader.execute_sql("SELECT * FROM t")
        reader.execute_sql("SELECT * FROM t")
        assert primary.calls == 2


class TestCachingReaderCapabilities:
    """The scenarios querychat cannot serve today without CachingReader."""

    def test_layer_level_source_through_bridge(self):
        """VISUALISE ... FROM <table> works through a bridged primary.

        This is the querychat scenario: today querychat rejects any query
        with a layer-level source because it cannot be replayed from one
        final SQL result. Through CachingReader the layer source is staged
        into the cache, so the query runs end to end.
        """
        primary = _CountingReader()
        reader = ggsql.CachingReader(primary)
        spec = reader.execute("VISUALISE x DRAW histogram MAPPING v AS x FROM t")
        assert spec is not None

    def test_registered_table_routes_to_cache(self):
        """A registered table is cache-resident; reading it skips the primary."""
        primary = _CountingReader()
        reader = ggsql.CachingReader(primary)
        reader.register("only_in_cache", pa.table({"v": [1, 2, 3]}))
        spec = reader.execute(
            "VISUALISE x DRAW histogram MAPPING v AS x FROM only_in_cache"
        )
        assert spec is not None
        assert primary.calls == 0

    def test_top_level_execute_fast_path(self):
        """ggsql.execute() recognizes CachingReader as native (no bridge)."""
        reader = ggsql.CachingReader("duckdb://memory")
        spec = ggsql.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point", reader)
        assert spec.metadata()["rows"] == 1


class TestArrowBoundaryConversion:
    """Edge cases for the Arrow C Data Interface boundary, with IPC parity.

    Each test runs twice: once on the default zero-copy C stream path and
    once with GGSQL_ARROW_IPC=1 (the legacy IPC fallback).
    """

    @pytest.fixture(params=["stream", "ipc"])
    def boundary_mode(self, request, monkeypatch):
        if request.param == "ipc":
            monkeypatch.setenv("GGSQL_ARROW_IPC", "1")
        else:
            monkeypatch.delenv("GGSQL_ARROW_IPC", raising=False)
        return request.param

    @pytest.fixture
    def reader(self, boundary_mode):
        return ggsql.DuckDBReader("duckdb://memory")

    def test_dictionary_encoded_column(self, boundary_mode):
        """Dictionary-encoded columns cross the Python->Rust boundary.

        Exercised via a custom Python reader because DuckDBReader.register
        cannot ingest dictionary columns (DuckDB ArrowVTab limitation,
        pre-existing in both boundary modes).
        """
        codes = ["a", "b", "a", "c", "b"] * 20

        class DictReader:
            def execute_sql(self, sql):
                return pa.table(
                    {
                        "x": list(range(len(codes))),
                        "cat": pa.array(codes).dictionary_encode(),
                    }
                )

        spec = ggsql.execute(
            "SELECT * FROM t VISUALISE x, cat DRAW point", DictReader()
        )
        assert spec.metadata()["rows"] == len(codes)

    def test_timestamp_with_timezone(self, reader):
        ts = pa.array(
            [datetime(2024, 1, 1, 12, 0), datetime(2024, 6, 15, 8, 30)],
            type=pa.timestamp("us", tz="UTC"),
        )
        reader.register("t", pa.table({"ts": ts}))
        result = reader.execute_sql("SELECT ts FROM t")
        # Compare as epoch microseconds: to_pylist() on tz-aware timestamps
        # needs a tz database, which Windows lacks without the tzdata package
        result_us = result.column("ts").cast(pa.int64()).to_pylist()
        assert sorted(result_us) == sorted(ts.cast(pa.int64()).to_pylist())

    def test_empty_result_set(self, reader):
        """A 0-row query result crosses the Rust->Python boundary cleanly."""
        reader.register("t", pa.table({"x": pa.array([1, 2, 3], type=pa.int64())}))
        result = reader.execute_sql("SELECT * FROM t WHERE x > 100")
        assert result.num_rows == 0

    def test_register_empty_table_parity(self, reader):
        """Registering a 0-row table fails identically in both modes.

        Pre-existing behavior: py_to_df (both IPC and C stream) collapses a
        0-batch table to DataFrame::empty() with an empty schema, which
        DuckDB's ArrowVTab rejects. Documented here as parity, not fixed.
        """
        table = pa.table({"x": pa.array([], type=pa.int64())})
        with pytest.raises(ValueError, match="at least one column"):
            reader.register("t", table)

    def test_chunked_table(self, reader):
        batches = [
            pa.record_batch({"x": pa.array(range(i * 100, (i + 1) * 100))})
            for i in range(5)
        ]
        table = pa.Table.from_batches(batches)
        reader.register("t", table)
        result = reader.execute_sql("SELECT x FROM t")
        assert sorted(result.column("x").to_pylist()) == list(range(500))

    def test_nullable_columns(self, reader):
        table = pa.table(
            {
                "a": pa.array([1, None, 3], type=pa.int64()),
                "b": pa.array(["x", None, "z"]),
            }
        )
        reader.register("t", table)
        result = reader.execute_sql("SELECT * FROM t")
        assert result.column("a").to_pylist().count(None) == 1
        assert sorted(v for v in result.column("a").to_pylist() if v is not None) == [1, 3]
        assert result.column("b").to_pylist().count(None) == 1

    def test_extension_type(self, reader):
        uuid_type = getattr(pa, "uuid", None)
        if uuid_type is None:
            pytest.skip("no uuid extension type in this pyarrow")
        import uuid as uuid_mod

        uuids = [uuid_mod.UUID(int=i) for i in range(5)]
        table = pa.table({"u": pa.array(uuids, type=uuid_type())})
        reader.register("t", table)
        result = reader.execute_sql("SELECT u FROM t")
        # Extension metadata degrades to the storage type (binary) through
        # DuckDB in both boundary modes; the values must survive
        assert sorted(result.column("u").to_pylist()) == sorted(u.bytes for u in uuids)

    def test_ipc_fallback_env_var(self, monkeypatch):
        """GGSQL_ARROW_IPC=1 forces the legacy IPC path (smoke test)."""
        monkeypatch.setenv("GGSQL_ARROW_IPC", "1")
        reader = ggsql.DuckDBReader("duckdb://memory")
        reader.register("t", pa.table({"x": [1, 2, 3]}))
        result = reader.execute_sql("SELECT * FROM t")
        assert result.column("x").to_pylist() == [1, 2, 3]
