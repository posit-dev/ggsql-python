# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Python bindings for the [ggsql](https://github.com/posit-dev/ggsql) Rust crate — a SQL extension for declarative data visualization. The Rust crate handles parsing, validation, and Vega-Lite generation; this repo wraps it via PyO3/maturin and adds a Python-native API layer (`render_altair()`, `VegaLiteWriter.render_chart()`).

## Build & Development

Requires Rust toolchain and Python 3.10+. Uses `uv` for Python dependency management.

```bash
# Install dev dependencies
uv sync

# Build the Rust extension in-place (required after any Rust changes)
uv run maturin develop

# Run all tests
uv run pytest tests/ -v

# Run a single test
uv run pytest tests/test_ggsql.py::TestValidate::test_valid_query_with_visualise -v

# Rust checks (CI runs these)
cargo fmt -- --check
cargo clippy -- -D warnings
```

To pick up a new version of the upstream `ggsql` Rust crate, bump its version in `Cargo.toml` and re-run `maturin develop`.

## Architecture

**Rust layer** (`src/lib.rs`): Single-file PyO3 module exposing `DuckDBReader`, `VegaLiteWriter`, `Validated`, `Spec`, `validate()`, and `execute()` to Python. Data crosses the Rust↔Python boundary via Arrow IPC serialization (`arrow::ipc::StreamWriter`/`StreamReader` on the Rust side, `pyarrow.ipc` on the Python side). The `py_to_df` helper accepts any object that `pyarrow.table()` can convert (pyarrow Tables, polars DataFrames, pandas DataFrames, etc.). Custom Python readers are bridged to the Rust `Reader` trait via `PyReaderBridge`; native readers (currently just `DuckDBReader`) use a fast path that skips the bridge (see `try_native_readers!` macro).

**Python layer** (`python/ggsql/__init__.py`): Re-exports Rust bindings and adds `render_altair()` (convenience function that registers a DataFrame, executes, and returns an Altair chart) and a Python `VegaLiteWriter` wrapper that adds `render_chart()`. The `_json_to_altair_chart()` helper dispatches to the correct Altair chart class based on the Vega-Lite spec structure (layer, facet, concat, etc.).

**Key design pattern**: Two-stage API — `reader.execute(query) -> Spec`, then `writer.render(spec) -> str` or `writer.render_chart(spec) -> AltairChart`. The `render_altair()` shortcut collapses both stages.

## `.cargo/config.toml`

Sets `GGSQL_SKIP_GENERATE=1` so tree-sitter uses its pre-generated parser rather than regenerating from `grammar.js`. Don't remove this.
