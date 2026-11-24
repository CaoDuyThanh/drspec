"""The optional trace: what is written, when, and that it can never get in the way."""

from __future__ import annotations

import json
import math
import shutil
import sys
import weakref
from pathlib import Path

import duckdb
import pytest
import typer.main
from typer.testing import CliRunner

from drspec import mcp_server, worker_ipc
from drspec.cli import app as app_module
from drspec.cli.app import app
from drspec.core import embedder, embedding_runner, tracing
from drspec.core.embedding_backlog import Item
from drspec.core.settings import (
    SettingError,
    get_setting,
    read_all_settings,
    set_setting,
)
from drspec.core.trace_report import (
    TraceError,
    clear_trace,
    parse_since,
    read_events,
    trace_summary,
    trace_tail,
)
from drspec.db import get_connection, init_schema
from drspec.db.staging import publish_transaction
from drspec.embedding_supervisor import EmbeddingSupervisor
from drspec.worker_ipc import (
    WorkerConnection,
    WorkerIPCServer,
    _Session,
    _WorkerCursor,
    decode_rows,
    encode_rows,
)
from tests.fakes import FakeTime, fake_socket_pair
from tests.helpers import folder_listing, table_counts

runner = CliRunner()


def log_of(directory: Path) -> Path:
    """Where the log of the project whose database is `directory/contracts.db` is written (the folder exists)."""
    path = tracing.log_file_for(directory / "contracts.db")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _events(directory: Path):
    return read_events(directory / "contracts.db")


@pytest.fixture
def trace_dir(tmp_path):
    tracing.bind(tmp_path / "contracts.db")
    return tmp_path


def _trace_on(level="all", slow_ms=500.0, max_mb=20.0):
    tracing.configure(level, slow_ms, max_mb)


@pytest.fixture
def memdb():
    """An empty project database in memory: for the tests that need a connection and nothing on disk."""
    connection = duckdb.connect(":memory:")
    init_schema(connection)
    yield connection
    connection.close()


@pytest.fixture(scope="module")
def initialized_project(tmp_path_factory):
    """A project that `init` made, once for the module; the CLI tests copy it (a copy is about 10 ms, an `init` 250 ms)."""
    root = tmp_path_factory.mktemp("initialized")
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(root)
        assert runner.invoke(app, ["init"]).exit_code == 0
    before = (table_counts(root / "_drspec" / "contracts.db"), folder_listing(root))
    yield root
    assert (table_counts(root / "_drspec" / "contracts.db"), folder_listing(root)) == before, "a test wrote into the template the module copies"


@pytest.fixture
def project(initialized_project, tmp_path, monkeypatch):
    """A copy of the initialized project, with the working folder in it."""
    shutil.copytree(initialized_project, tmp_path, dirs_exist_ok=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestOffByDefault:
    def test_nothing_is_written_and_no_file_appears(self, trace_dir):
        tracing.emit("request", 5000.0, op="execute")
        with tracing.span("rescan", kind="parse"):
            pass
        assert not log_of(trace_dir).exists()

    def test_the_setting_defaults_to_off(self, memdb):
        assert get_setting(memdb, "trace.level") == "off"
        assert read_all_settings(memdb)["trace.slow_ms"] == 500.0

    def test_a_scan_and_status_leave_no_trace_file(self, project):
        # the scan is the command with the most `span` and `emit` sites, so it is the one that is run
        (project / "a.py").write_text("def f():\n    return 1\n")
        assert runner.invoke(app, ["scan", "."]).exit_code == 0
        assert runner.invoke(app, ["status"]).exit_code == 0
        assert not log_of(project / "_drspec").exists()


class TestLevels:
    def test_all_writes_every_event_with_the_common_fields(self, trace_dir):
        _trace_on("all")
        tracing.emit("request", 0.2, op="execute", sql=None)
        (event,) = _events(trace_dir)
        assert {"t", "pid", "proc", "event", "ms"} <= set(event)
        assert event["event"] == "request" and event["op"] == "execute"
        assert "sql" not in event  # empty fields are dropped

    def test_slow_writes_only_events_at_or_above_the_threshold(self, trace_dir):
        _trace_on("slow", slow_ms=100.0)
        tracing.emit("request", 99.9)
        tracing.emit("request", 100.0)
        tracing.emit("request", 450.0)
        assert [e["ms"] for e in _events(trace_dir)] == [100.0, 450.0]

    def test_a_span_records_its_duration_and_the_fields_added_inside(self, trace_dir):
        _trace_on("all")
        with tracing.span("rescan", kind="parse") as fields:
            fields["rows"] = 7
        (event,) = _events(trace_dir)
        assert event["kind"] == "parse" and event["rows"] == 7 and event["ms"] >= 0

    def test_the_level_follows_the_settings(self, trace_dir):
        tracing.configure_from_settings({"trace.level": "slow", "trace.slow_ms": 7.0, "trace.max_mb": 1.0})
        assert tracing.level() == "slow"
        tracing.configure_from_settings({"trace.level": "off"})
        assert not tracing.enabled()

    def test_the_level_setting_accepts_only_its_words(self, memdb):
        assert set_setting(memdb, "trace.level", "SLOW") == "slow"
        with pytest.raises(SettingError, match="one of off, slow, all"):
            set_setting(memdb, "trace.level", "verbose")
        with pytest.raises(SettingError):
            set_setting(memdb, "trace.max_mb", 0)


class TestRotation:
    """A 1 KB limit (`max_mb=0.001`) and a few dozen lines of about 200 bytes show the same rotations as a 10 KB limit and
    hundreds of lines did."""

    def test_the_file_never_grows_past_the_limit_plus_one_line(self, trace_dir):
        _trace_on("all", max_mb=0.001)
        limit = int(0.001 * 1024 * 1024)
        longest = 0
        for i in range(40):
            tracing.emit("request", 1.0, op="execute", sql="x" * 100, rows=i)
            current = log_of(trace_dir)
            longest = max(longest, current.stat().st_size)
        assert longest <= limit
        assert Path(str(log_of(trace_dir)) + ".1").exists()
        assert Path(str(log_of(trace_dir)) + ".1").stat().st_size <= limit

    def test_only_one_older_file_is_kept(self, trace_dir):
        _trace_on("all", max_mb=0.001)
        for i in range(60):  # several rotations, so the older file is replaced more than once
            tracing.emit("request", 1.0, sql="x" * 100, rows=i)
        folder = log_of(trace_dir).parent
        assert sorted(p.name for p in folder.iterdir() if not p.name.endswith(".lock")) == [log_of(trace_dir).name, log_of(trace_dir).name + ".1"]

    def test_the_reader_joins_both_files_in_time_order(self, trace_dir):
        _trace_on("all", max_mb=0.001)
        for i in range(30):
            tracing.emit("request", 1.0, sql="x" * 100, rows=i)
        assert Path(str(log_of(trace_dir)) + ".1").exists()  # there are two files to join
        numbers = [e["rows"] for e in _events(trace_dir)]
        assert numbers == sorted(numbers) and numbers[-1] == 29


class TestNeverGetsInTheWay:
    def test_an_unwritable_location_neither_raises_nor_retries(self, tmp_path, monkeypatch, capsys):
        blocker = tmp_path / "a_file"
        blocker.write_text("x")
        monkeypatch.setenv(tracing.DIR_ENV_VAR, str(blocker / "logs"))  # a folder that cannot be made
        tracing.bind(tmp_path / "contracts.db")
        _trace_on("all")
        for _ in range(5):
            tracing.emit("request", 1.0)  # must not raise
        lines = [line for line in capsys.readouterr().err.splitlines() if "tracing is off" in line]
        assert len(lines) == 1  # said once, then it stops trying

    def test_a_full_disk_is_swallowed(self, trace_dir, monkeypatch):
        _trace_on("all")

        def full(fd, data):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(tracing.os, "write", full)
        tracing.emit("request", 1.0)  # must not raise
        tracing.emit("request", 1.0)
        assert not log_of(trace_dir).exists() or log_of(trace_dir).read_text() == ""  # and nothing was written

    def test_a_command_still_succeeds_when_the_trace_cannot_be_written(self, project):
        log_of(project / "_drspec").mkdir()  # a directory where the file should be
        result = runner.invoke(app, ["--trace", "status"])
        assert result.exit_code == 0
        assert "Items Needing Attention" in result.output or "items_needing_attention" in result.output


class TestWhatIsNotRecorded:
    def test_quoted_values_are_masked_and_the_prefix_is_cut(self):
        shape = tracing.sql_shape("SELECT  *\nFROM t WHERE name = 'secret value' AND x = ?")
        assert "secret" not in shape and shape == "SELECT * FROM t WHERE name = '?' AND x = ?"
        assert len(tracing.sql_shape("SELECT " + "a, " * 200)) == tracing.SQL_PREFIX_CHARS


class TestChoosingTheLevel:
    def test_the_environment_pins_a_command_process(self, monkeypatch):
        monkeypatch.setenv("DRSPEC_TRACE", "slow")
        tracing.init_process("cli", honor_env=True)
        assert tracing.level() == "slow"
        tracing.observe_worker(None)  # the worker says off; the environment wins
        assert tracing.level() == "slow"

    @pytest.mark.parametrize("raw,expected", [("1", "all"), ("on", "all"), ("ALL", "all"), ("slow", "slow"), ("0", "off"), ("off", "off"), ("nonsense", None)])
    def test_environment_values(self, raw, expected):
        assert tracing.parse_level(raw) == expected

    def test_the_worker_and_embedding_process_ignore_the_environment(self, monkeypatch):
        monkeypatch.setenv("DRSPEC_TRACE", "all")
        for proc in ("worker", "embed"):
            tracing.init_process(proc, honor_env=False)
            assert tracing.level() == "off"

    def test_a_command_follows_the_workers_level_when_nothing_pins_it(self):
        tracing.init_process("cli", honor_env=True)
        tracing.observe_worker({"level": "slow", "slow_ms": 40.0, "max_mb": 5.0})
        assert tracing.level() == "slow"
        tracing.observe_worker(None)
        assert tracing.level() == "off"

    def test_the_worker_does_not_follow_a_report(self):
        tracing.init_process("worker", honor_env=False)
        tracing.observe_worker({"level": "all"})
        assert tracing.level() == "off"


def _ask(server, sql, params=(), cid=None):
    """What the worker does with one `execute` request, after the socket: the reply."""
    return server._handle_request(_Session(), {"op": "execute", "sql": sql, "params": encode_rows([list(params)])[0], "cid": cid})


class _Clock(FakeTime):
    def perf_counter(self):  # pattern-ok: real-time-assert the fake clock's own method (FakeTime-like): it moves only when told
        return self.now


class _SlowLock:
    """A lock that takes a quarter of a second to get, on the fake clock."""

    def __init__(self, clock):
        self._clock = clock

    def acquire(self, *args, **kwargs):
        self._clock.now += 0.25
        return True

    def release(self):
        pass


class TestTheWorkersRequests:
    """`_handle_request` is called in this process, on an in-memory database: no socket, no thread, nothing to wait for (the
    wire itself is `test_worker_ipc.py`'s)."""

    @pytest.fixture
    def served(self, memdb, tmp_path, trace_dir):
        memdb.execute("CREATE TABLE t (x INTEGER)")
        server = WorkerIPCServer(memdb, tmp_path / "contracts.db", remediate_fn=lambda *a: 0)  # never started
        tracing.init_process("worker", honor_env=False)
        tracing.bind(tmp_path / "contracts.db")
        return server

    def test_a_request_records_lock_wait_and_run_time_under_the_commands_cid(self, served, trace_dir):
        _trace_on("all")
        _ask(served, "INSERT INTO t VALUES (?)", [7], cid="abc12345")
        reply = _ask(served, "SELECT x FROM t WHERE x = 7", cid="abc12345")
        assert decode_rows(reply["rows"]) == [(7,)]
        requests = [e for e in _events(trace_dir) if e["event"] == "request" and e["op"] == "execute"]
        assert len(requests) == 2
        for event in requests:
            assert event["cid"] == "abc12345"
            assert event["lock_ms"] >= 0 and event["run_ms"] >= 0
            assert event["ms"] == pytest.approx(event["lock_ms"] + event["run_ms"], abs=0.01)
        assert requests[1]["rows"] == 1 and requests[1]["sql"] == "SELECT x FROM t WHERE x = 7"

    def test_a_request_that_waits_for_the_lock_shows_the_wait(self, served, trace_dir, monkeypatch):
        _trace_on("all")
        clock = _Clock()
        monkeypatch.setattr(worker_ipc, "time", clock)  # the wait is on the fake clock: taking the lock costs 0.25 s of it
        served._lock = _SlowLock(clock)
        _ask(served, "INSERT INTO t VALUES (1)")  # (a read is answered from the committed state when the lock is held: only a write waits)
        (event,) = [e for e in _events(trace_dir) if e["event"] == "request" and e["op"] == "execute"]
        assert event["lock_ms"] >= 200 and event["run_ms"] < event["lock_ms"]

    def test_values_are_never_written(self, served, trace_dir):
        _trace_on("all")
        _ask(served, "INSERT INTO t VALUES (?)", [424242])
        assert "424242" not in log_of(trace_dir).read_text()

    def test_replies_carry_the_level_only_while_tracing_is_on(self, served, trace_dir):
        assert "trace" not in served._handle_request(_Session(), {"op": "execute", "sql": "SELECT 1"})
        _trace_on("slow", slow_ms=250.0)
        reply = served._handle_request(_Session(), {"op": "execute", "sql": "SELECT 1"})
        assert reply["trace"]["level"] == "slow" and reply["trace"]["slow_ms"] == 250.0

    def test_an_older_worker_that_ignores_the_cid_is_not_an_error(self):
        client_end, server_end = fake_socket_pair()
        # An older worker reads only the fields it knows and never replies with a trace report.
        server_end.sendall((json.dumps({"ok": True, "rows": [[1]]}) + "\n").encode())
        tracing.init_process("cli", honor_env=True)
        tracing.set_level("all", pinned=False)
        client = WorkerConnection(client_end)
        assert client.execute("SELECT 1").fetchall() == [(1,)]
        assert json.loads(server_end.recv(4096))["cid"]
        assert tracing.level() == "off"  # no report from the worker means it is not tracing


class TestPublishAndPass:
    def test_a_publish_records_the_time_in_each_phase(self, trace_dir):
        _trace_on("all")
        db_path = trace_dir / "contracts.db"
        conn = duckdb.connect(str(db_path))  # pattern-ok: duckdb-file the phases of a publish: a publish writes a file
        init_schema(conn)
        conn.execute("CREATE TABLE t (x INTEGER)")
        with publish_transaction(conn) as write:
            write.execute("INSERT INTO t VALUES (1)")
        conn.close()
        (event,) = [e for e in _events(trace_dir) if e["event"] == "rescan" and e["kind"] == "publish"]
        assert event["published"] is True
        phases = ("sync_ms", "commit_ms", "removal_ms")
        for phase in phases:
            assert event[phase] >= 0
        assert not {"copy_ms", "fsync_ms", "replace_ms", "checkpoint_ms"} & set(event)  # no copy of the file any more
        assert event["ms"] >= sum(event[p] for p in phases) - 0.01
        assert {"rss_mb", "mem_avail_mb", "db_mb", "load1"} <= set(event)  # the state of the machine (`db_mb` is read from the file)

    def test_a_failed_publish_is_recorded_as_not_published(self, trace_dir):
        _trace_on("all")
        db_path = trace_dir / "contracts.db"
        conn = duckdb.connect(str(db_path))  # pattern-ok: duckdb-file same
        init_schema(conn)
        with pytest.raises(RuntimeError):
            with publish_transaction(conn):
                raise RuntimeError("boom")
        conn.close()
        assert [e for e in _events(trace_dir) if e["event"] == "rescan"] == []  # nothing was published, so no publish event

    def test_a_worker_pass_records_what_it_did(self, tmp_path, trace_dir, monkeypatch):
        from drspec.staleness_worker import run_one_pass

        monkeypatch.chdir(tmp_path)
        conn = get_connection(tmp_path / "contracts.db")  # pattern-ok: duckdb-file same
        init_schema(conn)
        _trace_on("all")
        run_one_pass(conn, tmp_path / "contracts.db", read_all_settings(conn))
        (event,) = [e for e in _events(trace_dir) if e["event"] == "pass"]
        assert {"detect_ms", "discover_ms", "rescan_ms", "queued_before", "batch", "remediated", "settings"} <= set(event)
        assert event["settings"]["worker.rescan_budget"] == 200
        assert {"rss_mb", "mem_avail_mb", "db_mb", "load1"} <= set(event)
        conn.close()


class _Direct:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, *args, **kwargs):
        return self._conn.execute(*args, **kwargs)


class TestEmbeddingTrace:
    @pytest.fixture(autouse=True)
    def fake_model(self, monkeypatch):
        monkeypatch.setattr(embedder, "load_model", lambda threads: None)
        monkeypatch.setattr(embedder, "embed_text", lambda text: [0.5] * 384)
        monkeypatch.setattr(embedder, "model_is_cached", lambda: True)
        monkeypatch.setattr(embedding_runner.os, "nice", lambda n: 0)
        monkeypatch.setattr(embedding_runner, "pause_reason", lambda settings: None)

    def test_an_item_that_never_leaves_the_backlog_is_traced_and_the_loop_is_cut_short(self, memdb, trace_dir, monkeypatch):
        conn = memdb
        set_setting(conn, "trace.level", "all")
        item = Item("artifacts", ("a.py::f",), "hash-f")
        # The store claims success but the item stays owed, as when a vector never lands.
        monkeypatch.setattr(embedding_runner, "find_backlog", lambda *args, **kwargs: [item])
        monkeypatch.setattr(embedding_runner, "fetch_texts", lambda c, items: [(items[0], "def f(): pass")])

        class Connection(_Direct):
            def store_embeddings(self, results):
                return len(results)

        assert embedding_runner.run_embedding_process(Connection(conn), sleep=lambda s: None) == embedding_runner.EXIT_NOT_PERSISTED

        events = [e for e in _events(trace_dir) if e["event"] == "embed"]
        chunks = [e for e in events if e["kind"] == "chunk"]
        assert [c["round"] for c in chunks] == [1]  # the second round never starts
        assert "first" not in chunks[0] and chunks[0]["stored"] == 1
        (stopped,) = [e for e in events if e["kind"] == "not_persisted"]
        assert "first" not in stopped and stopped["items"] == 1 and "dropped_fields" not in stopped

    def test_a_pause_is_recorded_with_its_reason(self, memdb, trace_dir, monkeypatch):
        conn = memdb
        set_setting(conn, "trace.level", "all")
        item = Item("artifacts", ("a.py::f",), "hash-f")
        monkeypatch.setattr(embedding_runner, "find_backlog", lambda *a, **k: [item])
        monkeypatch.setattr(embedding_runner, "fetch_texts", lambda c, items: [])
        reasons = iter(["machine busy", None])
        monkeypatch.setattr(embedding_runner, "pause_reason", lambda settings: next(reasons))

        class Connection(_Direct):
            def store_embeddings(self, results):
                return 0

        embedding_runner.run_embedding_process(Connection(conn), sleep=lambda s: None)
        (pause,) = [e for e in _events(trace_dir) if e["event"] == "embed" and e["kind"] == "pause"]
        assert pause["reason"] == "machine_busy"


class _Process:
    def __init__(self):
        self.code = None
        self.pid = 4242

    def poll(self):
        return self.code


class TestSupervisorTrace:
    def test_start_exit_and_backoff_are_recorded(self, memdb, trace_dir, monkeypatch):
        conn = memdb
        _trace_on("all")
        monkeypatch.setattr("drspec.embedding_supervisor.find_backlog", lambda *a, **k: [Item("artifacts", ("a",), "h")])
        spawned = []
        supervisor = EmbeddingSupervisor(spawn=lambda db_path, threads=1: spawned.append(_Process()) or spawned[-1])
        settings = read_all_settings(conn)
        supervisor.tick(conn, "unused.db", settings, 1000.0)
        spawned[-1].code = 1
        supervisor.tick(conn, "unused.db", settings, 1010.0)
        events = [e for e in _events(trace_dir) if e["event"] == "supervisor"]
        assert [e["kind"] for e in events] == ["start", "exit", "backoff"]
        assert events[0]["owed"] == 1 and events[0]["child_pid"] == 4242
        assert events[1]["code"] == 1
        assert events[2]["wait_s"] == 60.0 and events[2]["reason"] == "exit_code"


def _write_trace(directory: Path, rows):
    with open(log_of(directory), "w") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _row(event, ms, t="2099-01-01T00:00:00.000+00:00", **fields):
    return {"t": t, "pid": 1, "proc": "worker", "event": event, "ms": ms, **fields}


class TestReading:
    def test_summary_gives_count_total_median_p95_and_max_per_event(self, tmp_path):
        _write_trace(tmp_path, [_row("request", ms, op="execute") for ms in (1, 2, 3, 4, 100)] + [_row("pass", 9.0)])
        data = trace_summary(tmp_path / "contracts.db")
        by_name = {row["event"]: row for row in data["summary"]}
        assert by_name["request execute"] == {
            "event": "request execute", "count": 5, "total_ms": 110.0, "median_ms": 3.0, "p95_ms": 100.0, "max_ms": 100.0,
        }
        assert by_name["pass"]["count"] == 1
        assert data["events_read"] == 6
        assert data["slowest"][0]["ms"] == 100.0 and data["slowest"][0]["event"] == "request"

    def test_slowest_is_limited_and_carries_the_cid(self, tmp_path):
        _write_trace(tmp_path, [_row("command", ms, command="search", cid=f"c{ms}") for ms in (5, 50, 500)])
        slowest = trace_summary(tmp_path / "contracts.db", slowest=2)["slowest"]
        assert [e["cid"] for e in slowest] == ["c500", "c50"] and slowest[0]["label"] == "search"

    def test_since_drops_older_events(self, tmp_path):
        _write_trace(tmp_path, [_row("pass", 1.0, t="2001-01-01T00:00:00.000+00:00"), _row("pass", 2.0)])
        assert trace_summary(tmp_path / "contracts.db", since="1h")["events_read"] == 1
        assert trace_summary(tmp_path / "contracts.db")["events_read"] == 2

    @pytest.mark.parametrize("text,seconds", [("30s", 30), ("15m", 900), ("1h", 3600), ("2d", 172800)])
    def test_since_units(self, text, seconds):
        assert parse_since(text) == seconds  # pattern-ok: real-time-assert parses a text to a number of seconds; nothing is timed

    def test_a_bad_since_is_an_error(self, tmp_path):
        with pytest.raises(TraceError):
            trace_summary(tmp_path / "contracts.db", since="soon")

    def test_one_commands_path_is_returned_in_time_order(self, tmp_path):
        _write_trace(
            tmp_path,
            [
                _row("request", 2.0, t="2099-01-01T00:00:02.000+00:00", op="execute", cid="aa"),
                _row("command", 9.0, t="2099-01-01T00:00:03.000+00:00", command="search", cid="aa"),
                _row("request", 1.0, t="2099-01-01T00:00:01.000+00:00", op="begin", cid="aa"),
                _row("request", 1.0, t="2099-01-01T00:00:01.500+00:00", op="execute", cid="bb"),
            ],
        )
        path = trace_summary(tmp_path / "contracts.db", cid="aa")["path"]
        assert [(e["event"], e.get("label")) for e in path] == [("request", "begin"), ("request", "execute"), ("command", "search")]

    def test_damaged_lines_are_skipped(self, tmp_path):
        log_of(tmp_path).write_text('{"event":"pass","ms":1,"t":"2099"}\nnot json\n{"no":"event"}\n\n')
        assert trace_summary(tmp_path / "contracts.db")["events_read"] == 1

    def test_clear_empties_both_files_but_keeps_the_current_one_in_place(self, tmp_path):
        _write_trace(tmp_path, [_row("pass", 1.0)])
        Path(str(log_of(tmp_path)) + ".1").write_text("x\n")
        result = clear_trace(tmp_path / "contracts.db")
        assert result["cleared_bytes"] > 0
        assert log_of(tmp_path).stat().st_size == 0 and not Path(str(log_of(tmp_path)) + ".1").exists()

    def test_clear_on_an_empty_directory_creates_nothing(self, tmp_path):
        assert clear_trace(tmp_path / "contracts.db") == {"cleared_bytes": 0}
        assert list(tmp_path.iterdir()) == []

    def test_tail_returns_the_latest_events(self, tmp_path):
        _write_trace(tmp_path, [_row("pass", float(i), t=f"2099-01-01T00:00:0{i}.000+00:00") for i in range(5)])
        assert [e["ms"] for e in trace_tail(tmp_path / "contracts.db", 2)["events"]] == [3.0, 4.0]


def _invoke(monkeypatch, args):
    """Run the CLI the way a shell would: the command name is read from `sys.argv`."""
    monkeypatch.setattr("sys.argv", ["drspec", *args])
    return runner.invoke(app, args)


@pytest.fixture
def fake_usage(monkeypatch):
    """What the operating system says this process has used: 200 MB at peak (204,800 KiB, as Linux counts) and 1.75 s of CPU, whatever it really used."""
    import resource
    from types import SimpleNamespace

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(resource, "getrusage", lambda who: SimpleNamespace(ru_maxrss=204800, ru_utime=1.5, ru_stime=0.25))


class TestCommandLine:
    """The options of the command line write one `command` event with the right fields and never a value. Each test works in a
    copy of one initialized project (`project`); the ones about the log or about the options alone need no project."""

    def test_the_flag_traces_one_command(self, project, monkeypatch, fake_usage):
        assert _invoke(monkeypatch, ["--trace", "search", "f"]).exit_code == 0
        (event,) = [e for e in _events(project / "_drspec") if e["event"] == "command"]
        assert event["command"] == "search" and event["status"] == "ok" and event["out_bytes"] > 0
        assert event["pattern_length"] == 1 and event["pattern_mode"] == "literal" and "pattern" not in event and event["cid"]
        assert event["proc"] == "cli"
        assert event["max_rss_mb"] == 200.0 and event["cpu_s"] == 1.75  # the process's peak memory and CPU seconds, as the system says them  # pattern-ok: real-time-assert the numbers come from `fake_usage`, not from the machine
        # Not switched on for the next command.
        before = len(_events(project / "_drspec"))
        assert _invoke(monkeypatch, ["status"]).exit_code == 0
        assert len(_events(project / "_drspec")) == before

    def test_the_environment_variable_traces_a_command(self, project, monkeypatch):
        monkeypatch.setenv("DRSPEC_TRACE", "all")
        assert _invoke(monkeypatch, ["status"]).exit_code == 0
        assert [e["command"] for e in _events(project / "_drspec") if e["event"] == "command"] == ["status"]

    def test_slow_level_skips_a_fast_command(self, project, monkeypatch):
        assert _invoke(monkeypatch, ["--trace-level", "slow", "status"]).exit_code == 0
        assert _events(project / "_drspec") == []

    def test_nested_commands_are_named_in_full_and_values_are_not_recorded(self):
        # the name and the options of the event are `_command_name`'s: no database, no command run
        command, flags = app_module._command_name(typer.main.get_command(app), ["--trace", "deps", "get", "a.py::f", "--depth", "2"])
        assert command == "deps get" and flags == ["--depth"]
        # the value of a global option is not a word of the command, even when it spells one
        command, flags = app_module._command_name(typer.main.get_command(app), ["--db", "search", "--trace-level", "slow", "status"])
        assert command == "status" and flags == []

    def test_the_search_pattern_is_never_written_only_its_length_and_kind(self, project, monkeypatch):
        _invoke(monkeypatch, ["--trace", "search", "secret_marker_" + "x" * 190])
        (event,) = [e for e in _events(project / "_drspec") if e["event"] == "command"]
        assert event["pattern_length"] == 204 and event["pattern_mode"] == "literal"
        assert "secret_marker" not in json.dumps(event)

    def test_a_failing_command_records_its_error_code(self, project, monkeypatch):
        _invoke(monkeypatch, ["--trace", "contract", "get", "no.py::missing"])
        (event,) = [e for e in _events(project / "_drspec") if e["event"] == "command"]
        assert event["status"] != "ok"

    def test_an_unknown_level_is_rejected(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)  # (the option is refused before any database is looked for)
        result = _invoke(monkeypatch, ["--trace-level", "loud", "status"])
        assert result.exit_code != 0

    def test_trace_commands_do_not_trace_themselves(self, project, monkeypatch):
        _invoke(monkeypatch, ["--trace", "status"])
        before = len(_events(project / "_drspec"))
        _invoke(monkeypatch, ["--trace", "trace", "summary"])
        _invoke(monkeypatch, ["--trace", "trace", "tail"])
        assert len(_events(project / "_drspec")) == before

    def test_trace_summary_prints_and_clear_empties(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_trace(tmp_path / "_drspec", [_row("command", 5.0, command="status")])  # (that a real command writes it: the first test)
        result = _invoke(monkeypatch, ["--json", "trace", "summary", "--slowest", "3"])
        data = json.loads(result.output)["data"]
        assert data["events_read"] >= 1 and any(row["event"] == "command status" for row in data["summary"])
        assert _invoke(monkeypatch, ["trace", "clear"]).exit_code == 0
        assert _events(tmp_path / "_drspec") == []

    def test_a_bad_since_is_a_clean_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _invoke(monkeypatch, ["--json", "trace", "summary", "--since", "soon"])
        assert result.exit_code == 1 and json.loads(result.output)["success"] is False


@pytest.mark.usefixtures("fixed_build")  # (every tool call asks for the build id, which hashes the package otherwise)
class TestMcp:
    def test_the_summary_tool_matches_the_command(self, tmp_path):
        _write_trace(tmp_path, [_row("request", ms, op="execute", cid="aa") for ms in (1, 2, 30)])
        db_path = tmp_path / "contracts.db"
        reply = mcp_server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "trace_summary", "arguments": {"slowest": 5, "format": "json"}}},
            db_path,
        )
        assert reply["result"]["isError"] is False
        assert json.loads(reply["result"]["content"][0]["text"]) == trace_summary(tmp_path / "contracts.db", slowest=5)

    def test_the_tool_returns_one_commands_path(self, tmp_path):
        _write_trace(tmp_path, [_row("command", 5.0, command="search", cid="aa"), _row("request", 1.0, op="execute", cid="bb")])
        reply = mcp_server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "trace_summary", "arguments": {"cid": "aa", "format": "json"}}},
            tmp_path / "contracts.db",
        )
        assert [e["event"] for e in json.loads(reply["result"]["content"][0]["text"])["path"]] == ["command"]

    def test_a_bad_argument_is_a_tool_error(self, tmp_path):
        reply = mcp_server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "trace_summary", "arguments": {"since": "soon"}}},
            tmp_path / "contracts.db",
        )
        assert reply["result"]["isError"] is True

    def test_a_tool_call_writes_a_command_event_when_traced(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DRSPEC_TRACE", "all")
        tracing.init_process("mcp", honor_env=True)
        tracing.bind(tmp_path / "contracts.db")
        mcp_server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "worker_status", "arguments": {}}},
            tmp_path / "contracts.db",
        )
        (event,) = [e for e in _events(tmp_path) if e["event"] == "command"]
        assert event["command"] == "worker_status" and event["proc"] == "mcp" and event["status"] == "ok"

    def test_every_listed_tool_has_a_handler(self):
        names = {tool["name"] for tool in mcp_server._TOOLS}
        assert "trace_summary" in names and names == set(mcp_server._TOOL_HANDLERS)


class TestSeveralWritersRotatingOneLog:
    def test_every_writer_keeps_tracing_and_no_line_is_damaged(self, trace_dir):
        # six threads write at once, each write opening the file and taking the lock file on its own, as six processes would (it was six processes)
        import threading

        _trace_on("all", max_mb=0.01)  # 10 KB

        def write():
            for i in range(600):
                tracing.emit("request", 1.0, op="execute", sql="SELECT " + "x" * 60, rows=i)
            tracing.emit("request", 1.0, op="done")

        writers = [threading.Thread(target=write) for _ in range(6)]
        for writer in writers:
            writer.start()
        for writer in writers:
            writer.join(120)
        assert tracing._failures == 0 and tracing._blocked_until == 0.0
        lines = []
        for suffix in (".1", ""):
            path = Path(str(log_of(trace_dir)) + suffix)
            if path.exists():
                lines += path.read_text().splitlines()
        assert lines and all(json.loads(line)["event"] == "request" for line in lines)  # every line is whole
        # Each writer may append one more line after reading a size just under the limit,
        # so with six writers a file can pass it by up to six lines (about 200 bytes each).
        for suffix in ("", ".1"):
            assert Path(str(log_of(trace_dir)) + suffix).stat().st_size <= 10485 + 6 * 256

    def test_a_collision_on_the_rename_is_not_a_failure(self, trace_dir, monkeypatch):
        _trace_on("all", max_mb=0.001)
        real = tracing.os.replace
        calls = []

        def someone_else_got_there_first(source, target):
            calls.append(1)
            raise FileNotFoundError(source)

        monkeypatch.setattr(tracing.os, "replace", someone_else_got_there_first)
        for i in range(40):
            tracing.emit("request", 1.0, sql="x" * 100, rows=i)
        assert calls and tracing._failures == 0
        del real

    def test_one_failure_pauses_tracing_for_a_while_instead_of_ending_it(self, trace_dir, monkeypatch):
        _trace_on("all")
        clock = {"now": 1000.0}
        monkeypatch.setattr(tracing.time, "monotonic", lambda: clock["now"])
        broken = {"on": True}
        real_write = tracing._write

        def write(data):
            if broken["on"]:
                raise OSError("disk full")
            real_write(data)

        monkeypatch.setattr(tracing, "_write", write)
        tracing.emit("request", 1.0)  # fails: paused for the first wait
        broken["on"] = False
        tracing.emit("request", 1.0, rows=1)  # still paused
        assert _events(trace_dir) == []
        clock["now"] += tracing.FIRST_RETRY_SECONDS + 0.1
        tracing.emit("request", 1.0, rows=2)  # the wait is over and the disk works again
        assert [e["rows"] for e in _events(trace_dir)] == [2]

    def test_each_failure_doubles_the_wait_up_to_a_limit(self, trace_dir, monkeypatch):
        _trace_on("all")
        clock = {"now": 1000.0}
        monkeypatch.setattr(tracing.time, "monotonic", lambda: clock["now"])
        monkeypatch.setattr(tracing, "_write", lambda data: (_ for _ in ()).throw(OSError("no")))
        waits = []
        for _ in range(12):
            tracing.emit("request", 1.0)
            waits.append(tracing._blocked_until - clock["now"])
            clock["now"] = tracing._blocked_until + 0.01
        assert waits[:3] == [5.0, 10.0, 20.0] and max(waits) == tracing.MAX_RETRY_SECONDS


class _CountedJson:
    """`json` as the trace reader sees it, counting the events alive at once (a count, the same on every machine: nothing is measured in bytes)."""

    class Event(dict):
        pass

    def __init__(self):
        self.alive = 0
        self.most_alive = 0
        self._refs = []  # (a weak reference must stay alive for its callback to run)

    def _gone(self, reference):
        self.alive -= 1

    def loads(self, text):
        event = self.Event(json.loads(text))
        self._refs.append(weakref.ref(event, self._gone))
        self.alive += 1
        self.most_alive = max(self.most_alive, self.alive)
        return event

    def __getattr__(self, name):
        return getattr(json, name)


class TestReadingALargeLog:
    """6,000 events are enough to tell a stream from a read of everything: streamed, a few events are alive at once (the slowest five and the
    one being read); holding every event, all 6,000 would be."""

    @pytest.fixture
    def counted(self, monkeypatch):
        from drspec.core import trace_report

        counted = _CountedJson()
        monkeypatch.setattr(trace_report, "json", counted)
        return counted

    def _big(self, directory: Path, events: int):
        with open(log_of(directory), "w") as handle:
            for i in range(events):
                handle.write(json.dumps(_row("request", float(i % 977), t=f"2099-01-01T00:{(i // 60) % 60:02d}:{i % 60:02d}.000+00:00", op="execute", sql="SELECT " + "x" * 60, cid=f"c{i % 50}", n=i)) + "\n")

    def test_the_summary_streams_instead_of_holding_every_event(self, tmp_path, counted):
        self._big(tmp_path, 6000)
        data = trace_summary(tmp_path / "contracts.db", slowest=5)
        assert data["events_read"] == 6000 and data["summary"][0]["count"] == 6000
        assert len(data["slowest"]) == 5 and data["slowest"][0]["ms"] == 976.0
        assert counted.most_alive <= 10, counted.most_alive  # (measured 7: the slowest five, the one read, one more; holding every event: 6000)

    def test_the_numbers_match_a_plain_computation(self, tmp_path):
        self._big(tmp_path, 1200)
        data = trace_summary(tmp_path / "contracts.db", slowest=3)
        values = sorted(float(i % 977) for i in range(1200))
        row = data["summary"][0]
        assert row["total_ms"] == sum(values) and row["max_ms"] == values[-1]
        assert row["median_ms"] == (values[599] + values[600]) / 2 and row["p95_ms"] == values[math.ceil(0.95 * 1200) - 1]

    def test_a_cid_and_the_tail_stream_too(self, tmp_path, counted):
        self._big(tmp_path, 3000)
        path = trace_summary(tmp_path / "contracts.db", cid="c7")["path"]
        tail = trace_tail(tmp_path / "contracts.db", 3)
        assert len(path) == 60 and tail["events_total"] == 3000 and [e["ms"] for e in tail["events"]] == [float(n % 977) for n in (2997, 2998, 2999)]
        assert counted.most_alive <= 65, counted.most_alive  # (measured 62: the 60 events of one request are kept for its path; holding every event: 3000)


class TestMemoryAndCpu:
    def test_a_process_reports_peak_memory_and_cpu_seconds(self, fake_usage):
        assert tracing.process_usage() == {"max_rss_mb": 200.0, "cpu_s": 1.75}  # (KiB on Linux, user plus system)

    def test_a_snapshot_reports_memory_files_and_load(self, trace_dir):
        (trace_dir / "contracts.db").write_bytes(b"x" * 2_000_000)
        (trace_dir / "contracts.db.wal").write_bytes(b"x" * 500_000)
        snapshot = tracing.system_snapshot()
        assert snapshot["rss_mb"] > 5 and snapshot["mem_avail_mb"] > 0 and snapshot["load1"] >= 0
        assert snapshot["db_mb"] == pytest.approx(1.91, abs=0.01) and snapshot["wal_mb"] == pytest.approx(0.48, abs=0.01)

    def test_what_cannot_be_read_is_left_out_not_zero(self, tmp_path):
        tracing.bind(tmp_path / "missing.db")
        snapshot = tracing.system_snapshot()
        assert "db_mb" not in snapshot and "wal_mb" not in snapshot and "rss_mb" in snapshot

    def test_the_embedding_process_reports_its_start_chunks_and_exit(self, memdb, trace_dir, monkeypatch, fake_usage):
        from drspec.db import insert_artifact
        from drspec.db.queries import add_usage_pending, fold_usage_pending

        monkeypatch.setattr(embedder, "load_model", lambda threads: None)
        monkeypatch.setattr(embedder, "embed_text", lambda text: [0.5] * 384)
        monkeypatch.setattr(embedder, "model_is_cached", lambda: True)
        monkeypatch.setattr(embedding_runner.os, "nice", lambda n: 0)
        monkeypatch.setattr(embedding_runner, "pause_reason", lambda settings: None)
        connection = memdb
        set_setting(connection, "trace.level", "all")
        insert_artifact(
            connection, function_id="a.py::f", file_path="a.py", function_name="f", signature="def f():",
            body="def f(): pass", code_hash="h", language="python", start_line=1, end_line=2,
        )
        add_usage_pending(connection, [("node", "a.py::f", "", 1.0)])
        fold_usage_pending(connection, 1000.0, 7.0)

        class Real(_Direct):
            def store_embeddings(self, results):
                from drspec.core.embedding_backlog import store_embeddings

                return store_embeddings(self._conn, results)

        assert embedding_runner.run_embedding_process(Real(connection), sleep=lambda s: None) == 0
        by_kind = {e["kind"]: e for e in _events(trace_dir) if e["event"] == "embed"}
        assert "rss_mb" in by_kind["start"] and "rss_mb" in by_kind["chunk"]
        assert by_kind["exit"]["code"] == 0 and by_kind["exit"]["max_rss_mb"] == 200.0 and by_kind["exit"]["cpu_s"] == 1.75


class _Loopback:
    """A client of `server` in this process: a request goes through `_handle_request` as it does after the socket."""

    def __init__(self, server):
        self._server = server
        self._session = _Session()

    def _send(self, message):
        return self._server._handle_request(self._session, message)

    def execute(self, sql, params=None):
        reply = self._send({"op": "execute", "sql": sql, "params": encode_rows([params or []])[0]})
        if not reply["ok"]:
            raise RuntimeError(reply["error"])
        return _WorkerCursor(decode_rows(reply.get("rows", [])))

    def begin(self):
        self._send({"op": "begin"})

    def commit(self):
        self._send({"op": "commit"})

    def rollback(self):
        self._send({"op": "rollback"})


class TestATraceSettingAppliesAtOnce:
    @pytest.fixture
    def served(self, memdb, tmp_path):
        server = WorkerIPCServer(memdb, tmp_path / "contracts.db", remediate_fn=lambda *a: 0)  # in this process, never started
        tracing.init_process("worker", honor_env=False)
        tracing.bind(tmp_path / "contracts.db")
        return server

    def test_writing_and_resetting_the_level_takes_effect_without_waiting_for_a_pass(self, served):
        from drspec.core.settings import reset_setting

        client = _Loopback(served)
        assert tracing.level() == "off"
        set_setting(client, "trace.level", "all")
        assert tracing.level() == "all"
        set_setting(client, "trace.level", "slow")
        set_setting(client, "trace.slow_ms", 40)
        assert tracing.level() == "slow" and tracing._slow_ms == 40.0
        reset_setting(client, "trace.level")
        assert tracing.level() == "off"

    def test_other_settings_do_not_touch_the_trace(self, served):
        client = _Loopback(served)
        set_setting(client, "trace.level", "all")
        set_setting(client, "worker.rescan_budget", 3)
        assert tracing.level() == "all"

    def test_the_reply_to_the_write_already_carries_the_new_level(self, served):
        reply = served._handle_request(
            _Session(),
            {"op": "execute", "sql": "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
             "params": ["setting:trace.level", '"all"']},
        )
        assert reply["ok"] and reply["trace"]["level"] == "all"
