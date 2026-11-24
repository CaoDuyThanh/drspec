"""The staleness worker: what one pass decides, how the loop paces itself, and the one PID file that makes it a singleton.

Most of this is decisions, so it is tested as decisions: an in-memory DuckDB with the schema (no folder, no scan), the
re-scan itself replaced by a recorder (`remediation`) and, for the loop, everything `run_worker` reaches outside itself
replaced by `_World` (no database, no socket, no model, no waiting: the clock moves only when something sleeps). A real
thing is used only where it is the objective, and the test says so: a file's `stat` against its stored snapshot, a walk of
a real folder, the PID file, a race between real processes, and `TestARealPass` / `TestARealWorker` (the re-scan through
the real parser and publish, and a worker process stopped by `stop_worker`).
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from drspec import staleness_worker
from drspec.core.scanner import Scanner
from drspec.core.settings import SETTINGS
from drspec.db.connection import init_schema
from drspec.db.queries import (
    enqueue_staleness,
    enqueue_staleness_many,
    list_scanned_files,
    list_staleness_queue,
    next_rescan_batch,
    record_rescan_failure,
    settle_scanned_files,
    upsert_scanned_file,
)
from drspec.staleness_worker import (
    _detect_drift,
    _scan_waiting,
    acquire_singleton,
    detection_due,
    get_worker_status,
    is_worker_running,
    release_singleton,
    run_one_pass,
    run_worker,
    spawn_worker_if_not_running,
    stop_worker,
)
from tests.fakes import FakeTime
from tests.helpers import fid, table_counts
from tests.helpers import publish as sync_scan_results

pytestmark = pytest.mark.usefixtures("no_git")  # no test here starts git (a listing of a project asks it)

#: A database path outside any `_drspec/` folder: the project root is unknown, so discovery is off and nothing is read from it.
NO_ROOT = Path("contracts.db")


def _settings(**overrides):
    """Default settings with `worker.rescan_budget=...`-style overrides (dots written as `__`)."""
    values = {key: setting.default for key, setting in SETTINGS.items()}
    values.update({key.replace("__", "."): value for key, value in overrides.items()})
    return values


@pytest.fixture
def conn():
    """An empty project database in memory."""
    connection = duckdb.connect(":memory:")
    init_schema(connection)
    yield connection
    connection.close()


@pytest.fixture
def remediation(monkeypatch):
    """The re-scan replaced by a recorder that settles what it is given, as a publish does (off the queue, snapshot stored)."""
    log = SimpleNamespace(batches=[], seconds=[])

    def fake(connection, db_path, batch, pass_seconds=None):
        log.batches.append(list(batch))
        log.seconds.append(pass_seconds)
        root = Path(db_path).parent.parent  # the project folder, as the worker has it (absolute paths in `batch` ignore it)
        for path in batch:
            if (root / path).exists():
                stat = (root / path).stat()
                upsert_scanned_file(connection, path, stat.st_mtime, stat.st_size)
        settle_scanned_files(connection, batch, root=root)
        return len(batch)

    monkeypatch.setattr(staleness_worker, "_remediate_batch", fake)
    return log


def _pass(connection, db_path=NO_ROOT, detect=True, **overrides):
    return run_one_pass(connection, db_path, _settings(**overrides), detect=detect)


def _queue(connection):
    return [row[0] for row in connection.execute("SELECT file_path FROM staleness_queue ORDER BY flagged_at, file_path").fetchall()]


@pytest.fixture
def tracked(conn, tmp_path):
    """One file on disk and its stored snapshot, as a scan leaves them (an absolute path: no working folder to move)."""
    path = tmp_path / "a.py"
    path.write_text("def foo():\n    return 1\n")
    stat = path.stat()
    upsert_scanned_file(conn, str(path), stat.st_mtime, stat.st_size)
    return path


@pytest.mark.unit
class TestDetectDrift:
    """The objective is the comparison of the real `stat` of a file with its stored snapshot, so there is one real file."""

    def test_no_drift_when_file_unchanged(self, conn, tracked):
        assert _detect_drift(conn) == []

    def test_a_different_size_is_a_change(self, conn, tracked):
        tracked.write_text("def foo():\n    return 2\n\n\ndef bar():\n    pass\n")
        assert _detect_drift(conn) == [(str(tracked), "size_changed")]

    def test_the_same_size_with_another_modification_time_is_a_change(self, conn, tracked):
        stat = tracked.stat()
        os.utime(tracked, (stat.st_atime, stat.st_mtime + 10))
        assert _detect_drift(conn) == [(str(tracked), "mtime_changed")]

    def test_detects_a_missing_file(self, conn, tracked):
        tracked.unlink()
        assert _detect_drift(conn) == [(str(tracked), "missing")]


@pytest.mark.unit
class TestRunOnePass:
    def test_unchanged_project_detects_and_remediates_nothing(self, conn, tracked, remediation):
        result = _pass(conn)
        assert (result["detected"], result["discovered"], result["remediated"]) == (0, 0, 0) and result["pause_reason"] == "none"
        assert remediation.batches == [[]]  # nothing was handed over (the pass still asks, with an empty batch)

    def test_drift_is_queued_and_handed_to_the_re_scan(self, conn, tracked, remediation):
        tracked.write_text("def foo():\n    return 2\n\n\ndef bar():\n    return foo()\n")
        result = _pass(conn)
        assert (result["detected"], result["remediated"]) == (1, 1)
        assert remediation.batches == [[str(tracked)]]
        assert list_staleness_queue(conn) == []

    def test_a_deleted_file_is_handed_to_the_re_scan_and_forgotten(self, conn, tracked, remediation):
        tracked.unlink()
        result = _pass(conn)
        assert (result["detected"], result["remediated"]) == (1, 1)
        assert remediation.batches == [[str(tracked)]]
        assert list_scanned_files(conn) == [] and list_staleness_queue(conn) == []

    def test_a_previously_queued_file_that_failed_to_remediate_is_retried(self, conn, remediation):
        """A file flagged in an earlier pass must still be remediated by a later one: the queue, not just fresh detection, is the worklist."""
        enqueue_staleness(conn, "a.py", "mtime_changed")
        result = _pass(conn)
        assert result["remediated"] == 1 and remediation.batches == [["a.py"]]
        assert list_staleness_queue(conn) == []


@pytest.mark.unit
class TestBoundedPass:
    """A pass re-scans at most `worker.rescan_budget` files, so a burst of changes can't turn one pass into a long stall."""

    def test_only_the_budgeted_number_of_files_is_rescanned_per_pass(self, conn, remediation):
        enqueue_staleness_many(conn, [(f"f{i}.py", "mtime_changed") for i in range(5)])
        counts = [_pass(conn, worker__rescan_budget=2)["remediated"] for _ in range(4)]
        assert counts == [2, 2, 1, 0]
        assert _queue(conn) == []

    def test_search_queued_files_go_first_then_drift_then_new_files(self, conn, remediation):
        # Queued in the worst order for the rule: new first, search last.
        for name, reason in (("new.py", "new_file"), ("drift.py", "mtime_changed"), ("searched.py", "search_drift")):
            enqueue_staleness_many(conn, [(name, reason)])
        for _ in range(3):
            _pass(conn, worker__rescan_budget=1)
        assert remediation.batches == [["searched.py"], ["drift.py"], ["new.py"]]

    def test_a_failing_file_is_not_read_again_while_it_is_unchanged(self, conn, tmp_path):
        # nothing is timed: a file that failed is read again only once it changes, which is judged from its real `stat`, so there is one real file
        broken = tmp_path / "broken.py"
        broken.write_text("def broken(:\n    return 1\n")
        stat = broken.stat()
        enqueue_staleness_many(conn, [("broken.py", "mtime_changed")])
        record_rescan_failure(conn, "broken.py", "syntax error", stat.st_mtime, stat.st_size, now=1000.0)
        assert next_rescan_batch(conn, 5, now=1000.0 + 30, root=tmp_path) == []  # unchanged: not read again
        assert next_rescan_batch(conn, 5, now=1000.0 + 10**7, root=tmp_path) == []  # however long after: still the same file
        record_rescan_failure(conn, "broken.py", "syntax error", stat.st_mtime, stat.st_size, now=2000.0)
        assert conn.execute("SELECT attempts FROM rescan_backoff").fetchone()[0] == 2
        assert next_rescan_batch(conn, 5, now=2000.0 + 121, root=tmp_path) == []

    def test_editing_a_failing_file_reads_it_at_once(self, conn, tmp_path, remediation, monkeypatch):
        monkeypatch.chdir(tmp_path)
        broken = tmp_path / "broken.py"
        broken.write_text("def broken(:\n    return 1\n")
        stat = broken.stat()
        enqueue_staleness_many(conn, [("broken.py", "mtime_changed")])
        record_rescan_failure(conn, "broken.py", "syntax error", stat.st_mtime, stat.st_size, now=time.time())
        assert next_rescan_batch(conn, 5, now=time.time(), root=tmp_path) == []  # unchanged since it failed: not read again  # pattern-ok: real-time-assert `now` is an argument; an unchanged file stays out whatever the time

        broken.write_text("def broken():\n    return 1\n\n\n# fixed\n")  # (another size: the file is read at once)
        assert next_rescan_batch(conn, 5, now=time.time(), root=tmp_path) == ["broken.py"]  # pattern-ok: real-time-assert the same `now` argument
        assert conn.execute("SELECT COUNT(*) FROM rescan_backoff").fetchone()[0] == 0

        assert _pass(conn)["remediated"] == 1 and remediation.batches == [["broken.py"]]
        assert _queue(conn) == []

    def test_drift_the_worker_finds_does_not_downgrade_a_search_queued_file(self, conn, tracked, remediation):
        tracked.write_text("def foo():\n    return 2\n")  # drifts from its scan snapshot
        enqueue_staleness_many(conn, [(str(tracked), "search_drift")])
        result = _pass(conn, worker__rescan_budget=1)
        assert result["detected"] == 0  # already queued, so not newly detected
        assert result["remediated"] == 1


@pytest.mark.unit
class TestNewFileDiscovery:
    """The objective is the walk of a real folder (what it offers and what it ignores), so there is a small real tree.
    The re-scan is the recorder; the real one is in `TestARealPass`."""

    @pytest.fixture
    def project(self, conn, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)  # paths in the database are relative to the working folder
        (tmp_path / "_drspec").mkdir()
        return tmp_path / "_drspec" / "contracts.db"

    def test_a_file_added_after_the_scan_is_found_and_handed_to_the_re_scan(self, conn, project, remediation):
        Path("added.py").write_text("def added():\n    return 1\n")
        result = _pass(conn, project)
        assert (result["discovered"], result["remediated"]) == (1, 1)
        assert remediation.batches == [["added.py"]]

    def test_the_second_pass_finds_nothing_new(self, conn, project, remediation):
        Path("added.py").write_text("def added():\n    return 1\n")
        _pass(conn, project)
        result = _pass(conn, project)
        assert (result["detected"], result["discovered"], result["remediated"]) == (0, 0, 0)

    def test_discovery_follows_the_scanners_ignore_rules(self, conn, project, remediation):
        Path("node_modules").mkdir()
        Path("node_modules/dep.js").write_text("function dep() {}\n")
        Path("notes.txt").write_text("not a source file\n")
        assert _pass(conn, project)["discovered"] == 0

    def test_a_large_tree_is_covered_over_several_passes(self, conn, project, remediation):
        for i in range(7):
            Path(f"m{i}.py").write_text("")
        found = [_pass(conn, project, worker__discovery_budget=3, worker__rescan_budget=100)["discovered"] for _ in range(4)]
        assert sum(found) == 7
        assert found[0] <= 3

    def test_with_a_database_outside_an_underscore_drspec_folder_discovery_is_off(self, conn, tmp_path, monkeypatch, remediation):
        """Without the usual `<root>/_drspec/` layout there is no way to know which directory is the project, so nothing is guessed."""
        monkeypatch.chdir(tmp_path)
        Path("added.py").write_text("def added():\n    return 1\n")  # it would be found if the root were known
        assert _pass(conn, tmp_path / "elsewhere" / "contracts.db")["discovered"] == 0


# ---- the re-scan itself, through the real parser and the real publish ------------------------------------------------------


@pytest.fixture(scope="module")
def built_project(tmp_path_factory):
    """A project scanned once for the module (a.py with `foo`); each test works on its own copy."""
    root = tmp_path_factory.mktemp("built")
    (root / "a.py").write_text("def foo():\n    return 1\n")
    db_path = root / "_drspec" / "contracts.db"
    db_path.parent.mkdir()
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(root)  # the snapshot is stored under the path relative to the working folder
        connection = duckdb.connect(str(db_path))  # pattern-ok: duckdb-file builds the template FILE that tests copy
        init_schema(connection)
        result = Scanner().scan_file("a.py", relative_to=root)
        sync_scan_results(connection, functions=result.functions, scanned_file_paths={"a.py"}, file_imports=result.file_imports)
        connection.close()
    before = table_counts(db_path)
    yield root
    assert table_counts(db_path) == before, "a test wrote into the template the module copies"


@pytest.fixture
def project(built_project, tmp_path, monkeypatch):
    root = tmp_path / "p"
    shutil.copytree(built_project, root)  # (keeps the times of a.py, so the snapshot still matches it)
    monkeypatch.chdir(root)
    return root / "_drspec" / "contracts.db"


def _real_pass(db_path, **overrides):
    """One pass on a fresh connection, closed at the end so that what the pass wrote is read back from the file."""
    connection = duckdb.connect(str(db_path))  # pattern-ok: duckdb-file a pass publishes a file read back after
    try:
        return run_one_pass(connection, db_path, _settings(**overrides))
    finally:
        connection.close()


def _state(db_path):
    """What the pass left in the database, read through one connection: the function ids, the files with a snapshot, the
    queue and the files waiting out a failure."""
    connection = duckdb.connect(str(db_path), read_only=True)  # pattern-ok: duckdb-file reads the published file back
    try:
        return SimpleNamespace(
            functions={row[0] for row in connection.execute("SELECT function_id FROM artifacts").fetchall()},
            scanned={row[0] for row in connection.execute("SELECT file_path FROM scanned_files").fetchall()},
            queue=[row[0] for row in connection.execute("SELECT file_path FROM staleness_queue ORDER BY file_path").fetchall()],
            backoff={row[0]: row[1:] for row in connection.execute("SELECT file_path, attempts, last_error FROM rescan_backoff").fetchall()},
        )
    finally:
        connection.close()


def _enqueue(db_path, *items):
    connection = duckdb.connect(str(db_path))  # pattern-ok: duckdb-file queues into the file the worker pass reads
    try:
        enqueue_staleness_many(connection, list(items))
    finally:
        connection.close()


@pytest.mark.component
class TestARealPass:
    """What no recorder can show: that the batch a pass hands over really is parsed and published (one project database
    file, one publish per test). One test per way in."""

    def test_a_real_content_change_is_detected_and_remediated(self, project):
        Path("a.py").write_text("def foo():\n    return 2\n\n\ndef bar():\n    return foo()\n")
        result = _real_pass(project)
        state = _state(project)
        assert (result["detected"], result["remediated"]) == (1, 1)
        assert state.functions == {fid("a.py", "foo"), fid("a.py", "bar")} and state.queue == []

    def test_a_deleted_file_is_detected_and_its_artifacts_removed(self, project):
        Path("a.py").unlink()
        result = _real_pass(project)
        state = _state(project)
        assert (result["detected"], result["remediated"]) == (1, 1)
        assert (state.functions, state.scanned, state.queue) == (set(), set(), [])

    def test_a_file_added_after_the_scan_is_found_and_scanned_without_a_manual_scan(self, project):
        Path("added.py").write_text("def added():\n    return 1\n")
        result = _real_pass(project)
        state = _state(project)
        assert (result["discovered"], result["remediated"]) == (1, 1)
        assert fid("added.py", "added") in state.functions and "added.py" in state.scanned

    def test_an_unparseable_file_does_not_stop_the_others(self, project):
        Path("broken.py").write_bytes(b"\xff\xfe not text \x00")  # cannot be read as source at all
        Path("good.py").write_text("def good():\n    return 1\n")
        _enqueue(project, ("broken.py", "mtime_changed"), ("good.py", "mtime_changed"))

        assert _real_pass(project)["remediated"] == 1  # the good one; the broken one stays queued
        state = _state(project)
        assert fid("good.py", "good") in state.functions and state.queue == ["broken.py"]
        attempts, error = state.backoff["broken.py"]
        assert attempts == 1 and error

    def test_a_batch_of_only_unparseable_files_publishes_nothing_and_waits(self, project):
        Path("broken.py").write_bytes(b"\xff\xfe not text \x00")
        _enqueue(project, ("broken.py", "mtime_changed"))
        assert _real_pass(project)["remediated"] == 0
        state = _state(project)
        assert state.queue == ["broken.py"] and state.backoff["broken.py"][0] == 1

    def test_a_failed_file_that_is_edited_is_scanned_at_once_and_leaves_the_backoff(self, project):
        Path("broken.py").write_text("def broken(:\n    return 1\n")
        stat = Path("broken.py").stat()
        connection = duckdb.connect(str(project))  # pattern-ok: duckdb-file the backoff of a file the pass publishes
        enqueue_staleness_many(connection, [("broken.py", "mtime_changed")])
        record_rescan_failure(connection, "broken.py", "syntax error", stat.st_mtime, stat.st_size, now=time.time())  # it just failed
        connection.close()

        Path("broken.py").write_text("def fixed():\n    return 1\n\n\n# fixed\n")  # edited: not made to wait out its minute
        assert _real_pass(project)["remediated"] == 1
        state = _state(project)
        assert state.backoff == {} and state.queue == []


# ---- the status the worker reports -----------------------------------------------------------------------------------------


class _Keep:
    """A connection that stays open when the code under test closes it."""

    def __init__(self, connection):
        self._connection = connection

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._connection, name)


def _status_world(monkeypatch, tmp_path, connection, pid=None):
    """What `get_worker_status` reaches, replaced: the database file (an empty placeholder that must exist), the connections
    to it (the in-memory one) and whether a worker is up."""
    db_path = tmp_path / "contracts.db"
    db_path.touch()
    handle = _Keep(connection)
    monkeypatch.setattr(staleness_worker, "get_connection", lambda path, read_only=False: handle)
    monkeypatch.setattr(staleness_worker, "get_worker_connection", lambda path, read_only=False, check_build=True: handle)
    monkeypatch.setattr(staleness_worker, "is_worker_running", lambda path: pid)
    return db_path


@pytest.mark.unit
class TestWorkerStatusBacklog:
    def test_reports_what_is_waiting(self, conn, tmp_path, monkeypatch):
        enqueue_staleness_many(conn, [("a.py", "mtime_changed"), ("b.py", "new_file"), ("c.py", "new_file")])
        record_rescan_failure(conn, "a.py", "boom", None, None, now=time.time())
        backlog = get_worker_status(_status_world(monkeypatch, tmp_path, conn))["backlog"]
        assert backlog == {"files_pending_rescan": 1, "files_pending_discovery": 2, "files_in_retry_wait": 0, "files_unreadable": 0}  # a.py is not on disk: it is pending, not parked


@pytest.mark.unit
class TestGetWorkerStatus:
    def test_a_worker_of_the_current_build_is_not_reported_as_outdated(self, conn, tmp_path, monkeypatch):
        monkeypatch.setattr(staleness_worker, "peek_worker_build", lambda path: "build-1")
        monkeypatch.setattr(staleness_worker, "build_id", lambda: "build-1")
        status = get_worker_status(_status_world(monkeypatch, tmp_path, conn, pid=4242))
        assert status["running"] is True and status["pid"] == 4242
        assert status["outdated"] is False and "note" not in status

    def test_a_worker_started_before_an_upgrade_is_reported_as_outdated(self, conn, tmp_path, monkeypatch):
        monkeypatch.setattr(staleness_worker, "peek_worker_build", lambda path: "build-1")
        monkeypatch.setattr(staleness_worker, "build_id", lambda: "build-2")
        status = get_worker_status(_status_world(monkeypatch, tmp_path, conn, pid=4242))
        assert status["outdated"] is True and "drspec worker stop" in status["note"]

    def test_a_missing_database_still_answers(self, tmp_path):
        status = get_worker_status(tmp_path / "nope" / "contracts.db")
        status.pop("model_cache")
        assert status == {"running": False, "pid": None, "staleness_queue_size": None, "backlog": None, "embedding": None}


@pytest.mark.component
class TestGetWorkerStatusOfARealProject:
    """The objective is the read of a real database file (and, when a worker is up, its real PID file), so there is one."""

    def test_not_running_reports_the_queue_size_from_a_direct_read(self, project):
        status = get_worker_status(project)
        assert status.pop("model_cache")["folder"]  # where the model is kept; covered in test_model_cache
        assert status == {
            "running": False, "pid": None, "staleness_queue_size": 0,
            "backlog": {"files_pending_rescan": 0, "files_pending_discovery": 0, "files_in_retry_wait": 0, "files_unreadable": 0},
            "embedding": {"enabled": True, "embedded": 0, "total": 1, "top_n": 2000, "backlog": 0, "state": "idle", "message": "0 of 1 embedded (top_n = 2000): the others are not found by meaning"},  # (one function in the project)
        }

    @pytest.mark.integration  # (asks the socket of a worker: there is none, and it says so)
    def test_running_reports_the_pid(self, project):
        assert acquire_singleton(project) is True
        try:
            status = get_worker_status(project)
            assert status["running"] is True and status["pid"] == os.getpid()
        finally:
            release_singleton(project)


# ---- the PID file ------------------------------------------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    """Only the folder of the database matters to the singleton: it holds the PID file."""
    return tmp_path / "contracts.db"


@pytest.mark.unit
class TestSingleton:
    def test_acquire_then_running_then_release(self, db_path):
        assert is_worker_running(db_path) is None
        assert acquire_singleton(db_path) is True
        assert is_worker_running(db_path) == os.getpid()
        assert acquire_singleton(db_path) is False  # already held by this (alive) process
        release_singleton(db_path)
        assert is_worker_running(db_path) is None

    def test_a_stale_pid_file_from_a_crashed_worker_is_reclaimed(self, db_path):
        (db_path.parent / "staleness_worker.pid").write_text("999999999")  # not a real PID
        assert is_worker_running(db_path) is None
        assert acquire_singleton(db_path) is True
        assert is_worker_running(db_path) == os.getpid()
        release_singleton(db_path)

    def test_release_without_acquire_is_a_no_op(self, db_path):
        assert release_singleton(db_path) is None  # must not raise
        assert is_worker_running(db_path) is None and not (db_path.parent / "staleness_worker.pid").exists()  # and creates nothing

    def test_a_losing_concurrent_acquire_backs_off_instead_of_both_winning(self, db_path, monkeypatch):
        """Forces the exact interleaving that used to let two callers both win: the old code staked the PID file's existence
        with `os.open(O_CREAT|O_EXCL)` and filled in its content with a separate `os.write()`, so a concurrent
        `is_worker_running()` could land in between, read an empty file, and misread a legitimate in-progress acquire as
        stale. The fix writes the PID to a temp file first and `os.link()`s it into place, making existence and content
        atomic. This test pauses caller A right after it writes its temp file (before linking), lets caller B legitimately
        win the real path, then resumes A and asserts A's own link fails and it backs off instead of also believing it won."""
        real_link = staleness_worker.os.link
        a_paused = threading.Event()
        resume_a = threading.Event()

        def _pausing_link(src, dst):
            a_paused.set()
            resume_a.wait(timeout=30)
            return real_link(src, dst)

        monkeypatch.setattr(staleness_worker.os, "link", _pausing_link)
        a_result = []
        a_thread = threading.Thread(target=lambda: a_result.append(acquire_singleton(db_path)))
        a_thread.start()
        assert a_paused.wait(timeout=30)  # A has written its temp file, paused right before linking

        monkeypatch.setattr(staleness_worker.os, "link", real_link)
        assert acquire_singleton(db_path) is True  # A holds only a temp file, so B's acquire legitimately wins first
        resume_a.set()
        a_thread.join(timeout=30)

        assert a_result == [False]  # A must not also believe it won
        assert is_worker_running(db_path) == os.getpid()
        release_singleton(db_path)


class _Handle:
    def close(self):
        pass


_IDLE = {"detected": 0, "discovered": 0, "remediated": 0, "pause": 0.0, "pause_reason": "none"}


class _World:
    """Everything `run_worker` reaches outside itself, replaced: the PID file, the database and its layout, the IPC server and
    the loopback to it, the embedding supervisor, the settings, one pass of work and the wait between passes."""

    def __init__(self, monkeypatch):
        self.clock = FakeTime()
        self.waits = []  # the seconds each wait was asked for
        self.wakes = iter(())  # what each wait answers (woken or not), then False
        self.script = []  # what each pass does, in order: a result dict, or an exception to raise; then idle
        self.passes = []  # the `detect` flag of each pass
        self.ticks = []  # what the embedding supervisor was given on each pass: (connection, db_path, settings)
        self.can_acquire = True
        self.after_first_wait = None  # something that happens while the worker sleeps for the first time
        self.max_waits = None  # a loop that waits more often than this is spinning: the wait raises
        self.acquired, self.released, self.opened = [], [], []
        self.servers = []  # the IPC servers `run_worker` made
        self.events = []  # what happened, in order, for the tests of an order (a batch, an exit)
        self.settings = _settings()
        def no_op(*args, **kwargs):
            return None
        def patch(name, value):
            monkeypatch.setattr(staleness_worker, name, value)
        patch("time", self.clock)
        patch("acquire_singleton", self._acquire)
        def release_singleton(db_path):
            self.released.append(db_path)
            self.events.append("exit")
        patch("release_singleton", release_singleton)
        for name in ("sweep_orphaned_staging", "ensure_embedding_layout", "ensure_fk_layout", "ensure_identity_layout",
                     "clear_startup_error", "init_schema", "requeue_for_new_facts", "requeue_outdated_partial_files"):
            patch(name, no_op)
        monkeypatch.setattr(duckdb, "connect", lambda path, *args, **kwargs: self.opened.append(path) or _Handle())
        patch("WorkerIPCServer", self._make_server)
        patch("connect_loopback", lambda path: _Handle())
        patch("EmbeddingSupervisor", lambda: _Supervisor(self.ticks))
        patch("read_all_settings", lambda connection: self.settings)
        patch("scanning_now", lambda connection: False)
        patch("run_one_pass", self._run_one_pass)
        patch("_wait_for_wake", self._wait_for_wake)

    def _make_server(self, *args, **kwargs):
        self.servers.append(_Server(*args, **kwargs))
        return self.servers[-1]

    def _acquire(self, db_path):
        self.acquired.append(db_path)
        return self.can_acquire

    def _run_one_pass(self, connection, db_path, settings=None, detect=True):
        self.passes.append(detect)
        step = self.script.pop(0) if self.script else _IDLE
        if isinstance(step, Exception):
            raise step
        if callable(step):  # a batch the test writes out: it can do things while it is "in flight"
            return step()
        return step

    def _wait_for_wake(self, event, seconds):
        self.waits.append(seconds)
        if self.max_waits is not None and len(self.waits) > self.max_waits:
            raise RuntimeError(f"the loop is spinning: {len(self.waits)} waits")
        self.clock.now += seconds
        if self.after_first_wait is not None and len(self.waits) == 1:
            self.after_first_wait()
        return next(self.wakes, False)


class _Server:
    def __init__(self, *args, **kwargs):
        self.wake = threading.Event()
        self.restart_requested = threading.Event()  # a client of another build asked for a replacement (`restart_when_idle`)

    def start(self):
        pass

    def stop(self):
        pass

    def compact_if_needed(self, *args):
        pass


class _Supervisor:
    def __init__(self, ticks):
        self._ticks = ticks

    def tick(self, *args):
        self._ticks.append(args)

    def stop(self):
        pass


@pytest.fixture
def world(monkeypatch):
    return _World(monkeypatch)


@pytest.mark.unit
class TestRunWorkerLoop:
    def test_exits_after_idling_with_nothing_to_do(self, world):
        run_worker(NO_ROOT, idle_timeout=0.3, poll_interval=0.1)  # returns only once idle
        assert world.waits == [0.1, 0.1, 0.1]
        assert world.released == [NO_ROOT]  # PID file released on the way out

    def test_backs_off_to_the_dormant_interval_while_idle(self, world):
        """After `dormant_after` seconds of nothing detected/remediated, each pass should wait `dormant_poll_interval` instead
        of the fast `poll_interval`, to keep idle CPU usage low."""
        run_worker(NO_ROOT, idle_timeout=1.0, poll_interval=0.2, dormant_after=0.5, dormant_poll_interval=0.9)
        # First waits use the fast interval; once accumulated idle time crosses dormant_after, later waits use the dormant one.
        assert world.waits[0] == pytest.approx(0.2)
        assert world.waits[-1] == pytest.approx(0.9)

    def test_an_idle_worker_waits_instead_of_spinning(self, world):
        """The objective is a statement about the loop: with nothing to do it asks to sleep for the poll interval and makes one pass per
        interval. An hour of the worker's own time (the clock is fake: nothing really sleeps) is 360 passes of 10 s, not thousands; a
        loop that skipped its sleep would pass for ever, and the wait refuses to be asked more than 1,000 times."""
        world.max_waits = 1000
        run_worker(NO_ROOT, idle_timeout=3600, poll_interval=10, dormant_after=10**9, dormant_poll_interval=300)
        assert len(world.passes) == 360
        assert world.waits == [10] * 360

    def test_a_worker_asked_to_replace_itself_finishes_the_batch_in_flight_and_only_then_exits(self, world):
        """A client of another build asks for a restart while a batch is being published: the batch ends first, then the worker exits
        (no pass is started after it). Events order it; no clock."""
        def batch():
            world.events.append("batch in flight")
            world.servers[0].restart_requested.set()  # the client's request arrives during the batch
            world.events.append("batch published")
            return {**_IDLE, "remediated": 3}

        world.script = [batch]
        run_worker(NO_ROOT, idle_timeout=100, poll_interval=0.1)
        assert world.events == ["batch in flight", "batch published", "exit"]
        assert len(world.passes) == 1 and world.released == [NO_ROOT]

    def test_a_scan_somebody_waits_for_is_finished_whole_before_the_worker_replaces_itself(self, world, monkeypatch):
        waiting = {"now": True}
        monkeypatch.setattr(staleness_worker, "scanning_now", lambda connection: waiting["now"])

        def first_batch():
            world.events.append("batch 1 published")
            world.servers[0].restart_requested.set()  # asked after the first of the scan's batches
            return {**_IDLE, "remediated": 800}

        def last_batch():
            world.events.append("batch 2 published")
            waiting["now"] = False  # the scan is complete: nobody waits for it now
            return {**_IDLE, "remediated": 400}

        world.script = [first_batch, last_batch]
        run_worker(NO_ROOT, idle_timeout=100, poll_interval=0.1)
        assert world.events == ["batch 1 published", "batch 2 published", "exit"]  # the whole scan, then the exit
        assert len(world.passes) == 2

    def test_an_idle_worker_asked_to_replace_itself_exits_without_another_pass(self, world):
        def idle_pass():
            world.servers[0].restart_requested.set()
            return _IDLE

        world.script = [idle_pass]
        run_worker(NO_ROOT, idle_timeout=100, poll_interval=0.1)
        assert world.events == ["exit"] and len(world.passes) == 1

    def test_a_client_request_cuts_a_dormant_sleep_short(self, world):
        """The wait returns early when something wakes the worker; that counts as activity, so the next wait is the normal
        interval, not dormant."""
        world.wakes = iter([False, False, True, False])  # idle, idle (now dormant), woken, idle
        run_worker(NO_ROOT, idle_timeout=1.0, poll_interval=0.2, dormant_after=0.3, dormant_poll_interval=0.5)
        assert world.waits[:4] == [pytest.approx(0.2), pytest.approx(0.2), pytest.approx(0.5), pytest.approx(0.2)]

    def test_a_pass_with_more_queued_goes_on_after_the_pause_it_asks_for(self, world):
        world.script = [{**_IDLE, "remediated": 3, "pause": 0.25, "pause_reason": "yield"}]
        run_worker(NO_ROOT, idle_timeout=0.2, poll_interval=0.1)
        assert world.waits[0] == pytest.approx(0.25) and world.passes[0] is True

    def test_the_settings_are_read_on_every_pass_so_a_change_applies_without_a_restart(self, world):
        """The embedding supervisor is given the settings of the pass it ticks on: turning embedding off while the worker sleeps
        reaches it on the next pass."""
        def a_client_turns_it_off():
            world.settings = {**world.settings, "embedding.enabled": False}  # (a new dict: each pass is handed its own)

        world.after_first_wait = a_client_turns_it_off
        world.settings = {**world.settings, "embedding.enabled": True}
        run_worker(NO_ROOT, idle_timeout=0.3, poll_interval=0.1)
        assert [tick[2]["embedding.enabled"] for tick in world.ticks] == [True, False, False]

    def test_a_single_bad_pass_does_not_kill_the_worker(self, world, caplog):
        """An uncaught exception from `run_one_pass` must cost one pass, not kill the whole loop (nothing else supervises this
        worker process), and must still be logged at ERROR every time rather than swallowed silently."""
        world.script = [RuntimeError("simulated transient failure")]
        with caplog.at_level("ERROR", logger="drspec.staleness_worker"):
            run_worker(NO_ROOT, idle_timeout=0.3, poll_interval=0.1)

        assert len(world.passes) > 1  # the loop kept going past the raising pass
        assert world.released == [NO_ROOT]  # PID file still released cleanly
        error_records = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(error_records) == 1
        assert "simulated transient failure" in caplog.text

    def test_does_nothing_if_another_worker_is_already_running(self, world):
        world.can_acquire = False
        run_worker(NO_ROOT, idle_timeout=0.1, poll_interval=0.05)  # returns at once
        assert world.acquired == [NO_ROOT]
        assert world.opened == [] and world.passes == []  # no database opened, no pass made
        assert world.released == []  # untouched: the PID file belongs to the other worker


def _database_outside_a_project(built_project, tmp_path, monkeypatch):
    """A copy of the built database in a folder that is not `_drspec/` (the worker has no project to walk, so it never asks git),
    with `a.py` beside it and the working folder there, so that the snapshot matches and the worker finds nothing to do."""
    folder = tmp_path / "elsewhere"
    folder.mkdir()
    shutil.copy2(built_project / "a.py", folder / "a.py")  # (keeps its times)
    shutil.copy(built_project / "_drspec" / "contracts.db", folder / "contracts.db")
    monkeypatch.chdir(folder)
    return folder / "contracts.db"


@pytest.mark.integration
class TestARealWorker:
    def test_the_real_loop_serves_the_database_and_leaves_when_idle(self, built_project, tmp_path, monkeypatch):
        """The one run of the loop with nothing replaced (the others in `TestRunWorkerLoop` replace what it reaches): the real
        database, the real IPC server and the real loopback, and the PID file released on the way out."""
        db_path = _database_outside_a_project(built_project, tmp_path, monkeypatch)
        run_worker(db_path, idle_timeout=0.3, poll_interval=0.1)  # returns only once idle
        assert is_worker_running(db_path) is None


@pytest.mark.unit
class TestStopWorker:
    @pytest.fixture
    def clock(self, monkeypatch, fake_time):
        monkeypatch.setattr(staleness_worker, "time", fake_time)
        return fake_time

    def _running(self, monkeypatch, answers):
        """`is_worker_running` answers from `answers` (then the last one forever); `os.kill` records what it is asked."""
        killed = []
        remaining = list(answers)
        monkeypatch.setattr(staleness_worker, "is_worker_running", lambda path: remaining.pop(0) if len(remaining) > 1 else remaining[0])
        monkeypatch.setattr(staleness_worker.os, "kill", lambda pid, sig: killed.append((pid, sig)))
        return killed

    def test_nothing_running_reports_no_pid_and_a_clean_exit(self, db_path):
        assert stop_worker(db_path) == (None, True)

    def test_waits_until_the_worker_has_released_its_pid_file(self, db_path, clock, monkeypatch):
        killed = self._running(monkeypatch, [4242, 4242, 4242, 4242, None])  # up when asked, still up for three looks, then gone
        assert stop_worker(db_path) == (4242, True)
        assert killed == [(4242, signal.SIGTERM)]
        assert len(clock.slept) == 3  # it looked again after each sleep, and returned as soon as the file was gone

    def test_reports_not_exited_when_the_worker_outlives_the_timeout(self, db_path, clock, monkeypatch):
        killed = self._running(monkeypatch, [4242])  # never goes away
        assert stop_worker(db_path, timeout=0.2) == (4242, False)
        assert killed == [(4242, signal.SIGTERM)]
        assert sum(clock.slept) >= 0.2  # it waited the whole timeout, on the fake clock


@pytest.mark.unit
class TestSpawnWorkerIfNotRunning:
    @pytest.fixture
    def spawned(self, monkeypatch):
        """`subprocess.Popen` replaced by a recorder, and the PID file by "no worker up"."""
        calls = []
        monkeypatch.setattr(staleness_worker.subprocess, "Popen", lambda *args, **kwargs: calls.append((args, kwargs)))
        monkeypatch.setattr(staleness_worker, "is_worker_running", lambda path: None)
        monkeypatch.delenv("DRSPEC_DISABLE_AUTO_WORKER", raising=False)
        return calls

    def test_starts_a_detached_worker_for_the_database(self, spawned):
        spawn_worker_if_not_running(NO_ROOT)
        ((args, kwargs),) = spawned
        assert args[0][-4:] == ["--db", str(NO_ROOT), "worker", "run"]
        assert kwargs["start_new_session"] is True and kwargs["stdout"] == subprocess.DEVNULL

    def test_disabled_by_env_var_does_not_spawn(self, spawned, monkeypatch):
        monkeypatch.setenv("DRSPEC_DISABLE_AUTO_WORKER", "1")
        spawn_worker_if_not_running(NO_ROOT)
        assert spawned == []

    def test_does_not_spawn_a_second_worker_if_one_is_already_running(self, spawned, monkeypatch):
        monkeypatch.setattr(staleness_worker, "is_worker_running", lambda path: 4242)
        spawn_worker_if_not_running(NO_ROOT)  # must see the existing PID and do nothing
        assert spawned == []

    def test_a_launch_that_fails_is_not_an_error(self, monkeypatch, spawned):
        tried = []

        def refuse(*args, **kwargs):
            tried.append(1)
            raise OSError("no such interpreter")

        monkeypatch.setattr(staleness_worker.subprocess, "Popen", refuse)
        assert spawn_worker_if_not_running(NO_ROOT) is None  # best effort: must not raise
        assert tried == [1]  # it did try, once, and gave up quietly


@pytest.mark.unit
class TestHowABatchIsCut:
    """A batch of the background drain ends after `worker.pass_seconds` of parsing; a scan someone waits for is cut by the
    number of files only, because a file that takes longer than that to read would end every batch after one file and
    each batch pays the whole fixed cost of a publish."""

    def _batches(self, conn, remediation, reason, count=1, **budgets):
        enqueue_staleness_many(conn, [(f"f{i}.py", reason) for i in range(count)])
        _pass(conn, detect=False, **{f"worker__{key}": value for key, value in budgets.items()})
        return remediation

    def test_a_scan_that_is_waited_for_has_no_time_cut(self, conn, remediation):
        log = self._batches(conn, remediation, "scan")
        assert (log.batches, log.seconds) == ([["f0.py"]], [None])  # pattern-ok: real-time-assert compares the recorded arguments of a fake

    def test_the_background_drain_keeps_its_time_cut(self, conn, remediation):
        log = self._batches(conn, remediation, "mtime_changed")
        assert (log.batches, log.seconds) == ([["f0.py"]], [SETTINGS["worker.pass_seconds"].default])  # pattern-ok: real-time-assert compares the recorded arguments of a fake

    def test_a_scan_is_still_cut_by_the_number_of_files(self, conn, remediation):
        assert [len(b) for b in self._batches(conn, remediation, "scan", 30, scan_budget=10).batches] == [10]

    def test_a_scan_is_cut_by_the_scan_budget_and_not_by_the_background_one(self, conn, remediation):
        assert [len(b) for b in self._batches(conn, remediation, "scan", 30, scan_budget=25, rescan_budget=10).batches] == [25]

    def test_a_background_batch_is_cut_by_the_background_budget_and_not_by_the_scan_one(self, conn, remediation):
        assert [len(b) for b in self._batches(conn, remediation, "mtime_changed", 30, scan_budget=25, rescan_budget=10).batches] == [10]

    def test_the_default_scan_budget_holds_a_large_project_in_one_batch(self):
        spec = SETTINGS["worker.scan_budget"]
        assert spec.default >= 3000 and spec.default > SETTINGS["worker.rescan_budget"].default
        assert spec.maximum >= spec.default


@pytest.mark.unit
class TestAScanDoesNotWaitOutTheWakeGap:
    def test_the_worker_can_tell_that_a_scan_is_waiting(self, conn):
        assert _scan_waiting(conn) is False
        enqueue_staleness_many(conn, [("a.py", "mtime_changed")])
        assert _scan_waiting(conn) is False  # background work waits out the gap
        enqueue_staleness_many(conn, [("b.py", "scan")])
        assert _scan_waiting(conn) is True

    def test_a_failed_question_means_keep_the_gap(self):
        class Broken:
            def execute(self, *args, **kwargs):
                raise RuntimeError("connection lost")

        assert _scan_waiting(Broken()) is False


@pytest.mark.unit
class TestDetectionWhileAScanWaits:
    def test_the_first_pass_always_detects(self):
        assert detection_due(None, False, 60.0, True) is True
        assert detection_due(None, False, 60.0, False) is True

    def test_a_pass_for_a_waiting_scan_does_not_repeat_the_sweep(self):
        assert detection_due(100.0, True, 60.0, True, now=101.0) is False

    def test_the_sweep_is_still_due_afterwards(self):
        assert detection_due(100.0, True, 60.0, False, now=101.0) is True  # a request cut the sleep short: look for changes now
        assert detection_due(100.0, False, 60.0, False, now=101.0) is False  # nothing due yet
        assert detection_due(100.0, False, 60.0, False, now=170.0) is True  # the interval ran out
