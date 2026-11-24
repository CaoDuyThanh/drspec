"""Foreign keys kept, nothing detached: a function's contract is never out of the database to change it."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

from drspec.core.embedding_backlog import Item, store_embeddings
from drspec.db import get_connection, init_schema, insert_artifact
from drspec.db.fk_migration import (
    BACKUP_SUFFIX,
    MARKER_KEY,
    backup_path,
    ensure_fk_layout,
    needs_fk_migration,
)
from drspec.db.migration import MigrationError
from drspec.db.queries import (
    complete_pending_removals,
    insert_contract,
    insert_reasoning_trace,
    insert_vision_finding,
    pending_removal_count,
    schedule_removal,
    set_artifact_embedding,
    update_artifact_status,
    upsert_dependency_edge,
)
from tests.helpers import file_hash

F, G = "a.py::f#11111111", "g.py::g#22222222"


def _artifact(conn, function_id=F, file_path="a.py", body="def f(): pass", code_hash="h1", name="f"):
    insert_artifact(
        conn, function_id=function_id, file_path=file_path, function_name=name, signature=f"def {name}():", body=body,
        code_hash=code_hash, language="python", start_line=1, end_line=2,
    )


def _populate(conn):
    _artifact(conn)
    _artifact(conn, G, "g.py", name="g")
    upsert_dependency_edge(conn, G, F, "f()", "e1", [0.5] * 384, "EXTRACTED")
    conn.execute("INSERT INTO code_references (caller_id, callee_id, reference_site, edge_hash, embedding, confidence) VALUES (?, ?, 'g.f', 'r1', NULL, 'EXTRACTED')", [G, F])
    insert_contract(conn, F, json.dumps({"intent": "x"}), 0.8, "assert True")
    insert_reasoning_trace(conn, F, "proposer", "{}")
    insert_vision_finding(conn, F, "outlier", "HIGH", "odd")
    set_artifact_embedding(conn, F, [0.25] * 384)


DEPENDENTS = ("dependencies", "code_references", "contracts", "reasoning_traces", "vision_findings", "artifact_embeddings")


def _counts(conn):
    return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in DEPENDENTS}


@pytest.fixture(scope="module")
def populated_template(tmp_path_factory):
    """The database with a row in every table, as a FILE, built ONCE for the module: only the kill tests need a file (a process dies with it open)."""
    path = tmp_path_factory.mktemp("populated") / "p.db"
    connection = get_connection(path)  # pattern-ok: duckdb-file the kill tests need a FILE (a process dies with it open)
    init_schema(connection)
    _populate(connection)
    connection.execute("CHECKPOINT")
    connection.close()
    before = file_hash(path)
    yield path
    assert file_hash(path) == before, "a test wrote into the file the module copies"


@pytest.fixture
def conn():
    """A database with a row in every table, in memory (30 ms to build): the tests that use it check what SQL does, not a file."""
    connection = duckdb.connect(":memory:")
    init_schema(connection)
    _populate(connection)
    yield connection
    connection.close()


class CountingConnection:
    """Wraps a connection to count its commits and record every statement."""

    def __init__(self, inner):
        self.inner = inner
        self.commits = 0
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append(sql)
        return self.inner.execute(sql, params) if params is not None else self.inner.execute(sql)

    def commit(self):
        self.commits += 1
        return self.inner.commit()

    def __getattr__(self, name):
        return getattr(self.inner, name)


class TestTheSchema:
    def test_artifacts_has_no_secondary_index_and_no_vector_column(self, conn):
        indexes = {row[0] for row in conn.execute("SELECT index_name FROM duckdb_indexes() WHERE table_name = 'artifacts'").fetchall()}
        assert indexes == set()
        columns = {row[0] for row in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'artifacts'").fetchall()}
        assert "embedding" not in columns

    def test_every_foreign_key_is_still_there_and_enforced(self, conn):
        with pytest.raises(duckdb.ConstraintException):
            insert_contract(conn, "missing.py::nope#00000000", "{}", 0.5)
        with pytest.raises(duckdb.ConstraintException):
            insert_reasoning_trace(conn, "missing.py::nope#00000000", "a", "{}")
        with pytest.raises(duckdb.ConstraintException):
            insert_vision_finding(conn, "missing.py::nope#00000000", "outlier", "HIGH", "x")
        with pytest.raises(duckdb.ConstraintException):
            upsert_dependency_edge(conn, F, "missing.py::nope#00000000", "x()", "e", None, "EXTRACTED")
        with pytest.raises(duckdb.ConstraintException):  # (the caller end too: a break that dropped only this key was not noticed)
            upsert_dependency_edge(conn, "missing.py::nope#00000000", F, "x()", "e", None, "EXTRACTED")
        for caller, callee in (("missing.py::nope#00000000", F), (F, "missing.py::nope#00000000")):
            with pytest.raises(duckdb.ConstraintException):
                conn.execute("INSERT INTO code_references (caller_id, callee_id, reference_site, edge_hash, embedding, confidence) VALUES (?, ?, 'x', 'r9', NULL, 'EXTRACTED')", [caller, callee])
        with pytest.raises(duckdb.ConstraintException):
            set_artifact_embedding(conn, "missing.py::nope#00000000", [0.1] * 384)
        with pytest.raises(duckdb.ConstraintException):
            conn.execute("DELETE FROM artifacts WHERE function_id = ?", [F])  # it still has dependents


class TestPlainStatements:
    def test_a_hundred_status_changes_and_vector_stores_keep_everything_with_no_commit_between(self, conn):
        counting = CountingConnection(conn)
        before = _counts(conn)
        for i in range(100):
            update_artifact_status(counting, F, "VERIFIED" if i % 2 else "STALE")
            set_artifact_embedding(counting, F, [0.01 * (i % 7)] * 384)
        assert _counts(conn) == before
        assert counting.commits == 0
        assert not any("_detached_" in sql or "CREATE TEMP" in sql for sql in counting.statements)

    def test_a_changed_body_a_moved_file_and_a_new_name_keep_the_dependents(self, conn):
        before = _counts(conn)
        insert_artifact(
            conn, function_id=F, file_path="moved.py", function_name="renamed", signature="def renamed():", body="def f(): return 2",
            code_hash="h2", language="python", start_line=3, end_line=4,
        )
        assert _counts(conn) == {**before, "artifact_embeddings": 0}  # the vector of the old text is gone, nothing else
        assert conn.execute("SELECT file_path, function_name FROM artifacts WHERE function_id = ?", [F]).fetchone() == ("moved.py", "renamed")

    def test_stored_vectors_are_read_back_for_search_and_counts(self, conn):
        from drspec.db.queries import (
            any_artifact_has_embedding,
            search_artifacts_semantic,
        )

        assert any_artifact_has_embedding(conn)
        found = search_artifacts_semantic(conn, [0.25] * 384)
        assert [match.artifact.function_id for match in found] == [F]
        assert found[0].score == pytest.approx(1.0, abs=1e-5)


FUNCTION = "a.py::f#11111111"
OPERATIONS = {  # (what a worker does to one function, and how many statements it takes: `status` and `vector` 2, `store` 5)
    "status": (lambda conn: update_artifact_status(conn, FUNCTION, "VERIFIED"), 2),
    "vector": (lambda conn: set_artifact_embedding(conn, FUNCTION, [0.5] * 384), 2),
    "store": (lambda conn: store_embeddings(conn, [(Item("artifacts", (FUNCTION,), "h1"), [0.5] * 384)]), 5),
}
CASES = [(operation, step) for operation, (_, statements) in OPERATIONS.items() for step in range(statements + 1)]  # (a death before each statement, and one run to the end)


class _Stopped(Exception):
    """The process 'died' here."""


def _snapshot(db: Path, to: Path) -> None:
    """The files a process leaves when it dies: the database and its write-ahead log, as the operating system has them (a copy of the
    bytes written so far, which is what survives `os._exit`: nothing is flushed from the process's memory)."""
    shutil.copyfile(db, to)
    wal = Path(str(db) + ".wal")
    if wal.exists():
        shutil.copyfile(wal, Path(str(to) + ".wal"))


def _state(path: Path) -> dict:
    connection = duckdb.connect(str(path), read_only=True)  # pattern-ok: duckdb-file reads what a death left
    try:
        state = _counts(connection)
        state["status"] = connection.execute("SELECT status FROM artifacts WHERE function_id = ?", [FUNCTION]).fetchone()[0]
        row = connection.execute("SELECT list_sum(embedding) FROM artifact_embeddings WHERE function_id = ?", [FUNCTION]).fetchone()
        state["vector sum"] = row[0] if row else None  # (None: the vector was deleted and not yet written back)
        return state
    finally:
        connection.close()


def _die_at(template: Path, tmp_path: Path, operation: str, step: int) -> "tuple[Path, bool]":
    """Run the operation on a copy of the database and 'die' before statement number `step` (counting executes and commits, from 0):
    the files are copied there and the operation is abandoned. Returns the copy and whether it died (`step` was inside the operation)."""
    db = tmp_path / "run.db"
    shutil.copyfile(template, db)
    left = tmp_path / "left.db"
    connection = get_connection(db)  # pattern-ok: duckdb-file the files of a database in use are what is copied
    calls = {"n": 0}

    class Dying:
        def __getattr__(self, name):
            return getattr(connection, name)

        def _call(self):
            calls["n"] += 1
            if calls["n"] > step:
                _snapshot(db, left)
                raise _Stopped()

        def execute(self, sql, params=None):
            self._call()
            return connection.execute(sql, params) if params is not None else connection.execute(sql)

        def commit(self):
            self._call()
            return connection.commit()

    died = False
    try:
        OPERATIONS[operation][0](Dying())
        _snapshot(db, left)  # (no death: the state after the end, the control)
    except _Stopped:
        died = True
    finally:
        connection.close()
    return left, died


class TestKillingTheProcess:
    """What a process leaves behind when it dies between two statements is the database file and its write-ahead log. The matrix makes that
    state IN-PROCESS (a copy of both files at the chosen statement) and opens it; ONE real `os._exit` case proves the copy is the same state."""

    @pytest.mark.parametrize("operation, step", CASES)
    def test_a_death_before_between_or_after_every_statement_loses_no_dependent(self, tmp_path, populated_template, operation, step):
        before = _state(populated_template)
        left, died = _die_at(populated_template, tmp_path, operation, step)
        assert died == (step < OPERATIONS[operation][1])  # (it stopped where it was asked to, or ran to the end)
        after = _state(left)
        for table in ("dependencies", "code_references", "contracts", "reasoning_traces", "vision_findings"):
            assert after[table] == before[table], (operation, step, table)

    @pytest.mark.integration  # the one real process: it proves the in-process copy is what a death leaves
    def test_the_copy_is_the_state_a_real_death_leaves(self, tmp_path, populated_template):
        """The one real process: killed with `os._exit` before the 3rd statement of `store`; its files are the same as the copy made at that point."""
        real = tmp_path / "real.db"
        shutil.copyfile(populated_template, real)
        src = Path(__file__).resolve().parents[2] / "src"
        env = {**os.environ, "PYTHONPATH": str(src)}
        done = subprocess.run([sys.executable, "-c", self.SCRIPT, str(real), "store", "2"], env=env, timeout=60, capture_output=True, text=True)  # pattern-ok: real-subprocess the one real death that shows the in-process copy is the same state
        assert done.returncode == 7, done.stderr[-500:]  # (killed where it was asked to be)
        copy, died = _die_at(populated_template, tmp_path, "store", 2)
        assert died and _state(copy) == _state(real) != _state(populated_template)  # (and the death changed something: the two are not the untouched file)

    SCRIPT = (
        "import sys, os\n"
        "from drspec.db import get_connection\n"
        "from drspec.db.queries import update_artifact_status, set_artifact_embedding\n"
        "from drspec.core.embedding_backlog import Item, store_embeddings\n"
        "conn = get_connection(sys.argv[1])\n"  # pattern-ok: duckdb-file inside the killed process's script: a FILE is the objective
        "step = int(sys.argv[3])\n"
        "calls = {'n': 0}\n"
        "class Hook:\n"
        "    def __init__(self, inner): self.inner = inner\n"
        "    def __getattr__(self, name): return getattr(self.inner, name)\n"
        "    def execute(self, sql, params=None):\n"
        "        calls['n'] += 1\n"
        "        if calls['n'] > step: os._exit(7)\n"
        "        return self.inner.execute(sql, params) if params is not None else self.inner.execute(sql)\n"
        "    def commit(self):\n"
        "        calls['n'] += 1\n"
        "        if calls['n'] > step: os._exit(7)\n"
        "        return self.inner.commit()\n"
        "hook = Hook(conn)\n"
        "if sys.argv[2] == 'status': update_artifact_status(hook, 'a.py::f#11111111', 'VERIFIED')\n"
        "elif sys.argv[2] == 'vector': set_artifact_embedding(hook, 'a.py::f#11111111', [0.5] * 384)\n"
        "else: store_embeddings(hook, [(Item('artifacts', ('a.py::f#11111111',), 'h1'), [0.5] * 384)])\n"
        "conn.close()\n"
    )



class TestRemovals:
    def test_a_recorded_function_is_deleted_with_all_that_refers_to_it_and_the_rest_is_untouched(self, conn):
        _artifact(conn, "h.py::h#33333333", "h.py", name="h")
        insert_contract(conn, "h.py::h#33333333", "{}", 0.5)
        schedule_removal(conn, [F])
        assert pending_removal_count(conn) == 1
        assert complete_pending_removals(conn) == 1
        assert conn.execute("SELECT function_id FROM artifacts ORDER BY 1").fetchall() == [("g.py::g#22222222",), ("h.py::h#33333333",)]
        counts = _counts(conn)
        assert counts["dependencies"] == 0 and counts["code_references"] == 0 and counts["reasoning_traces"] == 0 and counts["artifact_embeddings"] == 0
        assert counts["contracts"] == 1  # the other function's
        assert pending_removal_count(conn) == 0

    def test_a_crash_between_the_two_steps_is_finished_by_the_next_call(self, conn, monkeypatch):
        schedule_removal(conn, [F])
        real = conn.commit
        state = {"n": 0}

        class Crash(Exception):
            pass

        def commit_then_crash():
            real()  # the first commit is the one that ends the dependents' transaction
            state["n"] += 1
            if state["n"] == 1:
                raise Crash()

        class Proxy:
            def __getattr__(self, name):
                return getattr(conn, name)

            def commit(self):
                commit_then_crash()

            def rollback(self):
                conn.rollback()

        with pytest.raises(Crash):
            complete_pending_removals(Proxy())
        # the function is still there, its dependents are gone, and it is still recorded
        assert conn.execute("SELECT COUNT(*) FROM artifacts WHERE function_id = ?", [F]).fetchone()[0] == 1
        assert _counts(conn)["contracts"] == 0 and pending_removal_count(conn) == 1
        assert complete_pending_removals(conn) == 1
        assert conn.execute("SELECT COUNT(*) FROM artifacts WHERE function_id = ?", [F]).fetchone()[0] == 0

    def test_completing_twice_is_harmless(self, conn):
        schedule_removal(conn, [F])
        complete_pending_removals(conn)
        assert complete_pending_removals(conn) == 0

    def test_status_counts_what_is_pending(self, tmp_path):
        from drspec.core.backlog import maintenance_backlog

        connection = duckdb.connect(":memory:")
        init_schema(connection)
        _populate(connection)
        schedule_removal(connection, [F])
        assert maintenance_backlog(connection).get("removals_pending") == 1
        connection.close()


def _old_layout(path: Path) -> None:
    """A database as it was: the three indexes and the vector in the artifact row."""
    connection = duckdb.connect(str(path))  # pattern-ok: duckdb-file builds the OLD-layout FILE the migration is given
    schema = (Path(__file__).resolve().parents[2] / "src" / "drspec" / "db" / "schema.sql").read_text()
    schema = schema.replace(
        "    status TEXT DEFAULT 'PENDING',", "    embedding FLOAT[],\n    status TEXT DEFAULT 'PENDING',", 1
    ) + (
        "\nCREATE INDEX idx_artifacts_status ON artifacts(status);\nCREATE INDEX idx_artifacts_file_path ON artifacts(file_path);\n"
        "CREATE INDEX idx_artifacts_function_name ON artifacts(function_name);\n"
    )
    connection.execute(schema)
    connection.execute("DROP TABLE artifact_embeddings")
    for function_id, name, vector in ((F, "f", [0.25] * 384), (G, "g", None)):
        connection.execute(
            "INSERT INTO artifacts (function_id, file_path, function_name, signature, body, code_hash, language, start_line, end_line, embedding) "
            "VALUES (?, ?, ?, 'def x():', 'def x(): pass', 'h', 'python', 1, 2, ?)",
            [function_id, function_id.split("::")[0], name, vector],
        )
    connection.execute("INSERT INTO dependencies (caller_id, callee_id, call_site, edge_hash, confidence) VALUES (?, ?, 'f()', 'e', 'EXTRACTED')", [G, F])
    connection.execute("INSERT INTO contracts (function_id, contract_json, confidence_score) VALUES (?, '{}', 0.8)", [F])
    connection.execute("CHECKPOINT")
    connection.close()


class TestMigration:
    @pytest.fixture(scope="module")
    def old_template(self, tmp_path_factory):
        path = tmp_path_factory.mktemp("old_layout") / "contracts.db"
        _old_layout(path)
        before = file_hash(path)
        yield path
        assert file_hash(path) == before, "a test wrote into the file the module copies"

    @pytest.fixture
    def old(self, tmp_path, old_template):
        path = tmp_path / "_drspec" / "contracts.db"
        path.parent.mkdir()
        shutil.copyfile(old_template, path)
        return path

    def test_the_fixture_has_the_old_layout(self, old):
        connection = duckdb.connect(str(old), read_only=True)  # pattern-ok: duckdb-file the migrated file is the objective
        try:
            assert needs_fk_migration(connection)
        finally:
            connection.close()

    def test_the_rebuild_keeps_every_row_moves_the_vectors_drops_the_indexes_and_keeps_the_keys(self, old):
        assert ensure_fk_layout(old) == "migrated"
        connection = duckdb.connect(str(old), read_only=True)  # pattern-ok: duckdb-file the migrated file is the objective
        try:
            assert connection.execute("SELECT function_id FROM artifacts ORDER BY 1").fetchall() == [(F,), (G,)]
            assert connection.execute("SELECT function_id, len(embedding) FROM artifact_embeddings").fetchall() == [(F, 384)]
            assert connection.execute("SELECT embedding[1] FROM artifact_embeddings").fetchone()[0] == pytest.approx(0.25)
            assert connection.execute("SELECT COUNT(*) FROM duckdb_indexes() WHERE table_name = 'artifacts'").fetchone() == (0,)
            assert connection.execute("SELECT COUNT(*) FROM duckdb_constraints() WHERE constraint_type = 'FOREIGN KEY'").fetchone()[0] >= 5
            assert connection.execute("SELECT value FROM config WHERE key = ?", [MARKER_KEY]).fetchone() == ("v1",)
            assert not needs_fk_migration(connection)
            assert connection.execute("SELECT COUNT(*) FROM contracts").fetchone() == (1,)
            assert connection.execute("SELECT caller_id, callee_id FROM dependencies").fetchall() == [(G, F)]
        finally:
            connection.close()

    def test_the_backup_is_the_original_until_the_next_start(self, old):
        original = old.read_bytes()
        ensure_fk_layout(old)
        assert backup_path(old).read_bytes() == original and str(backup_path(old)).endswith(BACKUP_SUFFIX)
        assert ensure_fk_layout(old) == "current"
        assert not backup_path(old).exists()

    def test_an_interrupted_rebuild_leaves_the_old_file_byte_identical(self, old, monkeypatch):
        original = old.read_bytes()

        def fail(*_a, **_k):
            raise OSError("disk gone")

        monkeypatch.setattr("drspec.db.migration.os.replace", fail)
        with pytest.raises(MigrationError, match="not changed"):
            ensure_fk_layout(old)
        assert old.read_bytes() == original

    def test_a_current_database_is_only_marked(self, tmp_path):
        path = tmp_path / "_drspec" / "contracts.db"
        path.parent.mkdir()
        connection = get_connection(path)  # pattern-ok: duckdb-file a current FILE is what the migration must leave alone
        init_schema(connection)
        connection.close()
        assert ensure_fk_layout(path) == "current"
        assert not backup_path(path).exists()

    def test_all_three_rebuilds_chain_on_a_database_from_before(self, old):
        from drspec.db.identity_migration import ensure_identity_layout
        from drspec.db.migration import ensure_embedding_layout

        ensure_embedding_layout(old)
        ensure_fk_layout(old)
        ensure_identity_layout(old)
        connection = duckdb.connect(str(old), read_only=True)  # pattern-ok: duckdb-file the migrated file is the objective
        try:
            ids = [row[0] for row in connection.execute("SELECT function_id FROM artifacts ORDER BY 1").fetchall()]
            assert all("#" in function_id for function_id in ids) and len(ids) == 2
            assert connection.execute("SELECT COUNT(*) FROM artifact_embeddings").fetchone() == (1,)
            assert connection.execute("SELECT COUNT(*) FROM artifact_embeddings WHERE function_id IN (SELECT function_id FROM artifacts)").fetchone() == (1,)
        finally:
            connection.close()
