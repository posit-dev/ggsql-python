from __future__ import annotations

import json
from typing import Any, Union

import altair
import narwhals as nw
from narwhals.typing import IntoFrame

from ggsql._ggsql import (
    DuckDBReader,
    VegaLiteWriter as _RustVegaLiteWriter,
    Validated,
    Spec,
    validate as _rust_validate,
    execute as _rust_execute,
)

for _cls in (DuckDBReader, _RustVegaLiteWriter, Validated, Spec):
    _cls.__module__ = "ggsql._ggsql"
del _cls

DuckDBReader.__doc__ = """DuckDB database reader for executing SQL queries."""

Validated.__doc__ = """Result of :func:`validate` — query inspection without SQL execution."""

Spec.__doc__ = """Resolved visualization specification returned by ``reader.execute()``."""


def validate(query: str) -> Validated:
    """Validate query syntax and semantics without executing SQL.

    Parameters
    ----------
    query : str
        The ggsql query to validate.

    Returns
    -------
    Validated
        Validation result with query inspection methods.
    """
    return _rust_validate(query)


def execute(query: str, reader: Any) -> Spec:
    """Execute a ggsql query using a custom Python reader.

    For native readers, prefer :meth:`DuckDBReader.execute` directly.

    Parameters
    ----------
    query : str
        The ggsql query to execute.
    reader
        A native Reader or any object with an
        ``execute_sql(sql: str) -> polars.DataFrame`` method.

    Returns
    -------
    Spec
        The resolved visualization specification ready for rendering.
    """
    return _rust_execute(query, reader)

__all__ = [
    # Classes
    "DuckDBReader",
    "VegaLiteWriter",
    "Validated",
    "Spec",
    # Functions
    "validate",
    "execute",
    "render_altair",
]
__version__ = "0.2.7"

# Type alias for any Altair chart type
AltairChart = Union[
    altair.Chart,
    altair.LayerChart,
    altair.FacetChart,
    altair.ConcatChart,
    altair.HConcatChart,
    altair.VConcatChart,
    altair.RepeatChart,
]


def _json_to_altair_chart(vegalite_json: str, **kwargs: Any) -> AltairChart:
    """Convert a Vega-Lite JSON string to the appropriate Altair chart type."""
    spec = json.loads(vegalite_json)

    if "layer" in spec:
        return altair.LayerChart.from_json(vegalite_json, **kwargs)
    elif "facet" in spec or "spec" in spec:
        return altair.FacetChart.from_json(vegalite_json, **kwargs)
    elif "concat" in spec:
        return altair.ConcatChart.from_json(vegalite_json, **kwargs)
    elif "hconcat" in spec:
        return altair.HConcatChart.from_json(vegalite_json, **kwargs)
    elif "vconcat" in spec:
        return altair.VConcatChart.from_json(vegalite_json, **kwargs)
    elif "repeat" in spec:
        return altair.RepeatChart.from_json(vegalite_json, **kwargs)
    else:
        return altair.Chart.from_json(vegalite_json, **kwargs)


class VegaLiteWriter:
    """Vega-Lite v6 JSON output writer.

    Methods
    -------
    render(spec)
        Render a Spec to a Vega-Lite JSON string.
    render_chart(spec, **kwargs)
        Render a Spec to an Altair chart object.
    """

    def __init__(self) -> None:
        self._inner = _RustVegaLiteWriter()

    def render(self, spec: Spec) -> str:
        """Render a Spec to a Vega-Lite JSON string."""
        return self._inner.render(spec)

    def render_chart(self, spec: Spec, **kwargs: Any) -> AltairChart:
        """Render a Spec to an Altair chart object.

        Parameters
        ----------
        spec
            The resolved visualization specification from ``reader.execute()``.
        **kwargs
            Additional keyword arguments passed to ``altair.Chart.from_json()``.
            Common options include ``validate=False`` to skip schema validation.

        Returns
        -------
        AltairChart
            An Altair chart object (Chart, LayerChart, FacetChart, etc.).
        """
        vegalite_json = self.render(spec)
        return _json_to_altair_chart(vegalite_json, **kwargs)


def render_altair(
    df: IntoFrame,
    viz: str,
    **kwargs: Any,
) -> AltairChart:
    """Render a DataFrame with a VISUALISE spec to an Altair chart.

    Parameters
    ----------
    df
        Data to visualize. Accepts polars, pandas, or any narwhals-compatible
        DataFrame. LazyFrames are collected automatically.
    viz
        VISUALISE spec string (e.g., "VISUALISE x, y DRAW point")
    **kwargs
        Additional keyword arguments passed to `from_json()`.
        Common options include `validate=False` to skip schema validation.

    Returns
    -------
    AltairChart
        An Altair chart object (Chart, LayerChart, FacetChart, etc.).
    """
    df = nw.from_native(df, pass_through=True)

    if isinstance(df, nw.LazyFrame):
        df = df.collect()

    if not isinstance(df, nw.DataFrame):
        raise TypeError("df must be a narwhals DataFrame or compatible type")

    pl_df = df.to_polars()

    # Create temporary reader and register data
    reader = DuckDBReader("duckdb://memory")
    reader.register("__data__", pl_df)

    # Build full query: SELECT * FROM __data__ + VISUALISE clause
    query = f"SELECT * FROM __data__ {viz}"

    # Execute and render
    spec = reader.execute(query)
    writer = VegaLiteWriter()
    vegalite_json = writer.render(spec)

    return _json_to_altair_chart(vegalite_json, **kwargs)
