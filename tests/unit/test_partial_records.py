"""The records of what the parser lost in a file (`partial:` and `skipped:`) go with the file when it is deleted or ignored, and a
database that already holds such records of files it no longer knows is cleaned when the worker starts."""

from __future__ import annotations

import json

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.core import partial

runner = CliRunner()

LOSES_A_FUNCTION = """struct AwsModel {
  template <class T>
  static int toMap(
    const T& input
  ) = delete;
  static int other() { return 1; }
};
"""


def _partial_files():
    """The files `status --partial` lists as losing functions."""
    data = json.loads(runner.invoke(app, ["--json", "status", "--partial"]).output)["data"]
    return [entry["file"] for entry in data["files"]]


def _records(project):
    connection = duckdb.connect(str(project / "_drspec" / "contracts.db"), read_only=True)  # pattern-ok: duckdb-file kept in memory by `kept_duckdb`
    try:
        return sorted(key for (key,) in connection.execute("SELECT key FROM config WHERE starts_with(key, 'partial:') OR starts_with(key, 'skipped:')").fetchall())
    finally:
        connection.close()


@pytest.fixture
def project(kept_project):
    (kept_project / "keep.cpp").write_text(LOSES_A_FUNCTION)
    (kept_project / "gone.cpp").write_text(LOSES_A_FUNCTION)
    (kept_project / "ok.cpp").write_text("int fine() { return 1; }\n")
    assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
    return kept_project


class TestARecordGoesWithItsFile:
    def test_both_files_are_partial_before(self, project):
        assert sorted(_partial_files()) == ["gone.cpp", "keep.cpp"]
        assert "partial:gone.cpp" in _records(project)

    def test_a_deleted_partly_parsed_file_is_no_longer_counted(self, project):
        (project / "gone.cpp").unlink()
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        assert _partial_files() == ["keep.cpp"]
        assert not [key for key in _records(project) if key.endswith("gone.cpp")]
        status = runner.invoke(app, ["status"]).output
        assert "gone.cpp" not in status

    def test_a_file_that_is_ignored_now_is_no_longer_counted(self, project):
        assert runner.invoke(app, ["config", "set", "scan.ignore", "gone.cpp"]).exit_code == 0
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        assert _partial_files() == ["keep.cpp"]
        assert not [key for key in _records(project) if key.endswith("gone.cpp")]


class TestARecordThatStayedBehind:
    @pytest.fixture
    def conn(self, project):
        connection = duckdb.connect(str(project / "_drspec" / "contracts.db"))  # pattern-ok: duckdb-file kept in memory by `kept_duckdb`
        yield connection
        connection.close()

    def _leave_records_of(self, conn, *paths):
        for path in paths:
            conn.execute("INSERT INTO config (key, value) VALUES (?, ?)", ["partial:" + path, json.dumps({"skipped": [{"name": "f", "line": 1}], "partial": []})])
            conn.execute("INSERT INTO config (key, value) VALUES (?, ?)", ["skipped:" + path, "3"])

    def test_the_records_of_files_the_index_does_not_know_are_removed_and_counted(self, project, conn):
        self._leave_records_of(conn, "devel/ghost.cpp", "install/other.h")
        before = _records(project)
        assert "partial:devel/ghost.cpp" in before and "skipped:install/other.h" in before
        removed = partial.forget_unknown_files(conn)
        assert removed == 4  # a partial and a skipped record of two files
        assert _records(project) == [key for key in before if "ghost" not in key and "other.h" not in key]
        assert "partial:keep.cpp" in _records(project)  # a file the index knows keeps its record
        assert partial.forget_unknown_files(conn) == 0  # once: nothing left to remove

    def test_nothing_is_removed_from_a_database_that_has_no_scanned_files_yet(self, kept_project):

        connection = duckdb.connect(str(kept_project / "_drspec" / "contracts.db"))  # pattern-ok: duckdb-file kept in memory by `kept_duckdb`
        connection.execute("INSERT INTO config (key, value) VALUES ('partial:x.cpp', '{}')")
        assert partial.forget_unknown_files(connection) == 0  # not scanned by this version: its files are not known yet, not gone
        assert connection.execute("SELECT COUNT(*) FROM config WHERE key = 'partial:x.cpp'").fetchone()[0] == 1
        connection.close()

    def test_the_worker_start_does_it(self):
        from drspec import staleness_worker

        assert staleness_worker.forget_unknown_files is partial.forget_unknown_files  # what `run_worker` calls at its start


class TestTheTextViewOfPartialFiles:
    """`status --partial` in text caps the skipped names of each file at 10 and says how many more; the JSON keeps every name."""

    @pytest.fixture
    def many(self, kept_project):
        names = [f"fn_{i:02d}" for i in range(30)]
        connection = duckdb.connect(str(kept_project / "_drspec" / "contracts.db"))  # pattern-ok: duckdb-file kept in memory by `kept_duckdb`
        for path, count in (("big.h", 30), ("small.h", 2)):
            record = {"first_error_line": 4, "skipped": [{"name": name, "line": index + 5} for index, name in enumerate(names[:count])], "partial": []}
            connection.execute("INSERT INTO config (key, value) VALUES (?, ?)", ["partial:" + path, json.dumps(record)])
        connection.close()
        return names

    def test_the_text_shows_ten_names_and_how_many_more(self, many):
        text = runner.invoke(app, ["status", "--partial"]).output
        assert all(name in text for name in many[:10]) and not any(name in text for name in many[10:])
        assert "... and 20 more (use --json for all)" in text
        assert text.count("more (use --json for all)") == 1  # the file with 2 names has no such line
        assert "fn_00" in text and "fn_01" in text and "Functions Skipped: 30" in text  # the count stays whole

    def test_the_json_keeps_every_name(self, many):
        data = json.loads(runner.invoke(app, ["--json", "status", "--partial"]).output)["data"]
        big = next(entry for entry in data["files"] if entry["file"] == "big.h")
        assert big["skipped"] == many and big["functions_skipped"] == 30
        assert not any("more (use" in name for entry in data["files"] for name in entry["skipped"])

    def test_the_limit_still_limits_the_number_of_files(self, many):
        text = runner.invoke(app, ["status", "--partial", "--limit", "1"]).output
        assert "big.h" in text and "small.h" not in text and "... and 20 more" in text
