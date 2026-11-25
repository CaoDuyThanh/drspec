"""The errors a worker still holding the database causes end with the fix (`drspec worker stop`, then the command again), in JSON, in text and
in the MCP message; and a worker stops by itself when its database file is gone or replaced, and never creates it. In memory: a fake file
check, a file in tmp_path, no worker process started."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from drspec import staleness_worker
from drspec.cli.output import WORKER_STOP_HINT, error_response, output_pretty
from drspec.mcp_server import _db_unavailable_message

pytestmark = pytest.mark.unit

HINT = "Stop the old worker with `drspec worker stop`, then run the command again."


class TestTheFixIsNamedWhereTheErrorIs:
    @pytest.mark.parametrize("code", ["DATABASE_BUSY", "MIGRATION_FAILED"])
    def test_the_json_error_carries_the_exact_command(self, code):
        error = error_response(code, "Conflicting lock is held in python3.8 (PID 4242)")["error"]
        assert error["hint"] == HINT == WORKER_STOP_HINT

    def test_an_error_with_another_cause_has_no_hint(self):
        assert "hint" not in error_response("FUNCTION_NOT_FOUND", "No function with ID a.py::f")["error"]

    def test_the_text_form_shows_the_hint_under_the_error(self, capsys):
        output_pretty(error_response("DATABASE_BUSY", "Could not reach the drspec worker: busy"))
        out = capsys.readouterr().out
        assert "Code: DATABASE_BUSY" in out and f"Hint: {HINT}" in out

    def test_the_mcp_message_names_the_command(self):
        message = _db_unavailable_message(RuntimeError("locked"))
        assert "run 'drspec worker stop', then run the command again." in message


class TestTheWorkerStopsWhenItsFileIsGone:
    IDENTITY = (1, 2)

    def _fake(self, result):
        def stat(path):
            if isinstance(result, BaseException):
                raise result
            return os.stat_result((0o600, result[1], result[0], 1, 0, 0, 0, 0, 0, 0))
        return stat

    def test_a_missing_file_means_stop(self):
        assert staleness_worker.database_replaced(Path("gone.db"), self.IDENTITY, stat=self._fake(FileNotFoundError())) is True

    def test_the_same_file_means_keep_going(self):
        assert staleness_worker.database_replaced(Path("db"), self.IDENTITY, stat=self._fake((1, 2))) is False

    def test_another_file_at_the_path_means_stop(self):
        assert staleness_worker.database_replaced(Path("db"), self.IDENTITY, stat=self._fake((9, 2))) is True

    def test_an_unreadable_path_is_not_a_reason_to_stop(self):
        assert staleness_worker.database_replaced(Path("db"), self.IDENTITY, stat=self._fake(PermissionError())) is False

    def test_the_check_at_a_loop_point_exits_and_does_not_create_the_file(self, tmp_path, monkeypatch):
        missing = tmp_path / "_drspec" / "contracts.db"
        monkeypatch.setattr(staleness_worker, "_watched_database", (missing, (1, 2)))
        monkeypatch.setattr(staleness_worker, "_stop_requested", staleness_worker.threading.Event())
        with pytest.raises(SystemExit):
            staleness_worker._exit_if_stop_requested()
        assert not missing.exists() and not missing.parent.exists()  # nothing was created

    def test_a_worker_with_its_file_keeps_going(self, tmp_path, monkeypatch):
        db = tmp_path / "contracts.db"
        db.write_bytes(b"")
        info = os.stat(db)
        monkeypatch.setattr(staleness_worker, "_watched_database", (db, (info.st_dev, info.st_ino)))
        monkeypatch.setattr(staleness_worker, "_stop_requested", staleness_worker.threading.Event())
        assert staleness_worker._exit_if_stop_requested() is None  # it returns (no exit): the file is the one it opened

    def test_a_worker_that_has_not_opened_a_file_is_not_watched(self, monkeypatch):
        monkeypatch.setattr(staleness_worker, "_watched_database", None)
        monkeypatch.setattr(staleness_worker, "_stop_requested", staleness_worker.threading.Event())
        assert staleness_worker._exit_if_stop_requested() is None  # it returns: nothing is watched, so nothing stops it
