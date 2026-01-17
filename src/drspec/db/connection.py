"""DuckDB connection management for DrSpec."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional

import duckdb

# Default database path relative to current working directory
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


def get_connection(db_path: Optional[Path] = None) -> duckdb.DuckDBPyConnection:
    """Get a DuckDB connection.

    Args:
        db_path: Optional custom database path. If None, uses _drspec/contracts.db.

    Returns:
        DuckDB connection object.

    Raises:
        FileNotFoundError: If the database directory doesn't exist.
    """
    path = get_db_path(db_path)

    # Ensure parent directory exists
    if not path.parent.exists():
        raise FileNotFoundError(
            f"Database directory does not exist: {path.parent}. "
            "Run 'drspec init' first."
        )

    return duckdb.connect(str(path))


@contextmanager
def get_connection_context(
    db_path: Optional[Path] = None,
) -> Generator[duckdb.DuckDBPyConnection, None, None]:
    """Context manager for DuckDB connections.

    Ensures connection is properly closed after use.

    Args:
        db_path: Optional custom database path.

    Yields:
        DuckDB connection object.
    """
    conn = get_connection(db_path)
    try:
        yield conn
    finally:
        conn.close()


def init_schema(
    conn: duckdb.DuckDBPyConnection,
    rebuild: bool = False,
) -> None:
    """Initialize database schema from schema.sql.

    Args:
        conn: DuckDB connection.
        rebuild: If True, drop all tables and recreate. Use for development.
    """
    # Read schema file
    schema_path = Path(__file__).parent / "schema.sql"
    schema_sql = schema_path.read_text()

    # Execute in transaction
    conn.begin()
    try:
        if rebuild:
            # Drop tables in reverse dependency order
            # vision_findings depends on artifacts
            conn.execute("DROP TABLE IF EXISTS vision_findings")
            conn.execute("DROP SEQUENCE IF EXISTS seq_vision_findings_id")
            conn.execute("DROP TABLE IF EXISTS reasoning_traces")
            conn.execute("DROP SEQUENCE IF EXISTS seq_reasoning_traces_id")
            conn.execute("DROP TABLE IF EXISTS config")
            conn.execute("DROP TABLE IF EXISTS dependencies")
            conn.execute("DROP TABLE IF EXISTS queue")
            conn.execute("DROP TABLE IF EXISTS contracts")
            conn.execute("DROP TABLE IF EXISTS artifacts")

        # Execute schema
        conn.execute(schema_sql)
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
