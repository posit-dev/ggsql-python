# Changelog

## 0.3.0 (unreleased)

### Changed

- Upgraded to ggsql Rust crate v0.3.0. See the [upstream changelog](https://github.com/posit-dev/ggsql/blob/main/CHANGELOG.md) for details on new features, bug fixes, and breaking changes.
- Replaced polars with Arrow (via pyarrow) for the Rust↔Python data bridge. `DuckDBReader.execute_sql()`, `Spec.data()`, `Spec.layer_data()`, and `Spec.stat_data()` now return `pyarrow.Table` instead of `polars.DataFrame`. `DuckDBReader.register()` accepts `pyarrow.Table` (polars DataFrames are still accepted via automatic conversion). Custom readers returning polars DataFrames from `execute_sql()` continue to work without changes.
- Runtime dependency changed from `polars` to `pyarrow`.

## 0.2.7

Synced with ggsql Rust crate v0.2.7.

## 0.1.4

Initial release.
