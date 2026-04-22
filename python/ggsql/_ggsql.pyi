from __future__ import annotations

from typing import Any

import polars as pl

class DuckDBReader:
    """DuckDB database reader for executing SQL queries.

    Creates an in-memory or file-based DuckDB connection that can execute
    SQL queries and register DataFrames as queryable tables.

    Examples
    --------
    >>> reader = DuckDBReader("duckdb://memory")
    >>> df = reader.execute_sql("SELECT 1 as x, 2 as y")

    >>> reader = DuckDBReader("duckdb://memory")
    >>> reader.register("data", pl.DataFrame({"x": [1, 2, 3]}))
    >>> df = reader.execute_sql("SELECT * FROM data WHERE x > 1")
    """

    def __init__(self, connection: str) -> None: ...
    def execute(self, query: str) -> Spec:
        """Execute a ggsql query and return the visualization specification.

        This is the main entry point for creating visualizations. It parses
        the query, executes the SQL portion, and returns a Spec ready
        for rendering.

        Parameters
        ----------
        query : str
            The ggsql query (SQL + VISUALISE clause).

        Returns
        -------
        Spec
            The resolved visualization specification ready for rendering.

        Raises
        ------
        ValueError
            If the query syntax is invalid, has no VISUALISE clause, or SQL execution fails.

        Examples
        --------
        >>> reader = DuckDBReader("duckdb://memory")
        >>> spec = reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")
        >>> writer = VegaLiteWriter()
        >>> json_output = writer.render(spec)
        """
        ...

    def execute_sql(self, sql: str) -> pl.DataFrame:
        """Execute a SQL query and return the result as a DataFrame.

        Parameters
        ----------
        sql : str
            The SQL query to execute.

        Returns
        -------
        polars.DataFrame
            The query result as a polars DataFrame.

        Raises
        ------
        ValueError
            If the SQL is invalid or execution fails.
        """
        ...

    def register(self, name: str, df: pl.DataFrame, replace: bool = False) -> None:
        """Register a DataFrame as a queryable table.

        After registration, the DataFrame can be queried by name in SQL.

        Parameters
        ----------
        name : str
            The table name to register under.
        df : polars.DataFrame
            The DataFrame to register. Must be a polars DataFrame.
        replace : bool
            If True, replace an existing table with the same name.

        Raises
        ------
        ValueError
            If registration fails or the table name is invalid.
        """
        ...

    def unregister(self, name: str) -> None:
        """Unregister a previously registered table.

        Parameters
        ----------
        name : str
            The table name to unregister.

        Raises
        ------
        ValueError
            If the table wasn't registered via this reader or unregistration fails.
        """
        ...

class VegaLiteWriter:
    """Vega-Lite JSON output writer.

    Converts visualization specifications to Vega-Lite v6 JSON.

    Examples
    --------
    >>> writer = VegaLiteWriter()
    >>> spec = reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")
    >>> json_output = writer.render(spec)
    """

    def __init__(self) -> None: ...
    def render(self, spec: Spec) -> str:
        """Render a Spec to Vega-Lite JSON output.

        Parameters
        ----------
        spec : Spec
            The visualization specification from ``reader.execute()``.

        Returns
        -------
        str
            The output (i.e., Vega-Lite JSON string).

        Raises
        ------
        ValueError
            If rendering fails.

        Examples
        --------
        >>> reader = DuckDBReader("duckdb://memory")
        >>> spec = reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")
        >>> writer = VegaLiteWriter()
        >>> json_output = writer.render(spec)
        """
        ...

class Validated:
    """Result of ``validate()`` — query inspection and validation without SQL execution.

    Contains information about query structure and any validation errors or warnings.
    """

    def valid(self) -> bool:
        """Whether the query is valid (no errors).

        Returns
        -------
        bool
            True if the query is syntactically and semantically valid.
        """
        ...

    def has_visual(self) -> bool:
        """Whether the query contains a VISUALISE clause.

        Returns
        -------
        bool
            True if the query has a VISUALISE clause.
        """
        ...

    def sql(self) -> str:
        """The SQL portion (before VISUALISE).

        Returns
        -------
        str
            The SQL part of the query.
        """
        ...

    def visual(self) -> str:
        """The VISUALISE portion (raw text).

        Returns
        -------
        str
            The VISUALISE part of the query.
        """
        ...

    def errors(self) -> list[dict[str, Any]]:
        """Validation errors (fatal issues).

        Returns
        -------
        list[dict]
            List of error dictionaries with ``message`` and optional ``location`` keys.
        """
        ...

    def warnings(self) -> list[dict[str, Any]]:
        """Validation warnings (non-fatal issues).

        Returns
        -------
        list[dict]
            List of warning dictionaries with ``message`` and optional ``location`` keys.
        """
        ...

class Spec:
    """Result of ``reader.execute()``, ready for rendering.

    Contains the resolved plot specification, data, and metadata.
    Use ``writer.render(spec)`` to generate output.

    Examples
    --------
    >>> spec = reader.execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point")
    >>> print(f"Rows: {spec.metadata()['rows']}")
    >>> writer = VegaLiteWriter()
    >>> json_output = writer.render(spec)
    """

    def metadata(self) -> dict[str, Any]:
        """Get visualization metadata.

        Returns
        -------
        dict
            Dictionary with ``rows``, ``columns``, and ``layer_count`` keys.
        """
        ...

    def sql(self) -> str:
        """The main SQL query that was executed.

        Returns
        -------
        str
            The SQL query string.
        """
        ...

    def visual(self) -> str:
        """The VISUALISE portion (raw text).

        Returns
        -------
        str
            The VISUALISE clause text.
        """
        ...

    def layer_count(self) -> int:
        """Number of layers.

        Returns
        -------
        int
            The number of DRAW clauses in the visualization.
        """
        ...

    def data(self) -> pl.DataFrame | None:
        """Get global data (main query result).

        Returns
        -------
        polars.DataFrame | None
            The main query result DataFrame, or None if not available.
        """
        ...

    def layer_data(self, index: int) -> pl.DataFrame | None:
        """Get layer-specific data (from FILTER or FROM clause).

        Parameters
        ----------
        index : int
            The layer index (0-based).

        Returns
        -------
        polars.DataFrame | None
            The layer-specific DataFrame, or None if the layer uses global data.
        """
        ...

    def stat_data(self, index: int) -> pl.DataFrame | None:
        """Get stat transform data (e.g., histogram bins, density estimates).

        Parameters
        ----------
        index : int
            The layer index (0-based).

        Returns
        -------
        polars.DataFrame | None
            The stat transform DataFrame, or None if no stat transform.
        """
        ...

    def layer_sql(self, index: int) -> str | None:
        """Layer filter/source query, or None if using global data.

        Parameters
        ----------
        index : int
            The layer index (0-based).

        Returns
        -------
        str | None
            The filter SQL query, or None if the layer uses global data directly.
        """
        ...

    def stat_sql(self, index: int) -> str | None:
        """Stat transform query, or None if no stat transform.

        Parameters
        ----------
        index : int
            The layer index (0-based).

        Returns
        -------
        str | None
            The stat transform SQL query, or None if no stat transform.
        """
        ...

    def warnings(self) -> list[dict[str, Any]]:
        """Validation warnings from preparation.

        Returns
        -------
        list[dict]
            List of warning dictionaries with ``message`` and optional ``location`` keys.
        """
        ...

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

    Raises
    ------
    ValueError
        If validation fails unexpectedly (not for syntax errors, which are captured).
    """
    ...

def execute(query: str, reader: Any) -> Spec:
    """Execute a ggsql query using a custom Python reader.

    This is a convenience function for custom readers. For native readers,
    prefer using ``reader.execute()`` directly.

    Parameters
    ----------
    query : str
        The ggsql query to execute.
    reader : Reader | object
        The database reader to execute SQL against. Can be a native Reader
        for optimal performance, or any Python object with an
        ``execute_sql(sql: str) -> polars.DataFrame`` method.

    Returns
    -------
    Spec
        The resolved visualization specification ready for rendering.

    Raises
    ------
    ValueError
        If parsing, validation, or SQL execution fails.

    Examples
    --------
    >>> # Using native reader (prefer reader.execute() instead)
    >>> reader = DuckDBReader("duckdb://memory")
    >>> spec = execute("SELECT 1 AS x, 2 AS y VISUALISE x, y DRAW point", reader)
    >>> writer = VegaLiteWriter()
    >>> json_output = writer.render(spec)

    >>> # Using custom Python reader
    >>> class MyReader:
    ...     def execute_sql(self, sql: str) -> pl.DataFrame:
    ...         return pl.DataFrame({"x": [1, 2, 3], "y": [10, 20, 30]})
    >>> reader = MyReader()
    >>> spec = execute("SELECT * FROM data VISUALISE x, y DRAW point", reader)
    """
    ...
