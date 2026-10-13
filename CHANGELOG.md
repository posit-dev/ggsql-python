# Changelog

## UNRELEASED

### Changed

- Data now crosses the Python↔Rust boundary via the zero-copy Arrow C Data Interface instead of Arrow IPC serialization, reducing memory copies and peak RSS for large results (on a 1M-row × 50-col register+read microbenchmark: ~5.7× faster, ~1 GB lower peak RSS). Set `GGSQL_ARROW_IPC=1` to restore the previous IPC behavior. The minimum pyarrow version is now 15.

## 0.5.2

### Added

- New native writers `SVGWriter`, `PDFWriter`, and `HepWriter`, rendered directly by ggsql (via hephaestus) with no JavaScript round-trip. Each exposes `render(spec)` returning `str` (SVG) or `bytes` (PDF/HEP); render warnings are surfaced via `warnings.warn()`.
- New `ggsql.save(spec, file, ...)` helper that writes a spec to disk, picking the writer from the file extension (`.svg`, `.pdf`, `.hep`, or `.json` for Vega-Lite).
- Enabled the crate's `builtin-data` feature, making builtin datasets such as `ggsql:world` available.
- New `ggsql.CachingReader` that caches query results to avoid redundant source reads. Wraps a `duckdb://` connection string or any custom Python reader; reads are memoized with a configurable TTL and size budget, and derived `__ggsql_*` tables run on a private in-memory DuckDB cache.

### Changed

- Upgraded to ggsql Rust crate v0.5.2 (from v0.3.3). See the [crate changelog](https://github.com/posit-dev/ggsql/blob/main/CHANGELOG.md) for the many language and rendering improvements in 0.4.0–0.5.2.
- Upgraded the Arrow dependency from v56 to v58.
- Declared Rust MSRV is now 1.88 (required by the native-writer dependency graph, e.g. parley 0.9).

## 0.3.3

### Changed

- Upgraded to ggsql Rust crate v0.3.3 (hotfix).
- `VegaLiteWriter.render_chart()` and `render_altair()` now accept `validate`, `height`, and `width` as explicit keyword arguments. `height` and `width` default to `None`, which preserves whatever dimensions ggsql produces. `validate` defaults to `False` (same behavior as before, now a named parameter).
- When Altair can't deserialize a spec into the expected chart subclass (e.g., `LayerChart`), the converter now falls back to `altair.Chart` instead of raising. The chart still displays correctly; only Altair-level round-tripping (`.to_dict()`) is lost.

## 0.3.2

### Changed

- Upgraded to ggsql Rust crate v0.3.2.
- `render_altair()` and `VegaLiteWriter.render_chart()` now default to `validate=False` when creating Altair chart objects. This avoids `ValidationError`s for valid specs (e.g., boxplots) that use Vega-Lite features not yet reflected in Altair's schema. Pass `validate=True` to re-enable.

## 0.3.1

### Changed

- Upgraded to ggsql Rust crate v0.3.1.
- Replaced polars with Arrow (via pyarrow) for the Rust↔Python data bridge. `DuckDBReader.execute_sql()`, `Spec.data()`, `Spec.layer_data()`, and `Spec.stat_data()` now return `pyarrow.Table` instead of `polars.DataFrame`. `DuckDBReader.register()` accepts `pyarrow.Table` (polars DataFrames are still accepted via automatic conversion). Custom readers returning polars DataFrames from `execute_sql()` continue to work without changes.
- Runtime dependency changed from `polars` to `pyarrow`.

## 0.2.7

Synced with ggsql Rust crate v0.2.7.

## 0.1.4

Initial release.
