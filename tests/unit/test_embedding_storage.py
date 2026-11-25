"""Embeddings are stored as variable-length lists, verified after every store,
and a database that still uses fixed-size arrays is rebuilt."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.core import embedder, embedding_runner
from drspec.core.embedding_backlog import (
    Item,
    VectorNotPersisted,
    embedding_counts,
    find_backlog,
    meta_key,
    meta_value,
    store_embeddings,
)
from drspec.core.settings import read_all_settings, set_setting
from drspec.db import get_connection, init_schema, insert_artifact
from drspec.db.migration import (
    LAYOUT_KEY,
    MigrationError,
    backup_path,
    ensure_embedding_layout,
    migrate_embedding_layout,
    needs_embedding_migration,
)
from drspec.db.queries import (
    enqueue_staleness,
    insert_contract,
    insert_reasoning_trace,
    insert_vision_finding,
    search_artifacts_semantic,
    set_config,
    upsert_dependency_edge,
    upsert_scanned_file,
)
from drspec.embedding_supervisor import EmbeddingSupervisor
from tests.helpers import file_hash
from drspec.learning.history import LearningEvent, init_learning_schema, insert_learning_event

runner = CliRunner()
DIM = 384
SCHEMA = Path(__file__).resolve().parents[2] / "src" / "drspec" / "db" / "schema.sql"


def _vector(value: float):
    return [float(value)] * DIM


class TestTheCorruptionIsGone:
    """DuckDB 1.3.2 reads some rows of a fixed `FLOAT[n]` column as NULL after a vector is cleared and written back in a later session.

    The input is the SMALLEST that shows it, found by bisecting the old (fixed-array) column on this DuckDB: **3 rows of 6 values** (2 rows
    never show it, 3 rows with 5 values never show it; one row that was never embedded, one cleared and written back, one left alone). The
    list column reads back every row at the same size. A round 100 x 384 showed nothing more and cost 100 inserts and three sessions of
    38,400 floats."""

    ROWS, VALUES = 3, 6

    def _scenario(self, path: Path, column_type: str):
        connection = duckdb.connect(str(path))  # pattern-ok: duckdb-file the bug is about a FILE read again in a LATER session: closing and reopening the file is the objective
        connection.execute(f"CREATE TABLE a (id TEXT PRIMARY KEY, embedding {column_type})")
        rows = [[f"f{i}", [float(i + 1)] * self.VALUES if i % 10 else None] for i in range(self.ROWS)]  # (f0 was never embedded)
        connection.executemany("INSERT INTO a VALUES (?, ?)", rows)
        connection.execute("CHECKPOINT")
        connection.close()
        connection = duckdb.connect(str(path))  # pattern-ok: duckdb-file the bug is about a FILE read again in a LATER session: closing and reopening the file is the objective
        connection.execute("UPDATE a SET embedding = NULL WHERE id = 'f1'")
        connection.close()
        connection = duckdb.connect(str(path))  # pattern-ok: duckdb-file the bug is about a FILE read again in a LATER session: closing and reopening the file is the objective
        connection.execute("UPDATE a SET embedding = ? WHERE id = 'f1'", [[9.0] * self.VALUES])
        connection.execute("CHECKPOINT")
        connection.close()
        connection = duckdb.connect(str(path))  # pattern-ok: duckdb-file the bug is about a FILE read again in a LATER session: closing and reopening the file is the objective
        try:
            scanned = {row[0] for row in connection.execute("SELECT id FROM a WHERE embedding IS NOT NULL").fetchall()}
            projected = {row[0] for row in connection.execute("SELECT id, embedding IS NOT NULL FROM a").fetchall() if row[1]}
            counted = connection.execute("SELECT COUNT(embedding) FROM a").fetchone()[0]
        finally:
            connection.close()
        return scanned, projected, counted

    def test_a_list_column_reads_back_every_row(self, tmp_path):
        scanned, projected, counted = self._scenario(tmp_path / "list.db", "FLOAT[]")
        expected = {f"f{i}" for i in range(self.ROWS) if i % 10 or i == 1}
        assert scanned == projected == expected
        assert counted == len(expected)

    @pytest.mark.xfail(reason="DuckDB 1.3.2 mis-reads a fixed FLOAT[n] column here; documents why the column is a list", strict=False)
    def test_a_fixed_array_column_does_not(self, tmp_path):
        scanned, projected, counted = self._scenario(tmp_path / "array.db", f"FLOAT[{self.VALUES}]")
        assert scanned == projected and counted == len(scanned)

    def test_the_schema_declares_lists(self):
        connection = duckdb.connect(":memory:")
        init_schema(connection)
        types = dict(connection.execute("SELECT table_name, data_type FROM information_schema.columns WHERE column_name = 'embedding'").fetchall())
        connection.close()
        assert types == {"artifact_embeddings": "FLOAT[]", "dependencies": "FLOAT[]", "code_references": "FLOAT[]"}

    def test_the_real_store_path_keeps_every_vector_across_sessions(self, tmp_path):
        path = tmp_path / "contracts.db"
        connection = get_connection(path)  # pattern-ok: duckdb-file the bug is about a FILE read again in a LATER session: closing and reopening the file is the objective
        init_schema(connection)
        items = []
        connection.execute("BEGIN")  # (one commit for the rows: the bug is about sessions and checkpoints, not about commits)
        for i in range(3):  # (the same 3 rows as above, with the 384 values the store requires)
            insert_artifact(
                connection, function_id=f"a.py::f{i}", file_path="a.py", function_name=f"f{i}", signature=f"def f{i}():",
                body=f"def f{i}(): pass", code_hash=f"h{i}", language="python", start_line=1, end_line=2,
                embedding=_vector(i + 1) if i % 10 else None,
            )
            items.append(Item("artifacts", (f"a.py::f{i}",), f"h{i}"))
        connection.execute("COMMIT")
        connection.close()
        connection = get_connection(path)  # pattern-ok: duckdb-file the bug is about a FILE read again in a LATER session: closing and reopening the file is the objective
        insert_artifact(  # a re-scan clearing one function's vector
            connection, function_id="a.py::f1", file_path="a.py", function_name="f1", signature="def f1():",
            body="def f1(): return 1", code_hash="h1b", language="python", start_line=1, end_line=2, embedding=None,
        )
        connection.close()
        connection = get_connection(path)  # pattern-ok: duckdb-file the bug is about a FILE read again in a LATER session: closing and reopening the file is the objective
        assert store_embeddings(connection, [(Item("artifacts", ("a.py::f1",), "h1b"), _vector(9.0))]) == 1
        connection.execute("CHECKPOINT")
        connection.close()
        connection = get_connection(path)  # pattern-ok: duckdb-file the bug is about a FILE read again in a LATER session: closing and reopening the file is the objective
        assert embedding_counts(connection) == {"embedded": 2, "total": 3}
        assert connection.execute("SELECT COUNT(*) FROM artifact_embeddings").fetchone()[0] == 2
        connection.close()


class TestVectorLength:
    @pytest.fixture
    def conn(self):
        connection = duckdb.connect(":memory:")  # (what is checked is what SQL does with a vector, not a file)
        init_schema(connection)
        insert_artifact(
            connection, function_id="a.py::f", file_path="a.py", function_name="f", signature="def f():",
            body="def f(): pass", code_hash="h", language="python", start_line=1, end_line=2,
        )
        yield connection
        connection.close()

    def test_a_vector_of_the_wrong_size_is_refused_on_store(self, conn):
        with pytest.raises(ValueError, match="384"):
            store_embeddings(conn, [(Item("artifacts", ("a.py::f",), "h"), [0.1] * 10)])
        assert conn.execute("SELECT COUNT(*) FROM artifact_embeddings").fetchone()[0] == 0

    def test_one_bad_vector_stores_nothing_from_the_batch(self, conn):
        insert_artifact(
            conn, function_id="a.py::g", file_path="a.py", function_name="g", signature="def g():",
            body="def g(): pass", code_hash="hg", language="python", start_line=3, end_line=4,
        )
        with pytest.raises(ValueError):
            store_embeddings(conn, [(Item("artifacts", ("a.py::g",), "hg"), _vector(1)), (Item("artifacts", ("a.py::f",), "h"), [0.1])])
        assert conn.execute("SELECT COUNT(*) FROM artifact_embeddings").fetchone()[0] == 0

    def test_a_query_vector_of_the_wrong_size_is_refused(self, conn):
        with pytest.raises(ValueError, match="384"):
            search_artifacts_semantic(conn, [0.1] * 5)

    def test_the_other_writers_check_too(self, conn):
        with pytest.raises(ValueError):
            insert_artifact(
                conn, function_id="a.py::h", file_path="a.py", function_name="h", signature="def h():",
                body="def h(): pass", code_hash="hh", language="python", start_line=5, end_line=6, embedding=[1.0],
            )
        with pytest.raises(ValueError):
            upsert_dependency_edge(conn, "a.py::f", "a.py::f", "f()", "e", [1.0], "EXTRACTED")

    def test_semantic_search_ranks_the_closest_vector_first(self, conn):
        insert_artifact(
            conn, function_id="a.py::g", file_path="a.py", function_name="g", signature="def g():",
            body="def g(): pass", code_hash="hg", language="python", start_line=3, end_line=4,
        )
        store_embeddings(conn, [(Item("artifacts", ("a.py::f",), "h"), [1.0] + [0.0] * (DIM - 1)), (Item("artifacts", ("a.py::g",), "hg"), [0.0, 1.0] + [0.0] * (DIM - 2))])
        hits = search_artifacts_semantic(conn, [1.0] + [0.0] * (DIM - 1))
        assert [hit.artifact.function_id for hit in hits] == ["a.py::f", "a.py::g"]
        assert hits[0].score == pytest.approx(1.0)


class _Dropping:
    """A connection that accepts a vector write and silently forgets it."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        statement = sql.lstrip().upper()
        if (statement.startswith("UPDATE") and "embedding" in sql) or statement.startswith("INSERT INTO ARTIFACT_EMBEDDINGS"):
            return self._conn.execute("SELECT 1")
        return self._conn.execute(sql, params) if params is not None else self._conn.execute(sql)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class TestReadBack:
    @pytest.fixture
    def conn(self):
        connection = duckdb.connect(":memory:")  # (what is checked is what SQL does with a vector, not a file)
        init_schema(connection)
        for name in ("f", "g"):
            insert_artifact(
                connection, function_id=f"a.py::{name}", file_path="a.py", function_name=name, signature=f"def {name}():",
                body=f"def {name}(): pass", code_hash=f"h{name}", language="python", start_line=1, end_line=2,
            )
        yield connection
        connection.close()

    def test_a_store_that_does_not_stick_is_reported(self, conn):
        with pytest.raises(VectorNotPersisted) as caught:
            store_embeddings(_Dropping(conn), [(Item("artifacts", ("a.py::f",), "hf"), _vector(1))])
        assert [item.keys for item in caught.value.items] == [("a.py::f",)]

    def test_a_good_store_is_not(self, conn):
        assert store_embeddings(conn, [(Item("artifacts", ("a.py::f",), "hf"), _vector(1))]) == 1

    @pytest.fixture
    def model(self, monkeypatch):
        monkeypatch.setattr(embedder, "load_model", lambda threads: None)
        monkeypatch.setattr(embedder, "embed_text", lambda text: _vector(0.5))
        monkeypatch.setattr(embedder, "model_is_cached", lambda: True)
        monkeypatch.setattr(embedding_runner.os, "nice", lambda n: 0)
        monkeypatch.setattr(embedding_runner, "pause_reason", lambda settings: None)

    class _Connection:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, *args, **kwargs):
            return self._conn.execute(*args, **kwargs)

        def store_embeddings(self, results):
            return store_embeddings(_Dropping(self._conn), results)

    def test_the_runner_stops_with_a_state_a_log_line_and_no_second_round(self, conn, model, capsys):
        from drspec.db.queries import add_usage_pending, fold_usage_pending

        add_usage_pending(conn, [("node", "a.py::f", "", 1.0)])
        fold_usage_pending(conn, 1000.0, 7.0)
        rounds = []
        code = embedding_runner.run_embedding_process(self._Connection(conn), sleep=rounds.append)
        assert code == embedding_runner.EXIT_NOT_PERSISTED
        state = json.loads(conn.execute("SELECT value FROM config WHERE key = 'worker:embedding_state'").fetchone()[0])
        assert state["state"] == "error" and state["detail"] == "vector did not persist"
        assert "vector did not persist" in capsys.readouterr().out

    def test_a_round_that_stores_but_leaves_everything_owed_stops_too(self, conn, model, monkeypatch):
        item = Item("artifacts", ("a.py::f",), "hf")
        calls = {"n": 0}

        def owed(*args, **kwargs):
            calls["n"] += 1
            return [item]

        monkeypatch.setattr(embedding_runner, "find_backlog", owed)
        monkeypatch.setattr(embedding_runner, "fetch_texts", lambda c, items: [(items[0], "x")])

        class Connection(self._Connection):
            def store_embeddings(self, results):
                return len(results)

        assert embedding_runner.run_embedding_process(Connection(conn), sleep=lambda s: None) == embedding_runner.EXIT_NOT_PERSISTED
        assert calls["n"] == 2  # the backlog at the start and once after the first round, not forever

    def test_a_normal_run_still_finishes_with_every_vector_stored(self, conn, model):
        from drspec.db.queries import add_usage_pending, fold_usage_pending

        add_usage_pending(conn, [("node", "a.py::f", "", 1.0), ("node", "a.py::g", "", 1.0)])
        fold_usage_pending(conn, 1000.0, 7.0)

        class Real(self._Connection):
            def store_embeddings(self, results):
                return store_embeddings(self._conn, results)

        assert embedding_runner.run_embedding_process(Real(conn), sleep=lambda s: None) == 0
        assert embedding_counts(conn)["embedded"] == 2

    def test_the_supervisor_backs_off_and_keeps_the_runners_message(self, conn, monkeypatch):
        class Process:
            code = None

            def poll(self):
                return self.code

        spawned = []
        supervisor = EmbeddingSupervisor(spawn=lambda db_path, threads=1: spawned.append(Process()) or spawned[-1])
        monkeypatch.setattr("drspec.embedding_supervisor.find_backlog", lambda *a, **k: [Item("artifacts", ("a.py::f",), "hf")])
        settings = read_all_settings(conn)
        supervisor.tick(conn, "unused.db", settings, 1000.0)
        embedding_runner.write_state(conn, "error", "vector did not persist")
        spawned[-1].code = embedding_runner.EXIT_NOT_PERSISTED
        supervisor.tick(conn, "unused.db", settings, 1010.0)
        supervisor.tick(conn, "unused.db", settings, 1020.0)
        assert len(spawned) == 1 and supervisor.retry_at > 1060.0
        state = json.loads(conn.execute("SELECT value FROM config WHERE key = 'worker:embedding_state'").fetchone()[0])
        assert state["detail"] == "vector did not persist"


# ---------------------------------------------------------------------------
# Rebuilding an old database
# ---------------------------------------------------------------------------


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tables(path: Path):
    connection = duckdb.connect(str(path), read_only=True)  # pattern-ok: duckdb-file the rebuilt (or left-alone) FILE and its backup are the objective
    try:
        return {name: connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for (name,) in connection.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
    finally:
        connection.close()


def make_old_database(path: Path) -> None:
    """A database laid out as before: fixed `FLOAT[384]` columns, a row in every table."""
    connection = duckdb.connect(str(path))  # pattern-ok: duckdb-file the rebuilt (or left-alone) FILE and its backup are the objective
    schema = SCHEMA.read_text().replace("embedding FLOAT[],", "embedding FLOAT[384],")
    # the layout of that time: the vector in the artifact row, and its own table not yet there
    schema = schema.replace("    status TEXT DEFAULT 'PENDING',", "    embedding FLOAT[384],\n    status TEXT DEFAULT 'PENDING',", 1)
    connection.execute(schema)
    connection.execute("DROP TABLE artifact_embeddings")
    connection.execute("BEGIN")  # (one commit for the whole fixture: it is built once, and a commit per row costs a disk flush each)
    for name in ("a", "b", "c", "d", "e", "f"):
        connection.execute(
            "INSERT INTO artifacts (function_id, file_path, function_name, signature, body, code_hash, language, start_line, end_line, embedding) "
            "VALUES (?, 'x.py', ?, ?, ?, ?, 'python', 1, 2, ?)",
            [f"x.py::{name}", name, f"def {name}():", f"def {name}(): pass", f"h{name}", _vector(1) if name in "abc" else None],
        )
    for name in "abc":
        set_config(connection, meta_key("artifacts", (f"x.py::{name}",)), meta_value(f"h{name}"))
    upsert_dependency_edge(connection, "x.py::a", "x.py::b", "b()", "e1", _vector(2), "EXTRACTED")
    upsert_dependency_edge(connection, "x.py::b", "x.py::c", "c()", "e2", None, "INFERRED")
    set_config(connection, meta_key("dependencies", ("x.py::a", "x.py::b")), meta_value("e1"))
    connection.execute("INSERT INTO code_references (caller_id, callee_id, reference_site, edge_hash, embedding, confidence) VALUES ('x.py::c', 'x.py::d', 'c.d', 'r1', NULL, 'EXTRACTED')")
    insert_contract(connection, "x.py::a", json.dumps({"intent": "adds"}), 0.8)
    insert_reasoning_trace(connection, "x.py::a", "proposer", "{}")
    insert_reasoning_trace(connection, "x.py::a", "critic", "{}")
    insert_vision_finding(connection, "x.py::a", "outlier", "HIGH", "odd")
    init_learning_schema(connection)
    insert_learning_event(connection, LearningEvent(commit_sha="abc123", function_id="x.py::a", pattern_description="p"))
    set_setting(connection, "worker.rescan_budget", 7)
    upsert_scanned_file(connection, "x.py", 1.0, 10)
    enqueue_staleness(connection, "x.py", "mtime_changed")
    connection.execute("COMMIT")
    connection.execute("CHECKPOINT")
    connection.close()


@pytest.fixture(scope="module")
def old_template(tmp_path_factory):
    """The old database, built ONCE for the module (about 1 s of DuckDB work: the whole schema, 12 inserts and a checkpoint); each test copies the file."""
    path = tmp_path_factory.mktemp("old_layout") / "contracts.db"
    make_old_database(path)
    before = file_hash(path)
    yield path
    assert file_hash(path) == before, "a test wrote into the file the module copies"


@pytest.fixture(scope="module")
def migrated_template(old_template, tmp_path_factory):
    """The old database after ONE migration, for the tests that check what the migration left and do not check the migration itself (they
    copy this file; the tests that check the migration run it on their own copy)."""
    path = tmp_path_factory.mktemp("migrated") / "contracts.db"
    shutil.copyfile(old_template, path)
    assert ensure_embedding_layout(path) == "migrated"
    return path


class TestRebuildingAnOldDatabase:
    @pytest.fixture
    def old(self, tmp_path, old_template):
        path = tmp_path / "_drspec" / "contracts.db"
        path.parent.mkdir()
        shutil.copyfile(old_template, path)
        return path

    @pytest.fixture
    def migrated(self, tmp_path, migrated_template):
        path = tmp_path / "_drspec" / "contracts.db"
        path.parent.mkdir()
        shutil.copyfile(migrated_template, path)
        return path

    def test_the_fixture_really_is_the_old_layout(self, old):
        connection = duckdb.connect(str(old), read_only=True)  # pattern-ok: duckdb-file the rebuilt (or left-alone) FILE and its backup are the objective
        try:
            assert needs_embedding_migration(connection)
            types = {row[0] for row in connection.execute("SELECT data_type FROM information_schema.columns WHERE column_name = 'embedding'").fetchall()}
        finally:
            connection.close()
        assert types == {"FLOAT[384]"}

    def test_every_table_keeps_its_rows_and_the_vectors_are_left_empty(self, old):
        before = _tables(old)
        original = _digest(old)
        assert ensure_embedding_layout(old) == "migrated"
        after = _tables(old)
        assert after == {**before, "config": before["config"] + 1, "artifact_embeddings": 0}  # the layout marker and the (empty) vector table are new
        connection = duckdb.connect(str(old), read_only=True)  # pattern-ok: duckdb-file the rebuilt (or left-alone) FILE and its backup are the objective
        try:
            assert not needs_embedding_migration(connection)
            types = {row[0] for row in connection.execute("SELECT data_type FROM information_schema.columns WHERE column_name = 'embedding'").fetchall()}
            assert types == {"FLOAT[]"}
            assert [connection.execute(f"SELECT COUNT(embedding) FROM {t}").fetchone()[0] for t in ("artifact_embeddings", "dependencies", "code_references")] == [0, 0, 0]
            assert connection.execute("SELECT value FROM config WHERE key = ?", [LAYOUT_KEY]).fetchone()[0] == "list"
        finally:
            connection.close()
        assert _digest(backup_path(old)) == original  # the backup is the old file as it was

    def test_contracts_settings_and_history_survive(self, migrated):
        connection = duckdb.connect(str(migrated))  # pattern-ok: duckdb-file the rebuilt (or left-alone) FILE and its backup are the objective
        try:
            assert json.loads(connection.execute("SELECT contract_json FROM contracts").fetchone()[0]) == {"intent": "adds"}
            assert read_all_settings(connection)["worker.rescan_budget"] == 7
            assert connection.execute("SELECT commit_sha FROM learning_history").fetchone()[0] == "abc123"
            assert connection.execute("SELECT COUNT(*) FROM reasoning_traces").fetchone()[0] == 2
            assert connection.execute("SELECT status FROM artifacts WHERE function_id = 'x.py::a'").fetchone()[0] == "PENDING"
        finally:
            connection.close()

    def test_sequences_continue_where_they_stopped(self, old, old_template):
        probe = duckdb.connect(str(old))  # pattern-ok: duckdb-file the rebuilt (or left-alone) FILE and its backup are the objective
        names = [row[0] for row in probe.execute("SELECT sequence_name FROM duckdb_sequences()").fetchall()]
        expected = {name: probe.execute(f"SELECT nextval('{name}')").fetchone()[0] for name in names}
        probe.close()
        # the probe above advanced the sequences; rebuild the fixture so the comparison starts level
        shutil.copyfile(old_template, old)
        ensure_embedding_layout(old)
        connection = duckdb.connect(str(old))  # pattern-ok: duckdb-file the rebuilt (or left-alone) FILE and its backup are the objective
        try:
            got = {name: connection.execute(f"SELECT nextval('{name}')").fetchone()[0] for name in names}
        finally:
            connection.close()
        assert got == expected

    def test_exactly_the_previously_embedded_items_are_owed_again(self, migrated):
        connection = duckdb.connect(str(migrated))  # pattern-ok: duckdb-file the rebuilt (or left-alone) FILE and its backup are the objective
        try:
            owed = {(item.table, item.keys) for item in find_backlog(connection, 10, 7.0, 1000.0)}
        finally:
            connection.close()
        assert owed == {
            ("artifacts", ("x.py::a",)), ("artifacts", ("x.py::b",)), ("artifacts", ("x.py::c",)),
            ("dependencies", ("x.py::a", "x.py::b")),
        }

    def test_a_new_vector_can_be_stored_afterwards(self, migrated):
        connection = duckdb.connect(str(migrated))  # pattern-ok: duckdb-file the rebuilt (or left-alone) FILE and its backup are the objective
        try:
            assert store_embeddings(connection, [(Item("artifacts", ("x.py::a",), "ha"), _vector(3))]) == 1
            assert embedding_counts(connection)["embedded"] == 1
        finally:
            connection.close()

    def test_the_backup_goes_at_the_next_successful_start_and_a_fresh_database_just_gets_the_marker(self, old, tmp_path):
        ensure_embedding_layout(old)
        assert backup_path(old).exists()
        kept = duckdb.connect(str(backup_path(old)), read_only=True)  # (the backup is the OLD database, not just a file: an empty one was not noticed)  # pattern-ok: duckdb-file the rebuilt (or left-alone) FILE and its backup are the objective
        try:
            assert needs_embedding_migration(kept) and kept.execute("SELECT COUNT(*) FROM artifacts").fetchone() == (6,)
        finally:
            kept.close()
        assert ensure_embedding_layout(old) == "current"
        assert not backup_path(old).exists()

        fresh = tmp_path / "fresh" / "contracts.db"
        fresh.parent.mkdir()
        connection = duckdb.connect(str(fresh))  # pattern-ok: duckdb-file the rebuilt (or left-alone) FILE and its backup are the objective
        init_schema(connection)
        connection.close()
        assert ensure_embedding_layout(fresh) == "current"
        connection = duckdb.connect(str(fresh), read_only=True)  # pattern-ok: duckdb-file the rebuilt (or left-alone) FILE and its backup are the objective
        assert connection.execute("SELECT value FROM config WHERE key = ?", [LAYOUT_KEY]).fetchone()[0] == "list"
        connection.close()
        assert ensure_embedding_layout(tmp_path / "nothing.db") == "absent"

    def test_a_failing_rebuild_leaves_the_old_database_byte_for_byte_unchanged(self, old, monkeypatch):
        original = _digest(old)

        def fail(source, target):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr("drspec.db.migration._build", fail)
        with pytest.raises(MigrationError, match="not changed"):
            ensure_embedding_layout(old)
        assert _digest(old) == original
        assert not backup_path(old).exists()
        assert [p.name for p in old.parent.iterdir()] == ["contracts.db"]  # no staging folder left behind

    def test_a_row_count_mismatch_aborts_the_rebuild(self, old, monkeypatch):
        original = _digest(old)
        real = duckdb.DuckDBPyConnection.execute

        class Lying:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def execute(self, sql, *args):
                result = self._inner.execute(sql, *args)
                if sql.startswith("INSERT INTO main.contracts"):
                    self._inner.execute("DELETE FROM main.contracts")
                return result

        real_connect = duckdb.connect
        monkeypatch.setattr("drspec.db.migration.duckdb.connect", lambda *a, **k: Lying(real_connect(*a, **k)) if a and str(a[0]).endswith("contracts.db") and not k else real_connect(*a, **k))
        del real
        with pytest.raises(MigrationError, match="rows after the copy"):
            migrate_embedding_layout(old)
        assert _digest(old) == original

    def test_the_worker_refuses_to_start_on_a_failed_rebuild_and_says_why(self, old, monkeypatch):
        from drspec.staleness_worker import get_worker_status, read_startup_error, run_worker

        monkeypatch.chdir(old.parent.parent)  # run_worker moves the process to the project folder and does not move it back: restored after this test
        monkeypatch.setattr("drspec.db.migration._build", lambda s, t: (_ for _ in ()).throw(RuntimeError("no space")))
        with pytest.raises(MigrationError):
            run_worker(old)
        assert "no space" in read_startup_error(old)
        assert "no space" in get_worker_status(old)["startup_error"]
        assert not (old.parent / "worker.sock").exists() and not (old.parent / "staleness_worker.pid").exists()

    def test_the_worker_command_prints_the_reason_and_exits_nonzero(self, old, monkeypatch):
        monkeypatch.chdir(old.parent.parent)  # the worker moves the process to the project folder and does not move it back: restored after this test
        monkeypatch.setattr("drspec.db.migration._build", lambda s, t: (_ for _ in ()).throw(RuntimeError("no space")))
        result = runner.invoke(app, ["--db", str(old), "worker", "run"])
        assert result.exit_code == 1 and "did not start" in result.output and "no space" in result.output

    def test_a_successful_start_clears_an_old_failure_note(self, tmp_path, monkeypatch):
        from drspec.staleness_worker import read_startup_error, record_startup_error, run_worker

        # (a database already in the current layout: the migration step passes without a rebuild, which is all this test needs; the rebuild is the
        # objective of the tests around it, and it costs seconds of disk flushes)
        path = tmp_path / "_drspec" / "contracts.db"
        path.parent.mkdir()
        connection = duckdb.connect(str(path))  # pattern-ok: duckdb-file run_worker opens the file given to it
        init_schema(connection)
        connection.close()
        record_startup_error(path, "earlier failure")
        monkeypatch.chdir(tmp_path)  # run_worker moves the process to the project folder and does not move it back: pytest restores the folder after this test
        monkeypatch.setattr("drspec.staleness_worker.WorkerIPCServer.start", lambda self: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            run_worker(path)
        assert read_startup_error(path) is None  # the migration step passed before the (stubbed) server start

    def test_init_on_an_existing_project_rebuilds_it(self, old, monkeypatch):
        monkeypatch.chdir(old.parent.parent)
        result = runner.invoke(app, ["--json", "init", "--non-interactive"])  # pattern-ok: init-for-setup `init` on an old project IS the objective (it must rebuild or report the failed rebuild)
        assert result.exit_code == 0, result.output
        connection = duckdb.connect(str(old), read_only=True)  # pattern-ok: duckdb-file the rebuilt (or left-alone) FILE and its backup are the objective
        try:
            assert not needs_embedding_migration(connection)
        finally:
            connection.close()

    def test_init_reports_a_failed_rebuild(self, old, monkeypatch):
        monkeypatch.chdir(old.parent.parent)
        monkeypatch.setattr("drspec.db.migration._build", lambda s, t: (_ for _ in ()).throw(RuntimeError("no space")))
        result = runner.invoke(app, ["--json", "init", "--non-interactive"])  # pattern-ok: init-for-setup `init` on an old project IS the objective (it must rebuild or report the failed rebuild)
        assert result.exit_code == 1 and json.loads(result.output)["error"]["code"] == "MIGRATION_FAILED"
