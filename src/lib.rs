// Allow useless_conversion due to false positive from pyo3 macro expansion
// See: https://github.com/PyO3/pyo3/issues/4327
#![allow(clippy::useless_conversion)]

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyCapsule, PyDict, PyList};
use std::io::Cursor;

use arrow::array::RecordBatchIterator;
use arrow::ffi_stream::{ArrowArrayStreamReader, FFI_ArrowArrayStream};
use arrow::ipc::reader::StreamReader;
use arrow::ipc::writer::StreamWriter;

use ggsql::dataframe::DataFrame;
use ggsql::reader::cache::{CacheConfig, CacheConfigOverride};
use ggsql::reader::Spec;
use ggsql::reader::{CachingReader as RustCachingReader, DuckDBReader as RustDuckDBReader, Reader};
use ggsql::validate::{validate as rust_validate, ValidationWarning};
use ggsql::writer::{
    HepWriter as RustHepWriter, PdfWriter as RustPdfWriter, SvgWriter as RustSvgWriter,
    VegaLiteWriter as RustVegaLiteWriter, Writer as RustWriter,
};
use ggsql::GgsqlError;

// ============================================================================
// Helper Functions for DataFrame Conversion
// ============================================================================

/// Whether to use the legacy Arrow IPC serialization path instead of the
/// zero-copy Arrow C Data Interface path. Enabled by setting GGSQL_ARROW_IPC
/// to any non-empty value other than "0".
fn use_ipc_fallback() -> bool {
    match std::env::var_os("GGSQL_ARROW_IPC") {
        Some(v) => !v.is_empty() && v != "0",
        None => false,
    }
}

/// Convert a ggsql DataFrame to a Python pyarrow Table via Arrow IPC
/// serialization (legacy fallback; see GGSQL_ARROW_IPC)
fn df_to_py_ipc(py: Python<'_>, df: &DataFrame) -> PyResult<Py<PyAny>> {
    let rb = df.inner();
    let mut buffer = Vec::new();
    {
        let mut writer = StreamWriter::try_new(&mut buffer, &rb.schema()).map_err(|e| {
            PyErr::new::<pyo3::exceptions::PyValueError, _>(format!(
                "Failed to create IPC writer: {}",
                e
            ))
        })?;
        writer.write(rb).map_err(|e| {
            PyErr::new::<pyo3::exceptions::PyValueError, _>(format!(
                "Failed to write RecordBatch: {}",
                e
            ))
        })?;
        writer.finish().map_err(|e| {
            PyErr::new::<pyo3::exceptions::PyValueError, _>(format!(
                "Failed to finish IPC stream: {}",
                e
            ))
        })?;
    }

    let pa = py.import("pyarrow")?;
    let ipc_mod = pa.getattr("ipc")?;
    let io = py.import("io")?;
    let bytes_io = io.call_method1("BytesIO", (PyBytes::new(py, &buffer),))?;
    let reader = ipc_mod.call_method1("open_stream", (&bytes_io,))?;
    let table = reader.call_method0("read_all")?;
    Ok(table.into())
}

/// Convert a ggsql DataFrame to a Python pyarrow Table.
///
/// Uses the zero-copy Arrow C Data Interface by default; set GGSQL_ARROW_IPC=1
/// to fall back to Arrow IPC serialization.
fn df_to_py(py: Python<'_>, df: &DataFrame) -> PyResult<Py<PyAny>> {
    if use_ipc_fallback() {
        df_to_py_ipc(py, df)
    } else {
        df_to_py_stream(py, df)
    }
}

/// Convert a ggsql DataFrame to a Python pyarrow Table via the Arrow C Data
/// Interface (zero-copy).
fn df_to_py_stream(py: Python<'_>, df: &DataFrame) -> PyResult<Py<PyAny>> {
    let rb = df.inner().clone();
    let schema = rb.schema();
    let reader = RecordBatchIterator::new(std::iter::once(Ok(rb)), schema);
    let stream = FFI_ArrowArrayStream::new(Box::new(reader));
    let stream_ptr = Box::into_raw(Box::new(stream));

    // SAFETY: stream_ptr is a valid allocation we own; the capsule takes
    // ownership, and c_stream_capsule_destructor reclaims it.
    let capsule_ptr = unsafe {
        pyo3::ffi::PyCapsule_New(
            stream_ptr.cast(),
            c"arrow_array_stream".as_ptr(),
            Some(c_stream_capsule_destructor),
        )
    };
    if capsule_ptr.is_null() {
        // Capsule creation failed; reclaim the allocation and propagate.
        unsafe { drop(Box::from_raw(stream_ptr)) };
        return Err(PyErr::fetch(py));
    }
    // SAFETY: capsule_ptr is a new reference to a valid PyCapsule object.
    let capsule = unsafe { Py::<PyAny>::from_owned_ptr(py, capsule_ptr) };

    let shim = Py::new(py, ArrowStreamShim { capsule })?;
    let pa = py.import("pyarrow")?;
    let table = pa
        .getattr("RecordBatchReader")?
        .call_method1("from_stream", (shim,))?
        .call_method0("read_all")?;
    Ok(table.into())
}

/// Capsule destructor honoring the Arrow C Stream Interface contract: if the
/// stream was never consumed (its release callback is still set), release it;
/// then reclaim the allocation. When pyarrow consumes the stream it moves the
/// contents out and nulls the release callback, so this destructor only frees
/// the (now-empty) allocation in that case.
unsafe extern "C" fn c_stream_capsule_destructor(capsule: *mut pyo3::ffi::PyObject) {
    let ptr = unsafe { pyo3::ffi::PyCapsule_GetPointer(capsule, c"arrow_array_stream".as_ptr()) }
        .cast::<FFI_ArrowArrayStream>();
    if ptr.is_null() {
        return;
    }
    let mut stream = unsafe { Box::from_raw(ptr) };
    if let Some(release) = stream.release {
        // SAFETY: per the C Data Interface contract, release is called at
        // most once and marks the stream as released.
        unsafe { release(&mut *stream) };
    }
}

/// Python-side holder for an Arrow C Stream capsule, exposing the
/// `__arrow_c_stream__` protocol so pyarrow can consume the stream.
#[pyclass]
struct ArrowStreamShim {
    capsule: Py<PyAny>,
}

#[pymethods]
impl ArrowStreamShim {
    #[pyo3(signature = (requested_schema=None))]
    fn __arrow_c_stream__(
        &self,
        py: Python<'_>,
        requested_schema: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        if requested_schema.is_some_and(|s| !s.is_none()) {
            return Err(PyErr::new::<pyo3::exceptions::PyNotImplementedError, _>(
                "requested_schema is not supported",
            ));
        }
        Ok(self.capsule.clone_ref(py))
    }
}

/// Convert a Python pyarrow Table to a ggsql DataFrame via Arrow IPC
/// serialization (legacy fallback; see GGSQL_ARROW_IPC)
fn py_to_df_ipc(py: Python<'_>, obj: &Bound<'_, PyAny>) -> PyResult<DataFrame> {
    let pa = py.import("pyarrow")?;
    let table = if obj.is_instance(&pa.getattr("Table")?)? {
        obj.clone()
    } else {
        pa.call_method1("table", (obj,)).map_err(|e| {
            let type_name = obj
                .get_type()
                .name()
                .map(|n| n.to_string())
                .unwrap_or_else(|_| "<unknown>".to_string());
            PyErr::new::<pyo3::exceptions::PyTypeError, _>(format!(
                "Expected a pyarrow.Table or compatible type, got {}: {}",
                type_name, e
            ))
        })?
    };

    let ipc_mod = pa.getattr("ipc")?;
    let io = py.import("io")?;
    let sink = io.call_method0("BytesIO")?;
    let writer = ipc_mod.call_method1("new_stream", (&sink, table.getattr("schema")?))?;
    writer.call_method1("write_table", (&table,))?;
    writer.call_method0("close")?;
    sink.call_method1("seek", (0i64,))?;
    let ipc_bytes: Vec<u8> = sink.call_method0("read")?.extract()?;

    let cursor = Cursor::new(ipc_bytes);
    let reader = StreamReader::try_new(cursor, None).map_err(|e| {
        PyErr::new::<pyo3::exceptions::PyValueError, _>(format!("Failed to read IPC stream: {}", e))
    })?;

    let batches: Vec<_> = reader
        .into_iter()
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| {
            PyErr::new::<pyo3::exceptions::PyValueError, _>(format!(
                "Failed to read RecordBatch: {}",
                e
            ))
        })?;

    if batches.is_empty() {
        return Ok(DataFrame::empty());
    }

    let combined = arrow::compute::concat_batches(&batches[0].schema(), &batches).map_err(|e| {
        PyErr::new::<pyo3::exceptions::PyValueError, _>(format!("Failed to concat batches: {}", e))
    })?;

    Ok(DataFrame::from_record_batch(combined))
}

/// Convert a Python pyarrow Table to a ggsql DataFrame.
///
/// Uses the zero-copy Arrow C Data Interface by default; set GGSQL_ARROW_IPC=1
/// to fall back to Arrow IPC serialization.
fn py_to_df(py: Python<'_>, obj: &Bound<'_, PyAny>) -> PyResult<DataFrame> {
    if use_ipc_fallback() {
        py_to_df_ipc(py, obj)
    } else {
        py_to_df_stream(py, obj)
    }
}

/// Convert a Python pyarrow Table to a ggsql DataFrame via the Arrow C Data
/// Interface (zero-copy).
fn py_to_df_stream(py: Python<'_>, obj: &Bound<'_, PyAny>) -> PyResult<DataFrame> {
    let pa = py.import("pyarrow")?;
    let table = if obj.is_instance(&pa.getattr("Table")?)? {
        obj.clone()
    } else {
        pa.call_method1("table", (obj,)).map_err(|e| {
            let type_name = obj
                .get_type()
                .name()
                .map(|n| n.to_string())
                .unwrap_or_else(|_| "<unknown>".to_string());
            PyErr::new::<pyo3::exceptions::PyTypeError, _>(format!(
                "Expected a pyarrow.Table or compatible type, got {}: {}",
                type_name, e
            ))
        })?
    };

    let capsule = table.call_method0("__arrow_c_stream__")?;
    let capsule = capsule.downcast::<PyCapsule>().map_err(|_| {
        PyErr::new::<pyo3::exceptions::PyValueError, _>(
            "__arrow_c_stream__ did not return a PyCapsule".to_string(),
        )
    })?;
    let stream_ptr = capsule.pointer().cast::<FFI_ArrowArrayStream>();
    if stream_ptr.is_null() {
        return Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(
            "Invalid Arrow C stream capsule".to_string(),
        ));
    }

    // SAFETY: stream_ptr comes from a valid "arrow_array_stream" capsule
    // produced by pyarrow. from_raw moves the stream out and sets the
    // capsule's copy to a released state, so the capsule destructor will not
    // double-release it (it still frees the now-empty allocation).
    let reader = unsafe { ArrowArrayStreamReader::from_raw(stream_ptr) }.map_err(|e| {
        PyErr::new::<pyo3::exceptions::PyValueError, _>(format!(
            "Failed to import Arrow C stream: {}",
            e
        ))
    })?;

    let batches: Vec<_> = reader.collect::<Result<Vec<_>, _>>().map_err(|e| {
        PyErr::new::<pyo3::exceptions::PyValueError, _>(format!(
            "Failed to read RecordBatch from Arrow C stream: {}",
            e
        ))
    })?;

    if batches.is_empty() {
        return Ok(DataFrame::empty());
    }

    let combined = arrow::compute::concat_batches(&batches[0].schema(), &batches).map_err(|e| {
        PyErr::new::<pyo3::exceptions::PyValueError, _>(format!("Failed to concat batches: {}", e))
    })?;

    Ok(DataFrame::from_record_batch(combined))
}

/// Convert a Python object to a ggsql DataFrame - for use inside Python::attach
/// This variant is used by PyReaderBridge where we already hold the GIL.
fn py_to_df_inner(obj: &Bound<'_, PyAny>) -> PyResult<DataFrame> {
    let py = obj.py();
    py_to_df(py, obj)
}

/// Convert validation errors/warnings to a Python list of dicts
fn errors_to_pylist(
    py: Python<'_>,
    items: &[(String, Option<(usize, usize)>)],
) -> PyResult<Py<PyList>> {
    let list = PyList::empty(py);
    for (message, location) in items {
        let dict = PyDict::new(py);
        dict.set_item("message", message)?;
        if let Some((line, column)) = location {
            let loc_dict = PyDict::new(py);
            loc_dict.set_item("line", line)?;
            loc_dict.set_item("column", column)?;
            dict.set_item("location", loc_dict)?;
        } else {
            dict.set_item("location", py.None())?;
        }
        list.append(dict)?;
    }
    Ok(list.into())
}

/// Convert ValidationWarning slice to Python list format
fn warnings_to_pylist(py: Python<'_>, warnings: &[ValidationWarning]) -> PyResult<Py<PyList>> {
    let items: Vec<_> = warnings
        .iter()
        .map(|w| {
            (
                w.message.clone(),
                w.location.as_ref().map(|l| (l.line, l.column)),
            )
        })
        .collect();
    errors_to_pylist(py, &items)
}

// ============================================================================
// PyReaderBridge - Bridges Python reader objects to Rust Reader trait
// ============================================================================

/// Bridges a Python reader object to the Rust Reader trait.
///
/// This allows any Python object with an `execute_sql(sql: str) -> pyarrow.Table`
/// method to be used as a ggsql reader.
struct PyReaderBridge {
    obj: Py<PyAny>,
}

static ANSI_DIALECT: ggsql::reader::AnsiDialect = ggsql::reader::AnsiDialect;

impl Reader for PyReaderBridge {
    fn execute_sql(&self, sql: &str) -> ggsql::Result<DataFrame> {
        Python::attach(|py| {
            let bound = self.obj.bind(py);
            let result = bound.call_method1("execute_sql", (sql,)).map_err(|e| {
                GgsqlError::ReaderError(format!("Reader.execute_sql() failed: {}", e))
            })?;
            py_to_df_inner(&result).map_err(|e| GgsqlError::ReaderError(e.to_string()))
        })
    }

    fn register(&self, name: &str, df: DataFrame, replace: bool) -> ggsql::Result<()> {
        Python::attach(|py| {
            let py_table = df_to_py(py, &df).map_err(|e| GgsqlError::ReaderError(e.to_string()))?;
            self.obj
                .bind(py)
                .call_method1("register", (name, py_table, replace))
                .map_err(|e| GgsqlError::ReaderError(format!("Reader.register() failed: {}", e)))?;
            Ok(())
        })
    }

    fn unregister(&self, name: &str) -> ggsql::Result<()> {
        Python::attach(|py| {
            self.obj
                .bind(py)
                .call_method1("unregister", (name,))
                .map_err(|e| {
                    GgsqlError::ReaderError(format!("Reader.unregister() failed: {}", e))
                })?;
            Ok(())
        })
    }

    fn execute(&self, query: &str) -> ggsql::Result<ggsql::reader::Spec> {
        ggsql::reader::execute_with_reader(self, query)
    }

    fn dialect(&self) -> &dyn ggsql::reader::SqlDialect {
        &ANSI_DIALECT
    }
}

// ============================================================================
// Native Reader Detection Macro
// ============================================================================

/// Macro to try native readers and fall back to bridge.
/// Adding new native readers = add to the macro invocation list.
macro_rules! try_native_readers {
    ($query:expr, $reader:expr, $($native_type:ty),*) => {{
        $(
            if let Ok(native) = $reader.downcast::<$native_type>() {
                return native.borrow().inner.execute($query)
                    .map(|s| PySpec { inner: s })
                    .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(e.to_string()));
            }
        )*
    }};
}

// ============================================================================
// PyDuckDBReader
// ============================================================================

/// DuckDB database reader for executing SQL queries.
///
/// Creates an in-memory or file-based DuckDB connection that can execute
/// SQL queries and register DataFrames as queryable tables.
///
/// Examples
/// --------
/// >>> reader = DuckDBReader("duckdb://memory")
/// >>> table = reader.execute_sql("SELECT 1 as x, 2 as y")
///
/// >>> reader = DuckDBReader("duckdb://memory")
/// >>> reader.register("data", pa.table({"x": [1, 2, 3]}))
/// >>> table = reader.execute_sql("SELECT * FROM data WHERE x > 1")
#[pyclass(name = "DuckDBReader", unsendable)]
struct PyDuckDBReader {
    inner: RustDuckDBReader,
}

#[pymethods]
impl PyDuckDBReader {
    /// Create a new DuckDB reader from a connection string.
    ///
    /// Parameters
    /// ----------
    /// connection : str
    ///     Connection string. Use "duckdb://memory" for in-memory database
    ///     or "duckdb://path/to/file.db" for file-based database.
    ///
    /// Returns
    /// -------
    /// DuckDBReader
    ///     A configured DuckDB reader instance.
    ///
    /// Raises
    /// ------
    /// ValueError
    ///     If the connection string is invalid or the database cannot be opened.
    #[new]
    fn new(connection: &str) -> PyResult<Self> {
        let inner = RustDuckDBReader::from_connection_string(connection)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(e.to_string()))?;
        Ok(Self { inner })
    }

    /// Register a pyarrow Table as a queryable table.
    ///
    /// After registration, the table can be queried by name in SQL.
    ///
    /// Parameters
    /// ----------
    /// name : str
    ///     The table name to register under.
    /// table : pyarrow.Table
    ///     The Arrow table to register.
    ///
    /// Raises
    /// ------
    /// ValueError
    ///     If registration fails or the table name is invalid.
    #[pyo3(signature = (name, table, replace=false))]
    fn register(
        &self,
        py: Python<'_>,
        name: &str,
        table: &Bound<'_, PyAny>,
        replace: bool,
    ) -> PyResult<()> {
        let df = py_to_df(py, table)?;
        self.inner
            .register(name, df, replace)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(e.to_string()))
    }

    /// Unregister a previously registered table.
    ///
    /// Parameters
    /// ----------
    /// name : str
    ///     The table name to unregister.
    ///
    /// Raises
    /// ------
    /// ValueError
    ///     If the table wasn't registered via this reader or unregistration fails.
    fn unregister(&self, name: &str) -> PyResult<()> {
        self.inner
            .unregister(name)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(e.to_string()))
    }

    /// Execute a SQL query and return the result as a pyarrow Table.
    ///
    /// Parameters
    /// ----------
    /// sql : str
    ///     The SQL query to execute.
    ///
    /// Returns
    /// -------
    /// pyarrow.Table
    ///     The query result as a pyarrow Table.
    ///
    /// Raises
    /// ------
    /// ValueError
    ///     If the SQL is invalid or execution fails.
    fn execute_sql(&self, py: Python<'_>, sql: &str) -> PyResult<Py<PyAny>> {
        let df = self
            .inner
            .execute_sql(sql)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(e.to_string()))?;
        df_to_py(py, &df)
    }

    /// Execute a ggsql query and return the visualization specification.
    ///
    /// This is the main entry point for creating visualizations. It parses
    /// the query, executes the SQL portion, and returns a PySpec ready
    /// for rendering.
    ///
    /// Parameters
    /// ----------
    /// query : str
    ///     The ggsql query (SQL + VISUALISE clause).
    ///
    /// Returns
    /// -------
    /// Spec
    ///     The resolved visualization specification ready for rendering.
    ///
    /// Raises
    /// ------
    /// ValueError
    ///     If the query syntax is invalid, has no VISUALISE clause, or SQL execution fails.
    ///
    /// Examples
    /// --------
    /// >>> reader = DuckDBReader("duckdb://memory")
    /// >>> spec = reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")
    /// >>> writer = VegaLiteWriter()
    /// >>> json_output = writer.render(spec)
    fn execute(&self, query: &str) -> PyResult<PySpec> {
        self.inner
            .execute(query)
            .map(|s| PySpec { inner: s })
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(e.to_string()))
    }
}

// ============================================================================
// PyCachingReader
// ============================================================================

/// Caching reader: wraps a primary reader with an in-memory DuckDB cache.
///
/// Source reads go to the primary and are memoized (TTL + LRU byte budget);
/// all ggsql-generated derived SQL runs on the private in-memory cache.
/// Configuration is read from the environment (GGSQL_CACHE_DISABLED,
/// GGSQL_CACHE_TTL, GGSQL_CACHE_MAX_BYTES) with keyword arguments taking
/// precedence.
///
/// Examples
/// --------
/// >>> reader = CachingReader("duckdb://memory", ttl_secs=60)
/// >>> spec = reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")
///
/// >>> # Wrapping a custom Python reader (bridge protocol):
/// >>> class MyReader:
/// ...     def execute_sql(self, sql): ...
/// ...     def register(self, name, table, replace): ...
/// ...     def unregister(self, name): ...
/// >>> reader = CachingReader(MyReader())
#[pyclass(name = "CachingReader", unsendable)]
struct PyCachingReader {
    inner: RustCachingReader,
}

#[pymethods]
impl PyCachingReader {
    /// Create a new caching reader.
    ///
    /// Parameters
    /// ----------
    /// primary : str or object
    ///     Either a "duckdb://..." connection string (a fresh native
    ///     DuckDBReader is constructed internally) or a Python object
    ///     implementing the reader protocol: execute_sql(sql) ->
    ///     pyarrow.Table, register(name, table, replace), unregister(name).
    /// ttl_secs : int, optional
    ///     Seconds before a memoized read is treated as stale.
    ///     Default 300 (or GGSQL_CACHE_TTL).
    /// max_bytes : int, optional
    ///     Cumulative byte budget across memoized results before LRU
    ///     eviction. Default 512 MiB (or GGSQL_CACHE_MAX_BYTES).
    /// enabled : bool, optional
    ///     When False, source reads always hit the primary: the memo is
    ///     neither consulted nor written. Note this only disables
    ///     memoization — registered tables still live in the private
    ///     in-memory cache, reads referencing them still route there,
    ///     and derived SQL still runs on the cache backend.
    ///     Default True (unless GGSQL_CACHE_DISABLED is set).
    ///
    /// Raises
    /// ------
    /// ValueError
    ///     If a connection string has an unsupported scheme, or a
    ///     DuckDBReader instance is passed (pass its connection
    ///     string instead).
    #[new]
    #[pyo3(signature = (primary, *, ttl_secs=None, max_bytes=None, enabled=None))]
    fn new(
        primary: &Bound<'_, PyAny>,
        ttl_secs: Option<u64>,
        max_bytes: Option<u64>,
        enabled: Option<bool>,
    ) -> PyResult<Self> {
        // Private in-memory DuckDB is the cache backend: derived tables,
        // registered data, and memoized reads all live there.
        let cache = RustDuckDBReader::from_connection_string("duckdb://memory")
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(e.to_string()))?;

        let (primary_reader, primary_uri): (Box<dyn Reader + Send>, String) = if let Ok(uri) =
            primary.extract::<&str>()
        {
            if !uri.starts_with("duckdb://") {
                return Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(format!(
                        "Unsupported connection string {uri:?}: only duckdb:// primaries are currently supported"
                    )));
            }
            let reader = RustDuckDBReader::from_connection_string(uri)
                .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(e.to_string()))?;
            (Box::new(reader), uri.to_string())
        } else {
            // Moving the inner reader out of a live pyclass isn't
            // possible; guide the user to the connection-string form.
            if primary.downcast::<PyDuckDBReader>().is_ok() {
                return Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(
                        "CachingReader cannot wrap an existing DuckDBReader; pass its connection string instead (e.g. CachingReader(\"duckdb://memory\"))",
                    ));
            }
            // The URI only feeds cache-key derivation, which just needs a
            // stable-per-instance string; the type name suffices.
            let type_name = primary.get_type().name()?.to_str()?.to_string();
            (
                Box::new(PyReaderBridge {
                    obj: primary.clone().unbind(),
                }),
                type_name,
            )
        };

        let config = CacheConfig::from_env().merge(CacheConfigOverride {
            enabled,
            ttl_secs,
            max_bytes,
        });

        Ok(Self {
            inner: RustCachingReader::with_config(
                primary_reader,
                Box::new(cache),
                primary_uri,
                "duckdb",
                config,
            ),
        })
    }

    /// Register a pyarrow Table as a queryable table.
    ///
    /// The table lands in the in-memory cache; reads referencing its name
    /// are routed to the cache rather than the primary.
    ///
    /// Parameters
    /// ----------
    /// name : str
    ///     The table name to register under.
    /// table : pyarrow.Table
    ///     The Arrow table to register.
    /// replace : bool
    ///     Whether to replace an existing table of the same name.
    #[pyo3(signature = (name, table, replace=false))]
    fn register(
        &self,
        py: Python<'_>,
        name: &str,
        table: &Bound<'_, PyAny>,
        replace: bool,
    ) -> PyResult<()> {
        let df = py_to_df(py, table)?;
        self.inner
            .register(name, df, replace)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(e.to_string()))
    }

    /// Unregister a previously registered table.
    ///
    /// Parameters
    /// ----------
    /// name : str
    ///     The table name to unregister.
    fn unregister(&self, name: &str) -> PyResult<()> {
        self.inner
            .unregister(name)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(e.to_string()))
    }

    /// Execute a SQL query against the source surface.
    ///
    /// Row-returning reads are memoized: an identical query within the TTL
    /// is served from the cache without hitting the primary.
    ///
    /// Parameters
    /// ----------
    /// sql : str
    ///     The SQL query to execute.
    ///
    /// Returns
    /// -------
    /// pyarrow.Table
    ///     The query result as a pyarrow Table.
    fn execute_sql(&self, py: Python<'_>, sql: &str) -> PyResult<Py<PyAny>> {
        let df = self
            .inner
            .execute_sql(sql)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(e.to_string()))?;
        df_to_py(py, &df)
    }

    /// Execute a SQL query against the cache backend (compute surface).
    ///
    /// All ggsql-generated derived SQL runs on the private in-memory cache
    /// connection, so this is the surface to use for statements that must
    /// take effect there — e.g. `INSTALL spatial; LOAD spatial`, which the
    /// spatial geom requires on the cache connection. Routing such
    /// statements through `execute_sql` would send them to the primary data
    /// source instead, where they typically fail (locked-down connections)
    /// and do nothing for the cache.
    ///
    /// Parameters
    /// ----------
    /// sql : str
    ///     The SQL statement to execute on the cache connection.
    ///
    /// Returns
    /// -------
    /// pyarrow.Table
    ///     The query result as a pyarrow Table.
    fn execute_sql_cached(&self, py: Python<'_>, sql: &str) -> PyResult<Py<PyAny>> {
        let df = self
            .inner
            .execute_sql_cached(sql)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(e.to_string()))?;
        df_to_py(py, &df)
    }

    /// Execute a ggsql query and return the visualization specification.
    ///
    /// Base reads hit the (memoized) primary; derived tables are
    /// materialized into the cache. Unlike a plain bridged reader, queries
    /// with layer-level sources (VISUALISE ... FROM <table>) work, because
    /// every layer source is staged through the cache.
    ///
    /// Parameters
    /// ----------
    /// query : str
    ///     The ggsql query (SQL + VISUALISE clause).
    ///
    /// Returns
    /// -------
    /// Spec
    ///     The resolved visualization specification ready for rendering.
    ///
    /// Raises
    /// ------
    /// ValueError
    ///     If the query syntax is invalid or SQL execution fails.
    fn execute(&self, query: &str) -> PyResult<PySpec> {
        self.inner
            .execute(query)
            .map(|s| PySpec { inner: s })
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(e.to_string()))
    }
}

// ============================================================================
// PyVegaLiteWriter
// ============================================================================

/// Vega-Lite JSON output writer.
///
/// Converts visualization specifications to Vega-Lite v6 JSON.
///
/// Examples
/// --------
/// >>> writer = VegaLiteWriter()
/// >>> spec = reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")
/// >>> json_output = writer.render(spec)
#[pyclass(name = "VegaLiteWriter")]
struct PyVegaLiteWriter {
    inner: RustVegaLiteWriter,
}

#[pymethods]
impl PyVegaLiteWriter {
    /// Create a new Vega-Lite writer.
    ///
    /// Returns
    /// -------
    /// VegaLiteWriter
    ///     A configured Vega-Lite writer instance.
    #[new]
    fn new() -> Self {
        Self {
            inner: RustVegaLiteWriter::new(),
        }
    }

    /// Render a Spec to Vega-Lite JSON output
    ///
    /// Parameters
    /// ----------
    /// spec : Spec
    ///     The visualization specification from reader.execute().
    ///
    /// Returns
    /// -------
    /// str
    ///     The output (i.e., Vega-Lite JSON string).
    ///
    /// Raises
    /// ------
    /// ValueError
    ///     If rendering fails.
    ///
    /// Examples
    /// --------
    /// >>> reader = DuckDBReader("duckdb://memory")
    /// >>> spec = reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")
    /// >>> writer = VegaLiteWriter()
    /// >>> json_output = writer.render(spec)
    fn render(&self, spec: &PySpec) -> PyResult<String> {
        self.inner
            .render(&spec.inner)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(e.to_string()))
    }
}

// ============================================================================
// PySVGWriter
// ============================================================================

/// SVG output writer (native hephaestus renderer).
///
/// Renders visualization specifications to SVG markup without any
/// JavaScript round-trip.
///
/// Examples
/// --------
/// >>> writer = SVGWriter(width=600, height=400, dpi=96)
/// >>> spec = reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")
/// >>> svg = writer.render(spec)
#[pyclass(name = "SVGWriter")]
struct PySVGWriter {
    inner: RustSvgWriter,
}

#[pymethods]
impl PySVGWriter {
    /// Create a new SVG writer.
    ///
    /// Parameters
    /// ----------
    /// width : int
    ///     Canvas width in pixels.
    /// height : int
    ///     Canvas height in pixels.
    /// dpi : float
    ///     Canvas resolution in dots per inch.
    ///
    /// Returns
    /// -------
    /// SVGWriter
    ///     A configured SVG writer instance.
    #[new]
    fn new(width: u32, height: u32, dpi: f64) -> Self {
        Self {
            inner: RustSvgWriter::new(width.max(1), height.max(1), dpi),
        }
    }

    /// Render a Spec to SVG markup.
    ///
    /// Parameters
    /// ----------
    /// spec : Spec
    ///     The visualization specification from reader.execute().
    ///
    /// Returns
    /// -------
    /// tuple[str, list[str]]
    ///     The SVG markup and a list of render warnings (usually empty).
    ///
    /// Raises
    /// ------
    /// ValueError
    ///     If rendering fails.
    fn render(&self, py: Python<'_>, spec: &PySpec) -> PyResult<(String, Vec<String>)> {
        // Rendering is pure Rust work on immutable data, so release the GIL
        // while it runs to avoid blocking other Python threads. This is
        // compiler-checked: detach requires a Send closure, so this only
        // builds if the writer and spec are safe to share.
        py.detach(|| self.inner.render_reporting(&spec.inner))
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(e.to_string()))
    }
}

// ============================================================================
// PyPDFWriter
// ============================================================================

/// PDF output writer (native hephaestus renderer).
///
/// Renders visualization specifications to PDF documents.
///
/// Examples
/// --------
/// >>> writer = PDFWriter(width=600, height=400, dpi=96)
/// >>> spec = reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")
/// >>> pdf_bytes = writer.render(spec)
#[pyclass(name = "PDFWriter")]
struct PyPDFWriter {
    inner: RustPdfWriter,
}

#[pymethods]
impl PyPDFWriter {
    /// Create a new PDF writer.
    ///
    /// Parameters
    /// ----------
    /// width : int
    ///     Canvas width in pixels.
    /// height : int
    ///     Canvas height in pixels.
    /// dpi : float
    ///     Canvas resolution in dots per inch.
    ///
    /// Returns
    /// -------
    /// PDFWriter
    ///     A configured PDF writer instance.
    #[new]
    fn new(width: u32, height: u32, dpi: f64) -> Self {
        Self {
            inner: RustPdfWriter::new(width.max(1), height.max(1), dpi),
        }
    }

    /// Render a Spec to a PDF document.
    ///
    /// Parameters
    /// ----------
    /// spec : Spec
    ///     The visualization specification from reader.execute().
    ///
    /// Returns
    /// -------
    /// tuple[bytes, list[str]]
    ///     The PDF document as bytes and a list of render warnings
    ///     (usually empty).
    ///
    /// Raises
    /// ------
    /// ValueError
    ///     If rendering fails.
    fn render(&self, py: Python<'_>, spec: &PySpec) -> PyResult<(Py<PyBytes>, Vec<String>)> {
        // Release the GIL while rendering (see PySVGWriter::render).
        let (bytes, warnings) = py
            .detach(|| self.inner.render_reporting(&spec.inner))
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(e.to_string()))?;
        Ok((PyBytes::new(py, &bytes).unbind(), warnings))
    }
}

// ============================================================================
// PyHepWriter
// ============================================================================

/// .hep plot document writer (native hephaestus renderer).
///
/// Renders visualization specifications to `.hep` plot documents — a
/// self-contained description of the resolved plot that a compatible
/// consumer can render and re-layout at any size.
///
/// Examples
/// --------
/// >>> writer = HepWriter()
/// >>> spec = reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")
/// >>> doc_bytes = writer.render(spec)
#[pyclass(name = "HepWriter")]
struct PyHepWriter {
    inner: RustHepWriter,
}

#[pymethods]
impl PyHepWriter {
    /// Create a new .hep writer.
    ///
    /// Returns
    /// -------
    /// HepWriter
    ///     A configured .hep writer instance.
    #[new]
    fn new() -> Self {
        Self {
            inner: RustHepWriter::default(),
        }
    }

    /// Render a Spec to a .hep plot document.
    ///
    /// Parameters
    /// ----------
    /// spec : Spec
    ///     The visualization specification from reader.execute().
    ///
    /// Returns
    /// -------
    /// tuple[bytes, list[str]]
    ///     The .hep document as bytes and a list of render warnings
    ///     (usually empty).
    ///
    /// Raises
    /// ------
    /// ValueError
    ///     If rendering fails.
    fn render(&self, py: Python<'_>, spec: &PySpec) -> PyResult<(Py<PyBytes>, Vec<String>)> {
        // Release the GIL while rendering (see PySVGWriter::render).
        let (bytes, warnings) = py
            .detach(|| self.inner.render_reporting(&spec.inner))
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(e.to_string()))?;
        Ok((PyBytes::new(py, &bytes).unbind(), warnings))
    }
}

// ============================================================================
// PyValidated
// ============================================================================

/// Result of validate() - query inspection and validation without SQL execution.
///
/// Contains information about query structure and any validation errors/warnings.
/// The tree() method from Rust is not exposed as it's not useful in Python.
#[pyclass(name = "Validated")]
struct PyValidated {
    sql: String,
    visual: String,
    has_visual: bool,
    valid: bool,
    errors: Vec<(String, Option<(usize, usize)>)>,
    warnings: Vec<(String, Option<(usize, usize)>)>,
}

#[pymethods]
impl PyValidated {
    /// Whether the query contains a VISUALISE clause.
    ///
    /// Returns
    /// -------
    /// bool
    ///     True if the query has a VISUALISE clause.
    fn has_visual(&self) -> bool {
        self.has_visual
    }

    /// The SQL portion (before VISUALISE).
    ///
    /// Returns
    /// -------
    /// str
    ///     The SQL part of the query.
    fn sql(&self) -> &str {
        &self.sql
    }

    /// The VISUALISE portion (raw text).
    ///
    /// Returns
    /// -------
    /// str
    ///     The VISUALISE part of the query.
    fn visual(&self) -> &str {
        &self.visual
    }

    /// Whether the query is valid (no errors).
    ///
    /// Returns
    /// -------
    /// bool
    ///     True if the query is syntactically and semantically valid.
    fn valid(&self) -> bool {
        self.valid
    }

    /// Validation errors (fatal issues).
    ///
    /// Returns
    /// -------
    /// list[dict]
    ///     List of error dictionaries with 'message' and optional 'location' keys.
    fn errors(&self, py: Python<'_>) -> PyResult<Py<PyList>> {
        errors_to_pylist(py, &self.errors)
    }

    /// Validation warnings (non-fatal issues).
    ///
    /// Returns
    /// -------
    /// list[dict]
    ///     List of warning dictionaries with 'message' and optional 'location' keys.
    fn warnings(&self, py: Python<'_>) -> PyResult<Py<PyList>> {
        errors_to_pylist(py, &self.warnings)
    }
}

// ============================================================================
// PySpec
// ============================================================================

/// Result of reader.execute(), ready for rendering.
///
/// Contains the resolved plot specification, data, and metadata.
/// Use writer.render(spec) to generate output.
///
/// Examples
/// --------
/// >>> spec = reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")
/// >>> print(f"Rows: {spec.metadata()['rows']}")
/// >>> writer = VegaLiteWriter()
/// >>> json_output = writer.render(spec)
#[pyclass(name = "Spec")]
struct PySpec {
    inner: Spec,
}

#[pymethods]
impl PySpec {
    /// Get visualization metadata.
    ///
    /// Returns
    /// -------
    /// dict
    ///     Dictionary with 'rows', 'columns', and 'layer_count' keys.
    fn metadata(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let m = self.inner.metadata();
        let dict = PyDict::new(py);
        dict.set_item("rows", m.rows)?;
        dict.set_item("columns", m.columns.clone())?;
        dict.set_item("layer_count", m.layer_count)?;
        Ok(dict.into())
    }

    /// The main SQL query that was executed.
    ///
    /// Returns
    /// -------
    /// str
    ///     The SQL query string.
    fn sql(&self) -> &str {
        self.inner.sql()
    }

    /// The VISUALISE portion (raw text).
    ///
    /// Returns
    /// -------
    /// str
    ///     The VISUALISE clause text.
    fn visual(&self) -> &str {
        self.inner.visual()
    }

    /// Number of layers.
    ///
    /// Returns
    /// -------
    /// int
    ///     The number of DRAW clauses in the visualization.
    fn layer_count(&self) -> usize {
        self.inner.layer_count()
    }

    /// Get global data (main query result).
    ///
    /// Returns
    /// -------
    /// pyarrow.Table | None
    ///     The main query result as a pyarrow Table, or None if not available.
    fn data(&self, py: Python<'_>) -> PyResult<Option<Py<PyAny>>> {
        self.inner
            .layer_data(0)
            .map(|df| df_to_py(py, df))
            .transpose()
    }

    /// Get layer-specific data (from FILTER or FROM clause).
    ///
    /// Parameters
    /// ----------
    /// index : int
    ///     The layer index (0-based).
    ///
    /// Returns
    /// -------
    /// pyarrow.Table | None
    ///     The layer-specific data as a pyarrow Table, or None if the layer uses global data.
    fn layer_data(&self, py: Python<'_>, index: usize) -> PyResult<Option<Py<PyAny>>> {
        self.inner
            .layer_data(index)
            .map(|df| df_to_py(py, df))
            .transpose()
    }

    /// Get stat transform data (e.g., histogram bins, density estimates).
    ///
    /// Parameters
    /// ----------
    /// index : int
    ///     The layer index (0-based).
    ///
    /// Returns
    /// -------
    /// pyarrow.Table | None
    ///     The stat transform data as a pyarrow Table, or None if no stat transform.
    fn stat_data(&self, py: Python<'_>, index: usize) -> PyResult<Option<Py<PyAny>>> {
        self.inner
            .stat_data(index)
            .map(|df| df_to_py(py, df))
            .transpose()
    }

    /// Validation warnings from preparation.
    ///
    /// Returns
    /// -------
    /// list[dict]
    ///     List of warning dictionaries with 'message' and optional 'location' keys.
    fn warnings(&self, py: Python<'_>) -> PyResult<Py<PyList>> {
        warnings_to_pylist(py, self.inner.warnings())
    }
}

// ============================================================================
// Module Functions
// ============================================================================

/// Validate query syntax and semantics without executing SQL.
///
/// Parameters
/// ----------
/// query : str
///     The ggsql query to validate.
///
/// Returns
/// -------
/// Validated
///     Validation result with query inspection methods.
///
/// Raises
/// ------
/// ValueError
///     If validation fails unexpectedly (not for syntax errors, which are captured).
#[pyfunction]
fn validate(query: &str) -> PyResult<PyValidated> {
    let v = rust_validate(query)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(e.to_string()))?;

    Ok(PyValidated {
        sql: v.sql().to_string(),
        visual: v.visual().to_string(),
        has_visual: v.has_visual(),
        valid: v.valid(),
        errors: v
            .errors()
            .iter()
            .map(|e| {
                (
                    e.message.clone(),
                    e.location.as_ref().map(|l| (l.line, l.column)),
                )
            })
            .collect(),
        warnings: v
            .warnings()
            .iter()
            .map(|w| {
                (
                    w.message.clone(),
                    w.location.as_ref().map(|l| (l.line, l.column)),
                )
            })
            .collect(),
    })
}

/// Execute a ggsql query using a custom Python reader.
///
/// This is a convenience function for custom readers. For native readers,
/// prefer using `reader.execute()` directly.
///
/// Parameters
/// ----------
/// query : str
///     The ggsql query to execute.
/// reader : Reader | object
///     The database reader to execute SQL against. Can be a native Reader
///     for optimal performance, or any Python object with an
///     `execute_sql(sql: str) -> pyarrow.Table` method.
///
/// Returns
/// -------
/// Spec
///     The resolved visualization specification ready for rendering.
///
/// Raises
/// ------
/// ValueError
///     If parsing, validation, or SQL execution fails.
///
/// Examples
/// --------
/// >>> # Using native reader (prefer reader.execute() instead)
/// >>> reader = DuckDBReader("duckdb://memory")
/// >>> spec = execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point", reader)
/// >>> writer = VegaLiteWriter()
/// >>> json_output = writer.render(spec)
///
/// >>> # Using custom Python reader
/// >>> class MyReader:
/// ...     def execute_sql(self, sql: str) -> pa.Table:
/// ...         return pa.table({"x": [1, 2, 3], "y": [10, 20, 30]})
/// >>> reader = MyReader()
/// >>> spec = execute("SELECT * FROM data VISUALISE x, y DRAW point", reader)
#[pyfunction]
fn execute(query: &str, reader: &Bound<'_, PyAny>) -> PyResult<PySpec> {
    // Fast path: try all known native reader types
    // Add new native readers to this list as they're implemented
    try_native_readers!(query, reader, PyDuckDBReader, PyCachingReader);

    // Bridge path: wrap Python object as Reader
    let bridge = PyReaderBridge {
        obj: reader.clone().unbind(),
    };
    bridge
        .execute(query)
        .map(|s| PySpec { inner: s })
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(e.to_string()))
}

// ============================================================================
// Module Registration
// ============================================================================

#[pymodule]
fn _ggsql(m: &Bound<'_, PyModule>) -> PyResult<()> {
    // Classes
    m.add_class::<PyDuckDBReader>()?;
    m.add_class::<PyCachingReader>()?;
    m.add_class::<PySVGWriter>()?;
    m.add_class::<PyPDFWriter>()?;
    m.add_class::<PyHepWriter>()?;
    m.add_class::<PyVegaLiteWriter>()?;
    m.add_class::<PyValidated>()?;
    m.add_class::<PySpec>()?;
    m.add_class::<ArrowStreamShim>()?;

    // Functions
    m.add_function(wrap_pyfunction!(validate, m)?)?;
    m.add_function(wrap_pyfunction!(execute, m)?)?;

    Ok(())
}
