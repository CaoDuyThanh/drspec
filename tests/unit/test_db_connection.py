"""Tests for database connection module."""

import tempfile
from pathlib import Path

import pytest
import duckdb

from drspec.db.connection import (
    get_connection,
    get_connection_context,
    get_db_path,
    init_schema,
    ensure_db_directory,
    DEFAULT_DB_PATH,
)


class TestGetDbPath:
    """Tests for get_db_path function."""

    def test_returns_default_path_when_none(self):
        """Test default path is used when no path provided."""
        path = get_db_path(None)
        assert path == Path.cwd() / DEFAULT_DB_PATH

    def test_returns_custom_path_when_provided(self):
        """Test custom path is used when provided."""
        custom = Path("/tmp/custom.db")
        path = get_db_path(custom)
        assert path == custom


class TestGetConnection:
    """Tests for get_connection function."""

    def test_raises_when_directory_missing(self):
        """Test raises FileNotFoundError when directory doesn't exist."""
        with pytest.raises(FileNotFoundError) as exc:
            get_connection(Path("/nonexistent/path/test.db"))
        assert "does not exist" in str(exc.value)

    def test_creates_connection_when_directory_exists(self):
        """Test creates connection when directory exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = get_connection(db_path)
            assert conn is not None
            conn.close()

    def test_connection_is_functional(self):
        """Test returned connection can execute queries."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = get_connection(db_path)
            result = conn.execute("SELECT 1 + 1").fetchone()
            assert result[0] == 2
            conn.close()


class TestGetConnectionContext:
    """Tests for get_connection_context context manager."""

    def test_yields_connection(self):
        """Test context manager yields connection."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            with get_connection_context(db_path) as conn:
                assert conn is not None
                result = conn.execute("SELECT 42").fetchone()
                assert result[0] == 42

    def test_closes_connection_on_exit(self):
        """Test connection is closed after context exit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            with get_connection_context(db_path) as conn:
                pass
            # Connection should be closed - can't execute queries
            with pytest.raises(Exception):
                conn.execute("SELECT 1")


class TestInitSchema:
    """Tests for init_schema function."""

    def test_creates_all_tables(self):
        """Test schema creates all required tables."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = duckdb.connect(str(db_path))
            init_schema(conn)

            # Check all tables exist
            tables = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
            ).fetchall()
            table_names = {t[0] for t in tables}

            expected_tables = {
                "artifacts",
                "contracts",
                "queue",
                "dependencies",
                "reasoning_traces",
            }
            assert expected_tables.issubset(table_names)
            conn.close()

    def test_rebuild_drops_and_recreates(self):
        """Test rebuild=True drops and recreates tables."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = duckdb.connect(str(db_path))

            # Create schema and insert data
            init_schema(conn)
            conn.execute(
                "INSERT INTO artifacts (function_id, file_path, function_name, signature, body, code_hash, "
                "language, start_line, end_line) "
                "VALUES ('test::foo', 'test.py', 'foo', 'def foo():', 'pass', 'abc123', 'python', 1, 2)"
            )

            # Verify data exists
            count = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
            assert count == 1

            # Rebuild schema
            init_schema(conn, rebuild=True)

            # Verify tables are empty
            count = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
            assert count == 0
            conn.close()

    def test_schema_is_idempotent(self):
        """Test running init_schema multiple times is safe."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = duckdb.connect(str(db_path))

            # Run multiple times
            init_schema(conn)
            init_schema(conn)
            init_schema(conn)

            # Should not raise - tables already exist
            tables = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
            ).fetchall()
            assert len(tables) >= 5
            conn.close()


class TestEnsureDbDirectory:
    """Tests for ensure_db_directory function."""

    def test_creates_directory_if_missing(self):
        """Test creates parent directories if they don't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "subdir" / "nested" / "test.db"
            result = ensure_db_directory(db_path)

            assert result == db_path
            assert db_path.parent.exists()

    def test_returns_path_if_exists(self):
        """Test returns path when directory already exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            result = ensure_db_directory(db_path)

            assert result == db_path
