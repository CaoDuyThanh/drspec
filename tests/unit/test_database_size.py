"""The size of the database file: how much of it is free space, giving that back, and saying so in `status`; and the
Python block of `status` (calls, how many were not tied to a function, and why)."""

from __future__ import annotations

import json
import os
import shutil

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.core import storage_report
from drspec.db import get_connection, init_schema
from drspec.db.compaction import (
    FREE_SHARE,
    backup_path,
    ensure_compact,
    needs_compaction,
    storage_of,
)

runner = CliRunner()


TAIL_ROWS = 300  # rows kept after the junk
_state: dict = {}  # the factory of this module's folders, and the churned template once it has been built


@pytest.fixture(scope="module", autouse=True)
def _folders(tmp_path_factory):
    _state.clear()
    _state["factory"] = tmp_path_factory
    yield
    _state.clear()


def _build_churned(path, rows: int) -> None:
    """A database whose file holds a lot of free blocks: rows written (text that does not compress), a checkpoint, then removed."""
    connection = get_connection(path)  # pattern-ok: duckdb-file the objective is the real file (its free blocks, its rebuild)
    init_schema(connection)
    pieces = " || ".join(f"md5(i::VARCHAR || '{letter}')" for letter in "abcdefgh")
    connection.execute(f"INSERT INTO config (key, value) SELECT 'junk:' || i, {pieces} FROM range({rows}) t(i)")
    connection.execute("CHECKPOINT")
    # (something written after the junk, so that the blocks freed are in the middle of the file: the end of a file is cut)
    connection.execute("INSERT INTO config (key, value) VALUES ('keep', 'kept')")
    connection.execute(f"INSERT INTO config (key, value) SELECT 'tail:' || i, {pieces} FROM range({TAIL_ROWS}) t(i)")
    connection.execute("CHECKPOINT")
    connection.execute("DELETE FROM config WHERE starts_with(key, 'junk:')")
    connection.execute("CHECKPOINT")
    connection.close()


def _churned(path, rows: int = 4500) -> None:
    """`path` is a copy of a churned database (see `_build_churned`). 4,500 junk rows is the SMALLEST churn for which every assertion of this
    file holds (measured: 1,500 / 2,500 / 3,500 rows fail the free-block floor or the shrink ratio of the concurrent-rebuild test; 4,000 failed it once the
    schema had one more table (`member_references`: every table costs a block of 0.26 MB in both files, ratio 0.73 against 0.7), 4,500 and
    above pass, six runs in a row; the schema alone is about 2.1 MB, so the junk must be a large part of the file). The churn (three checkpoints of a file, each waiting for the disk) is done
    once per module and the finished file is copied for each test: a test that compacts it changes its own copy."""
    template = _state.get(("churned", rows))
    if template is None:
        template = _state["factory"].mktemp("churned") / "template.db"
        _build_churned(template, rows)
        _state[("churned", rows)] = template
    shutil.copyfile(template, path)


class TestFreeSpace:
    def test_it_is_measured_from_the_file(self, tmp_path):
        _churned(tmp_path / "c.db")
        connection = duckdb.connect(str(tmp_path / "c.db"))  # pattern-ok: duckdb-file the objective is the real file (its free blocks, its rebuild)
        info = storage_of(connection)
        connection.close()
        assert info["free_share"] > FREE_SHARE and info["free_mb"] > 0 and info["file_mb"] > info["used_mb"]
        assert 0 < info["free_percent"] <= 100

    def test_a_small_file_is_not_worth_a_rebuild(self, tmp_path):
        _churned(tmp_path / "c.db")
        connection = duckdb.connect(str(tmp_path / "c.db"))  # pattern-ok: duckdb-file the objective is the real file (its free blocks, its rebuild)
        assert needs_compaction(connection) is False  # below the size at which it matters
        assert needs_compaction(connection, min_bytes=0) is True
        connection.close()


class TestCompacting:
    def test_the_file_shrinks_and_every_row_stays(self, tmp_path):
        db = tmp_path / "c.db"
        _churned(db)
        before = os.path.getsize(db)
        measured = duckdb.connect(str(db))  # pattern-ok: duckdb-file the objective is the real file (its free blocks, its rebuild)
        free_before = storage_of(measured)["free_mb"]
        measured.close()
        assert ensure_compact(db, min_bytes=0) == "compacted"
        assert os.path.getsize(db) < before * 0.7  # the junk is a good part of the file; the schema alone is about 1.8 MB
        connection = duckdb.connect(str(db))  # pattern-ok: duckdb-file the objective is the real file (its free blocks, its rebuild)
        try:
            assert connection.execute("SELECT value FROM config WHERE key = 'keep'").fetchone() == ("kept",)
            assert connection.execute("SELECT COUNT(*) FROM config WHERE starts_with(key, 'tail:')").fetchone()[0] == TAIL_ROWS
            assert storage_of(connection)["free_mb"] < free_before / 2
        finally:
            connection.close()
        assert not backup_path(db).exists()

    def test_it_can_be_used_afterwards(self, tmp_path):
        db = tmp_path / "c.db"
        _churned(db)
        ensure_compact(db, min_bytes=0)
        connection = get_connection(db)  # pattern-ok: duckdb-file the objective is the real file (its free blocks, its rebuild)
        before = connection.execute("SELECT COUNT(*) FROM config").fetchone()[0]
        connection.execute("INSERT INTO config (key, value) VALUES ('after', 'ok')")
        assert connection.execute("SELECT COUNT(*) FROM config").fetchone()[0] == before + 1
        connection.close()

    def test_a_compact_file_is_left_alone(self, tmp_path):
        db = tmp_path / "c.db"
        connection = get_connection(db)  # pattern-ok: duckdb-file the objective is the real file (its free blocks, its rebuild)
        init_schema(connection)
        connection.close()
        stamp = os.stat(db).st_mtime_ns
        assert ensure_compact(db, min_bytes=0) == "compact"
        assert os.stat(db).st_mtime_ns == stamp

    def test_no_file_is_nothing_to_do(self, tmp_path):
        assert ensure_compact(tmp_path / "nowhere.db") == "absent"


class TestTheReportInStatus:
    def test_sizes_and_the_largest_parts(self, tmp_path):
        connection = duckdb.connect(":memory:")  # the report names the parts of the data; the bytes of a file are tested with a real one below
        init_schema(connection)
        connection.execute("INSERT INTO config (key, value) VALUES ('scan:file:a.py', ?)", ["x" * 5000])
        connection.execute("INSERT INTO config (key, value) VALUES ('other', 'y')")
        report = storage_report.database_report(connection)
        assert {"file_mb", "used_mb", "free_mb", "free_percent", "largest"} <= set(report)
        names = [part["name"] for part in report["largest"]]
        assert names[0] == "per-file facts" and "other settings and state" in names
        connection.close()

    def test_it_says_when_much_of_the_file_is_free(self, tmp_path, monkeypatch):
        _churned(tmp_path / "c.db")
        monkeypatch.setattr(storage_report, "MIN_BYTES", 0)
        connection = duckdb.connect(str(tmp_path / "c.db"))  # pattern-ok: duckdb-file the objective is the real file (its free blocks, its rebuild)
        report = storage_report.database_report(connection)
        connection.close()
        assert "free space" in report["note"] and "idle" in report["note"]

    def test_status_carries_it(self, kept_project):
        (kept_project / "a.py").write_text("def f():\n    return 1\n")
        assert runner.invoke(app, ["--json", "scan"]).exit_code == 0
        data = json.loads(runner.invoke(app, ["--json", "status"]).output)["data"]
        assert "file_mb" in data["database"] and data["database"]["largest"]  # (the size on disk is tested on a real file above)

    def test_status_says_how_many_parsers_the_backlog_drains_on(self, kept_project):
        import time

        from drspec.core.parse_pool import PARSERS_KEY
        from drspec.worker_ipc import get_worker_connection

        (kept_project / "a.py").write_text("def f():\n    return 1\n")
        assert runner.invoke(app, ["--json", "scan"]).exit_code == 0
        assert "parsers" not in json.loads(runner.invoke(app, ["--json", "status"]).output)["data"]  # nothing parsed in a pool yet
        worker = get_worker_connection(kept_project / "_drspec" / "contracts.db")
        try:
            record = json.dumps({"count": 1, "held_down_by": "machine busy", "at": time.time()})
            worker.execute("INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value", [PARSERS_KEY, record])
        finally:
            worker.close()
        data = json.loads(runner.invoke(app, ["--json", "status"]).output)["data"]
        assert data["parsers"]["line"] == "parsers: 1 (machine busy)"


PROGRAM = (
    "import os\n\n\ndef helper(x):\n    return len(x) + other(x)\n\n\ndef other(y):\n    return y.strip() + unknown_name(y)\n\n\n"
    "def main():\n    return helper(os.getcwd())\n"
)


class TestThePythonBlockOfStatus:
    def test_calls_and_why_some_were_not_tied_to_a_function(self, kept_project):
        (kept_project / "m.py").write_text(PROGRAM)
        assert runner.invoke(app, ["--json", "scan"]).exit_code == 0
        block = json.loads(runner.invoke(app, ["--json", "status"]).output)["data"]["python"]
        assert block["files"] == 1 and block["calls"] == 5
        # helper->other and main->helper are tied; `len` is the language's; `y.strip` and `os.getcwd` have receivers; `unknown_name` is nowhere
        assert block["unresolved_calls"] == 3 and block["library_calls"] == 1
        assert block["unresolved_reasons"] == {"no definition in the project": 1, "unknown receiver": 2}  # `unknown_name` is defined nowhere
        assert sum(block["unresolved_reasons"].values()) == block["unresolved_calls"]
        assert block["unresolved_share"] == 60.0

    def test_a_project_without_python_has_no_block(self, kept_project):
        (kept_project / "a.cpp").write_text("int f() { return 1; }\n")
        assert runner.invoke(app, ["--json", "scan"]).exit_code == 0
        assert "python" not in json.loads(runner.invoke(app, ["--json", "status"]).output)["data"]


class TestCompactingWhileTheWorkerRuns:
    """The free blocks are given back by the running worker: reads are answered during the rebuild, a write waits for it,
    and the connection the worker holds afterwards is the new file."""

    @staticmethod
    def _server(db):
        from drspec.worker_ipc import WorkerIPCServer

        conn = duckdb.connect(str(db))  # pattern-ok: duckdb-file the objective is the real file (its free blocks, its rebuild)
        return WorkerIPCServer(conn, db, remediate_fn=lambda *a, **k: 0)  # (not started: no socket is needed, `stop()` closes the connection)

    def test_reads_are_answered_during_the_rebuild_and_the_file_shrinks(self, tmp_path, serve):
        import threading

        db = tmp_path / "c.db"
        _churned(db)
        before = os.path.getsize(db)
        measured = duckdb.connect(str(db))  # pattern-ok: duckdb-file the objective is the real file (its free blocks, its rebuild)
        free_before = storage_of(measured)["free_mb"]
        measured.close()
        server = self._server(db)
        client = serve(server).client()
        try:
            expected = client.execute("SELECT COUNT(*) FROM config").fetchone()[0]
            result = {}
            worker = threading.Thread(target=lambda: result.update(state=server.compact_if_needed(FREE_SHARE, 0)))
            worker.start()
            answers = []
            while worker.is_alive():
                answers.append(client.execute("SELECT COUNT(*) FROM config").fetchone()[0])
                worker.join(0.01)  # a pause that ends the moment the rebuild does
            worker.join()
            assert result["state"] == "compacted"
            assert answers and set(answers) == {expected}
            assert client.execute("SELECT COUNT(*) FROM config").fetchone()[0] == expected
            client.execute("INSERT INTO config (key, value) VALUES ('written after', 'ok')")  # the new connection takes writes
            assert client.execute("SELECT value FROM config WHERE key = 'written after'").fetchone() == ("ok",)
        finally:
            client.close()
            server.stop()
        assert os.path.getsize(db) < before * 0.7  # the junk is a good part of the file; the schema alone is about 1.8 MB
        reopened = duckdb.connect(str(db))  # pattern-ok: duckdb-file the objective is the real file (its free blocks, its rebuild)
        try:
            assert reopened.execute("SELECT value FROM config WHERE key = 'written after'").fetchone() == ("ok",)
            # (the share of a file this small can be high with a few blocks free; what matters is how much was given back)
            assert storage_of(reopened)["free_mb"] < free_before / 2
        finally:
            reopened.close()

    def test_a_file_that_is_compact_is_left_alone(self, tmp_path):
        db = tmp_path / "c.db"
        connection = get_connection(db)  # pattern-ok: duckdb-file the objective is the real file (its free blocks, its rebuild)
        init_schema(connection)
        connection.close()
        server = self._server(db)
        try:
            assert server.compact_if_needed(FREE_SHARE, 0) == "compact"
        finally:
            server.stop()

    def test_free_blocks_still_in_the_log_are_seen_and_given_back(self, tmp_path):
        """The drop is in the write-ahead log (far below the size at which DuckDB checkpoints by itself), so the file
        shows no free blocks until a checkpoint; the idle worker must look after making one."""
        db = tmp_path / "c.db"
        connection = get_connection(db)  # pattern-ok: duckdb-file the objective is the real file (its free blocks, its rebuild)
        init_schema(connection)
        pieces = " || ".join(f"md5(i::VARCHAR || '{letter}')" for letter in "abcdefgh")
        connection.execute(f"CREATE TABLE junk AS SELECT i, {pieces} AS text FROM range(4000) t(i)")
        connection.execute("INSERT INTO config (key, value) VALUES ('keep', 'kept')")
        connection.close()  # (the one checkpoint: the file is compact)
        server = self._server(db)
        try:
            server._conn.execute("DROP TABLE junk")
            assert storage_of(server._conn)["free_share"] <= FREE_SHARE  # what a worker that did not look would see
            assert needs_compaction(server._conn, FREE_SHARE, 0) is False
            assert server.compact_if_needed(FREE_SHARE, 0) == "compacted"
            assert storage_of(server._conn)["free_share"] <= FREE_SHARE
            assert server._conn.execute("SELECT value FROM config WHERE key = 'keep'").fetchone() == ("kept",)
        finally:
            server.stop()

    def test_a_failed_rebuild_leaves_the_file_and_the_connection_as_they_were(self, tmp_path, monkeypatch):
        from drspec.db import compaction

        db = tmp_path / "c.db"
        _churned(db)
        before = os.path.getsize(db)
        server = self._server(db)
        monkeypatch.setattr(compaction, "build_replacement", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
        try:
            assert server.compact_if_needed(FREE_SHARE, 0) == "failed"
            assert server._conn.execute("SELECT value FROM config WHERE key = 'keep'").fetchone() == ("kept",)
        finally:
            server.stop()
        assert os.path.getsize(db) >= before * 0.9
        assert not [p for p in tmp_path.iterdir() if p.name.startswith(".drspec-scan-")]  # the working folder is gone
