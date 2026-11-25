"""The database connection module: where the database is, how a connection is opened (read-write, read-only), the
schema and its additions, the directory helper, and the import-time probe fix.

Schema behaviour is checked on an in-memory DuckDB database; only the tests whose point is the FILE (its folder, its
read-only mode, several readers at once) use a file, and they share one file made once for the module.
"""

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import duckdb
import pytest

from tests.helpers import file_hash

from drspec.db.connection import (
    DEFAULT_DB_PATH,
    ensure_db_directory,
    get_connection,
    get_db_path,
    init_schema,
)


@pytest.fixture
def mem():
    """An in-memory DuckDB database. Schema behaviour needs no file, and a file costs a checkpoint on close."""
    conn = duckdb.connect(":memory:")
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def db_file(tmp_path_factory) -> Path:
    """One database file for the module, created once and only read by the read-only tests."""
    path = tmp_path_factory.mktemp("db_file") / "test.db"
    get_connection(path).close()  # pattern-ok: duckdb-file the behaviour of `get_connection` on a FILE is the objective
    before = file_hash(path)
    yield path
    assert file_hash(path) == before, "a test wrote into the file the module shares"


@pytest.fixture
def two_artifacts(mem):
    """The schema, and two functions that a dependency can join."""
    init_schema(mem)
    for function_id, name in [("a.py::foo", "foo"), ("a.py::bar", "bar")]:
        mem.execute(
            "INSERT INTO artifacts (function_id, file_path, function_name, signature, body, "
            "code_hash, language, start_line, end_line) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [function_id, "a.py", name, f"def {name}():", f"def {name}(): pass", f"hash_{name}", "python", 1, 1],
        )
    return mem


class TestGetDbPath:
    def test_returns_default_path_when_none(self):
        assert get_db_path(None) == Path.cwd() / DEFAULT_DB_PATH

    def test_returns_custom_path_when_provided(self, tmp_path):
        custom = tmp_path / "custom.db"
        assert get_db_path(custom) == custom


class TestGetConnection:
    @pytest.mark.parametrize("read_only", [False, True])
    def test_a_missing_directory_is_a_clear_error(self, read_only, tmp_path):
        with pytest.raises(FileNotFoundError) as exc:
            get_connection(tmp_path / "no-such-folder" / "test.db", read_only=read_only)  # pattern-ok: duckdb-file same
        assert "does not exist" in str(exc.value)

    def test_a_connection_in_an_existing_folder_is_usable(self, tmp_path):
        conn = get_connection(tmp_path / "test.db")  # pattern-ok: duckdb-file same
        assert conn.execute("SELECT 1 + 1").fetchone()[0] == 2
        conn.close()


class TestGetConnectionReadOnly:
    def test_can_read_from_an_existing_database(self, db_file):
        conn = get_connection(db_file, read_only=True)  # pattern-ok: duckdb-file same
        assert conn.execute("SELECT 1 + 1").fetchone()[0] == 2
        conn.close()

    def test_a_write_attempt_is_rejected(self, db_file):
        conn = get_connection(db_file, read_only=True)  # pattern-ok: duckdb-file same
        with pytest.raises(duckdb.Error):
            conn.execute("CREATE TABLE attempt (x INTEGER)")
        conn.close()

    def test_raises_file_not_found_when_the_db_file_itself_is_missing(self, tmp_path):
        """Unlike a plain read-write connection (which would silently create an empty, schema-less file), a read-only
        request for a `.db` file that does not exist must fail clearly."""
        with pytest.raises(FileNotFoundError) as exc:
            get_connection(tmp_path / "never_created.db", read_only=True)  # pattern-ok: duckdb-file same
        assert "does not exist" in str(exc.value)

    def test_two_readonly_connections_coexist_without_blocking_each_other(self, db_file):
        conn_a = get_connection(db_file, read_only=True)  # pattern-ok: duckdb-file same
        conn_b = get_connection(db_file, read_only=True)  # pattern-ok: duckdb-file same
        assert conn_a.execute("SELECT 1").fetchone()[0] == 1
        assert conn_b.execute("SELECT 1").fetchone()[0] == 1
        conn_a.close()
        conn_b.close()


class TestInitSchema:
    def test_creates_all_tables(self, mem):
        init_schema(mem)
        names = {row[0] for row in mem.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'").fetchall()}
        assert {"artifacts", "contracts", "dependencies", "reasoning_traces"} <= names

    def test_rebuild_drops_and_recreates(self, mem):
        init_schema(mem)
        mem.execute(
            "INSERT INTO artifacts (function_id, file_path, function_name, signature, body, code_hash, "
            "language, start_line, end_line) VALUES ('test::foo', 'test.py', 'foo', 'def foo():', 'pass', 'abc123', 'python', 1, 2)"
        )
        assert mem.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1
        init_schema(mem, rebuild=True)
        assert mem.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0

    def test_schema_is_idempotent(self, mem):
        for _ in range(3):
            init_schema(mem)  # no error although the tables exist
        assert len(mem.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'").fetchall()) >= 5


class TestEmbeddingAndDependencyColumns:
    """The code-graph schema additions: artifacts.embedding lives in its own table; dependencies carries call_site,
    edge_hash, embedding and confidence."""

    def test_function_vectors_live_in_their_own_table_not_in_artifacts(self, mem):
        init_schema(mem)
        assert "embedding" not in {row[1] for row in mem.execute("PRAGMA table_info('artifacts')").fetchall()}
        cols = {row[1]: row[2] for row in mem.execute("PRAGMA table_info('artifact_embeddings')").fetchall()}
        assert cols["embedding"] == "FLOAT[]" and "function_id" in cols

    @pytest.mark.parametrize(
        "expected",
        [{"call_site": "VARCHAR", "edge_hash": "VARCHAR", "embedding": "FLOAT[]", "confidence": "VARCHAR"}],
    )
    def test_dependencies_has_the_new_columns(self, mem, expected):
        init_schema(mem)
        cols = {row[1]: row[2] for row in mem.execute("PRAGMA table_info('dependencies')").fetchall()}
        assert {name: cols[name] for name in expected} == expected

    def test_an_embedding_round_trips_through_its_column(self, mem, two_artifacts):
        init_schema(mem)
        vector = [0.0, 1.0, 2.0]
        mem.execute("INSERT INTO artifact_embeddings (function_id, embedding) VALUES (?, ?)", ["a.py::foo", vector])
        row = mem.execute("SELECT embedding FROM artifact_embeddings WHERE function_id = ?", ["a.py::foo"]).fetchone()
        assert list(row[0]) == vector

    def test_a_dependency_row_round_trips(self, mem, two_artifacts):
        init_schema(mem)
        vector = [0.0, 2.0, 4.0]
        mem.execute(
            "INSERT INTO dependencies (caller_id, callee_id, call_site, edge_hash, embedding, confidence) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ["a.py::foo", "a.py::bar", "bar()", "edgehash123", vector, "EXTRACTED"],
        )
        call_site, edge_hash, embedding, confidence = mem.execute(
            "SELECT call_site, edge_hash, embedding, confidence FROM dependencies WHERE caller_id = ? AND callee_id = ?",
            ["a.py::foo", "a.py::bar"],
        ).fetchone()
        assert (call_site, edge_hash, confidence) == ("bar()", "edgehash123", "EXTRACTED")
        assert list(embedding) == vector

    def test_the_dependency_primary_key_is_enforced(self, mem, two_artifacts):
        init_schema(mem)
        mem.execute("INSERT INTO dependencies (caller_id, callee_id, confidence) VALUES (?, ?, ?)", ["a.py::foo", "a.py::bar", "EXTRACTED"])
        with pytest.raises(duckdb.ConstraintException):
            mem.execute("INSERT INTO dependencies (caller_id, callee_id, confidence) VALUES (?, ?, ?)", ["a.py::foo", "a.py::bar", "INFERRED"])

    def test_an_artifact_still_used_by_a_dependency_cannot_be_deleted_outright(self, mem, two_artifacts):
        """DuckDB refuses the delete (no silent cascade). The app's order works: the dependency row first, then the artifact."""
        init_schema(mem)
        mem.execute("INSERT INTO dependencies (caller_id, callee_id, confidence) VALUES (?, ?, ?)", ["a.py::foo", "a.py::bar", "EXTRACTED"])
        with pytest.raises(duckdb.ConstraintException):
            mem.execute("DELETE FROM artifacts WHERE function_id = ?", ["a.py::bar"])
        mem.execute("DELETE FROM dependencies WHERE caller_id = ? AND callee_id = ?", ["a.py::foo", "a.py::bar"])
        mem.execute("DELETE FROM artifacts WHERE function_id = ?", ["a.py::bar"])
        assert mem.execute("SELECT COUNT(*) FROM artifacts WHERE function_id = ?", ["a.py::bar"]).fetchone()[0] == 0


class TestEnsureDbDirectory:
    @pytest.mark.parametrize("relative", ["subdir/nested/test.db", "test.db"])
    def test_the_folder_is_made_when_missing_and_the_path_comes_back(self, tmp_path, relative):
        db_path = tmp_path / relative
        assert ensure_db_directory(db_path) == db_path
        assert db_path.parent.exists()


class TestOptionalIntegrationProbeFix:
    """`connection.py` marks absent optional DuckDB dataframe integrations (pandas, pyarrow, polars) in `sys.modules`,
    so `conn.execute()` does not probe `sys.path` for them on every call. These tests check the behaviour, not the timing."""

    NAMES = ("pandas", "pyarrow", "polars")

    def _run_the_probe(self, monkeypatch, installed: bool):
        """Run the module's marking loop again as a process would at start: no name marked, and `find_spec` answering as if every package
        were installed (a spec) or absent (None), whatever the machine has. (The test session itself marks the three absent: see `conftest.py`.)"""
        for name in self.NAMES:
            monkeypatch.delitem(sys.modules, name, raising=False)
        monkeypatch.setattr(importlib.util, "find_spec", lambda name, package=None: object() if installed else None)
        import drspec.db.connection as connection

        importlib.reload(connection)
        return {name: sys.modules.get(name, "not-marked") for name in self.NAMES}

    def test_a_package_that_is_installed_is_never_hidden_and_an_absent_one_is_marked(self, monkeypatch):
        """The probe marks a name absent only when `find_spec` says it is absent: whatever is importable is not hidden."""
        assert self._run_the_probe(monkeypatch, installed=True) == {name: "not-marked" for name in self.NAMES}
        assert self._run_the_probe(monkeypatch, installed=False) == {name: None for name in self.NAMES}

    def test_database_operations_still_work_with_the_fix_applied(self, mem, two_artifacts):
        init_schema(mem)
        assert mem.execute("SELECT function_name FROM artifacts WHERE function_id = ?", ["a.py::foo"]).fetchone() == ("foo",)

    def test_import_of_a_genuinely_absent_optional_integration_still_raises_cleanly(self, monkeypatch):
        """`sys.modules[name] = None` must still behave like a normal failed import for other code doing `import pandas`
        directly, not swallow it or return a fake module. A made-up name is hidden the same way the fix hides the real ones, so the
        test does not depend on which of pandas/pyarrow/polars the machine has."""
        import importlib as _importlib

        name = "an_optional_integration_nobody_installed"
        monkeypatch.setitem(sys.modules, name, None)
        with pytest.raises(ModuleNotFoundError):
            _importlib.import_module(name)
        assert importlib.util.find_spec(name) is None


class TestLegacyQueueTable:
    def test_an_old_queue_table_is_dropped_so_functions_can_still_be_deleted(self, mem):
        from drspec.db import cascade_delete_artifact, insert_artifact

        init_schema(mem)
        mem.execute("CREATE TABLE queue (function_id TEXT PRIMARY KEY REFERENCES artifacts(function_id), status TEXT)")
        insert_artifact(
            mem, function_id="a.py::f", file_path="a.py", function_name="f", signature="def f():",
            body="pass", code_hash="h", language="python", start_line=1, end_line=2,
        )
        mem.execute("INSERT INTO queue VALUES ('a.py::f', 'PENDING')")

        init_schema(mem)

        assert "queue" not in {row[0] for row in mem.execute("SHOW TABLES").fetchall()}
        assert cascade_delete_artifact(mem, "a.py::f") is True
