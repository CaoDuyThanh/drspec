"""A method removed from its header and from its definition leaves nothing behind, in whatever order the two go: the index
after the edits is the index of a fresh scan of the same tree (artifacts, calls, references and the records kept for
them), with no declaration of a method that is not written anywhere and no call to it."""

from __future__ import annotations

import shutil
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.db import init_schema

runner = CliRunner()

HEADER = "#pragma once\nclass M { public: void work(); void keep(); };\n"
HEADER_WITHOUT = "#pragma once\nclass M { public: void keep(); };\n"
DEFINITIONS = '#include "m.h"\nvoid M::work() {}\nvoid M::keep() {}\n'
DEFINITIONS_WITHOUT = '#include "m.h"\nvoid M::keep() {}\n'
USER = '#include "m.h"\nvoid use(M& m) { m.work(); m.keep(); }\n'

#: Config rows that belong to a run (the worker, the settings, the scan records), not to what was indexed.
_RUN_PREFIXES = ("scan:run", "scan_run", "worker:", "setting:", "scan:last", "trace", "parser:", "scan:facts:", "scan:symbols", "scan:resolver", "scan:hash", "scan:records")


def _empty_project(root: Path) -> None:
    """The project database of `root` (in memory, kept between the commands: `kept_duckdb`), with the schema `init` makes: no `init` run."""
    (root / "_drspec").mkdir(exist_ok=True)
    connection = duckdb.connect(str(root / "_drspec" / "contracts.db"))
    init_schema(connection)
    connection.close()


def _state(root: Path) -> dict:
    conn = duckdb.connect(str(root / "_drspec" / "contracts.db"), read_only=True)
    try:
        return {
            "artifacts": sorted(row[0] for row in conn.execute("SELECT function_id FROM artifacts").fetchall()),
            "calls": sorted(conn.execute("SELECT caller_id, callee_id, confidence FROM dependencies").fetchall()),
            "references": sorted(conn.execute("SELECT caller_id, callee_id, confidence FROM code_references").fetchall()),
            "declarations": sorted(conn.execute("SELECT function_id, definition_id FROM function_declarations").fetchall()),
            "contexts": sorted(row[0] for row in conn.execute("SELECT key FROM config WHERE starts_with(key, 'artifact:context:')").fetchall()),
            "records": sorted(
                row[0] for row in conn.execute("SELECT key FROM config WHERE starts_with(key, 'scan:file:') OR starts_with(key, 'scan:imports:')").fetchall()
            ),
            "config": sorted(
                (key, value)
                for key, value in conn.execute("SELECT key, value FROM config").fetchall()
                if not key.startswith(_RUN_PREFIXES) and not key.startswith(("scan:file:", "scan:imports:", "scan:mentions:", "scan:jsfacts:", "artifact:context:"))
            ),
            "pending": conn.execute("SELECT COUNT(*) FROM pending_removals").fetchone()[0],
        }
    finally:
        conn.close()


def _scan(root: Path, monkeypatch) -> None:
    monkeypatch.chdir(root)
    assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0


def _fresh(root: Path, tmp_path: Path, monkeypatch) -> dict:
    """A scan of a copy of the tree, from nothing: what the index of this tree is."""
    copy = tmp_path / "fresh"
    if copy.exists():
        shutil.rmtree(copy)
    shutil.copytree(root, copy, ignore=shutil.ignore_patterns("_drspec"))
    monkeypatch.chdir(copy)
    _empty_project(copy)
    assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
    state = _state(copy)
    monkeypatch.chdir(root)
    return state


def _same_as_fresh(root: Path, tmp_path: Path, monkeypatch) -> dict:
    fresh = _fresh(root, tmp_path, monkeypatch)
    incremental = _state(root)
    for key in fresh:
        assert incremental[key] == fresh[key], f"{key}: after the edit {incremental[key]!r}, fresh {fresh[key]!r}"
    return incremental


@pytest.fixture
def project(tmp_path, monkeypatch, kept_duckdb, cached_cli_command):
    root = tmp_path / "project"
    root.mkdir()
    (root / "m.h").write_text(HEADER)
    (root / "m.cpp").write_text(DEFINITIONS)
    (root / "u.cpp").write_text(USER)
    monkeypatch.chdir(root)
    _empty_project(root)
    assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
    return root


def _callees(state: dict) -> list:
    return [callee.split("::", 1)[1].rsplit("#", 1)[0] for caller, callee, _confidence in state["calls"] if caller.startswith("u.cpp::use#")]


class TestBothGoInOneScan:
    def test_the_index_is_the_index_of_a_fresh_scan(self, project, tmp_path, monkeypatch):
        (project / "m.h").write_text(HEADER_WITHOUT)
        (project / "m.cpp").write_text(DEFINITIONS_WITHOUT)
        _scan(project, monkeypatch)
        state = _same_as_fresh(project, tmp_path, monkeypatch)
        assert not [function_id for function_id in state["artifacts"] if "::M::work" in function_id]  # no `m.h::M::work`
        assert not [key for key in state["contexts"] if "::M::work" in key]
        assert _callees(state) == ["M::keep"]  # `use` still calls `m.work()` in its text; nothing declares it
        assert state["pending"] == 0


class TestTheDefinitionGoesFirst:
    def test_the_prototype_stays_as_an_unmatched_declaration_and_goes_with_the_next_edit_of_the_header(self, project, tmp_path, monkeypatch):
        (project / "m.cpp").write_text(DEFINITIONS_WITHOUT)  # the header still declares `work`
        _scan(project, monkeypatch)
        state = _same_as_fresh(project, tmp_path, monkeypatch)
        assert "m.h::M::work#d9e613a7" in state["artifacts"] and sorted(_callees(state)) == ["M::keep", "M::work"]
        (project / "m.h").write_text(HEADER_WITHOUT)
        _scan(project, monkeypatch)
        state = _same_as_fresh(project, tmp_path, monkeypatch)
        assert not [function_id for function_id in state["artifacts"] if "::M::work" in function_id] and _callees(state) == ["M::keep"]


class TestTheDeclarationGoesFirst:
    def test_the_definition_stays_and_goes_with_the_next_edit_of_its_file(self, project, tmp_path, monkeypatch):
        (project / "m.h").write_text(HEADER_WITHOUT)  # `work` is still defined in m.cpp
        _scan(project, monkeypatch)
        state = _same_as_fresh(project, tmp_path, monkeypatch)
        # the definition is still in the index; nothing declares it for `use` to see, so `use` no longer reaches it (as in a fresh scan)
        assert "m.cpp::M::work#d9e613a7" in state["artifacts"] and _callees(state) == ["M::keep"]
        (project / "m.cpp").write_text(DEFINITIONS_WITHOUT)
        _scan(project, monkeypatch)
        state = _same_as_fresh(project, tmp_path, monkeypatch)
        assert not [function_id for function_id in state["artifacts"] if "::M::work" in function_id] and _callees(state) == ["M::keep"]
