"""The IPC between a command and the worker that owns the database: framing, locking, reconnect, and what a publish lets through.

The server and its clients are tested in one process over `tests.fakes.fake_socket_pair()`: the claim under test is protocol
correctness, not the operating system's sockets, so there is no socket file, no accept loop and no database file (an in-memory
DuckDB). Requests that need no client at all go straight to `server._dispatch`. The real socket is used once
(`TestTheRealSocket`, `TestEveryCommandAsksTheWorker`); no test of this file starts a process.
"""

from __future__ import annotations

import datetime
import decimal
import json
import socket
import threading
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.db import init_schema, insert_artifact
from drspec.db.staging import publish_transaction
from drspec.worker_ipc import (
    WorkerConnection,
    WorkerIPCServer,
    _reconstruct_exception,
    _Session,
    _try_connect,
    connect_loopback,
    decode_rows,
    encode_rows,
    is_plain_read,
    socket_path,
)
from tests.helpers import fid

#: The database path of a server that is never started: only its folder is looked at, to name the socket.
NO_DB = Path("contracts.db")


@pytest.fixture
def memdb():
    """The worker's one connection: an in-memory database with a table `t`."""
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE t (x INTEGER, y VARCHAR)")
    yield conn
    conn.close()


@pytest.fixture
def schemadb(memdb):
    """The same with the project schema, which a publish needs (`complete_pending_removals`, `usage_stats`)."""
    init_schema(memdb)
    return memdb


def _server(conn, remediate_fn=lambda *a: 0, db_path=NO_DB):
    return WorkerIPCServer(conn, db_path, remediate_fn=remediate_fn)


@pytest.fixture
def server(memdb):
    return _server(memdb)


@pytest.fixture
def served(server, serve):
    return serve(server)


class TestRowEncoding:
    def test_datetime_round_trips(self):
        rows = [(datetime.datetime(2024, 1, 1, 12, 0, 0),)]
        assert decode_rows(encode_rows(rows)) == rows

    def test_array_column_round_trips_with_the_same_values(self):
        # DuckDB returns nested columns (e.g. `FLOAT[]` embeddings) as Python lists or tuples; JSON has no tuple type, so a
        # round trip yields a list instead — fine, every caller that reads an embedding (`queries.py`'s
        # `embedding = list(row[4])`) already normalizes to `list` before using it either way.
        rows = [((1.0, 2.0, 3.0),)]
        assert decode_rows(encode_rows(rows)) == [([1.0, 2.0, 3.0],)]

    @pytest.mark.parametrize(
        "value",
        [
            datetime.datetime(2024, 5, 17, 9, 30),
            datetime.date(2024, 5, 17),
            datetime.time(13, 45, 1, 250),
            datetime.timedelta(days=2, seconds=3, microseconds=4),
            decimal.Decimal("12345.6789"),
            b"\x00\xffbytes",
        ],
    )
    def test_each_duckdb_value_type_round_trips_with_its_own_type(self, value):
        encoded = encode_rows([(value,)])
        json.dumps(encoded)  # must be wire-serializable
        decoded = decode_rows(encoded)[0][0]
        assert decoded == value
        assert type(decoded) is type(value)  # a datetime must not come back as a date

    def test_an_error_type_the_client_knows_is_rebuilt_as_that_type(self):
        assert isinstance(_reconstruct_exception("CatalogException", "x"), duckdb.Error)
        assert type(_reconstruct_exception("NoSuchError", "x")) is RuntimeError  # anything else is a plain error with the message


class TestWorkerConnectionBasics:
    def test_execute_and_fetchall(self, served):
        conn = served.client()
        conn.execute("INSERT INTO t VALUES (1, 'a')")
        assert conn.execute("SELECT x, y FROM t").fetchall() == [(1, "a")]

    def test_transaction_commit_is_visible_to_a_new_connection(self, served):
        conn = served.client()
        conn.begin()
        conn.execute("INSERT INTO t VALUES (2, 'b')")
        conn.commit()
        assert served.client().execute("SELECT COUNT(*) FROM t WHERE x = 2").fetchone() == (1,)

    def test_transaction_rollback_discards_the_write(self, served):
        conn = served.client()
        conn.begin()
        conn.execute("INSERT INTO t VALUES (3, 'c')")
        conn.rollback()
        assert served.client().execute("SELECT COUNT(*) FROM t WHERE x = 3").fetchone() == (0,)

    def test_a_fetched_datetime_round_trips_back_as_a_bound_parameter(self, served):
        """`db/queries.py::_fk_references_detached` fetches a row, then feeds it straight back as `execute(...)`'s own params
        to restore it — so a bound parameter can itself be a `datetime` this connection just fetched. `WorkerConnection.execute`
        only encoded row *results*, not outgoing params, so sending one crashed with a plain `TypeError` from `json.dumps`."""
        conn = served.client()
        conn.execute("CREATE TABLE stamped (created_at TIMESTAMP)")
        conn.execute("INSERT INTO stamped VALUES (now())")
        fetched_at = conn.execute("SELECT created_at FROM stamped").fetchone()[0]
        assert isinstance(fetched_at, datetime.datetime)

        # The exact pattern this bug came from: re-using a fetched value as a later statement's own bound parameter.
        conn.execute("INSERT INTO stamped VALUES (?)", [fetched_at])
        rows = conn.execute("SELECT created_at FROM stamped ORDER BY created_at").fetchall()
        assert len(rows) == 2
        assert all(isinstance(row[0], datetime.datetime) for row in rows)

    def test_a_date_valued_query_comes_back_as_a_date(self, served):
        """`CAST(created_at AS DATE)` (as `learn stats` runs) returns a `datetime.date`; the unencoded value used to make
        `json.dumps` raise in the worker's handler thread and drop the connection."""
        conn = served.client()
        conn.execute("CREATE TABLE ev (created_at TIMESTAMP)")
        conn.execute("INSERT INTO ev VALUES (TIMESTAMP '2024-03-05 10:00:00')")
        rows = conn.execute("SELECT CAST(created_at AS DATE) AS day, COUNT(*) FROM ev GROUP BY day").fetchall()
        assert rows == [(datetime.date(2024, 3, 5), 1)]

    def test_an_unencodable_value_is_a_named_error_and_the_session_survives(self, served):
        conn = served.client()
        with pytest.raises(RuntimeError, match="UUID"):
            conn.execute("SELECT uuid()").fetchall()
        assert conn.execute("SELECT 1").fetchone() == (1,)

    def test_a_real_duckdb_error_reconstructs_as_the_same_exception_type(self, served):
        with pytest.raises(duckdb.Error):
            served.client().execute("SELECT * FROM nonexistent_table")

    def test_a_dropped_client_mid_transaction_releases_the_lock(self, served):
        """One client vanishing mid-`begin()` must not wedge the worker for every other client — the handler's own `finally:`
        rolls back and releases on a clean EOF."""
        conn = served.client()
        handler = served.threads[-1]
        conn.begin()
        conn.execute("INSERT INTO t VALUES (4, 'd')")
        conn._sock.close()  # simulate a dropped connection, not a clean close()
        handler.join(5)  # the handler sees the end of the stream, rolls back and gives the lock up
        assert not handler.is_alive()

        conn2 = served.client()
        conn2.begin()  # would wait for the lock for ever if it had not been released
        assert conn2.execute("SELECT COUNT(*) FROM t WHERE x = 4").fetchone() == (0,)  # and the write was rolled back
        conn2.commit()


@pytest.mark.integration
class TestTheRealSocket:
    """The one test on an operating-system socket: `start()` binds the socket file beside the database, the accept loop serves
    what connects, the worker's own loopback is not client activity, and `stop()` removes the file."""

    def test_a_started_server_answers_over_its_socket_file_and_a_loopback_does_not_wake_it(self, tmp_path):
        db_path = tmp_path / "contracts.db"
        conn = duckdb.connect(":memory:")
        conn.execute("CREATE TABLE t (x INTEGER, y VARCHAR)")
        server = _server(conn, db_path=db_path)
        server.start()
        try:
            loopback = connect_loopback(db_path)
            external = WorkerConnection(_try_connect(socket_path(db_path), timeout=2.0))
            try:
                loopback.execute("INSERT INTO t VALUES (1, 'a')")
                assert not server.wake.is_set()  # the worker's own loopback is not a client asking for something
                assert external.execute("SELECT x, y FROM t").fetchall() == [(1, "a")]
                assert server.wake.is_set()
            finally:
                loopback.close()
                external.close()
        finally:
            server.stop()
        assert not socket_path(db_path).exists()


class TestTheAcceptLoop:
    def test_it_goes_on_after_a_timeout_and_ends_when_the_listener_is_closed(self, server):
        class Listener:
            def __init__(self):
                self.answers = [socket.timeout(), OSError("closed")]  # an accept that timed out (the loop looks at `stop()`), then the end

            def accept(self):
                raise self.answers.pop(0)

        listener = Listener()
        server._listener = listener
        server._accept_loop()  # returns by itself
        assert listener.answers == []


class TestWakeAndPublishSignals:
    """A dormant worker should run a pass soon after a client asks it for something, but not because of its own loopback or
    its embedding child."""

    def test_a_client_request_wakes_the_worker(self, server):
        assert not server.wake.is_set()
        assert server._dispatch(_Session(), {"op": "execute", "sql": "SELECT 1"})["ok"]
        assert server.wake.is_set()

    def test_a_session_that_marks_itself_internal_does_not(self, server):
        session = _Session()
        server._dispatch(session, {"op": "hello", "internal": True})
        server._dispatch(session, {"op": "execute", "sql": "SELECT 1"})
        assert not server.wake.is_set()

    def test_a_connection_that_marks_itself_internal_does_not(self, served):
        client = served.client()
        client.mark_internal()
        client.execute("SELECT 1")
        assert not served.server.wake.is_set()

    def test_a_client_asks_for_a_re_scan_over_the_wire_and_can_bound_the_time_it_takes(self, served):
        seen = []
        served.server._remediate_fn = lambda conn, path, files, *rest: seen.append((files, rest)) or len(files)
        client = served.client()
        assert client.remediate(["a.py", "b.py"]) == 2
        assert client.remediate(["c.py"], pass_seconds=1.5) == 1  # pattern-ok: real-time-assert `pass_seconds` is an argument passed over the socket
        assert seen == [(["a.py", "b.py"], ()), (["c.py"], (1.5,))]

    def test_a_publish_sets_the_published_event(self, server):
        server._remediate_fn = lambda *a: 1
        assert not server.published.is_set()
        assert server._dispatch(_Session(), {"op": "remediate", "file_paths": ["a.py"]}) == {"ok": True, "remediated": 1}
        assert server.published.is_set()

    def test_a_re_scan_that_published_nothing_does_not(self, server):
        assert server._dispatch(_Session(), {"op": "remediate", "file_paths": ["a.py"]}) == {"ok": True, "remediated": 0}
        assert not server.published.is_set()


class TestGetWorkerConnectionRespawn:
    def test_respawns_when_the_first_spawn_found_the_old_worker_still_registered(self, tmp_path, monkeypatch, fake_time):
        """Right after `worker stop`, the old worker still owns its PID file, so the first spawn attempt is a no-op. The wait
        loop must keep trying to spawn rather than only retrying the connection."""
        import drspec.staleness_worker as staleness_worker_module
        import drspec.worker_ipc as worker_ipc_module

        monkeypatch.delenv("DRSPEC_DISABLE_AUTO_WORKER", raising=False)
        monkeypatch.setattr(worker_ipc_module, "time", fake_time)  # the waits between tries cost no time
        monkeypatch.setattr(worker_ipc_module, "_POLL_INTERVAL_SECONDS", 0.01)
        monkeypatch.setattr(worker_ipc_module, "_RESPAWN_INTERVAL_SECONDS", 0.05)

        spawn_calls: list[Path] = []
        sentinel_socket = object()

        def _fake_spawn(db_path):
            spawn_calls.append(db_path)

        def _fake_try_connect(path, timeout):
            # The socket only appears once a *second* spawn has happened.
            return sentinel_socket if len(spawn_calls) >= 2 else None

        monkeypatch.setattr(staleness_worker_module, "spawn_worker_if_not_running", _fake_spawn)
        monkeypatch.setattr(worker_ipc_module, "_try_connect", _fake_try_connect)

        conn = worker_ipc_module.get_worker_connection(tmp_path / "contracts.db")

        assert conn._sock is sentinel_socket
        assert len(spawn_calls) == 2
        assert fake_time.slept and sum(fake_time.slept) >= 0.05  # it waited for the respawn interval, on the fake clock


class TestPublishThroughTheWorker:
    """A scan or re-scan publishes in one transaction on the worker's own connection, so what it writes is visible to the next
    request without any reconnecting."""

    def test_remediate_publish_is_visible_through_the_same_server_afterward(self, schemadb):
        def remediate(conn, path, file_paths):
            with publish_transaction(conn) as write_conn:
                write_conn.execute("INSERT INTO t VALUES (100, 'remediated')")
            return len(file_paths)

        server = _server(schemadb, remediate)
        session = _Session()
        assert server._dispatch(session, {"op": "remediate", "file_paths": ["a.py"]}) == {"ok": True, "remediated": 1}
        assert server._dispatch(session, {"op": "execute", "sql": "SELECT x FROM t WHERE x = 100"})["rows"] == [[100]]


class TestReadsDuringAPublish:
    """While the worker builds a publish, a plain read (and the size report of the file) is answered from the last committed state at
    once; a usage count, a file noticed as changed, a setting and a round of embeddings are held back and written before the publish
    gives up the lock; any other write waits, as it always did. The publish is held open by an event, so what
    happens during it is the same every time."""

    @pytest.fixture
    def slow_publish(self, schemadb):
        started, release, over = threading.Event(), threading.Event(), threading.Event()

        def remediate(conn, path, file_paths):
            with publish_transaction(conn) as write_conn:
                write_conn.execute("INSERT INTO t VALUES (7, 'new')")
                started.set()
                release.wait(10)
                over.set()
            return 1

        schemadb.execute("INSERT INTO t VALUES (1, 'old')")
        server = _server(schemadb, remediate)
        publisher = threading.Thread(target=lambda: server._dispatch(_Session(), {"op": "remediate", "file_paths": ["a.py"]}), daemon=True)
        publisher.start()
        try:
            assert started.wait(10)
            yield SimpleNamespace(server=server, release=release, publisher=publisher, over=over)
        finally:
            release.set()
            publisher.join(10)

    @staticmethod
    def _ask(server, sql, params=None):
        return server._dispatch(_Session(), {"op": "execute", "sql": sql, "params": params or []})

    def test_a_read_sees_the_state_before_the_publish_and_does_not_wait_for_it(self, slow_publish):
        answer = self._ask(slow_publish.server, "SELECT x FROM t ORDER BY x")
        assert not slow_publish.over.is_set()  # answered while the publish was still held open (a read that waited for it would find it over)
        assert answer["rows"] == [[1]]  # the row the publish is writing is not visible yet
        slow_publish.release.set()
        slow_publish.publisher.join(10)
        assert self._ask(slow_publish.server, "SELECT x FROM t ORDER BY x")["rows"] == [[1], [7]]

    def test_a_usage_count_written_during_the_publish_is_kept_and_written_after_it(self, slow_publish):
        self._ask(slow_publish.server, "INSERT INTO usage_stats (kind, item_id, related_id, pending) VALUES (?, ?, ?, ?)", ["f", "a", "", 1.0])
        assert not slow_publish.over.is_set()  # held back and answered while the publish was still open
        slow_publish.release.set()
        slow_publish.publisher.join(10)
        assert self._ask(slow_publish.server, "SELECT item_id FROM usage_stats")["rows"] == [["a"]]

    def test_the_size_report_of_the_file_is_answered_during_the_publish(self, slow_publish):
        answer = self._ask(slow_publish.server, "PRAGMA database_size")
        assert not slow_publish.over.is_set() and answer["ok"] and len(answer["rows"]) == 1  # a row of numbers, from the committed state

    def test_a_file_noticed_as_changed_is_queued_during_the_publish_and_is_there_after_it(self, slow_publish):
        answer = self._ask(
            slow_publish.server, "INSERT INTO staleness_queue (file_path, flagged_at, reason) VALUES (?, now(), ?)", ["a.py", "drift"]
        )
        assert answer["ok"] and not slow_publish.over.is_set()  # answered while the publish was still held open
        assert self._ask(slow_publish.server, "SELECT file_path FROM staleness_queue")["rows"] == []  # (not committed yet: a read sees the state before)
        slow_publish.release.set()
        slow_publish.publisher.join(10)
        assert self._ask(slow_publish.server, "SELECT file_path, reason FROM staleness_queue")["rows"] == [["a.py", "drift"]]

    def test_a_setting_written_during_the_publish_is_written_after_it_in_the_order_it_came(self, slow_publish):
        sql = "INSERT INTO config (key, value, updated_at) VALUES (?, ?, now()) ON CONFLICT (key) DO UPDATE SET value = excluded.value"
        for value in ("one", "two"):
            assert self._ask(slow_publish.server, sql, ["setting:x", value])["ok"]
        assert not slow_publish.over.is_set()
        slow_publish.release.set()
        slow_publish.publisher.join(10)
        assert self._ask(slow_publish.server, "SELECT value FROM config WHERE key = 'setting:x'")["rows"] == [["two"]]

    def test_an_insert_that_returns_rows_is_not_held_back(self, slow_publish):
        from drspec.worker_ipc import is_deferrable_write

        assert is_deferrable_write("INSERT INTO config (key, value) VALUES (?, ?)")
        assert not is_deferrable_write("INSERT INTO config (key, value) VALUES (?, ?) RETURNING key")
        assert not is_deferrable_write("INSERT INTO t VALUES (1, 'x')")

    def test_the_embeddings_of_a_round_are_taken_during_the_publish_and_stored_after_it(self, slow_publish, monkeypatch):
        from drspec.core import embedding_backlog

        stored = []
        monkeypatch.setattr(embedding_backlog, "_store", lambda conn, results: (stored.append(len(results)), [item for item, _vector in results])[1])
        monkeypatch.setattr(embedding_backlog, "unreadable", lambda conn, written: [])
        request = {"op": "store_embeddings", "results": [{"item": {"table": "artifacts", "keys": ["a.py::f#1"], "source_hash": "h"}, "vector": [0.0]}]}
        answer = slow_publish.server._dispatch(_Session(), request)
        assert answer == {"ok": True, "stored": 1, "unpersisted": []}
        assert stored == [] and not slow_publish.over.is_set()  # answered while the publish was still held open, nothing stored yet
        slow_publish.release.set()
        slow_publish.publisher.join(10)
        assert stored == [1]  # stored before the publish gave up the lock

    def test_what_was_held_back_is_written_before_the_connection_is_closed_by_a_stop(self, slow_publish, tmp_path):
        self._ask(slow_publish.server, "INSERT INTO staleness_queue (file_path, flagged_at, reason) VALUES (?, now(), ?)", ["b.py", "drift"])
        slow_publish.server._path = tmp_path / "w.sock"  # (a stop removes the socket file: not the placeholder's, which is in the repository)
        conn = slow_publish.server._conn
        statements = []
        real_execute = conn.execute

        class Recording:
            def __getattr__(self, name):
                return getattr(conn, name)

            def execute(self, sql, params=None):
                statements.append(sql.split()[0].lower() + " " + (sql.split()[2] if len(sql.split()) > 2 else ""))
                return real_execute(sql, params) if params is not None else real_execute(sql)

            def close(self):
                statements.append("close")

        slow_publish.server._conn = Recording()
        stopper = threading.Thread(target=slow_publish.server.stop, daemon=True)
        stopper.start()  # a stop waits for the lock the publish holds
        assert not slow_publish.over.is_set()
        slow_publish.release.set()
        slow_publish.publisher.join(10)
        stopper.join(10)
        assert [entry for entry in statements if entry.startswith("insert staleness_queue") or entry == "close"] == ["insert staleness_queue", "close"]

    def test_a_label_in_the_text_of_a_read_is_not_a_writing_word(self):
        from drspec.worker_ipc import is_plain_read

        assert is_plain_read("SELECT 'call', COUNT(*) FROM calls UNION ALL SELECT 'insert', 1")  # the storage report of `status`
        assert not is_plain_read("CALL pragma_database_size()") and not is_plain_read("SELECT nextval('seq')") and not is_plain_read("SELECT 1; DELETE FROM t")

    def test_the_start_of_a_scan_does_not_hold_the_small_requests_up_either(self, schemadb, monkeypatch):
        from drspec.core import scan_service

        inside, release, over = threading.Event(), threading.Event(), threading.Event()

        def start_scan(conn, db_path, spec):  # (listing the project and queuing its files: held open by an event)
            inside.set()
            release.wait(10)
            over.set()
            return {"scan_id": "s1"}

        monkeypatch.setattr(scan_service, "start_scan", start_scan)
        schemadb.execute("INSERT INTO t VALUES (1, 'old')")
        server = _server(schemadb)
        starter = threading.Thread(target=lambda: server._dispatch(_Session(), {"op": "scan_start", "spec": {}}), daemon=True)
        starter.start()
        try:
            assert inside.wait(10)
            assert server._dispatch(_Session(), {"op": "execute", "sql": "SELECT x FROM t", "params": []})["rows"] == [[1]]
            setting = "INSERT INTO config (key, value, updated_at) VALUES (?, ?, now()) ON CONFLICT (key) DO UPDATE SET value = excluded.value"
            assert server._dispatch(_Session(), {"op": "execute", "sql": setting, "params": ["setting:y", "1"]})["ok"]
            assert not over.is_set()  # all answered while the start was still held open
        finally:
            release.set()
            starter.join(10)
        assert server._dispatch(_Session(), {"op": "execute", "sql": "SELECT value FROM config WHERE key = 'setting:y'", "params": []})["rows"] == [["1"]]

    def test_a_read_does_not_wait_for_another_clients_open_transaction_and_sees_what_was_committed(self, schemadb):
        server = _server(schemadb)
        schemadb.execute("INSERT INTO t VALUES (1, 'old')")
        writer, reader = _Session(), _Session()
        assert server._dispatch(writer, {"op": "begin"})["ok"]  # the writer holds the lock until it commits
        try:
            assert server._dispatch(writer, {"op": "execute", "sql": "INSERT INTO t VALUES (2, 'new')", "params": []})["ok"]
            answer = server._dispatch(reader, {"op": "execute", "sql": "SELECT x FROM t ORDER BY x", "params": []})
            assert answer["rows"] == [[1]]  # answered while the transaction was open, from the committed state
        finally:
            assert server._dispatch(writer, {"op": "commit"})["ok"]
        assert server._dispatch(reader, {"op": "execute", "sql": "SELECT x FROM t ORDER BY x", "params": []})["rows"] == [[1], [2]]

    def test_any_other_write_waits_for_the_publish(self, slow_publish):
        done = threading.Event()

        def write():
            self._ask(slow_publish.server, "INSERT INTO t VALUES (8, 'other')")
            done.set()

        threading.Thread(target=write, daemon=True).start()
        # A write that is not held up finishes in microseconds, and a held one never does until the publish ends: a short wait
        # tells them apart as well as a long one.
        assert not done.wait(0.2)
        slow_publish.release.set()
        assert done.wait(10)

    @pytest.mark.parametrize(
        "sql, plain",
        [
            ("SELECT caller_id, updated_at FROM dependencies", True),
            ("select * from t where y = ?", True),
            ("WITH a AS (SELECT 1) SELECT * FROM a", True),
            ("INSERT INTO t VALUES (1, 'a')", False),
            ("SELECT nextval('seq')", False),
            ("UPDATE t SET x = 1", False),
        ],
    )
    def test_which_statements_count_as_plain_reads(self, sql, plain):
        assert is_plain_read(sql) is plain


@pytest.mark.integration  # a real unix socket, with the worker's server in this process: no process to start, nothing to wait for
class TestEveryCommandAsksTheWorker:
    """The original bug: two PROCESSES each opened the database file, and a read-only connection one of them held blocked the other's
    writer. The fix is structural: while a worker runs, a command never opens the file; it asks the worker, which owns the one connection.
    That is shown here without a second process: the worker's real server answers on its real socket (a thread of this process, as in
    `test_api.py`), and DuckDB's `connect` is wrapped to record every FILE a command opens, which must be none.
    What a separate process would show that this does not: that DuckDB's file lock is held across processes. That is DuckDB's own
    behaviour and not ours; what is ours is that no command takes the lock, and that is what is counted."""

    def test_status_search_and_a_queued_scan_are_answered_by_the_worker_and_open_no_file(self, tmp_path, monkeypatch):
        import duckdb as duckdb_module

        root = tmp_path / "p"
        db_path = root / "_drspec" / "contracts.db"
        db_path.parent.mkdir(parents=True)
        on_disk = duckdb_module.connect(str(db_path))  # pattern-ok: duckdb-file a valid file with the schema and nothing in it: a command that opened it would get an answer (the empty one), not an error
        init_schema(on_disk)
        on_disk.close()  # (the worker's own connection below is in memory, with the function in it)
        (root / "a.py").write_text("def foo():\n    return 1\n")
        connection = duckdb_module.connect(":memory:")
        init_schema(connection)
        insert_artifact(
            connection, function_id=fid("a.py", "foo"), file_path="a.py", function_name="foo", signature="def foo():",
            body="def foo():\n    return 1\n", code_hash="h", language="python", start_line=1, end_line=2,
        )
        monkeypatch.chdir(root)
        monkeypatch.delenv("DRSPEC_DISABLE_AUTO_WORKER", raising=False)  # the commands go through the worker, as they do for a user
        server = WorkerIPCServer(connection, db_path, remediate_fn=lambda *args: 0)
        server.start()  # binds the socket beside the database and accepts in a thread
        try:
            real_connect = duckdb_module.connect
            opened = []

            def recording(database=":memory:", *args, **kwargs):
                if str(database) != ":memory:":
                    opened.append(str(database))
                return real_connect(database, *args, **kwargs)

            monkeypatch.setattr(duckdb_module, "connect", recording)
            runner = CliRunner()
            for arguments in (["status"], ["search", "foo"], ["scan", "--no-wait"]):
                result = runner.invoke(app, ["--json", *arguments])
                assert result.exit_code == 0, (arguments, result.output)
                assert json.loads(result.output)["success"] is True, (arguments, result.output)
            assert opened == []  # no command opened the database file: they all asked the worker
            assert server.wake.is_set()  # and the worker is what answered
        finally:
            server.stop()


class TestARequestThatTakesAsLongAsItsWork:
    """A batch of a large project is published for minutes: the loopback request that asks for it is waited for without a read
    timeout. (Found on a first scan of 25,000 files: the request failed after 120 s, the answer that came later was read as the
    answer of the next request, and every pass after it failed with another request's rows, for ever.) The fake socket times a
    `recv` out at once when it would have to wait while a timeout is set; the batch is held open by an event."""

    def _held(self, schemadb, serve):
        started, release = threading.Event(), threading.Event()

        def remediate(conn, path, file_paths):
            started.set()
            release.wait(10)
            return 3

        server = _server(schemadb, remediate)
        client = serve(server).client()
        client._sock.settimeout(120.0)  # what `_try_connect` sets on a real connection (`REQUEST_TIMEOUT_SECONDS`)
        return server, client, started, release

    def test_a_batch_that_outlasts_the_read_timeout_is_still_answered(self, schemadb, serve):
        _server_, client, started, release = self._held(schemadb, serve)
        answer = {}
        client._sock.times_out_when_empty = True  # a `recv` that has to wait while the usual timeout is set raises at once
        waiter = threading.Thread(target=lambda: answer.update(remediated=client.remediate(["a.py"])), daemon=True)
        waiter.start()
        assert started.wait(10)
        waiter.join(0.1)  # the batch is still held open: with a timeout set, the fake socket would already have raised
        assert waiter.is_alive() and not answer
        release.set()
        waiter.join(10)
        assert answer == {"remediated": 3}
        assert client._sock.gettimeout() == 120.0  # the usual timeout is back for the next request
        client._sock.times_out_when_empty = False
        assert client.execute("SELECT 1").fetchall() == [(1,)]  # and the connection is in step: its answer is its own

    def test_an_ordinary_request_that_has_to_wait_still_times_out(self, schemadb, serve):
        from drspec.worker_ipc import WorkerIPCError

        server, client, started, release = self._held(schemadb, serve)
        client._sock.times_out_when_empty = True
        try:
            server._lock.acquire()  # the connection is held by something long: an ordinary request has to wait for it
            with pytest.raises(WorkerIPCError, match="Lost contact"):
                client.execute("INSERT INTO t VALUES (9, 'x')")
        finally:
            server._lock.release()
            release.set()
