"""Ctrl-C on a scan returns at once, while a batch is being read: the worker does not make the client wait for the batch,
stops reading the cancelled scan's files, and takes them off the queue when the batch ends."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest

from drspec.core import scan_service
from drspec.core.scanner import Scanner
from drspec.db import init_schema
from drspec.staleness_worker import _remediate_queued_files
from drspec.worker_ipc import WorkerIPCServer


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for index in range(12):
        (tmp_path / f"m{index:02d}.py").write_text(f"def fn_{index}(x):\n    return x + {index}\n")
    db_path = tmp_path / "_drspec" / "contracts.db"  # (only its folder is looked at, to find the project; the database is in memory)
    db_path.parent.mkdir()
    conn = duckdb.connect(":memory:")
    init_schema(conn)
    yield conn, db_path
    conn.close()


def _spec() -> dict:
    return {"scan_id": scan_service.new_scan_id(), "targets": [], "recursive": True, "full": False, "deep": False,
            "embed": False, "code_only": False, "docs_only": False, "wait": True}


class TestTheWorkerAnswersAtOnce:
    def test_cancel_and_detach_do_not_wait_for_the_batch_and_are_carried_out_when_it_ends(self, project, serve):
        conn, db_path = project
        spec = _spec()
        started = scan_service.start_scan(conn, db_path, spec)
        assert started["queued"] == 12
        files = [row[0] for row in conn.execute("SELECT file_path FROM scan_requests").fetchall()]
        inside, release, batch_over = threading.Event(), threading.Event(), threading.Event()

        def slow_batch(connection, path, file_paths, *rest):
            inside.set()
            release.wait(30)
            batch_over.set()
            return 0

        server = WorkerIPCServer(conn, db_path, remediate_fn=slow_batch)
        served = serve(server)
        try:
            batch = served.client()
            other = served.client()
            worker_thread = threading.Thread(target=lambda: batch.remediate(files))
            worker_thread.start()
            assert inside.wait(10)
            cancelled = other.scan_cancel(spec["scan_id"])
            progress = other.scan_progress(spec["scan_id"])
            other.scan_detach(spec["scan_id"])
            assert not batch_over.is_set()  # the three calls were answered while the batch was still held (a call that waited for it would see it over)
            assert cancelled == {"scan_id": spec["scan_id"], "cancelled": 12, "done": 0, "deferred": True, "publishing": False}
            assert progress["remaining"] == 12 and progress["finished"] is False
            assert scan_service.is_cancelling(spec["scan_id"])
            release.set()
            worker_thread.join(20)
            assert not worker_thread.is_alive()
            # the batch is over: the queue is cleaned up, the record says what was cancelled
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and scan_service.is_cancelling(spec["scan_id"]):
                time.sleep(0.05)
            assert not scan_service.is_cancelling(spec["scan_id"])
            assert other.execute("SELECT COUNT(*) FROM scan_requests").fetchone()[0] == 0
            assert other.execute("SELECT COUNT(*) FROM staleness_queue WHERE reason IN ('scan', 'scan_queued')").fetchone()[0] == 0
            assert scan_service.load_run(conn, spec["scan_id"]) is None or scan_service.load_run(conn, spec["scan_id"])["cancelled"] == 12
            batch.close()
            other.close()
        finally:
            release.set()

    def test_with_no_batch_running_a_cancel_is_done_at_once(self, project, serve):
        conn, db_path = project
        spec = _spec()
        scan_service.start_scan(conn, db_path, spec)
        client = serve(WorkerIPCServer(conn, db_path, remediate_fn=lambda *a: 0)).client()
        result = client.scan_cancel(spec["scan_id"])
        assert result == {"scan_id": spec["scan_id"], "cancelled": 12}  # not deferred
        assert client.execute("SELECT COUNT(*) FROM scan_requests").fetchone()[0] == 0
        assert not scan_service.is_cancelling(spec["scan_id"])
        client.close()


class TestABatchStopsReading:
    def test_files_of_a_cancelled_scan_are_not_read_and_nothing_is_published(self, project):
        conn, db_path = project
        spec = _spec()
        scan_service.start_scan(conn, db_path, spec)
        files = sorted(row[0] for row in conn.execute("SELECT file_path FROM scan_requests").fetchall())
        read = []
        real = Scanner.scan_file

        def scan_file(self, path, *args, **kwargs):
            read.append(path)
            if len(read) == 3:
                scan_service.mark_cancelling(spec["scan_id"])  # the client pressed Ctrl-C
            return real(self, path, *args, **kwargs)

        with patch.object(Scanner, "scan_file", scan_file):
            published = _remediate_queued_files(conn, db_path, files)
        assert published == 0 and len(read) == 3
        assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM scan_requests").fetchone()[0] == 12  # the cancel takes them off, not the batch
        scan_service.cancel(conn, spec["scan_id"])
        assert conn.execute("SELECT COUNT(*) FROM scan_requests").fetchone()[0] == 0

    def test_the_files_of_another_scan_in_the_same_batch_are_still_read_and_published(self, project):
        conn, db_path = project
        first, second = _spec(), _spec()
        scan_service.start_scan(conn, db_path, {**first, "targets": ["m00.py", "m01.py", "m02.py"]})
        scan_service.start_scan(conn, db_path, {**second, "targets": ["m05.py", "m06.py"]})
        files = sorted(row[0] for row in conn.execute("SELECT file_path FROM scan_requests").fetchall())
        scan_service.mark_cancelling(first["scan_id"])
        published = _remediate_queued_files(conn, db_path, files)
        assert published == 2
        assert {row[0] for row in conn.execute("SELECT DISTINCT file_path FROM artifacts").fetchall()} == {"m05.py", "m06.py"}
        scan_service.cancel(conn, first["scan_id"])


class _Fake:
    """A worker that is in the middle of a batch when the client cancels; `after` says what the record shows on each
    poll once the cancel is in (the last one repeats)."""

    def __init__(self, after, done_at_cancel=0, publishing=True):
        self.publishing = publishing
        self.polls = 0
        self.after = list(after)
        self.done_at_cancel = done_at_cancel
        self.detached = False

    def scan_start(self, **spec):
        return {"scan_id": spec["scan_id"], "queued": 40, "up_to_date": 0}

    def scan_progress(self, scan_id):
        self.polls += 1
        if self.polls == 1:
            raise KeyboardInterrupt
        record = self.after.pop(0) if len(self.after) > 1 else self.after[0]
        return {"scan_id": scan_id, "known": True, "queued": 40, "done_code": record.get("done", 0), "failed": 0, **record}

    def scan_cancel(self, scan_id):
        return {"scan_id": scan_id, "cancelled": 40, "done": self.done_at_cancel, "deferred": True, "publishing": self.publishing}

    def scan_detach(self, scan_id):
        self.detached = True
        return {}


class TestACancelIsInTimeUntilThePublishBegins:
    def test_a_cancel_is_in_time_before_and_too_late_after(self):
        try:
            assert scan_service.mark_cancelling("a") is True
            assert scan_service.begin_publish({"f.py": ("a", 0, 0, 0), "g.py": ("b", 0, 0, 0)}, ["f.py", "g.py", "h.py"]) == {"f.py"}
            assert scan_service.mark_cancelling("b") is False
        finally:
            scan_service.end_batch()
            scan_service._CANCELLING.clear()
        assert scan_service.mark_cancelling("c") is True
        scan_service._CANCELLING.clear()


class TestAMixedBatch:
    def test_files_of_a_scan_cancelled_in_time_that_were_already_read_are_not_published(self, project):
        conn, db_path = project
        first, second = _spec(), _spec()
        scan_service.start_scan(conn, db_path, {**first, "targets": ["m00.py", "m01.py", "m02.py"]})
        scan_service.start_scan(conn, db_path, {**second, "targets": ["m05.py", "m06.py"]})
        files = sorted(row[0] for row in conn.execute("SELECT file_path FROM scan_requests").fetchall()) + ["m09.py"]  # m09: queued by no scan
        read = []
        real = Scanner.scan_file

        def scan_file(self, path, *args, **kwargs):
            read.append(path)
            if len(read) == 2:
                scan_service.mark_cancelling(first["scan_id"])  # m00 is read, m01 is being read
            return real(self, path, *args, **kwargs)

        with patch.object(Scanner, "scan_file", scan_file):
            _remediate_queued_files(conn, db_path, files)
        assert {row[0] for row in conn.execute("SELECT DISTINCT file_path FROM artifacts").fetchall()} == {"m05.py", "m06.py", "m09.py"}
        assert scan_service.cancel(conn, first["scan_id"])["cancelled"] == 3  # all three are still queued for the cancel to take off
        assert not scan_service._publish_begun


class TestWhatTheClientSays:
    def test_it_waits_for_the_batch_and_reports_what_the_batch_did_when_it_was_publishing(self):
        # three polls with the batch still going, then it ends: it published everything, the cancel found nothing left
        worker = _Fake([{"finished": False, "remaining": 40, "done": 0, "cancelled": 0}] * 3 + [{"finished": True, "remaining": 0, "done": 40, "cancelled": 0}])
        seen = []
        summary = scan_service.run_scan(worker, {**_spec(), "scan_id": "s1"}, on_progress=seen.append, poll_seconds=0.01)
        assert [run.get("cancelling") for run in seen] == [True, True, True]  # the progress line says what it waits for
        assert summary["state"] == "cancelled" and worker.detached
        assert summary["files_done"] == 40 and summary["files_cancelled"] == 0
        assert summary["files_done"] + summary["files_cancelled"] == summary["files_queued"]
        assert summary["files_published_after_cancel"] == 40 and "already being published" in summary["message"]
        assert "batch_in_flight" not in summary

    def test_it_reports_the_files_a_discarded_batch_left_as_cancelled(self):
        # still reading when the cancel came: the answer does not wait, the batch leaves the files out
        worker = _Fake([{"finished": False, "remaining": 40, "done": 0, "cancelled": 0}], publishing=False)
        summary = scan_service.run_scan(worker, {**_spec(), "scan_id": "s1"}, poll_seconds=0.01)
        assert worker.polls == 2 and worker.detached
        assert summary["files_done"] == 0 and summary["files_cancelled"] == 40
        assert "files_published_after_cancel" not in summary and summary["message"] == "Cancelled: 0 of 40 file(s) were done, 40 not read"

    def test_files_an_earlier_batch_published_are_not_counted_as_published_after_the_cancel(self):
        worker = _Fake([{"finished": True, "remaining": 0, "done": 25, "cancelled": 15}], done_at_cancel=25)
        summary = scan_service.run_scan(worker, {**_spec(), "scan_id": "s1"}, poll_seconds=0.01)
        assert summary["files_done"] == 25 and summary["files_cancelled"] == 15 and "files_published_after_cancel" not in summary

    def test_a_batch_that_outlasts_the_wait_is_said_to_be_still_going(self, monkeypatch, fake_time):
        monkeypatch.setattr(scan_service, "time", fake_time)  # (the wait is the fake clock's: no real 0.2 s)
        worker = _Fake([{"finished": False, "remaining": 40, "done": 0, "cancelled": 0}])
        with patch.object(scan_service, "CANCEL_WAIT_SECONDS", 0.2):
            summary = scan_service.run_scan(worker, {**_spec(), "scan_id": "s1"}, poll_seconds=0.01)
        assert summary["state"] == "cancelled" and summary["batch_in_flight"] is True and worker.detached
        assert "still being finished" in summary["message"] and "its files are indexed" in summary["message"]

    def test_ctrl_c_during_a_publish_returns_within_a_second_and_says_how_big_the_batch_is(self, monkeypatch, fake_time):
        monkeypatch.setattr(scan_service, "time", fake_time)
        assert scan_service.CANCEL_WAIT_SECONDS < 1.0
        batch = {"phase": "publishing", "files_read": 3054, "files_total": 3054, "seconds": 3.0}
        worker = _Fake([{"finished": False, "remaining": 3054, "done": 0, "cancelled": 0, "batch": batch}])
        summary = scan_service.run_scan(worker, {**_spec(), "scan_id": "s1"}, poll_seconds=0.05)
        assert sum(fake_time.slept) < 1.0 + 0.05  # (all it waited, on the fake clock: the cancel wait; the batch needs 3 more seconds and is not waited for)  # pattern-ok: real-time-assert the fake clock's total: the wait of the code, counted, not measured
        assert summary["batch_in_flight"] is True
        assert summary["files_in_flight"] == 3054 and summary["files_cancelled"] == 0  # not cancelled: the batch is being published
        assert "The batch of 3054 file(s) the worker was publishing is still being finished, not undone" in summary["message"]

    def test_a_second_ctrl_c_stops_the_wait(self):
        worker = _Fake([{"finished": False, "remaining": 40, "done": 0, "cancelled": 0}])
        original = worker.scan_progress

        def interrupted(scan_id):
            if worker.polls >= 3:
                raise KeyboardInterrupt
            return original(scan_id)

        worker.scan_progress = interrupted
        summary = scan_service.run_scan(worker, {**_spec(), "scan_id": "s1"}, poll_seconds=0.01)
        assert summary["state"] == "cancelled" and summary["batch_in_flight"] is True and worker.detached


def test_the_progress_line_says_it_is_waiting_for_the_batch_being_published(capsys):
    from drspec.cli.commands.scan import _progress_line

    _progress_line({"queued": 40, "done": 0, "cancelling": True})
    assert "Cancelling: the batch being published is finished first" in capsys.readouterr().err
    _progress_line({"queued": 40, "done": 3, "done_code": 3})
    assert "Scanning 3/40" in capsys.readouterr().err


class _InterruptedFirstPoll:
    """A real connection to the worker, but the first look at the scan's progress is the user pressing Ctrl-C."""

    def __init__(self, inner, started):
        self.inner, self.started, self.polls = inner, started, 0

    def scan_start(self, **spec):
        return self.started

    def scan_progress(self, scan_id):
        self.polls += 1
        if self.polls == 1:
            raise KeyboardInterrupt
        return self.inner.scan_progress(scan_id)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class TestACancelWhileTheBatchIsPublishing:
    def _run(self, project, serve, batch_does, began_publishing=True):
        conn, db_path = project
        spec = _spec()
        started = scan_service.start_scan(conn, db_path, spec)
        files = [row[0] for row in conn.execute("SELECT file_path FROM scan_requests").fetchall()]
        inside, release = threading.Event(), threading.Event()

        def batch_fn(connection, path, file_paths, *rest):
            try:
                if began_publishing:
                    scan_service.begin_publish({}, file_paths)  # (what the real batch does after it has read its files)
                inside.set()
                release.wait(30)
                batch_does(connection, spec["scan_id"], file_paths)
                return len(file_paths)
            finally:
                scan_service.end_batch()

        served = serve(WorkerIPCServer(conn, db_path, remediate_fn=batch_fn))
        try:
            batch, client = served.client(), served.client()
            worker_thread = threading.Thread(target=lambda: batch.remediate(files))
            worker_thread.start()
            assert inside.wait(10)
            threading.Timer(0.5, release.set).start()
            with patch.object(scan_service, "CANCEL_WAIT_SECONDS", 20.0):  # (the default is under a second: the batch here ends after half of it)
                summary = scan_service.run_scan(_InterruptedFirstPoll(client, started), spec, poll_seconds=0.05)
            worker_thread.join(20)
            batch.close()
            client.close()
            return summary, started
        finally:
            release.set()

    def test_the_files_the_batch_published_are_done_and_not_cancelled(self, project, serve):
        def publishes(connection, scan_id, file_paths):
            scan_service.record_batch(connection, {}, {scan_id: [(path, False, 1) for path in file_paths]}, {}, {}, {}, {}, 0)

        summary, started = self._run(project, serve, publishes)
        assert summary["state"] == "cancelled" and summary["files_queued"] == 12
        assert summary["files_done"] == 12 and summary["files_cancelled"] == 0  # (it said done 0, cancelled 12 while they were indexed)
        assert summary["files_done"] + summary["files_cancelled"] + summary["files_failed"] == summary["files_queued"]
        assert summary["files_published_after_cancel"] == 12 and "already being published" in summary["message"]

    def test_a_batch_that_is_discarded_leaves_its_files_cancelled(self, project, serve):
        summary, started = self._run(project, serve, lambda *args: None, began_publishing=False)
        assert summary["files_done"] == 0 and summary["files_cancelled"] == 12
        assert summary["message"] == "Cancelled: 0 of 12 file(s) were done, 12 not read"


def _real_worker_env() -> dict:
    env = dict(os.environ)
    env.pop("DRSPEC_DISABLE_AUTO_WORKER", None)
    return env


def _cli(cwd: Path, env: dict, *args: str, timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "drspec", "--json", *args], cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout, check=False)


@pytest.mark.slow
class TestWithARealWorker:
    def test_ctrl_c_on_a_large_first_scan_cancels_every_file_and_nothing_is_left_queued(self, tmp_path):
        from drspec.worker_ipc import WorkerConnection, _try_connect, socket_path
        env = _real_worker_env()
        tmp = str(tmp_path)
        cwd = Path(tmp)
        for index in range(2500):
            (cwd / f"mod{index}.py").write_text("\n".join(f"def fn{index}_{n}(x):\n    return x + {n}\n" for n in range(8)))
        subprocess.run([sys.executable, "-m", "drspec", "init", "--project-root", str(cwd), "--no-ide", "--no-index"], cwd=str(cwd), env=env, capture_output=True, check=True)
        db_path = cwd / "_drspec" / "contracts.db"
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "drspec", "--json", "scan"], cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
            )

            def queued() -> int:
                sock = _try_connect(socket_path(db_path), timeout=2.0)
                if sock is None:
                    return 0
                client = WorkerConnection(sock)
                try:
                    return client.execute("SELECT COUNT(*) FROM scan_requests").fetchone()[0]
                finally:
                    client.close()

            def batch_running() -> bool:
                sock = _try_connect(socket_path(db_path), timeout=2.0)
                if sock is None:
                    return False
                client = WorkerConnection(sock)
                try:
                    row = client.execute("SELECT scan_id FROM scan_requests LIMIT 1").fetchone()
                    return row is not None and "batch" in client.scan_progress(row[0])
                finally:
                    client.close()

            deadline = time.monotonic() + 90
            while time.monotonic() < deadline and queued() < 2000:
                time.sleep(0.2)
            assert queued() >= 2000, "the scan never got under way"
            deadline = time.monotonic() + 90  # (Ctrl-C only once the worker holds a batch: before that nothing waits for it)
            while time.monotonic() < deadline and not batch_running():
                time.sleep(0.1)
            assert batch_running(), "the worker never started a batch"
            process.send_signal(signal.SIGINT)
            out, err = process.communicate(timeout=60)
            assert process.returncode == 130, (out, err)
            data = json.loads(out)["data"]
            assert data["state"] == "cancelled"
            # (Ctrl-C came while the worker was READING the batch: the command did not wait for it to be read and published, so nothing was done and
            # every queued file is said cancelled; a command that waited for the batch would report the files it published as done)
            assert data["files_done"] == 0 and data["files_cancelled"] == data["files_queued"] == 2500, "Ctrl-C waited for the batch"
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and queued() > 0:
                time.sleep(0.5)
            assert queued() == 0
        finally:
            _cli(cwd, env, "worker", "stop")
