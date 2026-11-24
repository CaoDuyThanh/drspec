"""A file that cannot be read as it is (a non-UTF-8 byte, a permission error) is parked: it is not read again, nor counted as pending
work, until it changes or a facts version changes. In memory, with one real file in `tmp_path`; no worker, no process."""

from __future__ import annotations

import duckdb
import pytest

from drspec import staleness_worker
from drspec.core.backlog import maintenance_backlog
from drspec.core.facts_version import requeue_for_new_facts
from drspec.core.status_text import short_lines
from drspec.db.connection import init_schema
from drspec.db.queries import (
    enqueue_staleness_many,
    get_rescan_backlog,
    list_unreadable_files,
    next_rescan_batch,
    record_rescan_failure,
)
from drspec.staleness_worker import get_worker_status

pytestmark = pytest.mark.unit

NOW = 1000.0  # the time the function is given: nothing here waits on a clock
ERROR = "Parse error: 'utf-8' codec can't decode byte 0xe9 in position 17"


@pytest.fixture
def conn():
    """An empty project database in memory."""
    connection = duckdb.connect(":memory:")
    init_schema(connection)
    yield connection
    connection.close()


def _unreadable(conn, tmp_path, name="bad_encoding.h"):
    """A queued file whose read failed as it is on disk now."""
    path = tmp_path / name
    path.write_bytes(b"int bad(int x) { return x; } // caf\xe9 \xff\xfe broken byte\n")
    stat = path.stat()
    enqueue_staleness_many(conn, [(name, "mtime_changed")])
    record_rescan_failure(conn, name, ERROR, stat.st_mtime, stat.st_size, now=NOW)
    return path


class TestAnUnchangedUnreadableFile:
    def test_it_is_parked_not_pending_and_not_read_again(self, conn, tmp_path):
        _unreadable(conn, tmp_path)
        backlog = get_rescan_backlog(conn, NOW, tmp_path)
        assert backlog == {"files_pending_rescan": 0, "files_pending_discovery": 0, "files_in_retry_wait": 0, "files_unreadable": 1}
        assert next_rescan_batch(conn, 10, NOW + 10**7, tmp_path) == []  # however long after: unchanged, so not read

    def test_a_scan_that_asks_for_it_reads_it_again(self, conn, tmp_path):
        _unreadable(conn, tmp_path)
        conn.execute("INSERT INTO scan_requests (file_path, scan_id) VALUES ('bad_encoding.h', 'scan-1')")  # a scan waits for this file
        assert next_rescan_batch(conn, 10, NOW, tmp_path) == ["bad_encoding.h"]  # it is read, so the scan can finish
        assert get_rescan_backlog(conn, NOW, tmp_path)["files_unreadable"] == 0

    def test_status_lists_it_with_its_error(self, conn, tmp_path):
        _unreadable(conn, tmp_path)
        assert list_unreadable_files(conn, tmp_path) == [{"file_path": "bad_encoding.h", "error": ERROR, "attempts": 1}]
        assert maintenance_backlog(conn, root=tmp_path)["unreadable"] == list_unreadable_files(conn, tmp_path)

    def test_the_short_text_says_it_on_one_line(self, conn, tmp_path):
        _unreadable(conn, tmp_path)
        lines = short_lines({"backlog": maintenance_backlog(conn, root=tmp_path)})
        assert "Unreadable files: 1 (list: --details)" in lines
        assert not any("pending rescan" in line or "retry wait" in line for line in lines)  # the parked file is no work to do

    def test_the_worker_status_queue_does_not_count_it(self, conn, tmp_path, monkeypatch):
        _unreadable(conn, tmp_path)
        (tmp_path / "_drspec").mkdir()  # the usual place of a project's database: its root is tmp_path
        db_path = tmp_path / "_drspec" / "contracts.db"
        db_path.touch()
        monkeypatch.setattr(staleness_worker, "get_connection", lambda path, read_only=False: _Keep(conn))
        monkeypatch.setattr(staleness_worker, "get_worker_connection", lambda path, read_only=False, check_build=True: _Keep(conn))
        monkeypatch.setattr(staleness_worker, "is_worker_running", lambda path: None)
        status = get_worker_status(db_path)
        assert status["staleness_queue_size"] == 0
        assert status["backlog"]["files_unreadable"] == 1

    def test_the_backlog_keeps_its_keys_and_adds_one(self, conn, tmp_path):
        keys = set(get_rescan_backlog(conn, NOW, tmp_path))
        assert keys == {"files_pending_rescan", "files_pending_discovery", "files_in_retry_wait", "files_unreadable"}


class TestAnEditedFileIsReadAgain:
    def test_an_edit_puts_it_back_in_the_batch(self, conn, tmp_path):
        path = _unreadable(conn, tmp_path)
        path.write_text("int good(int x) { return x; }\n")  # another size: a valid file now
        assert get_rescan_backlog(conn, NOW, tmp_path)["files_unreadable"] == 0
        assert next_rescan_batch(conn, 10, NOW, tmp_path) == ["bad_encoding.h"]
        assert conn.execute("SELECT COUNT(*) FROM rescan_backoff").fetchone()[0] == 0  # its failure is cleared with the read

    def test_a_deleted_file_is_resolved_at_once(self, conn, tmp_path):
        path = _unreadable(conn, tmp_path)
        path.unlink()
        assert next_rescan_batch(conn, 10, NOW, tmp_path) == ["bad_encoding.h"]


class TestAFactsVersionChange:
    def test_a_facts_version_change_reads_the_unreadable_files_again(self, conn, tmp_path):
        _unreadable(conn, tmp_path)
        assert next_rescan_batch(conn, 10, NOW, tmp_path) == []
        requeue_for_new_facts(conn)  # a parser records more now (the markers are unset in a new database)
        assert next_rescan_batch(conn, 10, NOW, tmp_path) == ["bad_encoding.h"]


class _Keep:
    """The in-memory connection, handed out for every call (the worker functions reach the database through it)."""

    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql, params=None):
        return self._connection.execute(sql, params or [])

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._connection, name)
