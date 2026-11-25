"""The re-scan queue is drained promptly, what the user asked for first, and the index says when it is incomplete."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.core import tracing
from drspec.core.discovery import discover_new_files
from drspec.core.index_notice import NOTE, queue_if_unindexed
from drspec.core.settings import SETTINGS, read_all_settings
from drspec.core.trace_report import trace_tail
from drspec.db import init_schema
from drspec.db.queries import enqueue_staleness_many, index_completeness, list_staleness_queue, next_rescan_batch
from drspec.staleness_worker import _pause_after, _remediate_queued_files, run_one_pass, run_worker
from tests.helpers import file_hash, folder_listing, table_counts

runner = CliRunner()


@pytest.fixture(scope="module")
def _empty_database(tmp_path_factory):
    """An initialised, empty database file, made once for the module; each test copies it."""
    path = tmp_path_factory.mktemp("empty") / "contracts.db"
    conn = duckdb.connect(str(path))  # pattern-ok: duckdb-file the template FILE the tests copy
    init_schema(conn)
    conn.execute("CHECKPOINT")
    conn.close()
    before = file_hash(path)
    yield path
    assert file_hash(path) == before, "a test wrote into the file the module copies"


@pytest.fixture
def empty_project(tmp_path, monkeypatch, _empty_database):
    """The working folder of a project whose database file is initialised and empty (a copy of the module's)."""
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / "_drspec" / "contracts.db"
    db_path.parent.mkdir()
    shutil.copyfile(_empty_database, db_path)
    return db_path


@pytest.fixture
def memory_project(tmp_path, monkeypatch):
    """The same project with its database in memory: for the tests that only check what SQL and the queue functions do."""
    monkeypatch.chdir(tmp_path)
    conn = duckdb.connect(":memory:")
    init_schema(conn)
    yield SimpleNamespace(root=tmp_path, conn=conn)
    conn.close()


def _files(root: Path, count: int) -> None:
    for i in range(count):
        (root / f"m{i:03d}.py").write_text(f"def fn_{i}(x):\n    return x + {i}\n")


class TestTheDrain:
    def test_each_batch_is_time_boxed_and_the_rest_stays_queued(self, empty_project):
        root = empty_project.parent.parent
        _files(root, 30)
        paths = sorted(p.name for p in root.glob("m*.py"))
        conn = duckdb.connect(str(empty_project))  # pattern-ok: duckdb-file a pass publishes a file and reads it back (a plain connection does not see the published file)
        try:
            assert _remediate_queued_files(conn, empty_project, paths, pass_seconds=0.0) == 1  # at least one, then it stops  # pattern-ok: real-time-assert `pass_seconds` is an argument (a budget given to the function), not a measured time
            assert _remediate_queued_files(conn, empty_project, paths[1:], pass_seconds=None) == 29  # pattern-ok: real-time-assert an argument, not a measured time
        finally:
            conn.close()

    def test_a_pass_reports_what_is_left_and_why_it_pauses(self, empty_project):
        root = empty_project.parent.parent
        _files(root, 40)
        conn = duckdb.connect(str(empty_project))  # pattern-ok: duckdb-file same
        try:
            settings = {**read_all_settings(conn), "worker.rescan_budget": 10}
            enqueue_staleness_many(conn, [(f"m{i:03d}.py", "new_file") for i in range(40)])
            first = run_one_pass(conn, empty_project, settings, detect=False)
        finally:
            conn.close()
        assert first["remediated"] == 10
        assert first["pause_reason"] in {"yield", "cap", "load", "memory"} and first["pause"] > 0
        for _ in range(10):  # a plain connection does not see the published file; reopen for each pass
            conn = duckdb.connect(str(empty_project))  # pattern-ok: duckdb-file same
            try:
                if not list_staleness_queue(conn):
                    last = run_one_pass(conn, empty_project, settings, detect=False)
                    break
                run_one_pass(conn, empty_project, settings, detect=False)
            finally:
                conn.close()
        assert last["remediated"] == 0 and last["pause_reason"] == "none" and last["pause"] == 0 and last["queued_after"] == 0

    def test_a_killed_worker_continues_from_the_persistent_queue_without_parsing_again(self, empty_project):
        root = empty_project.parent.parent
        _files(root, 20)
        conn = duckdb.connect(str(empty_project))  # pattern-ok: duckdb-file same
        settings = {**read_all_settings(conn), "worker.rescan_budget": 8}
        enqueue_staleness_many(conn, [(f"m{i:03d}.py", "new_file") for i in range(20)])
        first = run_one_pass(conn, empty_project, settings, detect=False)
        assert first["remediated"] == 8 and first["queued_after"] == 12
        conn.close()  # the worker dies here
        conn = duckdb.connect(str(empty_project))  # pattern-ok: duckdb-file same
        try:
            queued = list_staleness_queue(conn)
            assert len(queued) == 12 and not {f"m{i:03d}.py" for i in range(8)} & set(queued)
            assert run_one_pass(conn, empty_project, settings, detect=False)["remediated"] == 8
        finally:
            conn.close()

    def test_the_pass_event_carries_the_queue_the_gap_and_the_reason(self, empty_project):
        root = empty_project.parent.parent
        _files(root, 12)
        conn = duckdb.connect(str(empty_project))  # pattern-ok: duckdb-file same
        try:
            tracing.bind(empty_project)
            tracing.configure("all", 500, 20)
            settings = {**read_all_settings(conn), "worker.rescan_budget": 5}
            enqueue_staleness_many(conn, [(f"m{i:03d}.py", "new_file") for i in range(12)])
            run_one_pass(conn, empty_project, settings, detect=False)
        finally:
            conn.close()
        (event,) = [e for e in trace_tail(empty_project)["events"] if e["event"] == "pass"]
        assert event["remediated"] == 5 and event["queued_after"] >= 7 and event["gap_ms"] > 0 and event["pause_reason"] in {"yield", "cap", "load", "memory"}

    def test_a_queue_is_drained_back_to_back_and_detection_keeps_its_own_cadence(self, empty_project, monkeypatch):
        """With a queue to work through, the worker goes on after the short yield of each batch and does NOT wait for the detection interval (3 s here,
        60 s by default); the stat sweep and the walk run once per interval. Nothing here is timed: the clock of the loop moves only when the loop
        asks to wait (a fake clock, `_monotonic`), and each wait is recorded and returns at once. So the drains take no fake time, and the
        detection is due only after the waits the loop asks for add up to the interval; a slow machine changes nothing."""
        root = empty_project.parent.parent
        _files(root, 40)
        calls, waits = [], []
        clock = {"now": 0.0}  # the fake clock, in seconds: it moves only inside the fake wait
        import drspec.staleness_worker as worker_module

        real = worker_module.run_one_pass

        def counting(conn, db_path, settings=None, detect=True):
            calls.append(detect)
            return real(conn, db_path, settings, detect)

        def fake_wait(event, seconds):
            waits.append(seconds)
            clock["now"] += seconds  # the loop asked for this much time: it has passed, and nothing is slept
            return False

        monkeypatch.setattr(worker_module, "run_one_pass", counting)
        monkeypatch.setattr(worker_module, "_monotonic", lambda: clock["now"])
        monkeypatch.setattr(worker_module, "pause_reason", lambda settings: None)  # the machine running the tests may be busy
        monkeypatch.setattr(worker_module, "_wait_for_wake", fake_wait)
        conn = duckdb.connect(str(empty_project))  # pattern-ok: duckdb-file same
        init_schema(conn)
        from drspec.core.settings import set_setting

        set_setting(conn, "worker.rescan_budget", 5)
        conn.close()
        run_worker(empty_project, idle_timeout=0.5, poll_interval=3)
        # The first pass detects (and finds the files); the seven that drain them (40 files, 5 a batch) do not detect again; each is followed by the
        # short yield, never by the 3 s interval.
        assert calls[:8] == [True] + [False] * 7
        assert waits[:7] == [0.05] * 7
        conn = duckdb.connect(str(empty_project), read_only=True)  # pattern-ok: duckdb-file same
        try:
            assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone() == (40,)  # every file is indexed
            assert list_staleness_queue(conn) == []
        finally:
            conn.close()


def _artifacts_through_worker(db_path: Path) -> int:
    """The count of stored functions, asked of the running worker (it holds the file)."""
    from drspec.worker_ipc import WorkerIPCError, connect_loopback

    try:
        connection = connect_loopback(db_path, timeout=0.5)
    except (WorkerIPCError, OSError):
        return -1
    try:
        return connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
    finally:
        connection.close()


class TestThePauseRules:
    @pytest.fixture(autouse=True)
    def _quiet_machine(self, monkeypatch):
        monkeypatch.setattr("drspec.staleness_worker.pause_reason", lambda settings: None)

    BASE = {"worker.cpu_percent": 20, "worker.yield_ms": 50, "embedding.max_load_per_core": 1.0, "embedding.min_available_memory_mb": 0}

    def test_nothing_queued_means_no_pause(self):
        assert _pause_after(self.BASE, 1.0, 1.0, more_queued=False) == (0.0, "none")

    def test_a_short_yield_is_the_floor(self, monkeypatch):
        monkeypatch.setattr(os, "cpu_count", lambda: 64)
        assert _pause_after(self.BASE, 0.5, 0.5, more_queued=True) == (0.05, "yield")

    def test_a_core_above_the_cap_slows_the_drain_to_the_cap(self, monkeypatch):
        monkeypatch.setattr(os, "cpu_count", lambda: 1)
        pause, reason = _pause_after(self.BASE, 1.0, 1.0, more_queued=True)  # 1 s of CPU in 1 s on one core is 100%
        assert reason == "cap" and pause == pytest.approx(4.0)  # to average 20%, wait until 5 s have passed in all

    def test_the_cap_is_a_share_of_the_whole_machine(self, monkeypatch):
        monkeypatch.setattr(os, "cpu_count", lambda: 12)
        assert _pause_after(self.BASE, 1.0, 1.0, more_queued=True)[1] == "yield"  # one core of 12 is 8%

    @pytest.mark.parametrize("message,reason", [("machine busy", "load"), ("low memory", "memory")])
    def test_a_busy_or_short_machine_pauses_for_a_while(self, monkeypatch, message, reason):
        monkeypatch.setattr("drspec.staleness_worker.pause_reason", lambda settings: message)
        pause, why = _pause_after(self.BASE, 0.1, 0.1, more_queued=True)
        assert why == reason and pause >= 1.0

    def test_the_settings_exist_with_the_proposed_defaults(self):
        assert SETTINGS["worker.cpu_percent"].default == 20 and SETTINGS["worker.yield_ms"].default == 50
        assert SETTINGS["worker.pass_seconds"].default == 1.0 and SETTINGS["worker.rescan_budget"].default == 200  # pattern-ok: real-time-assert compares a setting's default value, nothing is timed
        assert SETTINGS["worker.recent_days"].default == 14.0


class TestTheOrder:
    def test_a_file_edited_lately_comes_before_an_old_one_even_when_it_sorts_last(self, memory_project):
        root, conn = memory_project.root, memory_project.conn
        for name, age_days in (("a_old.py", 100), ("b_old.py", 90), ("z_recent.py", 0)):
            path = root / name
            path.write_text("def f():\n    return 1\n")
            when = time.time() - age_days * 86400
            os.utime(path, (when, when))
        discover_new_files(conn, root, 100, recent_days=14)
        assert next_rescan_batch(conn, 10, time.time(), root) == ["z_recent.py", "a_old.py", "b_old.py"]  # pattern-ok: real-time-assert `time.time()` is the `now` the function is given, the answer does not depend on how long it takes

    def test_what_the_user_asked_for_comes_before_new_files(self, memory_project):
        conn = memory_project.conn
        enqueue_staleness_many(conn, [("a.py", "new_file_recent"), ("b.py", "new_file"), ("c.py", "mtime_changed"), ("d.py", "search_unscanned")])
        assert next_rescan_batch(conn, 10, time.time()) == ["d.py", "c.py", "a.py", "b.py"]  # pattern-ok: real-time-assert the same `now` argument

    def test_the_backlog_counts_both_kinds_of_new_file(self, memory_project):
        from drspec.db.queries import get_rescan_backlog

        conn = memory_project.conn
        enqueue_staleness_many(conn, [("a.py", "new_file_recent"), ("b.py", "new_file"), ("c.py", "mtime_changed")])
        assert get_rescan_backlog(conn, time.time())["files_pending_discovery"] == 2  # pattern-ok: real-time-assert the same `now` argument


class TestAskingForAFileThatIsNotIndexedYet:
    def test_a_lookup_of_an_unindexed_file_queues_it_first(self, memory_project):
        (memory_project.root / "late.py").write_text("def late():\n    return 1\n")
        conn = memory_project.conn
        queue_if_unindexed(conn, "late.py::late#12345678")
        assert conn.execute("SELECT reason FROM staleness_queue WHERE file_path = 'late.py'").fetchall() == [("search_unscanned",)]

    def test_a_missing_unsupported_or_already_scanned_file_is_not_queued(self, memory_project):
        root, conn = memory_project.root, memory_project.conn
        (root / "notes.txt").write_text("x")
        (root / "known.py").write_text("def f():\n    return 1\n")
        conn.execute("INSERT INTO scanned_files (file_path, mtime, size) VALUES ('known.py', 1.0, 1)")
        for identifier in ("nowhere.py::f", "notes.txt::f", "known.py::f", "::f", "plain"):
            queue_if_unindexed(conn, identifier)
        assert conn.execute("SELECT COUNT(*) FROM staleness_queue").fetchone() == (0,)

    def test_after_a_pass_the_second_try_finds_the_function(self, empty_project):
        """The lookup queues the file first; the worker's next pass reads it, and the same lookup then finds the function. (It was a real worker and
        three command processes; the worker's pass is `run_one_pass`, which is what it runs.)"""
        root = empty_project.parent.parent
        (root / "late.py").write_text("def late():\n    return 1\n")
        conn = duckdb.connect(str(empty_project))  # pattern-ok: duckdb-file a pass publishes a file: the file is the objective here
        try:
            queue_if_unindexed(conn, "late.py::late#12345678")
            run_one_pass(conn, empty_project, read_all_settings(conn), detect=False)
        finally:
            conn.close()
        conn = duckdb.connect(str(empty_project), read_only=True)  # pattern-ok: duckdb-file (a plain connection does not see the published file: reopened)
        try:
            assert conn.execute("SELECT function_name FROM artifacts").fetchall() == [("late",)]
            assert list_staleness_queue(conn) == []
        finally:
            conn.close()


@pytest.fixture(scope="module")
def _scanned_project(tmp_path_factory, initialised_template):
    """A project with one file, initialised (a copy of the session's) and scanned once for the module; each test works on a copy."""
    root = tmp_path_factory.mktemp("noted")
    shutil.copytree(initialised_template, root, dirs_exist_ok=True)
    (root / "a.py").write_text("def alpha():\n    return 1\n")
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(root)
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
    before = (table_counts(root / "_drspec" / "contracts.db"), folder_listing(root))
    yield root
    assert (table_counts(root / "_drspec" / "contracts.db"), folder_listing(root)) == before, "a test wrote into the template the module copies"


class TestTheIncompleteIndexNote:
    @pytest.fixture
    def project(self, _scanned_project, tmp_path, monkeypatch):
        root = tmp_path / "project"
        shutil.copytree(_scanned_project, root)
        monkeypatch.chdir(root)
        return root

    def _queue(self, project, names):
        for name in names:
            (project / name).write_text("def beta():\n    return 2\n")
        conn = duckdb.connect(str(project / "_drspec" / "contracts.db"))  # pattern-ok: duckdb-file queues into the copied project's FILE, which the CLI reads
        enqueue_staleness_many(conn, [(name, "new_file") for name in names])
        conn.close()

    def test_a_search_says_how_many_files_are_not_scanned_yet(self, project):
        self._queue(project, ["b.py", "c.py"])
        data = json.loads(runner.invoke(app, ["--json", "search", "alpha"]).output)["data"]
        assert data["index"] == {"pending": 2, "total": 3}
        assert data["note"] == NOTE.format(pending=2, total=3)
        text = runner.invoke(app, ["search", "alpha"]).output
        assert "Index incomplete: 2 of 3 files are not scanned yet" in text

    def test_every_search_mode_carries_it(self, project):
        self._queue(project, ["b.py"])
        for args in (["--lines"], ["--count"], ["--files-with-matches"]):
            data = json.loads(runner.invoke(app, ["--json", "search", "alpha", *args]).output)["data"]
            assert data["index"]["pending"] == 1, args

    def test_source_get_and_deps_get_carry_it_too(self, project):
        self._queue(project, ["b.py"])
        for args in (["source", "get"], ["deps", "get"]):
            data = json.loads(runner.invoke(app, ["--json", *args, "a.py::alpha"]).output)["data"]
            assert data["index"] == {"pending": 1, "total": 2}, args

    def test_nothing_is_said_when_the_queue_is_empty(self, project):
        data = json.loads(runner.invoke(app, ["--json", "search", "alpha"]).output)["data"]
        assert "index" not in data and "Index incomplete" not in json.dumps(data)
        assert "Index incomplete" not in runner.invoke(app, ["search", "alpha"]).output

    def test_files_that_only_drifted_are_not_counted_as_unscanned(self, project):
        conn = duckdb.connect(str(project / "_drspec" / "contracts.db"))  # pattern-ok: duckdb-file same
        enqueue_staleness_many(conn, [("a.py", "mtime_changed")])
        assert index_completeness(conn) == {"pending": 0, "total": 1}
        conn.close()

    def test_the_mcp_search_carries_it(self, project):
        from drspec import mcp_server

        self._queue(project, ["b.py"])
        reply = mcp_server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search", "arguments": {"pattern": "alpha", "format": "json"}}},
            project / "_drspec" / "contracts.db",
        )
        assert json.loads(reply["result"]["content"][0]["text"])["index"] == {"pending": 1, "total": 2}
