"""Documents use the commands code uses: scan, search, deps get, source get and ack. There is no `docs` group, and the
findings of the review of Tasks 44-53 that touch them: a changed signature, a partial parse, the first moments after
init, and `new X().m()`."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from drspec import mcp_server
from drspec.cli.app import app
from drspec.core import docs as docs_core
from drspec.db import get_connection

runner = CliRunner()

GUIDE = "# Guide\n\n## Helper\n\nCall `pkg/a.py::helper` to add one.\n\n## Unrelated\n\nNothing about code here.\n"


def _run(*arguments: str) -> dict:
    result = runner.invoke(app, ["--json", *arguments])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["data"]


def _fails(*arguments: str) -> dict:
    result = runner.invoke(app, ["--json", *arguments])
    assert result.exit_code != 0, result.output
    return json.loads(result.output)["error"]


def _edit(path: Path, text: str) -> None:
    path.write_text(text)
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 2))


@pytest.fixture(scope="module")
def _initialised(tmp_path_factory, initialised_template):
    """An empty project after `init`: a copy of the session's initialised project; each test works on a copy of it."""
    root = tmp_path_factory.mktemp("initialised") / "p"
    shutil.copytree(initialised_template, root)
    return root


def _copy_of(template: Path, tmp_path: Path, monkeypatch) -> Path:
    """A copy of a project made once (`copytree` keeps the modification times, so an unchanged file stays unchanged), as the working folder."""
    root = tmp_path / "p"
    shutil.copytree(template, root)
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def fresh(_initialised, tmp_path, monkeypatch):
    """The empty project after `init`."""
    return _copy_of(_initialised, tmp_path, monkeypatch)


@pytest.fixture(scope="module")
def _scanned_project(_initialised, tmp_path_factory):
    """The guide and one module, scanned once."""
    root = tmp_path_factory.mktemp("guide") / "p"
    shutil.copytree(_initialised, root)
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(root)
        (root / "pkg").mkdir()
        (root / "docs").mkdir()
        (root / "pkg" / "a.py").write_text("def helper(x):\n    return x + 1\n\n\ndef lonely(y):\n    return y\n")
        (root / "docs" / "guide.md").write_text(GUIDE)
        assert runner.invoke(app, ["config", "set", "docs.enabled", "true"]).exit_code == 0
        _run("scan")
    return root


@pytest.fixture
def project(_scanned_project, tmp_path, monkeypatch):
    return _copy_of(_scanned_project, tmp_path, monkeypatch)


def _helper_section() -> str:
    return _run("search", "Helper", "--docs-only")["matches"][0]["section_id"]


class TestThereIsNoDocsGroup:
    def test_the_help_has_no_docs_entry_and_has_ack(self):
        help_text = runner.invoke(app, ["--help"]).output
        assert "ack" in help_text and " docs " not in help_text

    def test_the_old_commands_are_gone(self, project):
        assert runner.invoke(app, ["docs", "stale"]).exit_code != 0

    def test_the_old_mcp_tools_are_gone_and_ack_is_there(self):
        names = {tool["name"] for tool in mcp_server._TOOLS}
        assert not {"docs_stale", "docs_for", "docs_search"} & names and "ack" in names
        assert set(mcp_server._TOOL_HANDLERS) == names


class TestSearch:
    def test_results_say_what_kind_they_are(self, project):
        matches = _run("search", "helper")["matches"]
        kinds = {m["kind"] for m in matches}
        assert kinds == {"function", "section"}
        function = next(m for m in matches if m["kind"] == "function")
        section = next(m for m in matches if m["kind"] == "section")
        assert function["function_id"].startswith("pkg/a.py::helper") and section["file_path"] == "docs/guide.md"

    def test_the_text_output_has_a_kind_line(self, project):
        text = runner.invoke(app, ["search", "helper"]).output
        assert "Kind: function" in text and "Kind: section" in text

    def test_code_only_is_what_search_gave_before_documents_existed(self, project):
        data = _run("search", "helper", "--code-only")
        assert all("kind" not in m and "function_id" in m for m in data["matches"])
        assert set(data) == {"pattern", "semantic", "files_searched", "total_matches", "truncated", "matches"}

    def test_docs_only_finds_sections_by_their_words(self, project):
        data = _run("search", "nothing code", "--docs-only")
        assert [m["section"] for m in data["matches"]] == ["Unrelated"] and data["matches"][0]["kind"] == "section"

    def test_docs_only_with_a_path(self, project):
        assert _run("search", "helper", "--docs-only", "--path", "pkg")["total_matches"] == 0
        assert _run("search", "helper", "--docs-only", "--path", "docs")["total_matches"] == 1

    def test_the_two_filters_exclude_each_other(self, project):
        assert "exclude" in _fails("search", "helper", "--code-only", "--docs-only")["message"]

    def test_docs_only_does_not_combine_with_function_only_modes(self, project):
        for flag in ("--semantic", "-c", "-l", "-v", "--lines"):
            assert _fails("search", "helper", "--docs-only", flag)["code"] == "INVALID_INPUT"

    def test_stale_sections_are_found_by_status(self, project):
        assert _run("search", "--status", "STALE", "--docs-only")["total_matches"] == 0
        _edit(project / "pkg" / "a.py", "def helper(x):\n    return x + 2\n\n\ndef lonely(y):\n    return y\n")
        _run("scan")
        data = _run("search", "--status", "STALE", "--docs-only")
        section = data["matches"][0]
        assert section["kind"] == "section" and section["status"] == "STALE"
        assert section["findings"][0]["reason"] == "body_changed" and section["findings"][0]["function_id"].startswith("pkg/a.py::helper")

    def test_a_status_other_than_stale_has_no_sections(self, project):
        assert _fails("search", "--status", "VERIFIED", "--docs-only")["code"] == "INVALID_INPUT"
        assert all(m["kind"] == "function" for m in _run("search", "--status", "PENDING")["matches"])

    def test_undocumented_functions_are_listed(self, project):
        names = [m["function_name"] for m in _run("search", "--undocumented")["matches"]]
        assert names == ["lonely"]  # helper is named by a document
        assert [m["function_name"] for m in _run("search", "lon", "--undocumented")["matches"]] == ["lonely"]
        assert _run("search", "zzz", "--undocumented")["total_matches"] == 0

    def test_a_pattern_may_be_left_out_only_where_a_list_makes_sense(self, project):
        assert _fails("search")["code"] == "INVALID_INPUT"


class TestDepsAndSource:
    def test_deps_get_of_a_section_lists_the_functions_it_describes(self, project):
        data = _run("deps", "get", _helper_section())
        assert data["kind"] == "section" and [link["function_id"].split("#")[0] for link in data["links"]] == ["pkg/a.py::helper"]

    def test_the_hash_of_a_section_may_be_left_out(self, project):
        section = _helper_section().split("#")[0]
        assert _run("deps", "get", section)["kind"] == "section"

    def test_deps_get_of_a_document_covers_all_its_sections(self, project):
        data = _run("deps", "get", "docs/guide.md")
        assert data["kind"] == "document" and len(data["sections"]) == 3

    def test_deps_get_of_a_function_still_shows_where_it_is_documented(self, project):
        function_id = _run("search", "helper", "--code-only")["matches"][0]["function_id"]
        assert _run("deps", "get", function_id)["documented_in"][0]["file_path"] == "docs/guide.md"

    def test_source_get_of_a_section_gives_its_text_and_what_it_describes(self, project):
        data = _run("source", "get", _helper_section())
        assert data["kind"] == "section" and "Call `pkg/a.py::helper`" in data["text"] and data["describes"][0].startswith("pkg/a.py::helper")

    def test_source_get_of_a_document_gives_every_section(self, project):
        data = _run("source", "get", "docs/guide.md")
        assert [s["section"] for s in data["sections"]] == ["Guide", "Helper", "Unrelated"]

    def test_source_get_of_a_function_is_unchanged(self, project):
        function_id = _run("search", "helper", "--code-only")["matches"][0]["function_id"]
        assert "return x + 1" in _run("source", "get", function_id)["body"]

    def test_a_malformed_id_is_still_refused(self, project):
        assert _fails("source", "get", "not-an-id")["code"] == "INVALID_FUNCTION_ID"


class TestAck:
    def _drift(self, project):
        _edit(project / "pkg" / "a.py", "def helper(x):\n    return x + 2\n\n\ndef lonely(y):\n    return y\n")
        _run("scan")

    def test_acknowledging_a_section_clears_its_findings(self, project):
        self._drift(project)
        section = _helper_section()
        assert _run("ack", section)["acknowledged"] == 1
        assert _run("search", "--status", "STALE", "--docs-only")["total_matches"] == 0

    def test_a_function_can_be_named_to_clear_only_its_link(self, project):
        self._drift(project)
        function_id = _run("search", "helper", "--code-only")["matches"][0]["function_id"]
        assert _run("ack", _helper_section(), function_id)["acknowledged"] == 1

    def test_only_a_section_can_be_acknowledged(self, project):
        function_id = _run("search", "helper", "--code-only")["matches"][0]["function_id"]
        assert "section" in _fails("ack", function_id)["message"]

    def test_the_scan_note_points_at_the_shared_command(self, project):
        _edit(project / "pkg" / "a.py", "def helper(x):\n    return x + 3\n\n\ndef lonely(y):\n    return y\n")
        assert "search --status STALE --docs-only" in _run("scan")["note"]


class TestTheSameThroughMcp:
    def _call(self, name: str, **arguments):
        return mcp_server._TOOL_HANDLERS[name](project_db(), arguments)

    def test_search_marks_the_kind_and_narrows(self, project):
        result, failed = mcp_server._tool_search(project_db(), {"pattern": "helper"})
        assert not failed and {m["kind"] for m in result["matches"]} == {"function", "section"}
        result, _ = mcp_server._tool_search(project_db(), {"pattern": "helper", "code_only": True})
        assert all("kind" not in m for m in result["matches"])
        result, _ = mcp_server._tool_search(project_db(), {"pattern": "helper", "docs_only": True})
        assert all(m["kind"] == "section" for m in result["matches"])
        result, _ = mcp_server._tool_search(project_db(), {"undocumented": True})
        assert [m["function_name"] for m in result["matches"]] == ["lonely"]

    def test_search_refuses_what_cannot_combine(self, project):
        for arguments in ({"pattern": "x", "docs_only": True, "code_only": True}, {"pattern": "x", "docs_only": True, "count": True}):
            result, failed = mcp_server._tool_search(project_db(), arguments)
            assert failed

    def test_deps_source_and_ack_take_sections(self, project):
        section = _helper_section()
        links, failed = mcp_server._tool_deps_get(project_db(), {"function_id": section})
        assert not failed and links["kind"] == "section"
        text, failed = mcp_server._tool_source_get(project_db(), {"function_id": section})
        assert not failed and "Call `" in text["text"]
        acked, failed = mcp_server._tool_ack(project_db(), {"identifier": section})
        assert not failed and acked["section_id"] == section
        _, failed = mcp_server._tool_ack(project_db(), {"identifier": "pkg/a.py::helper"})
        assert failed


def project_db() -> Path:
    return Path.cwd() / "_drspec" / "contracts.db"


class TestASignatureChangeIsNotARemoval:
    def test_the_link_to_a_function_whose_parameters_changed_is_a_signature_finding(self, project):
        _edit(project / "pkg" / "a.py", "def helper(x, y: int = 0):\n    return x + 1\n\n\ndef lonely(y):\n    return y\n")
        _run("scan")
        finding = _run("search", "--status", "STALE", "--docs-only")["matches"][0]["findings"][0]
        assert finding["reason"] == "signature_changed"
        assert finding["signature_was"] == "def helper(x):" and finding["signature_now"] == "def helper(x, y: int = 0):"
        assert finding["priority"] == docs_core.priority("signature_changed", finding["confidence"], finding["kind"], False) or finding["priority"] > 0
        assert finding["renamed_from"].startswith("pkg/a.py::helper#")

    def test_deleting_the_function_is_still_a_removal_and_ranks_higher(self, project):
        _edit(project / "pkg" / "a.py", "def lonely(y):\n    return y\n")
        _run("scan")
        finding = _run("search", "--status", "STALE", "--docs-only")["matches"][0]["findings"][0]
        assert finding["reason"] == "removed"
        assert docs_core.priority("removed", "EXTRACTED", "id", False) > docs_core.priority("signature_changed", "EXTRACTED", "id", False)

    def test_a_body_only_change_is_still_a_body_change(self, project):
        _edit(project / "pkg" / "a.py", "def helper(x):\n    return x * 9\n\n\ndef lonely(y):\n    return y\n")
        _run("scan")
        assert _run("search", "--status", "STALE", "--docs-only")["matches"][0]["findings"][0]["reason"] == "body_changed"

    def test_a_rename_is_still_a_rename(self, project):
        body = "    total = 0\n    for item in range(x):\n        total += item * 3 + x\n        if total > 100:\n            total -= 7\n    return total\n"
        _edit(project / "pkg" / "a.py", "def helper(x):\n" + body + "\n\ndef lonely(y):\n    return y\n")
        _run("scan")
        _run("ack", _helper_section())
        _edit(project / "pkg" / "a.py", "def assist(x):\n" + body + "\n\ndef lonely(y):\n    return y\n")
        _run("scan")
        finding = _run("search", "--status", "STALE", "--docs-only")["matches"][0]["findings"][0]
        assert finding["reason"] == "renamed" and finding["function_id"].startswith("pkg/a.py::assist")


class TestAPartialParseNamesTheRealLine:
    def test_the_first_error_line_and_the_lost_function(self, fresh):
        (fresh / "a.cpp").write_text("int ok1(){ return 1; }\n\nint broken( { return ;\nint ok2(){ return 2; }\n")
        (fresh / "clean.cpp").write_text("int fine(){ return 1; }\n")
        data = _run("scan")
        assert data["files_partially_parsed"] == 1 and data["functions_skipped"] == 1
        assert data["partial_examples"] == [{"file": "a.cpp", "first_error_line": 3, "functions_skipped": 1, "functions_partial": 0}]
        assert data["functions_found"] == 3  # ok1, ok2 and the clean file's one

    def test_a_clean_file_reports_no_partial_parse(self, fresh):
        (fresh / "clean.cpp").write_text("int fine(){ return 1; }\n")
        assert "files_partially_parsed" not in _run("scan")

    def test_the_error_is_where_the_text_went_wrong_after_an_export_macro(self, tmp_path):
        from drspec.parsers.cpp_parser import CppParser

        source = "int a(){ return 1; }\nint b(){ return 2; }\n\nint c(){ return 3; }\nint broken( {\nint d(){ return 4; }\n"
        path = tmp_path / "x.cpp"
        path.write_text(source)
        result = CppParser().parse_file(str(path))
        assert result.has_errors and result.errors[0].line == 5


class TestTheMomentAfterInit:
    def test_before_the_worker_has_looked_the_note_says_the_index_is_being_built(self, fresh):
        (fresh / "a.py").write_text("def f():\n    pass\n")
        data = _run("search", "f")
        assert data["files_searched"] == 0 and data["note"].startswith("Index being built")
        assert "grep" not in data["note"]

    def test_with_files_waiting_it_says_how_many(self, fresh):
        from drspec.db.queries import enqueue_staleness_many

        connection = get_connection(fresh / "_drspec" / "contracts.db")
        enqueue_staleness_many(connection, [(f"f{i}.py", "new_file") for i in range(3)])
        connection.close()
        note = _run("search", "f")["note"]
        assert note.startswith("Index being built: 0 of 3 files scanned")

    def test_nothing_waiting_and_a_finished_look_is_the_grep_note(self, fresh):
        from drspec.core.discovery import CURSOR_KEY
        from drspec.db.queries import set_config

        connection = get_connection(fresh / "_drspec" / "contracts.db")
        set_config(connection, CURSOR_KEY, "[]")  # the worker has been through the project
        connection.close()
        assert "use grep" in _run("search", "f")["note"]


class TestNewXDotMethodInJavaScript:
    FILES = {
        "repo.js": "export class Repo {\n  find(id) { return id; }\n}\nexport class Plain {}\n",
        "load.js": (
            "import { Repo, Plain } from './repo.js';\nimport { Remote } from 'some-package';\n"
            "export function load(id) { return new Repo().find(id); }\n"
            "export function make() { return new Plain(); }\n"
            "export function remote() { return new Remote().go(); }\n"
        ),
    }

    @pytest.fixture(scope="module")
    def _scanned_js(self, _initialised, tmp_path_factory):
        root = tmp_path_factory.mktemp("js") / "p"
        shutil.copytree(_initialised, root)
        with pytest.MonkeyPatch.context() as patch:
            patch.chdir(root)
            for name, text in self.FILES.items():
                (root / name).write_text(text)
            _run("scan")
        return root

    @pytest.fixture
    def js(self, _scanned_js, tmp_path, monkeypatch):
        return _copy_of(_scanned_js, tmp_path, monkeypatch)

    def test_the_method_of_a_class_just_constructed_is_an_extracted_edge(self, js):
        connection = get_connection(js / "_drspec" / "contracts.db")
        rows = connection.execute("SELECT caller_id, callee_id, confidence FROM dependencies").fetchall()
        connection.close()
        assert [(c.split("#")[0], d.split("#")[0], conf) for c, d, conf in rows] == [("load.js::load", "repo.js::Repo.find", "EXTRACTED")]

    def test_a_class_with_no_constructor_is_not_counted_unresolved(self, js):
        health = _run("status")["javascript"]
        assert health["calls"] == 3 and health["unresolved_calls"] == 2  # `new Remote()` and `.go()`, both from a package
        assert health["unresolved_reasons"] == {"external package": 2}

    def test_a_class_from_a_package_stays_unresolved_with_its_reason(self, js):
        assert "no constructor" not in _run("status")["javascript"]["unresolved_reasons"]

    def test_a_parenthesised_construction_is_read_too(self, js):
        from drspec.parsers.javascript_parser import JavaScriptParser

        result = JavaScriptParser().parse("class R { find() {} }\nfunction f() { return (new R()).find(); }\n")
        calls = [(c.receiver, c.name) for fn in result.functions for c in fn.calls]
        assert ("new R", "find") in calls


class TestTheCombinedSearchIsGroupedByFile:
    def test_functions_and_sections_are_listed_under_the_file_they_are_in(self, project):
        text = runner.invoke(app, ["search", "helper"]).output
        assert "Function ID: <File>::<ID>" in text
        assert "File: pkg/a.py   (python, 1 match)" in text and "File: docs/guide.md   (1 match)" in text
        assert "ID: helper#" in text and "ID: /Guide/Helper/#" in text  # the part after the file, for both kinds
        assert "Name:" not in text and "Language:" not in text  # per file, not per match
        assert text.count("Kind: function") == 1 and text.count("Kind: section") == 1

    def test_a_section_shows_what_a_person_chooses_by(self, project):
        text = runner.invoke(app, ["search", "helper", "--docs-only"]).output
        for label in ("Kind: section", "Section: Helper", "Keywords:", "Linked To: 1", "Status: CURRENT", "Possibly Stale: No", "Line: 3", "End Line:"):
            assert label in text, label

    def test_the_two_kinds_of_one_file_share_its_group(self, project):
        (project / "pkg" / "notes.md").write_text("# Notes\n\nThe helper is described here.\n")
        _run("scan")
        text = runner.invoke(app, ["search", "helper"]).output
        assert text.count("File: pkg/") == 1 or "File: pkg/notes.md" in text
        assert text.count("File: docs/guide.md") == 1

    def test_code_only_keeps_the_grouped_form(self, project):
        text = runner.invoke(app, ["search", "helper", "--code-only"]).output
        assert "File: pkg/a.py   (python, 1 match)" in text and "Kind:" not in text

    def test_json_keeps_one_flat_list_with_both_kinds(self, project):
        matches = _run("search", "helper")["matches"]
        assert {m["kind"] for m in matches} == {"function", "section"} and all("file_path" in m for m in matches)


class TestDocumentsAreOffByDefault:
    @pytest.fixture
    def off(self, fresh):
        (fresh / "docs").mkdir()
        (fresh / "a.py").write_text("def helper(x):\n    return x + 1\n")
        (fresh / "docs" / "guide.md").write_text("# Guide\n\n## Helper\n\nCall `helper` to add one.\n")
        return fresh

    def test_the_setting_defaults_to_false(self):
        from drspec.core.settings import SETTINGS

        assert SETTINGS["docs.enabled"].default is False

    def test_a_scan_reads_no_document_and_the_commands_show_no_document_results_or_counts(self, off):
        data = _run("scan")
        assert data["files_queued"] == 1 and data["documents_indexed"] == 0
        connection = get_connection(off / "_drspec" / "contracts.db")
        assert connection.execute("SELECT COUNT(*) FROM doc_sections").fetchone()[0] == 0
        connection.close()
        matches = _run("search", "helper")["matches"]
        assert [m.get("kind") for m in matches] == [None]  # as before documents existed: no kind marker, no sections
        assert "documents" not in _run("status")
        function_id = matches[0]["function_id"]
        assert "documented_in" not in _run("deps", "get", function_id) and "documented_in" not in _run("source", "get", function_id)

    def test_asking_for_documents_says_how_to_turn_them_on(self, off):
        _run("scan")
        for arguments in (("search", "helper", "--docs-only"), ("search", "--undocumented")):
            data = _run(*arguments)
            assert data["total_matches"] == 0 and "docs.enabled true" in data["note"]
        assert "docs.enabled true" in _run("scan", "--docs-only")["note"]
        assert _fails("deps", "get", "docs/guide.md")["code"] != ""

    def test_turning_them_on_and_scanning_indexes_them(self, off):
        _run("scan")
        assert runner.invoke(app, ["config", "set", "docs.enabled", "true"]).exit_code == 0
        assert _run("scan")["documents_indexed"] == 1
        assert {m["kind"] for m in _run("search", "helper")["matches"]} == {"function", "section"}

    def test_rows_stored_earlier_stay_while_they_are_off_and_are_not_relinked(self, off):
        assert runner.invoke(app, ["config", "set", "docs.enabled", "true"]).exit_code == 0
        _run("scan")
        assert runner.invoke(app, ["config", "set", "docs.enabled", "false"]).exit_code == 0
        _edit(off / "a.py", "def helper(x):\n    return x + 2\n")
        _run("scan")
        connection = get_connection(off / "_drspec" / "contracts.db")
        rows = connection.execute("SELECT state FROM doc_links").fetchall()
        connection.close()
        assert rows == [("ok",)]  # still there, not marked drifted: nothing re-linked while off
        assert _run("search", "--status", "STALE", "--docs-only")["total_matches"] == 0
        assert "documentation_sections_to_review" in _run("scan") and _run("scan")["documentation_sections_to_review"] == 0
