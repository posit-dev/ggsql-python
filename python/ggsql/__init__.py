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
    validate,
    execute,
)

# PyO3 classes default to __module__ = "builtins"; point them at their real
# home so docs tooling (great-docs/griffe) can locate them.
for _cls in (DuckDBReader, _RustVegaLiteWriter, Validated, Spec):
    _cls.__module__ = "ggsql._ggsql"
del _cls

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
__version__ = "0.5.2"

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
    kwargs.setdefault("validate", False)
    spec = json.loads(vegalite_json)

    chart_class: type[AltairChart] | None = None
    if "layer" in spec:
        chart_class = altair.LayerChart
    elif "facet" in spec or "spec" in spec:
        chart_class = altair.FacetChart
    elif "concat" in spec:
        chart_class = altair.ConcatChart
    elif "hconcat" in spec:
        chart_class = altair.HConcatChart
    elif "vconcat" in spec:
        chart_class = altair.VConcatChart
    elif "repeat" in spec:
        chart_class = altair.RepeatChart

    if chart_class is not None:
        try:
            return chart_class.from_json(vegalite_json, **kwargs)
        except Exception:
            pass

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

    def render_chart(
        self,
        spec: Spec,
        *,
        validate: bool = False,
        height: int | None = None,
        width: int | None = None,
        **kwargs: Any,
    ) -> AltairChart:
        """Render a Spec to an Altair chart object.

        Parameters
        ----------
        spec
            The resolved visualization specification from ``reader.execute()``.
        validate
            Whether to validate the spec against the Vega-Lite schema.
        height
            Chart height in pixels. When ``None`` (the default), the height
            produced by ggsql is used as-is.
        width
            Chart width in pixels. When ``None`` (the default), the width
            produced by ggsql is used as-is.
        **kwargs
            Additional keyword arguments passed to ``altair.Chart.from_json()``.

        Returns
        -------
        AltairChart
            An Altair chart object (Chart, LayerChart, FacetChart, etc.).
        """
        vegalite_json = self.render(spec)
        if height is not None or width is not None:
            spec_dict = json.loads(vegalite_json)
            if height is not None:
                spec_dict["height"] = height
            if width is not None:
                spec_dict["width"] = width
            vegalite_json = json.dumps(spec_dict)
        return _json_to_altair_chart(vegalite_json, validate=validate, **kwargs)


def render_altair(
    df: IntoFrame,
    viz: str,
    *,
    validate: bool = False,
    height: int | None = None,
    width: int | None = None,
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
    validate
        Whether to validate the spec against the Vega-Lite schema.
    height
        Chart height in pixels. When ``None`` (the default), the height
        produced by ggsql is used as-is.
    width
        Chart width in pixels. When ``None`` (the default), the width
        produced by ggsql is used as-is.
    **kwargs
        Additional keyword arguments passed to ``altair.Chart.from_json()``.

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

    arrow_table = df.to_arrow()

    # Create temporary reader and register data
    reader = DuckDBReader("duckdb://memory")
    reader.register("__data__", arrow_table)

    # Build full query: SELECT * FROM __data__ + VISUALISE clause
    query = f"SELECT * FROM __data__ {viz}"

    # Execute and render
    spec = reader.execute(query)
    writer = VegaLiteWriter()
    vegalite_json = writer.render(spec)

    if height is not None or width is not None:
        spec_dict = json.loads(vegalite_json)
        if height is not None:
            spec_dict["height"] = height
        if width is not None:
            spec_dict["width"] = width
        vegalite_json = json.dumps(spec_dict)

    return _json_to_altair_chart(vegalite_json, validate=validate, **kwargs)
