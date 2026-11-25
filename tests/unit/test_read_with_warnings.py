"""A syntax error that costs no function is a warning, not a partial parse: "partially parsed" counts only the files that
lost a function or kept one read in part. What is stored for either kind of file is still kept when it is rescanned."""

from __future__ import annotations

import json

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.core.partial import (
    lost_something,
    partial_summary,
    read_with_warnings,
    totals,
    worst_files,
)
from drspec.core.scan_service import partial_report
from drspec.db import init_schema

runner = CliRunner()

#: Two good functions around a declaration the grammar rejects: an error, and nothing lost.
WARNING_ONLY = "int one() { return 1; }\nint x = ;\nint two() { return 2; }\n"


@pytest.fixture
def project(kept_project):
    """The project folder (the working folder) with an empty database in memory: `init` is not what these tests are about."""
    return kept_project


@pytest.fixture
def connection():
    conn = duckdb.connect(":memory:")  # only what `config` holds is read: no file
    init_schema(conn)
    yield conn
    conn.close()


def _json(*args):
    result = runner.invoke(app, ["--json", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["data"]


INFOS = [
    {"file": "a.h", "skipped": [], "partial": [], "first_error_line": 3},
    {"file": "b.h", "skipped": [{"name": "f", "line": 1}], "partial": [], "first_error_line": 1},
    {"file": "c.h", "skipped": [], "partial": [{"name": "g", "line": 2, "function_id": "c.h::g#0"}], "first_error_line": 2},
    {"file": "d.h", "skipped": [], "partial": [], "first_error_line": 9},
]


class TestTheCounts:
    @pytest.mark.unit
    def test_only_a_file_that_lost_something_is_partial(self):
        assert [lost_something(info) for info in INFOS] == [False, True, True, False]

    @pytest.mark.unit
    def test_totals_say_how_many_of_each(self):
        assert totals(INFOS) == {"files": 2, "files_read_with_warnings": 2, "functions_skipped": 1, "functions_partial": 1}

    @pytest.mark.unit
    def test_the_lists_hold_only_the_files_that_lost_something(self):
        assert [item["file"] for item in worst_files(INFOS, 10)] == ["b.h", "c.h"]

    @pytest.mark.unit
    def test_the_report_of_a_scan(self):
        report = partial_report({info["file"]: {k: v for k, v in info.items() if k != "file"} for info in INFOS}, 0)
        assert report["files_partially_parsed"] == 2 and report["files_read_with_warnings"] == 2
        assert [item["file"] for item in report["partial_files"]] == ["b.h", "c.h"]

    @pytest.mark.unit
    def test_a_scan_with_warnings_only_reports_no_partial_file(self):
        report = partial_report({"a.h": {"skipped": [], "partial": [], "first_error_line": 3}}, 0)
        assert report == {"files_read_with_warnings": 1}

    @pytest.mark.component
    def test_the_summary_of_status(self, connection):
        for info in INFOS:
            connection.execute("INSERT INTO config (key, value) VALUES (?, ?)", ["partial:" + info["file"], json.dumps({k: v for k, v in info.items() if k != "file"})])
        summary = partial_summary(connection)
        assert summary["files"] == 2 and summary["files_read_with_warnings"] == 2
        assert [item["file"] for item in summary["all_files"]] == ["b.h", "c.h"]
        assert read_with_warnings(connection) == 2


    @pytest.mark.component
    def test_a_project_with_warnings_only_has_no_partial_summary(self, connection):
        connection.execute("INSERT INTO config (key, value) VALUES ('partial:a.h', ?)", [json.dumps(INFOS[0])])
        assert partial_summary(connection) == {} and read_with_warnings(connection) == 1


@pytest.mark.component  # a scan of real files in a project folder, in this process, into an in-memory database
class TestAScan:
    def test_a_file_with_an_error_and_no_loss_is_read_with_warnings(self, project):
        (project / "w.cpp").write_text(WARNING_ONLY)
        data = _json("scan")
        assert data["files_read_with_warnings"] == 1
        assert "files_partially_parsed" not in data and "partial_files" not in data
        status = _json("status")
        assert status["files_read_with_warnings"] == 1 and "partially_parsed" not in status
        assert status["cpp"]["files_partially_parsed"] == 0 and status["cpp"]["files_read_with_warnings"] == 1

    def test_a_file_that_lost_a_function_is_partial_and_the_other_is_not(self, project):
        (project / "w.cpp").write_text(WARNING_ONLY)
        (project / "l.py").write_text("def good():\n    return 1\n\ndef bad(:\n    return 2\n")
        data = _json("scan")
        assert data["files_partially_parsed"] == 1 and data["files_read_with_warnings"] == 1
        assert [item["file"] for item in data["partial_files"]] == ["l.py"]
        status = _json("status")
        assert status["partially_parsed"]["files"] == 1 and status["partially_parsed"]["files_read_with_warnings"] == 1

    def test_what_is_stored_for_a_file_with_warnings_stays_when_it_is_edited_into_a_worse_state(self, project):
        (project / "w.cpp").write_text(WARNING_ONLY)
        _json("scan")
        (project / "w.cpp").write_text("int one() { return 1; }\nint x = ;\nint y = ;\n")  # `two` is gone from the text
        data = _json("scan", "--full")
        assert data["files_read_with_warnings"] == 1
        found = _json("search", "two", "--code-only")
        assert found["total_matches"] == 1  # kept: the error may be hiding it
