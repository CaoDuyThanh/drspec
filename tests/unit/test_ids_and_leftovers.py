"""Small fixes found by reading a real C++ project: an id written without its namespace, the suggestions for an id that was not found
(asked of the whole index), the scan that says why its total fell, and the entry of a file that is gone that no scan removed."""

from __future__ import annotations

import json

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.core.dependency_info import find_similar_functions
from drspec.db.queries import AmbiguousFunctionId, insert_artifacts, resolve_function_id

runner = CliRunner()

NAMESPACED = "namespace zmp {\nstruct Avoid {\n  bool isDo();\n};\nbool Avoid::isDo() { return true; }\n}\n"


def _scan(*args):
    result = runner.invoke(app, ["--json", "scan", ".", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["data"]


def _conn(project):
    return duckdb.connect(str(project / "_drspec" / "contracts.db"))  # pattern-ok: duckdb-file kept in memory by `kept_duckdb`


class TestAnIdWithoutItsNamespace:
    @pytest.fixture
    def project(self, kept_project):
        (kept_project / "a.cpp").write_text(NAMESPACED)
        _scan()
        return kept_project

    def test_the_name_without_its_namespace_resolves_when_it_is_the_only_one_in_the_file(self, project):
        conn = _conn(project)
        try:
            resolved = resolve_function_id(conn, "a.cpp::Avoid::isDo")
        finally:
            conn.close()
        assert resolved.startswith("a.cpp::zmp::Avoid::isDo#")

    def test_deps_get_accepts_it(self, project):
        result = runner.invoke(app, ["--json", "deps", "get", "a.cpp::Avoid::isDo"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["function_id"].startswith("a.cpp::zmp::Avoid::isDo#")

    def test_two_namespaces_with_the_same_class_are_ambiguous_and_listed(self, project):
        (project / "a.cpp").write_text(NAMESPACED + NAMESPACED.replace("zmp", "other"))
        _scan()
        conn = _conn(project)
        try:
            with pytest.raises(AmbiguousFunctionId) as raised:
                resolve_function_id(conn, "a.cpp::Avoid::isDo")
        finally:
            conn.close()
        assert len(raised.value.candidates) == 2 and "a.cpp::other::Avoid::isDo" in str(raised.value) and "a.cpp::zmp::Avoid::isDo" in str(raised.value)

    def test_a_name_that_does_not_end_with_what_was_written_is_not_guessed(self, project):
        conn = _conn(project)
        try:
            assert resolve_function_id(conn, "a.cpp::Other::isDo") == "a.cpp::Other::isDo"  # unchanged: the caller reports it
            assert resolve_function_id(conn, "b.cpp::Avoid::isDo") == "b.cpp::Avoid::isDo"  # another file
        finally:
            conn.close()


class TestSuggestionsComeFromTheWholeIndex:
    def test_a_function_past_the_first_thousand_is_still_suggested(self, kept_project):
        conn = _conn(kept_project)
        try:
            from drspec.db import init_schema

            init_schema(conn)
            filler = [(f"f{i % 50}.cpp::filler{i}#00000000", f"f{i % 50}.cpp", f"filler{i}", "void f()", "", "h", "cpp", 1, 1, None, "PENDING", False) for i in range(1500)]
            target = ("z.cpp::zmp::Cls::target#12345678", "z.cpp", "target", "void target()", "", "h", "cpp", 1, 1, "zmp::Cls", "PENDING", False)
            insert_artifacts(conn, [*filler, target])
            assert find_similar_functions(conn, "z.cpp::Cls::targt") == ["z.cpp::zmp::Cls::target#12345678"]  # a misspelling: the functions of that file
            assert find_similar_functions(conn, "nowhere.cpp::Cls::zzzzzz") == []  # nothing like it anywhere: nothing is made up
            assert find_similar_functions(conn, "z.cpp::Cls::target") == ["z.cpp::zmp::Cls::target#12345678"]
            assert find_similar_functions(conn, "other.cpp::Cls::target")[0] == "z.cpp::zmp::Cls::target#12345678"
        finally:
            conn.close()

    def test_the_commands_that_look_a_function_up_use_the_same_suggestions(self, kept_project):
        (kept_project / "a.cpp").write_text(NAMESPACED)
        _scan()
        for command in (["deps", "get"], ["source", "get"]):
            result = runner.invoke(app, ["--json", *command, "elsewhere.cpp::Avoid::isDo"])
            assert result.exit_code != 0
            suggestions = json.loads(result.output)["error"]["details"].get("suggestions", [])
            assert any(item.startswith("a.cpp::zmp::Avoid::isDo#") for item in suggestions), (command, result.output[:300])


class TestAFullRescanSaysWhyTheTotalFell:
    @pytest.fixture
    def project(self, kept_project):
        (kept_project / "keep.py").write_text("def kept():\n    return 1\n")
        (kept_project / "drop.py").write_text("def one():\n    return 1\n\n\ndef two():\n    return 2\n")
        _scan()
        return kept_project

    def test_the_files_and_functions_removed_because_they_are_ignored_now_are_counted_with_the_reason(self, project):
        assert runner.invoke(app, ["config", "set", "scan.ignore", "drop.py"]).exit_code == 0
        data = _scan("--full")
        assert data["files_removed_as_ignored"] == 1 and data["functions_removed_as_ignored"] == 2
        assert data["removed_note"].startswith("1 file(s) with 2 function(s) were removed from the index")
        assert ".gitignore, scan.ignore or a generated folder" in data["removed_note"]
        text = runner.invoke(app, ["scan", ".", "--full"]).output
        assert "Removed Note:" not in text  # the second scan has nothing left to remove

    def test_a_scan_that_removes_nothing_has_no_such_note(self, project):
        assert "removed_note" not in _scan("--full")

    def test_the_text_output_has_the_note(self, project):
        assert runner.invoke(app, ["config", "set", "scan.ignore", "drop.py"]).exit_code == 0
        assert "1 file(s) with 2 function(s) were removed from the index" in runner.invoke(app, ["scan", ".", "--full"]).output  # the default: the sentence, without the label


class TestTheEntryOfAFileThatIsGone:
    """The functions of a file are in the index, its file is not on disk, and the file has no row of `scanned_files`: how a file created and
    deleted between a worker's read and its publish was left. Before, no scan removed it ("up to date, 0 removed")."""

    def _count(self, project, table_filter: str) -> int:
        conn = _conn(project)
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table_filter}").fetchone()[0]
        finally:
            conn.close()

    def test_a_scan_removes_the_functions_and_the_snapshot_of_such_a_file(self, kept_project):
        (kept_project / "keep.py").write_text("def kept():\n    return 1\n")
        (kept_project / "gone.py").write_text("def one():\n    return 1\n\n\ndef two():\n    return 2\n")
        _scan()
        conn = _conn(kept_project)
        conn.execute("DELETE FROM scanned_files WHERE file_path = 'gone.py'")  # the state: functions and snapshot, no row
        conn.close()
        (kept_project / "gone.py").unlink()
        assert self._count(kept_project, "artifacts WHERE file_path = 'gone.py'") == 2
        assert self._count(kept_project, "scanned_files WHERE file_path = 'gone.py'") == 0

        data = _scan()
        assert self._count(kept_project, "artifacts WHERE file_path = 'gone.py'") == 0
        assert self._count(kept_project, "config WHERE key = 'scan:file:gone.py'") == 0 and self._count(kept_project, "artifacts WHERE file_path = 'keep.py'") == 1
        assert data["functions_removed"] == 2 and not data["up_to_date"]
        assert _scan()["up_to_date"] is True  # and the next scan has nothing to do

    def test_a_file_that_vanishes_between_the_read_and_the_publish_is_not_published(self, kept_project, monkeypatch):
        from drspec import staleness_worker

        (kept_project / "flash.py").write_text("def blink():\n    return 1\n")
        real = staleness_worker.begin_publish

        def vanishing(requests, file_paths):
            (kept_project / "flash.py").unlink()  # deleted after it was read, before it is published
            return real(requests, file_paths)

        monkeypatch.setattr(staleness_worker, "begin_publish", vanishing)
        conn = _conn(kept_project)
        try:
            staleness_worker._remediate_queued_files(conn, kept_project / "_drspec" / "contracts.db", ["flash.py"])
        finally:
            conn.close()
        assert self._count(kept_project, "artifacts WHERE file_path = 'flash.py'") == 0
        assert self._count(kept_project, "config WHERE key = 'scan:file:flash.py'") == 0 and self._count(kept_project, "scanned_files WHERE file_path = 'flash.py'") == 0
