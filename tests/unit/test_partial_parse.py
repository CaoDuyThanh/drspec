"""A file with a construct the parser cannot read is indexed for what did parse; nothing stored for it is removed."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.db.queries import enqueue_staleness_many, list_staleness_queue
from drspec.staleness_worker import _remediate_queued_files

runner = CliRunner()

SPECIALISED = """template<typename T> class Node { public: ~Node() = default; int tick() { return 1; } };
int helper() { return 2; }
template<> Node<int>::~Node<int>() = default;
template<> Node<double>::~Node<double>() = delete;
int after() { return 3; }
"""


@pytest.fixture
def project(kept_project):
    return kept_project  # the working folder with an empty project database in memory: `init` is not what these tests are about


def _scan(*args):
    result = runner.invoke(app, ["--json", "scan", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["data"]


def _names(project: Path):
    conn = duckdb.connect(str(project / "_drspec" / "contracts.db"), read_only=True)
    try:
        return sorted(row[0] for row in conn.execute("SELECT function_name FROM artifacts").fetchall())
    finally:
        conn.close()


def _config(project: Path, key_prefix: str):
    conn = duckdb.connect(str(project / "_drspec" / "contracts.db"), read_only=True)
    try:
        return conn.execute("SELECT key, value FROM config WHERE starts_with(key, ?)", [key_prefix]).fetchall()
    finally:
        conn.close()


class TestValidCppTheGrammarCannotRead:
    def test_the_other_functions_are_indexed_and_the_specialisations_are_not_errors(self, project):
        (project / "node.cpp").write_text(SPECIALISED)
        (project / "clean.cpp").write_text("int clean() { return 0; }\n")
        data = _scan(".")
        assert _names(project) == ["after", "clean", "helper", "tick", "~Node"]
        assert data.get("error_count", 0) == 0 and data["skipped_constructs"] == 2
        assert "files_partially_parsed" not in data

    def test_search_finds_them(self, project):
        (project / "node.cpp").write_text(SPECIALISED)
        _scan(".")
        result = runner.invoke(app, ["--json", "search", "helper"])
        assert [m["function_name"] for m in json.loads(result.output)["data"]["matches"]] == ["helper"]

    def test_the_file_is_not_left_in_retry_wait(self, project):
        (project / "node.cpp").write_text(SPECIALISED)
        _scan(".")
        assert _config(project, "partial:") == []
        conn = duckdb.connect(str(project / "_drspec" / "contracts.db"), read_only=True)
        try:
            assert conn.execute("SELECT COUNT(*) FROM rescan_backoff").fetchone() == (0,)
        finally:
            conn.close()


class TestARealSyntaxError:
    SOURCE = "def one():\n    return 1\n\n\ndef two():\n    return 2\n\n\ndef three():\n    return 3\n\n\ndef four():\n    return 4\n"

    def _broken(self):
        return self.SOURCE.replace("def two():\n    return 2\n", "def two():\n    return (2 +\n")

    def test_the_clean_functions_are_indexed_and_the_broken_one_is_kept_and_marked_partial(self, project):
        (project / "m.py").write_text(self.SOURCE)
        _scan(".")
        (project / "m.py").write_text(self._broken())
        data = _scan(".")
        assert _names(project) == ["four", "one", "three", "two"]  # `two` is still stored, as it was
        # `two` could be recognised as a function, so it is kept and counted as partial, not as skipped
        assert data["files_partially_parsed"] == 1 and data["functions_partial"] == 1 and data["functions_skipped"] == 0
        assert data["partial_examples"] == [{"file": "m.py", "first_error_line": data["partial_examples"][0]["first_error_line"], "functions_skipped": 0, "functions_partial": 1}]
        assert data["error_count"] >= 1  # the real error is still reported
        search = json.loads(runner.invoke(app, ["--json", "search", "two", "--code-only"]).output)["data"]["matches"]
        assert [(m["function_name"], m.get("partial")) for m in search] == [("two", True)]
        assert json.loads(runner.invoke(app, ["--json", "source", "get", "m.py::two"]).output)["data"]["partial"] is True
        assert "partial" not in json.loads(runner.invoke(app, ["--json", "source", "get", "m.py::one"]).output)["data"]

    def test_a_half_typed_file_never_deletes_what_is_stored_and_the_next_clean_save_does(self, project):
        (project / "m.py").write_text(self.SOURCE)
        _scan(".")
        # `four` is deleted while a syntax error elsewhere is still there.
        half_typed = self._broken().replace("\n\ndef four():\n    return 4\n", "\n")
        (project / "m.py").write_text(half_typed)
        data = _scan(".")
        assert data["functions_removed"] == 0 and "four" in _names(project)
        # The next clean save: now the removal is real.
        (project / "m.py").write_text(self.SOURCE.replace("\n\ndef four():\n    return 4\n", "\n"))
        data = _scan(".")
        assert data["functions_removed"] == 1 and _names(project) == ["one", "three", "two"]
        assert _config(project, "partial:") == []  # the file is whole again

    def test_status_says_so(self, project):
        (project / "m.py").write_text(self._broken())
        _scan(".")
        result = runner.invoke(app, ["--json", "status"])
        partial = json.loads(result.output)["data"]["partially_parsed"]
        assert partial["files"] == 1 and partial["functions_partial"] == 1 and partial["functions_skipped"] == 0 and partial["examples"][0]["file"] == "m.py"
        assert "Partially Parsed" in runner.invoke(app, ["status"]).output

    def test_a_contract_on_the_broken_function_survives(self, project):
        from drspec.contracts import Contract

        (project / "m.py").write_text(self.SOURCE)
        _scan(".")
        contract = Contract.from_dict(dict(
            function_signature="def two():", intent_summary="Return two",
            invariants=[dict(name="rule0", logic="Output is positive", criticality="HIGH", on_fail="error")],
        )).to_json()
        assert runner.invoke(app, ["contract", "save", "m.py::two", "--confidence", "80"], input=contract).exit_code == 0
        (project / "m.py").write_text(self._broken())
        _scan(".")
        assert runner.invoke(app, ["--json", "contract", "get", "m.py::two"]).exit_code == 0


class TestOtherLanguages:
    def test_javascript_behaves_the_same(self, project):
        (project / "a.js").write_text("function good() { return 1; }\nfunction other() { return 2; }\n")
        _scan(".")
        (project / "a.js").write_text("function good() { return 1; }\nfunction other() { return (2 + ; }\n")
        data = _scan(".")
        assert data["files_partially_parsed"] == 1 and _names(project) == ["good", "other"]

    def test_python_with_the_error_in_a_method(self, project):
        (project / "c.py").write_text("class C:\n    def ok(self):\n        return 1\n\n    def bad(self):\n        return (1 +\n\n\ndef free():\n    return 3\n")
        data = _scan(".")
        assert "free" in _names(project) and "ok" in _names(project) and data["files_partially_parsed"] == 1


class TestTheWorker:
    def test_a_partial_file_is_settled_not_retried(self, project):
        (project / "m.py").write_text("def good():\n    return 1\n\n\ndef bad():\n    return (1 +\n")
        conn = duckdb.connect(str(project / "_drspec" / "contracts.db"))
        try:
            enqueue_staleness_many(conn, [("m.py", "new_file")])
            assert _remediate_queued_files(conn, project / "_drspec" / "contracts.db", ["m.py"]) == 1
        finally:
            conn.close()
        conn = duckdb.connect(str(project / "_drspec" / "contracts.db"), read_only=True)
        try:
            assert list_staleness_queue(conn) == []
            assert conn.execute("SELECT COUNT(*) FROM rescan_backoff").fetchone() == (0,)
            assert sorted(r[0] for r in conn.execute("SELECT function_name FROM artifacts").fetchall()) == ["bad", "good"]  # `bad` is kept, marked partial
            assert len(conn.execute("SELECT key FROM config WHERE starts_with(key, 'partial:')").fetchall()) == 1
        finally:
            conn.close()
