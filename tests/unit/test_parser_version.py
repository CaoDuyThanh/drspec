"""A file an older parser read in part is read again when the parser changes, and nothing else is."""

from __future__ import annotations

import json
import pathlib
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.core import parser_version, scan_service
from drspec.core.partial import KEY_PREFIX
from drspec.db.queries import list_staleness_queue
from drspec.staleness_worker import _remediate_queued_files

runner = CliRunner()

BROKEN = "def good():\n    return 1\n\n\ndef bad():\n    return (1 +\n"  # `bad` is kept in part: a lost function
CLEAN = "def fine():\n    return 1\n"
LOST = {"skipped": [{"name": "update", "line": 12}], "partial": [], "first_error_line": 12}  # what an old parser recorded


@pytest.fixture
def project(kept_project):
    """Six small files, scanned, in a project folder with an empty in-memory project database (`kept_project`): what is checked is what the parser
    version does to the records, not `init`."""
    for name, text in {"clean.py": CLEAN, "broken.py": BROKEN, "old_fixed.py": CLEAN, "old_broken.py": BROKEN, "other_version.py": CLEAN, "warned.py": CLEAN}.items():
        (kept_project / name).write_text(text)
    assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
    return kept_project


@pytest.fixture
def conn(project):
    connection = duckdb.connect(str(project / "_drspec" / "contracts.db"))
    yield connection
    connection.close()


def _record(conn, path: str):
    row = conn.execute("SELECT value FROM config WHERE key = ?", [KEY_PREFIX + path]).fetchone()
    return json.loads(row[0]) if row else None


def _write(conn, path: str, info: dict) -> None:
    conn.execute("DELETE FROM config WHERE key = ?", [KEY_PREFIX + path])
    conn.execute("INSERT INTO config (key, value) VALUES (?, ?)", [KEY_PREFIX + path, json.dumps(info)])


def _as_an_older_parser_left_it(conn) -> None:
    """Records as the index of an older release holds them (no version), plus one of another version and one with only
    warnings, and the current ones the scan wrote."""
    _write(conn, "old_fixed.py", LOST)  # the old parser lost `update`; the current one reads this file whole
    _write(conn, "old_broken.py", {"skipped": [], "partial": [{"name": "bad", "line": 5, "function_id": "x"}], "first_error_line": 6})
    _write(conn, "other_version.py", {**LOST, "parser": "an-older-one"})
    _write(conn, "warned.py", {"skipped": [], "partial": [], "first_error_line": 3})  # a syntax error, nothing lost
    conn.execute("DELETE FROM config WHERE key IN ('parser:requeued', 'parser:reread')")


class TestTheVersion:
    def test_a_partial_record_carries_the_version_of_the_parser_that_made_it(self, project, conn):
        record = _record(conn, "broken.py")
        assert record is not None and record["partial"]
        assert record["parser"] == parser_version.current() and len(record["parser"]) == 12

    def test_the_version_changes_when_a_parser_source_changes(self):
        real = pathlib.Path.read_bytes

        def changed(self):
            return real(self) + b"\n# edited" if self.name == "python_parser.py" else real(self)

        same = parser_version._computed.__wrapped__()
        assert same == parser_version.current()
        with patch.object(pathlib.Path, "read_bytes", changed):
            assert parser_version._computed.__wrapped__() != same

    def test_a_record_without_a_version_is_older_than_any(self):
        assert parser_version._outdated(LOST, "abc") and parser_version._outdated({**LOST, "parser": "x"}, "abc")
        assert not parser_version._outdated({**LOST, "parser": "abc"}, "abc")
        assert parser_version._outdated({"skipped": [], "partial": [], "first_error_line": 3}, "abc")  # warnings only: it may hide a loss
        assert not parser_version._outdated({"skipped": [], "partial": [], "first_error_line": 3, "parser": "abc"}, "abc")


class TestWhatIsQueued:
    def test_only_files_with_a_partial_record_of_an_older_version_are_queued_at_background_priority(self, project, conn):
        _as_an_older_parser_left_it(conn)
        before = conn.execute("SELECT * FROM scanned_files ORDER BY file_path").fetchall()
        edges_before = conn.execute("SELECT COUNT(*) FROM dependencies").fetchone()
        assert parser_version.requeue_outdated_partial_files(conn) == 4
        queue = dict(conn.execute("SELECT file_path, reason FROM staleness_queue").fetchall())
        assert queue == {name: "parser_upgrade" for name in ("old_fixed.py", "old_broken.py", "other_version.py", "warned.py")}
        assert "scan" not in queue.values()
        # nothing else is touched: the other files' hash, time and edges stay as they were
        assert conn.execute("SELECT * FROM scanned_files ORDER BY file_path").fetchall() == before
        assert conn.execute("SELECT COUNT(*) FROM dependencies").fetchone() == edges_before
        assert _record(conn, "clean.py") is None and "broken.py" not in queue  # a file with no record, and one the current parser stamped

    def test_the_priority_is_below_what_a_scan_asked_for(self, project, conn):
        import time

        from drspec.db.queries import next_rescan_batch

        _as_an_older_parser_left_it(conn)
        parser_version.requeue_outdated_partial_files(conn)
        conn.execute("INSERT INTO staleness_queue (file_path, reason) VALUES ('clean.py', 'scan')")
        conn.execute("INSERT INTO staleness_queue (file_path, reason) VALUES ('broken.py', 'new_file')")
        batch = next_rescan_batch(conn, 10, time.time(), Path.cwd())
        assert batch[0] == "clean.py" and batch[-1] == "broken.py"

    def test_a_file_that_the_project_does_not_know_is_not_queued(self, project, conn):
        _write(conn, "ghost.py", LOST)
        conn.execute("DELETE FROM config WHERE key = 'parser:requeued'")
        assert parser_version.requeue_outdated_partial_files(conn) == 0 and list_staleness_queue(conn) == []

    def test_a_scan_that_finds_a_changed_version_queues_them_and_the_worker_start_does_the_same(self, project, conn):
        _as_an_older_parser_left_it(conn)
        spec = {"scan_id": scan_service.new_scan_id(), "targets": [], "recursive": True, "full": False, "deep": False, "embed": False,
                "code_only": False, "docs_only": False, "wait": True}
        started = scan_service.start_scan(conn, project / "_drspec" / "contracts.db", spec)
        assert started["queued"] == 0  # no file changed: the scan itself has nothing to read
        assert sorted(list_staleness_queue(conn)) == ["old_broken.py", "old_fixed.py", "other_version.py", "warned.py"]
        conn.execute("DELETE FROM staleness_queue")
        _as_an_older_parser_left_it(conn)
        from drspec.staleness_worker import (
            requeue_outdated_partial_files,  # what `run_worker` calls at its start
        )

        assert requeue_outdated_partial_files(conn) == 4


class TestReadAgain:
    def _reread(self, project, conn):
        _as_an_older_parser_left_it(conn)
        assert parser_version.requeue_outdated_partial_files(conn) == 4
        files = sorted(list_staleness_queue(conn))
        assert _remediate_queued_files(conn, project / "_drspec" / "contracts.db", files) == 4

    def test_the_files_are_read_with_the_current_parser_and_the_numbers_are_in_status(self, project, conn):
        self._reread(project, conn)
        assert _record(conn, "old_fixed.py") is None  # whole now
        assert _record(conn, "other_version.py") is None and _record(conn, "warned.py") is None
        broken = _record(conn, "old_broken.py")
        assert broken["partial"] and broken["parser"] == parser_version.current()  # still loses a function, now stamped
        summary = parser_version.reread_summary(conn)
        assert summary["files_queued"] == 4 and summary["now_clean"] == 3 and summary["still_partial"] == 1 and summary["waiting"] == 0
        conn.close()
        data = json.loads(runner.invoke(app, ["--json", "status"]).output)["data"]
        block = data["parser_upgrade"]
        assert (block["files_queued"], block["now_clean"], block["still_partial"], block["waiting"]) == (4, 3, 1, 0)
        assert "4 file(s) that an older parser read in part were queued for a re-read: 3 now parse whole, 1 still lose functions" in block["message"]

    def test_status_counts_the_files_still_waiting(self, project, conn):
        _as_an_older_parser_left_it(conn)
        parser_version.requeue_outdated_partial_files(conn)
        assert parser_version.reread_summary(conn)["waiting"] == 4

    def test_a_second_scan_and_a_second_start_queue_nothing_and_an_edit_reads_only_that_file(self, project, conn):
        self._reread(project, conn)
        assert parser_version.requeue_outdated_partial_files(conn) == 0 and list_staleness_queue(conn) == []
        def spec():
            return {
                "scan_id": scan_service.new_scan_id(),
                "targets": [],
                "recursive": True,
                "full": False,
                "deep": False,
                "embed": False,
                "code_only": False,
                "docs_only": False,
                "wait": True,
            }
        db_path = project / "_drspec" / "contracts.db"
        assert scan_service.start_scan(conn, db_path, spec())["queued"] == 0 and list_staleness_queue(conn) == []
        (project / "clean.py").write_text(CLEAN + "\n\ndef more():\n    return 2\n")
        assert scan_service.start_scan(conn, db_path, spec())["queued"] == 1  # the edit, and no other file
        assert list_staleness_queue(conn) == ["clean.py"]


    def test_a_file_that_is_still_partial_is_not_queued_again_by_the_next_version_check(self, project, conn):
        self._reread(project, conn)
        conn.execute("DELETE FROM config WHERE key = 'parser:requeued'")  # a lost marker: the records are what counts
        assert parser_version.requeue_outdated_partial_files(conn) == 0

    def test_a_new_parser_version_queues_the_still_partial_files_again(self, project, conn):
        self._reread(project, conn)
        with patch.object(parser_version, "current", lambda: "next-release"):
            assert parser_version.requeue_outdated_partial_files(conn) == 2  # both files that lose a function
            assert sorted(list_staleness_queue(conn)) == ["broken.py", "old_broken.py"]
            assert parser_version.reread_summary(conn)["waiting"] == 2


@pytest.mark.slow  # pattern-ok: slow-socket-model-marker a real worker process, with integration and a reason
@pytest.mark.integration  # the objective is a real worker process: what it does when it STARTS
class TestWithARealWorker:
    def test_the_files_are_read_again_when_the_worker_starts_without_a_command(self, wait_until, tmp_path, monkeypatch):
        """A worker that starts finds the files an older parser read in part and reads them again, with no command asking for it. The commands run
        in THIS process (with the worker enabled they talk to it over its socket): the worker is the only process started."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "old_fixed.py").write_text(CLEAN)
        (tmp_path / "old_broken.py").write_text(BROKEN)
        db_path = tmp_path / "_drspec" / "contracts.db"
        assert runner.invoke(app, ["init", "--no-ide", "--non-interactive", "--no-index"]).exit_code == 0  # pattern-ok: init-for-setup the worker needs a project folder
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0  # (no worker yet: the scan runs in this process)
        connection = duckdb.connect(str(db_path))  # pattern-ok: duckdb-file the worker process opens this file
        _write(connection, "old_fixed.py", LOST)
        _write(connection, "old_broken.py", {"skipped": [], "partial": [{"name": "bad", "line": 5, "function_id": "x"}], "first_error_line": 6})
        connection.execute("DELETE FROM config WHERE key IN ('parser:requeued', 'parser:reread')")
        connection.close()

        def block() -> dict:
            result = runner.invoke(app, ["--json", "status"])
            assert result.exit_code == 0, result.output
            return json.loads(result.output)["data"].get("parser_upgrade") or {}

        monkeypatch.delenv("DRSPEC_DISABLE_AUTO_WORKER")  # from here a command starts a worker; nothing asks for the re-read
        try:
            block()
            assert wait_until(lambda: block().get("waiting") == 0, timeout=120, interval=0.1), block()
            final = block()
            assert (final["files_queued"], final["now_clean"], final["still_partial"]) == (2, 1, 1), final
        finally:
            runner.invoke(app, ["worker", "stop"])
