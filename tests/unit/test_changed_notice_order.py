"""The changed-since-scan notice compares the disk with what the index held BEFORE the answer was read, so a worker that publishes while the
answer is read cannot hide an edit. One note per file: an edited file the queue holds is `being_read`, a deleted one likewise, an unqueued
edited, deleted or new file is reported once, and a queued new file is left to the index-incomplete note. In memory: a database with rows
and a queue, and files in a tmp folder; the worker's publish is a plain update of the snapshot row; no worker process, no command."""

from __future__ import annotations

import duckdb
import pytest

from drspec.core.changed_notice import add_changed_notice, changed_since_scan, take_before
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
        path.write_text(f"void {name.split('.')[0]}() {{}}\n")
        stat = path.stat()
        connection.execute("INSERT INTO scanned_files (file_path, mtime, size) VALUES (?, ?, ?)", [name, stat.st_mtime, stat.st_size])


def _publish(connection, root, name):
    """What the worker's publish does to the snapshot of a re-read file: the stored size and mtime become the disk's."""
    stat = (root / name).stat()
    connection.execute("UPDATE scanned_files SET mtime = ?, size = ? WHERE file_path = ?", [stat.st_mtime, stat.st_size, name])


class TestThePublishDuringTheAnswerDoesNotHideAnEdit:
    def test_a_publish_between_the_answer_and_the_check_still_reports_the_edit(self, conn, tmp_path):
        _scanned(conn, tmp_path, "a.cpp")
        (tmp_path / "a.cpp").write_text("void a() { more(); }\n")  # edited on disk, the index still has the old answer
        before = take_before(conn)  # taken first, as the command does, before its reads
        _publish(conn, tmp_path, "a.cpp")  # the worker publishes the re-read while the answer is read
        data = add_changed_notice(conn, {}, tmp_path, ["a.cpp"], before=before)
        assert data["changed_since_scan"] == {"count": 1, "edited": ["a.cpp"]}
        assert data["note"].startswith("1 indexed file(s) changed since the last scan (a.cpp)")

    def test_a_queue_held_before_the_answer_still_says_the_file_is_being_read(self, conn, tmp_path):
        _scanned(conn, tmp_path, "a.cpp")
        (tmp_path / "a.cpp").write_text("void a() { more(); }\n")
        enqueue_staleness_many(conn, [("a.cpp", "search_drift")])
        before = take_before(conn)
        conn.execute("DELETE FROM staleness_queue")  # the worker takes the file off the queue when it publishes
        _publish(conn, tmp_path, "a.cpp")
        assert changed_since_scan(conn, tmp_path, ["a.cpp"], before=before) == {"edited": [], "deleted": [], "added": [], "being_read": ["a.cpp"]}

    def test_with_nothing_published_the_edit_is_reported_as_before(self, conn, tmp_path):
        _scanned(conn, tmp_path, "a.cpp")
        (tmp_path / "a.cpp").write_text("void a() { more(); }\n")
        before = take_before(conn)
        assert changed_since_scan(conn, tmp_path, ["a.cpp"], before=before)["edited"] == ["a.cpp"]


class TestOneNotePerFile:
    def test_each_file_is_named_in_one_sentence_only(self, conn, tmp_path):
        _scanned(conn, tmp_path, "a.cpp", "b.cpp", "gone.cpp")
        (tmp_path / "a.cpp").write_text("void a() { queued_edit(); }\n")  # queued: being read again
        (tmp_path / "b.cpp").write_text("void b() { edit(); }\n")  # not queued: edited
        (tmp_path / "gone.cpp").unlink()  # not queued: deleted
        (tmp_path / "late.cpp").write_text("void late() {}\n")  # queued new file: the index note covers it
        (tmp_path / "other.cpp").write_text("void other() {}\n")  # not queued new file: added
        enqueue_staleness_many(conn, [("a.cpp", "search_drift"), ("late.cpp", "new_file")])
        before = take_before(conn)

        changes = changed_since_scan(conn, tmp_path, ["a.cpp", "b.cpp", "gone.cpp"], before=before)
        assert changes == {"edited": ["b.cpp"], "deleted": ["gone.cpp"], "added": ["other.cpp"], "being_read": ["a.cpp"]}

        data = add_changed_notice(conn, {"note": None}, tmp_path, ["a.cpp", "b.cpp", "gone.cpp"], before=before)
        assert data["changed_since_scan"] == {
            "count": 4, "edited": ["b.cpp"], "deleted": ["gone.cpp"], "added": ["other.cpp"], "being_read": ["a.cpp"],
        }
        assert data["note"] == (
            "3 indexed file(s) changed since the last scan (b.cpp, gone.cpp (deleted), other.cpp (new)): results may be outdated; "
            "drspec scan <paths> updates them. 1 file(s) are being read again by the worker (a.cpp): ask again in a moment."
        )

    def test_a_queued_deleted_file_is_being_read_again_and_not_called_deleted(self, conn, tmp_path):
        _scanned(conn, tmp_path, "gone.cpp")
        (tmp_path / "gone.cpp").unlink()
        enqueue_staleness_many(conn, [("gone.cpp", "search_drift")])
        before = take_before(conn)
        assert changed_since_scan(conn, tmp_path, ["gone.cpp"], before=before) == {
            "edited": [], "deleted": [], "added": [], "being_read": ["gone.cpp"],
        }
