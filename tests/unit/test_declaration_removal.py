"""A method removed from its header and from its definition leaves nothing behind, in whatever order the two go: the index
after the edits is the index of a fresh scan of the same tree (artifacts, calls, references and the records kept for
them), with no declaration of a method that is not written anywhere and no call to it."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app

runner = CliRunner()

HEADER = "#pragma once\nclass M { public: void work(); void keep(); };\n"
HEADER_WITHOUT = "#pragma once\nclass M { public: void keep(); };\n"
DEFINITIONS = '#include "m.h"\nvoid M::work() {}\nvoid M::keep() {}\n'
DEFINITIONS_WITHOUT = '#include "m.h"\nvoid M::keep() {}\n'
USER = '#include "m.h"\nvoid use(M& m) { m.work(); m.keep(); }\n'

#: Config rows that belong to a run (the worker, the settings, the scan records), not to what was indexed.
_RUN_PREFIXES = ("scan:run", "scan_run", "worker:", "setting:", "scan:last", "trace", "parser:", "scan:facts:", "scan:symbols", "scan:resolver", "scan:hash", "scan:records")


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


@pytest.fixture
def project(kept_project):
    root = kept_project  # the working folder, with an empty project database in memory: `init` is not the objective
    (root / "m.h").write_text(HEADER)
    (root / "m.cpp").write_text(DEFINITIONS)
    (root / "u.cpp").write_text(USER)
    assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
    return root


def _callees(state: dict) -> list:
    return [callee.split("::", 1)[1].rsplit("#", 1)[0] for caller, callee, _confidence in state["calls"] if caller.startswith("u.cpp::use#")]


class TestTheStartingPoint:
    def test_the_declarations_are_aliases_of_the_definitions(self, project):
        state = _state(project)
        assert not [function_id for function_id in state["artifacts"] if function_id.startswith("m.h::")]
        assert sorted(_callees(state)) == ["M::keep", "M::work"]
