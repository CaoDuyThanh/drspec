"""`refs get` marks the base of a write chain with what is written through it (`Written Through`, `written_through` in JSON); a base is
still a read, so `--kind write` does not list it, and a chain that is only read has no mark. One scan of a small source, in memory,
made once for the module; the CLI is called in process (no process, no real project)."""

from __future__ import annotations

import json

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.core.refs import get_refs

runner = CliRunner()

SOURCE = """struct H { int n; };
struct Ctx { H avoid; int ctr; };
void writer(Ctx& c) {
  c.avoid.n = 1;
  c.avoid.n += 2;
  c.ctr++;
}
int reader(const Ctx& c) { return c.avoid.n; }
"""


@pytest.fixture(scope="module")
def scanned(tmp_path_factory):
    from tests.fakes import KeptDuckDB
    from drspec.db import init_schema

    with pytest.MonkeyPatch.context() as patch:
        folder = tmp_path_factory.mktemp("through")
        KeptDuckDB().install(patch)
        (folder / "_drspec").mkdir()
        (folder / "m.cpp").write_text(SOURCE)
        connection = duckdb.connect(str(folder / "_drspec" / "contracts.db"))  # pattern-ok: duckdb-file kept in memory by `KeptDuckDB`
        init_schema(connection)
        connection.close()
        patch.chdir(folder)
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        connection = duckdb.connect(str(folder / "_drspec" / "contracts.db"), read_only=True)  # pattern-ok: duckdb-file kept in memory by `KeptDuckDB`
        try:
            found = {
                "avoid": get_refs(connection, "Ctx::avoid"),
                "avoid_writes": get_refs(connection, "Ctx::avoid", kind="write"),
                "n_writes": get_refs(connection, "H::n", kind="write"),
                "ctr": get_refs(connection, "Ctx::ctr"),
            }
        finally:
            connection.close()
        found["text"] = runner.invoke(app, ["refs", "get", "Ctx::avoid"]).output
        payload = json.loads(runner.invoke(app, ["--json", "refs", "get", "Ctx::avoid"]).output)
        found["json"] = payload.get("data", payload)
    return found


def _accesses(result: dict) -> list:
    return [(access["line"], access["kind"], access.get("written_through", ""), function["function_name"])
            for owner in result["owners"] for entry in owner["files"] for function in entry["functions"] for access in function["accesses"]]


class TestTheBaseOfAWriteIsMarked:
    def test_the_base_of_each_write_says_what_is_written_through_it(self, scanned):
        assert sorted(_accesses(scanned["avoid"])) == [
            (4, "read", "n", "writer"),
            (5, "read", "n", "writer"),
            (8, "read", "", "reader"),  # a read of the base with no write through it: no mark
        ]

    def test_the_mark_is_in_the_json_and_is_absent_where_there_is_none(self, scanned):
        marked = [access for owner in scanned["json"]["owners"] for entry in owner["files"] for function in entry["functions"]
                  for access in function["accesses"] if "written_through" in access]
        assert [access["written_through"] for access in marked] == ["n", "n"]
        assert all("written_through" not in access for owner in scanned["json"]["owners"] for entry in owner["files"]
                   for function in entry["functions"] for access in function["accesses"] if access["line"] == 8)

    def test_the_text_says_it_on_the_line_of_the_base(self, scanned):
        assert scanned["text"].count("Written Through: n") == 2

    def test_a_member_written_through_its_own_base_is_not_listed_as_a_write_of_the_base(self, scanned):
        assert scanned["avoid_writes"]["total_accesses"] == 0  # `--kind write` lists the writes of `avoid`, not the base of `avoid.n`

    def test_the_writes_of_the_member_itself_are_still_listed(self, scanned):
        assert sorted(_accesses(scanned["n_writes"])) == [(4, "write", "", "writer"), (5, "readwrite", "", "writer")]

    def test_a_counter_written_directly_has_no_mark(self, scanned):
        assert _accesses(scanned["ctr"]) == [(6, "readwrite", "", "writer")]
