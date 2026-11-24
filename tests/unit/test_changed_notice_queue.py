"""The changed-since-scan notice does not report a queued file as changed: a queued edited or deleted file is `being_read` (the worker is
reading it again), and a queued new file is covered by the index-incomplete note. A file that is changed, gone or new and NOT queued is
still reported. In memory: a database with rows and a queue, and files in a tmp folder; no worker, no command, no database file."""

from __future__ import annotations


import duckdb
import pytest

from drspec.core.changed_notice import changed_since_scan
from drspec.db import init_schema
from drspec.db.queries import enqueue_staleness_many

pytestmark = pytest.mark.unit


@pytest.fixture
def conn():
    connection = duckdb.connect(":memory:")
    init_schema(connection)
    yield connection
    connection.close()


def _scanned(connection, root, *names):
    """Files written to `root` and recorded as scanned with their real size and mtime."""
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"void {name.split('.')[0].split('/')[-1]}() {{}}\n")
        stat = path.stat()
        connection.execute("INSERT INTO scanned_files (file_path, mtime, size) VALUES (?, ?, ?)", [name, stat.st_mtime, stat.st_size])


def _queue(connection, *names):
    enqueue_staleness_many(connection, [(name, "new_file") for name in names])


class TestAQueuedFileIsNotReportedAsChanged:
    def test_a_queued_edited_file_is_being_read_again(self, conn, tmp_path):
        _scanned(conn, tmp_path, "a.cpp")
        (tmp_path / "a.cpp").write_text("void a() { more(); }\n")
        _queue(conn, "a.cpp")
        assert changed_since_scan(conn, tmp_path, ["a.cpp"]) == {"edited": [], "deleted": [], "added": [], "being_read": ["a.cpp"]}

    def test_a_queued_deleted_file_is_being_read_again(self, conn, tmp_path):
        _scanned(conn, tmp_path, "a.cpp", "b.cpp")
        (tmp_path / "b.cpp").unlink()
        _queue(conn, "b.cpp")
        assert changed_since_scan(conn, tmp_path, ["a.cpp", "b.cpp"]) == {"edited": [], "deleted": [], "added": [], "being_read": ["b.cpp"]}

    def test_a_queued_new_file_beside_the_answer_is_not_reported(self, conn, tmp_path):
        _scanned(conn, tmp_path, "src/a.cpp")
        (tmp_path / "src" / "late.cpp").write_text("void late() {}\n")
        _queue(conn, "src/late.cpp")
        assert changed_since_scan(conn, tmp_path, ["src/a.cpp"])["added"] == []


class TestAnUnqueuedFileIsStillReported:
    def test_an_unqueued_edited_file_is_reported_next_to_a_queued_one(self, conn, tmp_path):
        _scanned(conn, tmp_path, "a.cpp", "b.cpp")
        (tmp_path / "a.cpp").write_text("void a() { more(); }\n")
        (tmp_path / "b.cpp").write_text("void b() { more(); }\n")
        _queue(conn, "a.cpp")
        assert changed_since_scan(conn, tmp_path, ["a.cpp", "b.cpp"])["edited"] == ["b.cpp"]

    def test_an_unqueued_deleted_file_is_reported_next_to_a_queued_one(self, conn, tmp_path):
        _scanned(conn, tmp_path, "a.cpp", "b.cpp")
        (tmp_path / "a.cpp").unlink()
        (tmp_path / "b.cpp").unlink()
        _queue(conn, "a.cpp")
        assert changed_since_scan(conn, tmp_path, ["a.cpp", "b.cpp"])["deleted"] == ["b.cpp"]

    def test_an_unqueued_new_file_is_reported_next_to_a_queued_one(self, conn, tmp_path):
        _scanned(conn, tmp_path, "src/a.cpp")
        (tmp_path / "src" / "late.cpp").write_text("void late() {}\n")
        (tmp_path / "src" / "other.cpp").write_text("void other() {}\n")
        _queue(conn, "src/late.cpp")
        assert changed_since_scan(conn, tmp_path, ["src/a.cpp"])["added"] == ["src/other.cpp"]

    def test_an_edited_file_with_an_empty_queue_is_reported(self, conn, tmp_path):
        _scanned(conn, tmp_path, "a.cpp")
        (tmp_path / "a.cpp").write_text("void a() { more(); }\n")
        assert changed_since_scan(conn, tmp_path, ["a.cpp"])["edited"] == ["a.cpp"]

    def test_a_queued_file_that_is_not_part_of_the_answer_changes_nothing(self, conn, tmp_path):
        _scanned(conn, tmp_path, "a.cpp", "b.cpp")
        (tmp_path / "b.cpp").write_text("void b() { more(); }\n")
        _queue(conn, "elsewhere.cpp")
        assert changed_since_scan(conn, tmp_path, ["a.cpp", "b.cpp"])["edited"] == ["b.cpp"]
