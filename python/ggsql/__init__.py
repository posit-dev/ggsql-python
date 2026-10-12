from __future__ import annotations

import json
import math
import warnings
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version
from pathlib import Path
from typing import Any, Union

import altair
import narwhals as nw
from narwhals.typing import IntoFrame

from ggsql._ggsql import (
    DuckDBReader,
    HepWriter as _RustHepWriter,
    PDFWriter as _RustPDFWriter,
    SVGWriter as _RustSVGWriter,
    VegaLiteWriter as _RustVegaLiteWriter,
    Validated,
    Spec,
    validate,
    execute,
)

# PyO3 classes default to __module__ = "builtins"; point them at their real
# home so docs tooling (great-docs/griffe) can locate them.
for _cls in (
    DuckDBReader,
    _RustHepWriter,
    _RustPDFWriter,
    _RustSVGWriter,
    _RustVegaLiteWriter,
    Validated,
    Spec,
):
    _cls.__module__ = "ggsql._ggsql"
del _cls

__all__ = [
    # Classes
    "DuckDBReader",
    "SVGWriter",
    "PDFWriter",
    "HepWriter",
    "VegaLiteWriter",
    "Validated",
    "Spec",
    # Functions
    "validate",
    "execute",
    "render_altair",
    "save",
]

try:
    __version__ = _package_version("ggsql")
except PackageNotFoundError:  # not installed (e.g., running from a source tree)
    __version__ = "0.0.0"

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


def _warn_render_issues(issues: list[str]) -> None:
    """Surface writer render warnings (from Rust's render_reporting) to the user."""
    if not issues:
        return
    message = f"ggsql writer reported {len(issues)} issue(s) while rendering:"
    message += "\n" + "\n".join(f"  - {issue}" for issue in issues)
    warnings.warn(message, UserWarning, stacklevel=3)


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


def _validate_dpi(dpi: float) -> None:
    """Reject non-positive or non-finite DPI before reaching the renderer."""
    if not math.isfinite(dpi) or dpi <= 0:
        raise ValueError(f"dpi must be a positive, finite number, got {dpi!r}")


def _validate_dimension(name: str, value: int) -> None:
    """Reject non-positive canvas dimensions before reaching the renderer."""
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")


def _stringify_options(options: dict[str, Any]) -> list[tuple[str, str]]:
    """Convert writer option values to the strings the crate expects.

    Keys pass through unchanged: the crate normalises them (case- and
    separator-insensitive), so ``embed_fonts=True`` matches the crate's
    ``embed-fonts`` spelling. Booleans become ``"true"``/``"false"``.
    """
    pairs = []
    for key, value in options.items():
        if isinstance(value, bool):
            value = "true" if value else "false"
        pairs.append((key, str(value)))
    return pairs


class SVGWriter:
    """SVG output writer using ggsql's native renderer.

    Parameters
    ----------
    width
        Canvas width in pixels (or in `units` if given).
    height
        Canvas height in pixels (or in `units` if given).
    dpi
        Canvas resolution in dots per inch.
    **options
        Additional writer options, validated by ggsql (an unknown key or
        bad value raises ``ValueError``):

        - ``units``: ``"px"`` (default), ``"in"``, ``"cm"``, ``"mm"``, or
          ``"pt"`` — how `width`/`height` are read.
        - ``background``: any CSS color; ``"transparent"`` emits no
          background (default is white).
        - ``text``: ``"text"`` (default, real selectable text) or
          ``"outline"`` (glyphs as paths, self-contained).
        - ``embed_fonts``: inline font files so the SVG renders identically
          anywhere (default ``False``; can make files much larger).
        - ``id_prefix``: prefix for generated element ids (avoids id
          collisions when inlining multiple SVGs into one page).

    Methods
    -------
    render(spec)
        Render a Spec to an SVG string.
    """

    def __init__(
        self, width: int = 600, height: int = 400, dpi: float = 96.0, **options: Any
    ) -> None:
        _validate_dimension("width", width)
        _validate_dimension("height", height)
        _validate_dpi(dpi)
        self._inner = _RustSVGWriter(width, height, dpi, _stringify_options(options))

    def render(self, spec: Spec) -> str:
        """Render a Spec to an SVG string.

        Render warnings reported by the writer are surfaced via
        ``warnings.warn()``.
        """
        output, issues = self._inner.render(spec)
        _warn_render_issues(issues)
        return output


class PDFWriter:
    """PDF output writer using ggsql's native renderer.

    Parameters
    ----------
    width
        Canvas width in pixels (or in `units` if given).
    height
        Canvas height in pixels (or in `units` if given).
    dpi
        Canvas resolution in dots per inch.
    **options
        Additional writer options, validated by ggsql (an unknown key or
        bad value raises ``ValueError``):

        - ``units``: ``"px"`` (default), ``"in"``, ``"cm"``, ``"mm"``, or
          ``"pt"`` — how `width`/`height` are read.
        - ``background``: any CSS color; ``"transparent"`` emits no
          background (default is white).
        - ``compress``: compress content streams (default ``True``).
        - ``links``: preserve hyperlinks (default ``True``).

    Methods
    -------
    render(spec)
        Render a Spec to a PDF document (bytes).
    """

    def __init__(
        self, width: int = 600, height: int = 400, dpi: float = 96.0, **options: Any
    ) -> None:
        _validate_dimension("width", width)
        _validate_dimension("height", height)
        _validate_dpi(dpi)
        self._inner = _RustPDFWriter(width, height, dpi, _stringify_options(options))

    def render(self, spec: Spec) -> bytes:
        """Render a Spec to a PDF document as bytes.

        Render warnings reported by the writer are surfaced via
        ``warnings.warn()``.
        """
        output, issues = self._inner.render(spec)
        _warn_render_issues(issues)
        return output


class HepWriter:
    """.hep plot document writer using ggsql's native renderer.

    A `.hep` document is a self-contained description of the resolved plot
    that a compatible consumer can render and re-layout at any size.

    Parameters
    ----------
    width, height, dpi
        Optional size/resolution *hints* recorded in the document for the
        consumer to default to — they do not fix the rendered size. If any
        one is given, the others default to 600, 400, and 96.0 so the hint
        is complete. By default no hint is recorded.
    **options
        Additional writer options, validated by ggsql (an unknown key or
        bad value raises ``ValueError``):

        - ``background``: any CSS color; ``"transparent"`` for none.
        - ``lossy``: drop what the format cannot carry (reported as render
          warnings) instead of failing (default ``False``).
        - ``embed_fonts``: inline the font files the plot's text needs
          (default ``False``).

    Methods
    -------
    render(spec)
        Render a Spec to a .hep plot document (bytes).
    """

    def __init__(
        self,
        width: int | None = None,
        height: int | None = None,
        dpi: float | None = None,
        **options: Any,
    ) -> None:
        if width is not None or height is not None or dpi is not None:
            width = 600 if width is None else width
            height = 400 if height is None else height
            dpi = 96.0 if dpi is None else dpi
            _validate_dimension("width", width)
            _validate_dimension("height", height)
            _validate_dpi(dpi)
        self._inner = _RustHepWriter(width, height, dpi, _stringify_options(options))

    def render(self, spec: Spec) -> bytes:
        """Render a Spec to a .hep plot document as bytes.

        Render warnings reported by the writer are surfaced via
        ``warnings.warn()``.
        """
        output, issues = self._inner.render(spec)
        _warn_render_issues(issues)
        return output


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


def save(
    spec: Spec,
    file: str | Path,
    *,
    width: int = 600,
    height: int = 400,
    dpi: float = 96.0,
    **options: Any,
) -> Path:
    """Save a ggsql spec to a file.

    The format is derived from the file extension: `.svg`, `.pdf`, and
    `.hep` are written by the corresponding native writer, and `.json`
    writes the Vega-Lite representation.

    Parameters
    ----------
    spec
        The resolved visualization specification from ``reader.execute()``.
    file
        Output file path. Extension determines format: `.svg`, `.pdf`,
        `.hep`, or `.json`.
    width
        Canvas width in pixels (ignored for `.hep` and `.json`).
    height
        Canvas height in pixels (ignored for `.hep` and `.json`).
    dpi
        Canvas resolution in dots per inch (ignored for `.hep` and `.json`).
    **options
        Additional writer options (e.g. ``background="transparent"``),
        passed to the native writer — see `SVGWriter`, `PDFWriter`, and
        `HepWriter` for the supported keys. Ignored for `.json`.

    Returns
    -------
    Path
        The path that was written.

    Raises
    ------
    ValueError
        If the file extension is not supported.
    """
    path = Path(file)
    ext = path.suffix.lower()
    if ext == ".svg":
        path.write_text(
            SVGWriter(width, height, dpi, **options).render(spec), encoding="utf-8"
        )
    elif ext == ".pdf":
        path.write_bytes(PDFWriter(width, height, dpi, **options).render(spec))
    elif ext == ".hep":
        path.write_bytes(HepWriter(**options).render(spec))
    elif ext == ".json":
        path.write_text(VegaLiteWriter().render(spec), encoding="utf-8")
    else:
        raise ValueError(
            f"Unsupported file extension {ext!r}. Use .svg, .pdf, .hep, or .json."
        )
    return path


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
