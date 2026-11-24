"""A function a parse error kept out of the index is findable as skipped: `status --partial` lists every partly parsed file,
and a search, a source lookup or a dependency lookup for a skipped name says why it is not there."""

from __future__ import annotations

import json
import re

import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.mcp_server import (
    _tool_deps_get,
    _tool_search,
    _tool_source_get,
    _tool_status,
)

runner = CliRunner()

BROKEN_A = "int ok1(){ return 1; }\n\nint broken( { return ;\nint ok2(){ return 2; }\n"
BROKEN_B = "int fine_b(){ return 1; }\nint also_broken( { return ;\nint more_b(){ return 3; }\nint another_broken( { return ;\n"
NOTE = re.compile(r"not indexed: skipped by a parse error at line 3 of a\.cpp \(read on \d{4}-\d{2}-\d{2}\)")
NOTE_ALONE = re.compile("^" + NOTE.pattern + "$")


@pytest.fixture
def project(kept_project):
    """A project of partly parsed C++ files, scanned, in a folder with an empty in-memory project database (`kept_project`)."""
    (kept_project / "a.cpp").write_text(BROKEN_A)
    (kept_project / "b.cpp").write_text(BROKEN_B)
    (kept_project / "a.h").write_text("int ok1();\nint ok2();\n")
    (kept_project / "clean.cpp").write_text('#include "a.h"\nint fine(){ return ok1(); }\n')
    assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
    return kept_project


def _data(*args: str):
    result = runner.invoke(app, ["--json", *args])
    return result.exit_code, json.loads(result.output)


class TestTheListOfPartlyParsedFiles:
    def test_every_file_with_what_it_skipped_and_when_it_was_read(self, project):
        code, body = _data("status", "--partial")
        data = body["data"]
        assert code == 0 and data["total"] == 2 and data["shown"] == 2
        rows = {row["file"]: row for row in data["files"]}
        assert set(rows) == {"a.cpp", "b.cpp"}
        a = rows["a.cpp"]
        assert (a["first_error_line"], a["skipped"], a["functions_skipped"], a["functions_partial"]) == (3, ["broken"], 1, 0)
        assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", a["last_read"])
        assert rows["b.cpp"]["functions_skipped"] == 2 and sorted(rows["b.cpp"]["skipped"]) == ["also_broken", "another_broken"]
        assert [row["file"] for row in data["files"]][0] == "b.cpp"  # the worst first

    def test_limit_and_format_json(self, project):
        assert _data("status", "--partial", "--limit", "1")[1]["data"]["files"][0]["file"] == "b.cpp"
        assert _data("status", "--partial", "--limit", "1")[1]["data"]["shown"] == 1 and _data("status", "--partial", "--limit", "1")[1]["data"]["total"] == 2
        plain = runner.invoke(app, ["status", "--partial", "--format", "json"])
        assert plain.exit_code == 0 and json.loads(plain.output)["data"]["total"] == 2

    def test_the_text_lists_them(self, project):
        text = runner.invoke(app, ["status", "--partial"]).output
        assert "File: a.cpp" in text and "First Error Line: 3" in text and "- broken" in text and "Last Read:" in text

    def test_the_plain_status_is_as_it_was(self, project):
        assert "partially_parsed" in _data("status")[1]["data"] and "files" not in _data("status")[1]["data"]

    def test_the_mcp_tool_lists_them_too(self, project):
        result, failed = _tool_status(project / "_drspec" / "contracts.db", {"partial": True, "limit": 1})
        assert not failed and result["total"] == 2 and result["shown"] == 1 and result["files"][0]["file"] == "b.cpp"
        everything, _ = _tool_status(project / "_drspec" / "contracts.db", {"partial": True})
        assert everything["shown"] == 2 and "artifacts" not in everything

    def test_a_project_without_partly_parsed_files_lists_none(self, kept_project):
        (kept_project / "ok.cpp").write_text("int fine(){ return 1; }\n")
        runner.invoke(app, ["--json", "scan", "."])
        assert _data("status", "--partial")[1]["data"] == {"total": 0, "shown": 0, "files": []}


class TestASkippedNameIsSaidToBeSkipped:
    def test_search_says_so_instead_of_returning_nothing(self, project):
        code, body = _data("search", "broken")
        data = body["data"]
        names = {entry["name"] for entry in data["not_indexed"]}
        assert names == {"broken", "also_broken", "another_broken"} and data["matches"] == []
        first = next(entry for entry in data["not_indexed"] if entry["name"] == "broken")
        assert NOTE_ALONE.match(first["message"]) and first["line"] == 3 and first["first_error_line"] == 3
        assert "3 function(s) matching this were skipped by a parse error" in data["not_indexed_note"]

    def test_one_match_has_the_message_itself(self, project):
        data = _data("search", "ok1", "--include", "cpp")[1]["data"]
        assert "not_indexed" not in data  # ok1 is indexed
        data = _data("search", "^broken$", "-E")[1]["data"]
        assert NOTE_ALONE.match(data["not_indexed_note"]) and len(data["not_indexed"]) == 1

    def test_a_name_that_is_indexed_or_unknown_has_no_note(self, project):
        assert "not_indexed" not in _data("search", "ok2")[1]["data"]
        assert "not_indexed" not in _data("search", "nothing_like_it")[1]["data"]
        assert "not_indexed" not in _data("search", "broken", "-v")[1]["data"]

    def test_the_mcp_search_says_so_too(self, project):
        result, failed = _tool_search(project / "_drspec" / "contracts.db", {"pattern": "also_broken"})
        assert not failed and result["matches"] == []
        assert result["not_indexed"][0]["file_path"] == "b.cpp" and "line 2 of b.cpp" in result["not_indexed_note"]

    @pytest.mark.parametrize("given", ["a.cpp::broken", "a.cpp::broken#deadbeef", "broken"])
    def test_source_get_and_deps_get_say_not_indexed(self, project, given):
        for command in (["source", "get", given], ["deps", "get", given]):
            code, body = _data(*command)
            assert code == 1 and body["success"] is False
            assert NOTE.search(body["error"]["message"]), body["error"]["message"]
            assert body["error"]["details"]["not_indexed"][0]["name"] == "broken"
        for tool in (_tool_source_get, _tool_deps_get):
            message, failed = tool(project / "_drspec" / "contracts.db", {"function_id": given})
            assert failed and "not indexed: skipped by a parse error at line 3 of a.cpp (read on " in message

    def test_a_name_that_was_never_there_is_just_not_found(self, project):
        code, body = _data("source", "get", "a.cpp::never_there")
        assert code == 1 and "not indexed" not in body["error"]["message"] and "not_indexed" not in body["error"]["details"]

    def test_a_skipped_name_is_matched_in_its_own_file(self, project):
        # `broken` is skipped in a.cpp only: the same id in another file is just not found
        code, body = _data("source", "get", "b.cpp::broken")
        assert code == 1 and "not indexed" not in body["error"]["message"]


class TestNoCallersIsNotNone:
    def test_the_callers_say_how_many_files_are_partly_parsed(self, project):
        data = _data("deps", "get", "a.cpp::ok2", "--direction", "callers")[1]["data"]
        assert data["callers"] == [] and data["partially_parsed_files"] == 2
        assert "2 file(s) in this project are partly parsed" in data["callers_note"] and "drspec status --partial" in data["callers_note"]
        text = runner.invoke(app, ["deps", "get", "a.cpp::ok2", "--direction", "callers"]).output
        assert "Callers Note: 2 file(s) in this project are partly parsed" in text


    def test_a_function_with_callers_gets_it_too_and_callees_do_not(self, project):
        data = _data("deps", "get", "a.cpp::ok1", "--direction", "both")[1]["data"]
        assert data["callers"] and data["partially_parsed_files"] == 2
        assert "callers_note" not in _data("deps", "get", "a.cpp::ok1", "--direction", "callees")[1]["data"]

    def test_the_mcp_tool_returns_the_same_fields(self, project):
        result, failed = _tool_deps_get(project / "_drspec" / "contracts.db", {"function_id": "a.cpp::ok2", "direction": "callers"})
        assert not failed and result["partially_parsed_files"] == 2 and "partly parsed" in result["callers_note"]
        callees, _ = _tool_deps_get(project / "_drspec" / "contracts.db", {"function_id": "a.cpp::ok2", "direction": "callees"})
        assert "callers_note" not in callees

    def test_a_project_with_no_partly_parsed_file_has_no_note(self, kept_project):
        (kept_project / "ok.cpp").write_text("int fine(){ return 1; }\nint user(){ return fine(); }\n")
        runner.invoke(app, ["--json", "scan", "."])
        data = _data("deps", "get", "ok.cpp::user", "--direction", "callers")[1]["data"]
        assert "callers_note" not in data and "partially_parsed_files" not in data
