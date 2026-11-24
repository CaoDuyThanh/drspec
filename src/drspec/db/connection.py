"""DuckDB connection management for DrSpec."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Optional

import duckdb

# DuckDB's Python binding probes for optional pandas/pyarrow/polars via
# importlib.find_spec on every parameterized execute() call. A failed
# import isn't cached, so when none of these are installed this walks
# all of sys.path on every call — a real bottleneck on bulk inserts.
# Batching into fewer execute()/executemany() calls doesn't help; the
# cost scales with bound parameter values, not call count.
#
# Marking a confirmed-absent module as None in sys.modules is the
# documented idiom for "deliberately absent": both `import` and
# `find_spec` check sys.modules before sys.meta_path, so this is
# honored correctly and only applied to modules already absent.
for _optional_df_integration in ("pandas", "pyarrow", "polars"):
    if _optional_df_integration not in sys.modules and importlib.util.find_spec(_optional_df_integration) is None:
        sys.modules[_optional_df_integration] = None
del _optional_df_integration

DEFAULT_DB_PATH = "_drspec/contracts.db"


def get_db_path(db_path: Optional[Path] = None) -> Path:
    """Get the database path, using default if not provided.

    Args:
        db_path: Optional custom database path.

    Returns:
        Path to the database file.
    """
    if db_path is not None:
        return Path(db_path)
    return Path.cwd() / DEFAULT_DB_PATH


def get_connection(db_path: Optional[Path] = None, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Get a DuckDB connection.

    Args:
        db_path: Optional custom database path. If None, uses _drspec/contracts.db.
        read_only: Request a read-only connection for a command that never
            writes. Unlike a read-write connection (which blocks every other
            process for as long as it's held), a read-only connection isn't
            blocked by other readers and doesn't block writers. DuckDB
            rejects writes through it, so misuse fails loudly, not silently.

    Returns:
        DuckDB connection object.

    Raises:
        FileNotFoundError: If the database directory doesn't exist, or
            (`read_only=True` only) if the database file doesn't exist yet
            — a read-write connection would silently create an empty file
            instead, but DuckDB has no such fallback for read-only, so
            this turns that `duckdb.IOException` into the same error.
    """
    path = get_db_path(db_path)

    if not path.parent.exists():
        raise FileNotFoundError(
            f"Database directory does not exist: {path.parent}. "
            "Run 'drspec init' first."
        )

    if read_only:
        try:
            return duckdb.connect(str(path), read_only=True)
        except duckdb.IOException as e:
            raise FileNotFoundError(f"Database file does not exist: {path}. Run 'drspec init' first. ({e})") from e

    return duckdb.connect(str(path))


def init_schema(
    conn: duckdb.DuckDBPyConnection,
    rebuild: bool = False,
) -> None:
    """Initialize database schema from schema.sql.

    Args:
        conn: DuckDB connection.
        rebuild: If True, drop all tables and recreate. Use for development.
    """
    schema_path = Path(__file__).parent / "schema.sql"
    schema_sql = schema_path.read_text()

    conn.begin()
    try:
        if rebuild:
            # Reverse dependency order
            conn.execute("DROP TABLE IF EXISTS scan_requests")
            conn.execute("DROP TABLE IF EXISTS doc_file_links")
            conn.execute("DROP TABLE IF EXISTS doc_links")
            conn.execute("DROP TABLE IF EXISTS doc_sections")
            conn.execute("DROP TABLE IF EXISTS pending_removals")
            conn.execute("DROP TABLE IF EXISTS function_declarations")
            conn.execute("DROP TABLE IF EXISTS artifact_embeddings")
            conn.execute("DROP TABLE IF EXISTS vision_findings")
            conn.execute("DROP SEQUENCE IF EXISTS seq_vision_findings_id")
            conn.execute("DROP TABLE IF EXISTS reasoning_traces")
            conn.execute("DROP SEQUENCE IF EXISTS seq_reasoning_traces_id")
            conn.execute("DROP TABLE IF EXISTS config")
            conn.execute("DROP TABLE IF EXISTS dependencies")
            conn.execute("DROP TABLE IF EXISTS code_references")
            conn.execute("DROP TABLE IF EXISTS member_references")
            conn.execute("DROP TABLE IF EXISTS contracts")
            conn.execute("DROP TABLE IF EXISTS artifacts")

        conn.execute(schema_sql)
        # The contract queue no longer exists. Its rows reference `artifacts`,
        # so an old table left in place would block deleting a function.
        conn.execute("DROP TABLE IF EXISTS queue")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def ensure_db_directory(db_path: Optional[Path] = None) -> Path:
    """Ensure database directory exists, creating it if necessary.

    Args:
        db_path: Optional custom database path.

    Returns:
        Path to the database file.
    """
    path = get_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
