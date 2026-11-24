"""A worker that outlives an upgrade is noticed, reported and replaced."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest
from typer.testing import CliRunner

from drspec import worker_ipc
from drspec.cli.app import app
from drspec.core import build_id as build_module
from drspec.core.trace_report import trace_summary, trace_tail
from drspec.db import init_schema
from drspec.db.staging import STAGING_PREFIX
from drspec.staleness_worker import get_worker_status, run_worker
from drspec.worker_ipc import (
    get_worker_connection,
    worker_is_outdated,
)

runner = CliRunner()

OLD_BUILD = "0.0.1+layout1.oldcode"
CURRENT_BUILD = "9.9.9+layout3.fixedfortests"


class TestTheIdentifier:
    def test_it_names_the_version_and_the_layout(self, monkeypatch):
        from drspec import __version__

        monkeypatch.delenv(build_module.BUILD_OVERRIDE_ENV)  # (the session pins the identifier: this test is about the real one)
        build_module.reset_for_tests()
        try:
            assert build_module.build_id().startswith(f"{__version__}+layout{build_module.LAYOUT_NUMBER}.")
        finally:
            monkeypatch.undo()  # the pin is back before the real identifier is forgotten, so the next test cannot see it
            build_module.reset_for_tests()

    def test_it_is_stable_within_a_process(self):
        assert build_module.build_id() == build_module.build_id()

    def test_a_changed_source_file_changes_it(self, tmp_path, monkeypatch):
        package = tmp_path / "pkg"
        package.mkdir()
        (package / "a.py").write_text("x = 1\n")
        (package / "schema.sql").write_text("select 1;\n")
        monkeypatch.setattr(build_module, "__file__", str(package / "core" / "build_id.py"))
        (package / "core").mkdir()
        first = build_module._fingerprint()
        assert build_module._fingerprint() == first
        (package / "a.py").write_text("x = 22\n")
        assert build_module._fingerprint() != first

    def test_a_new_file_changes_it(self, tmp_path, monkeypatch):
        package = tmp_path / "pkg"
        (package / "core").mkdir(parents=True)
        (package / "a.py").write_text("x = 1\n")
        monkeypatch.setattr(build_module, "__file__", str(package / "core" / "build_id.py"))
        first = build_module._fingerprint()
        (package / "b.py").write_text("y = 1\n")
        assert build_module._fingerprint() != first


class _WorkersOfThisProcess:
    """Workers that answer for a given build, without a process or a socket file: each `_try_connect` is given the client end of a fake socket pair whose
    other end is served by a `WorkerIPCServer` of this process that says it runs the next build of `builds` (the last one for ever)."""

    def __init__(self, monkeypatch, builds):
        from tests.fakes import fake_socket_pair

        self.builds = list(builds)
        self.connects = 0
        self.stopped = []
        self.busy = False  # a worker found now is publishing a batch (its flag is set, as `_op_remediate` sets it)
        self.servers = []
        self._ends = []
        self._threads = []
        memory = duckdb.connect(":memory:")
        init_schema(memory)
        self._memory = memory

        def connect(path, timeout=None):
            build = self.builds[min(self.connects, len(self.builds) - 1)]
            self.connects += 1
            server = worker_ipc.WorkerIPCServer(memory, Path("contracts.db"), remediate_fn=lambda *args: 0)
            self.servers.append(server)
            if self.busy:
                server._publishing.set()
            real = server._dispatch
            server._dispatch = lambda session, request: {"ok": True, "build": build} if request.get("op") == "hello" and not request.get("internal") else real(session, request)
            client_end, server_end = fake_socket_pair()
            thread = threading.Thread(target=server._handle_client, args=(server_end,), daemon=True)
            thread.start()
            self._threads.append(thread)
            self._ends += [client_end, server_end]
            return client_end

        monkeypatch.setattr(worker_ipc, "_try_connect", connect)
        monkeypatch.setattr(worker_ipc, "_restart_attempted", set())
        monkeypatch.setattr("drspec.staleness_worker.stop_worker", lambda db_path: (self.stopped.append(db_path), (4242, True))[1])
        # an idle worker that was asked to replace itself exits at once: its PID file is gone (it is `stopped`, as a SIGTERM did)
        monkeypatch.setattr("drspec.staleness_worker.wait_for_worker_exit", lambda db_path, timeout=10.0: (self.stopped.append(db_path), True)[1])
        monkeypatch.setattr("drspec.staleness_worker.spawn_worker_if_not_running", lambda db_path: None)
        monkeypatch.setattr(worker_ipc, "_POLL_INTERVAL_SECONDS", 0.0)

    def finish(self):
        for end in self._ends:
            end.close()
        for thread in self._threads:
            thread.join(5)
        self._memory.close()


@pytest.fixture
def workers(monkeypatch, tmp_path):
    """`workers(*builds)`: the workers that answer, in order (see `_WorkersOfThisProcess`); the build of "now" is a fixed name."""
    monkeypatch.setenv(build_module.BUILD_OVERRIDE_ENV, CURRENT_BUILD)
    monkeypatch.setattr(build_module, "_cached", None)
    monkeypatch.delenv("DRSPEC_DISABLE_AUTO_WORKER", raising=False)
    made = []

    def make(*builds):
        made.append(_WorkersOfThisProcess(monkeypatch, builds))
        return made[-1]

    yield make
    for one in made:
        one.finish()


class TestAnOutdatedWorker:
    def test_status_reports_it_and_leaves_it_running(self, workers, tmp_path, monkeypatch):
        faked = workers(OLD_BUILD)
        db_path = tmp_path / "_drspec" / "contracts.db"
        monkeypatch.setattr("drspec.staleness_worker.is_worker_running", lambda path: 4242)
        assert worker_is_outdated(db_path)  # (it is recognised)
        status = get_worker_status(db_path)
        assert status["running"] is True
        assert status["outdated"] is True
        assert status["build"] == OLD_BUILD and status["current_build"] == build_module.build_id()
        assert "drspec worker stop" in status["note"]
        assert faked.stopped == []  # asking changed nothing

    def test_it_is_replaced_only_once_per_process(self, workers, tmp_path):
        """Two installs sharing a project must not keep replacing each other's worker."""
        faked = workers(OLD_BUILD)
        db_path = tmp_path / "_drspec" / "contracts.db"
        worker_ipc._restart_attempted.add(str(db_path.resolve()))
        connection = get_worker_connection(db_path)
        try:
            assert connection.worker_build() == OLD_BUILD
        finally:
            connection.close()
        assert faked.stopped == []

    def test_a_worker_too_old_to_report_a_build_counts_as_outdated(self, workers, tmp_path):
        workers("")
        assert worker_is_outdated(tmp_path / "_drspec" / "contracts.db") is True

    def test_a_command_replaces_it_before_sending_its_request(self, workers, tmp_path):
        faked = workers(OLD_BUILD, CURRENT_BUILD)  # the first connection finds the old worker, the next one the new
        db_path = tmp_path / "_drspec" / "contracts.db"
        connection = get_worker_connection(db_path)
        try:
            assert connection.execute("SELECT 41 + 1").fetchone() == (42,)
            assert connection.worker_build() == build_module.build_id()
        finally:
            connection.close()
        assert faked.stopped == [db_path]  # the old one was stopped, once, before the request went out
        assert not worker_is_outdated(db_path)


class TestABusyWorkerIsNotCut:
    """A worker with a batch in flight, or a scan that somebody waits for, is not stopped by a command of a newer build: it
    finishes, and replaces itself at its next idle moment. The command is answered by it meanwhile."""

    def test_a_command_goes_on_with_the_worker_that_is_publishing_and_does_not_stop_it(self, workers, tmp_path):
        faked = workers(OLD_BUILD)
        faked.busy = True  # the batch is held open: its publishing flag is set until the test lets go
        db_path = tmp_path / "_drspec" / "contracts.db"
        connection = get_worker_connection(db_path)
        try:
            assert connection.execute("SELECT 41 + 1").fetchone() == (42,)  # answered by the old worker, from the committed state
            assert connection.worker_build() == OLD_BUILD  # still the same worker
        finally:
            connection.close()
        assert faked.stopped == [] and faked.connects == 1  # nothing was stopped and no second worker was looked for
        assert faked.servers[0].restart_requested.is_set()  # it knows it is to be replaced when it is idle

    def test_an_idle_worker_is_replaced_at_once(self, workers, tmp_path):
        faked = workers(OLD_BUILD, CURRENT_BUILD)
        db_path = tmp_path / "_drspec" / "contracts.db"
        connection = get_worker_connection(db_path)
        try:
            assert connection.worker_build() == build_module.build_id()
        finally:
            connection.close()
        assert faked.servers[0].restart_requested.is_set() and faked.stopped == [db_path] and faked.connects == 2

    def test_the_request_answers_busy_only_while_something_is_in_flight_or_waited_for(self, schemadb_server):
        server = schemadb_server
        assert server._dispatch(worker_ipc._Session(), {"op": "restart_when_idle"}) == {"ok": True, "busy": False}
        server._publishing.set()
        assert server._dispatch(worker_ipc._Session(), {"op": "restart_when_idle"}) == {"ok": True, "busy": True}
        server._publishing.clear()
        server._conn.execute("INSERT INTO staleness_queue (file_path, flagged_at, reason) VALUES ('a.py', now(), 'scan')")  # a scan is waited for
        assert server._dispatch(worker_ipc._Session(), {"op": "restart_when_idle"})["busy"] is True

    def test_a_worker_too_old_to_know_the_request_is_stopped_as_before(self, workers, tmp_path, monkeypatch):
        faked = workers(OLD_BUILD, CURRENT_BUILD)
        monkeypatch.setattr(worker_ipc.WorkerConnection, "request_restart", lambda self: (_ for _ in ()).throw(worker_ipc.WorkerIPCError("Unknown operation: 'restart_when_idle'")))
        db_path = tmp_path / "_drspec" / "contracts.db"
        connection = get_worker_connection(db_path)
        try:
            assert connection.worker_build() == build_module.build_id()
        finally:
            connection.close()
        assert faked.stopped == [db_path]  # SIGTERM, the old way (wait_for_worker_exit was not used)


@pytest.fixture
def schemadb_server():
    memory = duckdb.connect(":memory:")
    init_schema(memory)
    yield worker_ipc.WorkerIPCServer(memory, Path("contracts.db"), remediate_fn=lambda *args: 0)
    memory.close()


class TestInitReplacesAnOutdatedWorker:
    def _initialised(self, tmp_path):
        result = runner.invoke(app, ["--json", "init", "--project-root", str(tmp_path), "--no-ide"])
        assert result.exit_code == 0, result.output

    def _reinit(self, tmp_path, running, outdated, stop):
        state = {"running": running}

        def _is_running(_db):
            return 4242 if state["running"] else None

        def _stop(_db):
            state["running"] = False
            return 4242, True

        with patch("drspec.cli.commands.init.is_worker_running", side_effect=_is_running), \
                patch("drspec.cli.commands.init.worker_is_outdated", return_value=outdated), \
                patch("drspec.cli.commands.init.stop_worker", side_effect=_stop if stop is None else stop) as stopped, \
                patch("drspec.cli.commands.init.ensure_embedding_layout", return_value="current") as rebuilt, \
                patch("drspec.cli.commands.init.is_initialized_database", return_value=True):
            result = runner.invoke(app, ["--json", "init", "--project-root", str(tmp_path), "--no-ide"])
        return result, stopped, rebuilt

    def test_an_outdated_worker_is_stopped_and_the_rebuild_runs(self, tmp_path):
        self._initialised(tmp_path)
        result, stopped, rebuilt = self._reinit(tmp_path, running=True, outdated=True, stop=None)
        assert result.exit_code == 0, result.output
        assert stopped.call_count == 1 and rebuilt.call_count == 1

    def test_a_current_worker_is_left_alone(self, tmp_path):
        self._initialised(tmp_path)
        result, stopped, rebuilt = self._reinit(tmp_path, running=True, outdated=False, stop=None)
        assert result.exit_code == 0, result.output
        assert stopped.call_count == 0 and rebuilt.call_count == 0

    def test_a_worker_that_cannot_be_stopped_is_an_error_that_says_how_to_stop_it(self, tmp_path):
        self._initialised(tmp_path)

        def refuse(_db):
            raise PermissionError("not yours")

        result, _stopped, rebuilt = self._reinit(tmp_path, running=True, outdated=True, stop=refuse)
        assert result.exit_code == 1
        assert "drspec worker stop" in result.output and rebuilt.call_count == 0


class TestTheStartSweepsAfterTheRebuild:
    def test_a_working_copy_left_by_a_killed_rebuild_is_removed(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        db_path = tmp_path / "_drspec" / "contracts.db"
        db_path.parent.mkdir()
        conn = duckdb.connect(str(db_path))  # pattern-ok: duckdb-file a real worker's project FILE
        init_schema(conn)
        conn.close()
        leftover = db_path.parent / f"{STAGING_PREFIX}1234-abcd"
        leftover.mkdir()
        (leftover / "old.db").write_bytes(b"x" * 100)

        # Its owner still looks alive at the first sweep, and is gone by the time the rebuild is done.
        calls = {"n": 0}

        def alive(_pid):
            calls["n"] += 1
            return calls["n"] == 1

        with patch("drspec.db.staging._process_alive", side_effect=alive), \
                patch("drspec.staleness_worker.ensure_embedding_layout", return_value="migrated"), \
                patch("drspec.staleness_worker._wait_for_wake", return_value=False):  # (the loop's idle waits are not slept)
            run_worker(db_path, idle_timeout=0.2, poll_interval=0.1)
        assert not leftover.exists()


class TestMemoryAndCpuAreShown:
    def _write(self, tmp_path, rows):
        from drspec.core import tracing

        log = tracing.log_file_for(tmp_path / "contracts.db")
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def _row(self, event, ms, **fields):
        return {"t": "2099-01-01T00:00:00.000+00:00", "pid": 1, "proc": "cli", "event": event, "ms": ms, **fields}

    def test_the_summary_reports_a_peak_and_median_memory_and_total_cpu(self, tmp_path):
        self._write(tmp_path, [
            self._row("command", 10, command="search", max_rss_mb=100.0, cpu_s=0.5),
            self._row("command", 12, command="search", max_rss_mb=300.0, cpu_s=1.5),
            self._row("command", 14, command="search", max_rss_mb=200.0, cpu_s=1.0),
            self._row("pass", 5, rss_mb=80.0, load1=0.4),
            self._row("request", 1, op="execute"),
        ])
        by_name = {row["event"]: row for row in trace_summary(tmp_path / "contracts.db")["summary"]}
        assert by_name["command search"]["peak_rss_mb"] == 300.0
        assert by_name["command search"]["median_rss_mb"] == 200.0
        assert by_name["command search"]["total_cpu_s"] == 3.0
        assert by_name["pass"]["peak_rss_mb"] == 80.0 and "total_cpu_s" not in by_name["pass"]
        assert "peak_rss_mb" not in by_name["request execute"]

    def test_the_tail_and_the_slowest_show_the_fields(self, tmp_path):
        self._write(tmp_path, [
            self._row("command", 10, command="search", max_rss_mb=100.0, cpu_s=0.5),
            self._row("pass", 5, rss_mb=80.0, db_mb=12.0, wal_mb=0.0, load1=0.4, mem_avail_mb=4000.0),
        ])
        events = {event["event"]: event for event in trace_tail(tmp_path / "contracts.db")["events"]}
        assert events["command"]["max_rss_mb"] == 100.0 and events["command"]["cpu_s"] == 0.5
        assert events["pass"]["rss_mb"] == 80.0 and events["pass"]["load1"] == 0.4
        assert {"db_mb", "wal_mb", "mem_avail_mb"} <= events["pass"].keys()
        slowest = trace_summary(tmp_path / "contracts.db")["slowest"]
        assert slowest[0]["max_rss_mb"] == 100.0


class TestADeeplyNestedProject:
    """`<project>/_drspec/worker.sock` can exceed what a Unix socket path allows."""

    def test_a_short_path_keeps_the_socket_beside_the_database(self, tmp_path):
        assert worker_ipc.socket_path(tmp_path / "_drspec" / "contracts.db") == tmp_path / "_drspec" / "worker.sock"

    def test_a_long_path_moves_the_socket_to_a_short_place_every_process_derives(self, tmp_path, tmp_path_factory, monkeypatch):
        # The system temporary folder of this test is a SHORT folder of the session (it was `tmp_path / "sys-tmp"`: under the long test folder, whose
        # length depends on the session number, `pytest-1801` against `pytest-999`: the test failed on a machine that had run pytest enough times).
        system_tmp = tmp_path_factory.mktemp("sys")
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(system_tmp))
        db_path = tmp_path / ("a" * 60) / ("b" * 60) / "_drspec" / "contracts.db"
        db_path.parent.mkdir(parents=True)
        path = worker_ipc.socket_path(db_path)
        assert path.parent == system_tmp and len(os.fsencode(str(path))) <= worker_ipc._MAX_SOCKET_PATH_BYTES  # it fits a socket path
        assert path == worker_ipc.socket_path(db_path)
        other = tmp_path / ("a" * 60) / ("c" * 60) / "_drspec" / "contracts.db"
        other.parent.mkdir(parents=True)
        assert worker_ipc.socket_path(other) != path

    def test_a_path_with_no_short_place_left_is_a_clear_error(self, tmp_path, monkeypatch):
        db_path = tmp_path / ("a" * 120) / "_drspec" / "contracts.db"
        db_path.parent.mkdir(parents=True)
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / ("t" * 120)))
        with pytest.raises(worker_ipc.WorkerIPCError, match="too long"):
            worker_ipc.socket_path(db_path)
