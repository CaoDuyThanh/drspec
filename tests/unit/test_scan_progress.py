"""A scan that waits sees the batch it is in: how many files were read so far, then that the batch is being published."""

from __future__ import annotations

import threading
import time
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


def _spec(**extra) -> dict:
    return {"scan_id": scan_service.new_scan_id(), "targets": [], "recursive": True, "full": False, "deep": False,
            "embed": False, "code_only": False, "docs_only": False, "wait": True, **extra}


class TestTheBatchSaysWhereItIs:
    def test_the_files_read_so_far_grow_while_reading_and_the_publish_is_said_to_be_publishing(self, project):
        conn, db_path = project
        spec = _spec()
        scan_service.start_scan(conn, db_path, spec)
        files = sorted(row[0] for row in conn.execute("SELECT file_path FROM scan_requests").fetchall())
        seen = []
        real = Scanner.scan_file

        def scan_file(self, path, *args, **kwargs):
            seen.append(scan_service.progress(conn, spec["scan_id"]).get("batch"))
            return real(self, path, *args, **kwargs)

        import drspec.staleness_worker as worker

        publishing = []
        real_sync = worker.sync_scan_results

        def sync(*args, **kwargs):
            publishing.append(scan_service.progress(conn, spec["scan_id"]).get("batch"))
            return real_sync(*args, **kwargs)

        with patch.object(Scanner, "scan_file", scan_file), patch.object(worker, "sync_scan_results", sync):
            assert _remediate_queued_files(conn, db_path, files) == 12
        assert [batch["phase"] for batch in seen] == ["reading"] * 12
        assert [batch["files_read"] for batch in seen] == list(range(1, 13)) and {batch["files_total"] for batch in seen} == {12}
        assert publishing and publishing[0]["phase"] == "publishing" and publishing[0]["files_read"] == 12 and publishing[0]["files_total"] == 12
        # the batch is over: nothing of it is left, and the scan is finished
        after = scan_service.progress(conn, spec["scan_id"])
        assert "batch" not in after and after["finished"] and after["done"] == 12

    def test_a_scan_with_no_file_in_the_batch_sees_nothing_of_it(self, project):
        conn, db_path = project
        first, second = _spec(), _spec()
        scan_service.start_scan(conn, db_path, {**first, "targets": ["m00.py", "m01.py", "m02.py"]})
        scan_service.start_scan(conn, db_path, {**second, "targets": ["m05.py", "m06.py"]})
        scan_service.begin_batch(scan_service.request_flags(conn, ["m00.py", "m01.py", "m02.py"]), ["m00.py", "m01.py", "m02.py"])
        try:
            assert scan_service.progress(conn, first["scan_id"])["batch"]["files_total"] == 3
            assert "batch" not in scan_service.progress(conn, second["scan_id"])
        finally:
            scan_service.end_batch()
        assert "batch" not in scan_service.progress(conn, first["scan_id"])

    def test_a_batch_of_files_no_scan_asked_for_has_no_progress(self, project):
        conn, _ = project
        scan_service.begin_batch({}, ["m00.py", "m01.py"])
        assert scan_service._batch is None


class TestTheWorkerAnswersWhileTheBatchRuns:
    def test_progress_comes_back_at_once_with_the_batch_state_while_the_batch_holds_the_lock(self, project, serve):
        conn, db_path = project
        spec = _spec()
        scan_service.start_scan(conn, db_path, spec)
        files = sorted(row[0] for row in conn.execute("SELECT file_path FROM scan_requests").fetchall())
        requests = scan_service.request_flags(conn, files)
        reached, go_on, finish, moved_on = threading.Event(), threading.Event(), threading.Event(), threading.Event()

        def batch(connection, path, file_paths, *rest):
            scan_service.begin_batch(requests, file_paths)
            for file_path in file_paths[:5]:
                scan_service.note_read(file_path, requests)
            reached.set()
            go_on.wait(30)
            moved_on.set()
            scan_service.begin_publish(requests, file_paths)
            reached.set()
            finish.wait(30)
            return 0

        served = serve(WorkerIPCServer(conn, db_path, remediate_fn=batch))
        try:
            worker_thread = threading.Thread(target=lambda: served.client().remediate(files))
            worker_thread.start()
            assert reached.wait(10)
            client = served.client()
            reading = client.scan_progress(spec["scan_id"])
            assert not moved_on.is_set()  # answered while the batch was still held (a call that waited for it would find it moved on)
            assert reading["batch"]["phase"] == "reading" and reading["batch"]["files_read"] == 5 and reading["batch"]["files_total"] == 12
            reached.clear()
            go_on.set()
            assert reached.wait(10)
            publishing = client.scan_progress(spec["scan_id"])
            assert publishing["batch"]["phase"] == "publishing" and publishing["batch"]["files_read"] == 12
            finish.set()
            worker_thread.join(20)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and "batch" in client.scan_progress(spec["scan_id"]):
                time.sleep(0.05)
            assert "batch" not in client.scan_progress(spec["scan_id"])  # the worker cleared it when the batch ended
            client.close()
        finally:
            go_on.set()
            finish.set()


class TestTheProgressLine:
    def test_reading_and_publishing_are_told_apart(self, capsys):
        from drspec.cli.commands.scan import _progress_line

        _progress_line({"queued": 3054, "done": 0, "failed": 0, "batch": {"phase": "reading", "files_read": 1200, "files_total": 3054, "seconds": 9.4}})
        reading = capsys.readouterr().err
        assert "Scanning 1200/3054 files: reading (9 s)" in reading
        _progress_line({"queued": 3054, "done": 0, "failed": 0, "batch": {"phase": "publishing", "files_read": 3054, "files_total": 3054, "seconds": 4.0}})
        publishing = capsys.readouterr().err
        assert "Publishing 3054 files read (4 s), 0/3054 files done so far" in publishing
        _progress_line({"queued": 10, "done": 4, "failed": 1, "done_code": 5, "done_documents": 0})
        assert "Scanning 5/10 files (code 5, documents 0)" in capsys.readouterr().err

    def test_files_an_earlier_batch_published_count_towards_the_total(self, capsys):
        from drspec.cli.commands.scan import _progress_line

        _progress_line({"queued": 100, "done": 40, "failed": 2, "batch": {"phase": "reading", "files_read": 10, "files_total": 58, "seconds": 1.0}})
        assert "Scanning 52/100 files: reading (1 s)" in capsys.readouterr().err

    def test_every_poll_of_a_long_batch_changes_the_line(self, capsys):
        from drspec.cli.commands.scan import _progress_line

        lines = []
        for seconds in (10.0, 11.2, 12.4):
            _progress_line({"queued": 3054, "done": 0, "failed": 0, "batch": {"phase": "publishing", "files_read": 3054, "files_total": 3054, "seconds": seconds}})
            lines.append(capsys.readouterr().err)
        assert len(set(lines)) == 3  # a publish that reports nothing else still moves the clock
