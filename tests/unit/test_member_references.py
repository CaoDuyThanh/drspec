"""Member accesses (C++ first) found by a scan and typed by the same means as calls: stored as references of the function to the
field of the class that declares it, with the kind and the line; an access whose receiver type is unknown is stored with the member
name and no class. The call and reference tables do not change."""

from __future__ import annotations

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.db.queries import get_member_references

runner = CliRunner()

HEADER = """#pragma once
#include <vector>
struct AvoidanceResult {
  std::vector<int> data;
  bool ok;
};
struct Base {
  int shared;
};
struct Derived : public Base {
  int own;
  void touch();
};
struct Holder {
  AvoidanceResult result;
  std::vector<AvoidanceResult> history;
  int count;
  void run(AvoidanceResult& r, AvoidanceResult* p);
};
"""

SOURCE = """#include "m.h"
void Holder::run(AvoidanceResult& r, AvoidanceResult* p) {
  r.data.empty();
  r.data.push_back(1);
  r.ok = true;
  if (r.ok) { count++; }
  int x = p->data[0];
  this->count = 2;
  result.ok = false;
  history[0].ok = true;
  auto f = [&]() { return r.ok; };
  unknown.thing = 1;
  std::pair<int, AvoidanceResult> pr;
  pr.first = 3;
  int local = count;
}
void Derived::touch() { shared = 1; own = 2; }
int read(Derived& d) { return d.shared + d.own; }
"""


@pytest.fixture(scope="module")
def scanned(tmp_path_factory):
    """One scan of the project above, made once for the tests that only read what it stored: the rows, the rows of one class and member,
    and the call and reference counts (an in-memory database, no `init`, no file)."""
    from tests.fakes import KeptDuckDB
    from drspec.db import init_schema

    with pytest.MonkeyPatch.context() as patch:
        folder = tmp_path_factory.mktemp("scanned")
        KeptDuckDB().install(patch)
        (folder / "_drspec").mkdir()
        (folder / "m.h").write_text(HEADER)
        (folder / "m.cpp").write_text(SOURCE)
        connection = duckdb.connect(str(folder / "_drspec" / "contracts.db"))  # pattern-ok: duckdb-file kept in memory by `KeptDuckDB`
        init_schema(connection)
        connection.close()
        patch.chdir(folder)
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        connection = duckdb.connect(str(folder / "_drspec" / "contracts.db"), read_only=True)  # pattern-ok: duckdb-file kept in memory by `KeptDuckDB`
        try:
            rows = _short(get_member_references(connection))
            asked = _short(get_member_references(connection, owner_class="AvoidanceResult", member="ok"))
            references = connection.execute("SELECT COUNT(*) FROM code_references").fetchone()[0]
            callers = {r[0].split("::", 1)[1].rsplit("#", 1)[0] for r in connection.execute("SELECT caller_id FROM dependencies").fetchall()}
        finally:
            connection.close()
    return {"rows": rows, "asked": asked, "references": references, "callers": callers}


def _short(rows):
    short = lambda function_id: function_id.split("::", 1)[1].rsplit("#", 1)[0]  # noqa: E731
    return [(short(r["caller_id"]), r["owner_class"], r["member"], r["kind"], r["line"]) for r in rows]


class TestEveryAccessIsStoredOnTheFieldOfItsClass:
    def test_the_acceptance_shapes(self, scanned):
        run = [(owner, member, kind, line) for caller, owner, member, kind, line in scanned["rows"] if caller == "Holder::run"]
        assert ("AvoidanceResult", "data", "read", 3) in run  # r.data.empty()
        assert ("AvoidanceResult", "data", "write", 4) in run  # r.data.push_back(1)
        assert ("AvoidanceResult", "ok", "write", 5) in run  # r.ok = true
        assert ("AvoidanceResult", "ok", "read", 6) in run  # if (r.ok)
        assert ("Holder", "count", "readwrite", 6) in run  # count++ : the implicit this
        assert ("AvoidanceResult", "data", "read", 7) in run  # p->data[0]
        assert ("Holder", "count", "write", 8) in run  # this->count = 2
        assert ("AvoidanceResult", "ok", "write", 9) in run  # result.ok = false: a field of the class, then its member
        assert ("Holder", "result", "read", 9) in run
        assert ("AvoidanceResult", "ok", "write", 10) in run  # history[0].ok: the element of a vector
        assert ("AvoidanceResult", "ok", "read", 11) in run  # in a lambda

    def test_a_query_by_class_and_member_finds_every_function_that_touches_the_field(self, scanned):
        assert [(caller, kind, line) for caller, _owner, _member, kind, line in scanned["asked"]] == [
            ("Holder::run", "write", 5), ("Holder::run", "read", 6), ("Holder::run", "write", 9), ("Holder::run", "write", 10), ("Holder::run", "read", 11),
        ]

    def test_an_inherited_field_is_the_field_of_the_class_that_declares_it(self, scanned):
        touch = [(owner, member, kind) for caller, owner, member, kind, _line in scanned["rows"] if caller == "Derived::touch"]
        assert sorted(touch) == [("Base", "shared", "write"), ("Derived", "own", "write")]
        read = [(owner, member) for caller, owner, member, _kind, _line in scanned["rows"] if caller == "read"]
        assert sorted(read) == [("Base", "shared"), ("Derived", "own")]

    def test_a_local_and_a_member_of_a_library_type_are_not_stored(self, scanned):
        run = [(owner, member) for caller, owner, member, _kind, _line in scanned["rows"] if caller == "Holder::run"]
        assert ("", "first") not in run and all(member != "local" and member != "x" for _owner, member in run)  # std::pair, the locals

    def test_a_receiver_whose_type_is_unknown_is_stored_by_member_name_without_a_class(self, scanned):
        unresolved = [(member, kind, line) for caller, owner, member, kind, line in scanned["rows"] if caller == "Holder::run" and owner == ""]
        assert unresolved == [("thing", "write", 12)]

    def test_the_call_and_reference_tables_are_untouched(self, scanned):
        assert scanned["references"] == 0 and "Holder::run" not in scanned["callers"]  # field accesses are not calls


@pytest.fixture
def project(kept_project):
    (kept_project / "m.h").write_text(HEADER)
    (kept_project / "m.cpp").write_text(SOURCE)
    assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
    return kept_project


def _refs(project, **kwargs):
    connection = duckdb.connect(str(project / "_drspec" / "contracts.db"), read_only=True)  # pattern-ok: duckdb-file kept in memory by `kept_duckdb`
    try:
        rows = get_member_references(connection, **kwargs)
    finally:
        connection.close()
    return _short(rows)


def _all(project):
    return sorted(_refs(project))


class TestTheRowsFollowTheFunction:
    def test_an_edit_replaces_the_rows_of_that_function_only(self, project):
        before = _all(project)
        (project / "m.cpp").write_text(SOURCE.replace("  r.ok = true;\n", "  r.data.clear();\n"))
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        after = _all(project)
        assert ("Holder::run", "AvoidanceResult", "ok", "write", 5) in before and ("Holder::run", "AvoidanceResult", "ok", "write", 5) not in after
        assert ("Holder::run", "AvoidanceResult", "data", "write", 5) in after
        assert [row for row in before if row[0] != "Holder::run"] == [row for row in after if row[0] != "Holder::run"]  # the others are as they were

    def test_a_deleted_file_takes_its_rows_and_the_result_equals_a_fresh_index(self, project, tmp_path_factory, monkeypatch):
        (project / "extra.cpp").write_text('#include "m.h"\nvoid extra(Holder& h) { h.count = 1; }\n')
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        assert any(row[0] == "extra" for row in _all(project))
        (project / "extra.cpp").unlink()
        (project / "m.cpp").write_text(SOURCE.replace("  r.ok = true;\n", ""))
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        incremental = _all(project)
        assert not any(row[0] == "extra" for row in incremental)
        fresh = _fresh_index(project, tmp_path_factory, monkeypatch)
        assert incremental == fresh

    def test_a_header_edit_that_changes_a_class_is_followed_by_the_functions_that_use_it(self, project, tmp_path_factory, monkeypatch):
        (project / "m.h").write_text(HEADER.replace("  bool ok;\n", "  bool okay;\n"))
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        incremental = _all(project)
        assert incremental == _fresh_index(project, tmp_path_factory, monkeypatch)
        assert not any(row[2] == "ok" and row[1] == "AvoidanceResult" for row in incremental)  # the field is gone: its accesses are unresolved now


def _fresh_index(project, tmp_path_factory, monkeypatch):
    """The same files, scanned from nothing in a second project (an empty in-memory database made as `init` makes its schema, no
    `init`, no file): what the incremental index has to equal."""
    import shutil

    import duckdb

    from drspec.db import init_schema

    fresh = tmp_path_factory.mktemp("fresh") / "p"
    (fresh / "_drspec").mkdir(parents=True)
    for path in project.iterdir():
        if path.is_file() and path.suffix in (".h", ".cpp"):
            shutil.copy2(path, fresh / path.name)
    connection = duckdb.connect(str(fresh / "_drspec" / "contracts.db"))  # pattern-ok: duckdb-file kept in memory by `kept_duckdb`, as `kept_project` makes it
    init_schema(connection)
    connection.close()
    with monkeypatch.context() as inside:
        inside.chdir(fresh)
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        return _all(fresh)


class TestStatusSaysHowManyThereAre:
    def test_the_counts_by_kind_and_the_unresolved_ones(self, project):
        import json

        data = json.loads(runner.invoke(app, ["--json", "status"]).output)["data"]
        members = data["member_references"]
        rows = _all(project)
        assert members["total"] == len(rows) and members["unresolved"] == len([row for row in rows if row[1] == ""])
        assert members["resolved"] == members["total"] - members["unresolved"]
        assert members["by_kind"] == {kind: len([row for row in rows if row[1] != "" and row[3] == kind]) for kind in ("read", "write", "readwrite")}
        assert members["unresolved"] == 1 and members["by_kind"]["write"] >= 5  # `unknown.thing = 1`; the writes of the acceptance example

    def test_the_text_output_has_the_block_and_a_project_without_accesses_has_none(self, project, kept_project):
        text = runner.invoke(app, ["status"]).output  # the default: the label and its numbers, on one line
        assert "Member References:" in text and "unresolved 1" in text and "readwrite 1" in text
        details = runner.invoke(app, ["status", "--details"]).output  # --details: the block as it was
        assert "Member References:" in details and "Unresolved: 1" in details and "Readwrite:" in details
        (project / "m.cpp").write_text('#include "m.h"\nint nothing() { return 1; }\n')
        (project / "m.h").write_text("#pragma once\nint nothing();\n")
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        import json

        assert "member_references" not in json.loads(runner.invoke(app, ["--json", "status"]).output)["data"]
        assert "Member References" not in runner.invoke(app, ["status"]).output  # a block with nothing in it is not printed


REF_HEADER = """#pragma once
struct S {
  int a;
  int b;
  int c;
  void set(int& x);
  void look(const int& x);
};
void fill(int& out, const int& in, int value);
void both(int& x);
void both(double& x);
void mixed(int& x);
void mixed(const int& x);
"""

REF_SOURCE = """#include "r.h"
void fill(int& out, const int& in, int value) { out = in + value; }
void S::set(int& x) { x = 1; }
void S::look(const int& x) {}
void use(S& s) {
  fill(s.a, s.b, s.c);
  s.set(s.b);
  s.look(s.c);
  both(s.a);
  mixed(s.b);
}
"""


class TestAMemberPassedAsANonConstReference:
    """A read that is an argument of a call is read AND written when the callee's declared parameter is a non-const reference
    (the rule from the type, where the callee is found as a call is); anything the declarations do not settle stays a read."""

    @pytest.fixture
    def refs(self, kept_project):
        (kept_project / "r.h").write_text(REF_HEADER)
        (kept_project / "r.cpp").write_text(REF_SOURCE)
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        return [(member, kind, line) for caller, owner, member, kind, line in _refs(kept_project) if caller == "use"]

    def test_a_non_const_reference_parameter_makes_it_read_and_written(self, refs):
        assert ("a", "readwrite", 6) in refs  # fill(s.a, ...): `int& out`
        assert ("b", "readwrite", 7) in refs  # s.set(s.b): `void set(int& x)`, a method found through the receiver's type

    def test_a_const_reference_or_a_value_parameter_stays_a_read(self, refs):
        assert ("b", "read", 6) in refs and ("c", "read", 6) in refs  # fill(..., const int& in, int value)
        assert ("c", "read", 8) in refs  # s.look(s.c): `const int&`

    def test_overloads_that_agree_make_it_written_and_overloads_that_differ_leave_it_a_read(self, refs):
        assert ("a", "readwrite", 9) in refs  # both(int&) and both(double&): the argument is a non-const reference in both
        assert ("b", "read", 10) in refs  # mixed(int&) and mixed(const int&): nothing is said


class TestDeclarationsInDifferentFiles:
    def test_two_files_that_declare_the_same_function_differently_leave_the_argument_a_read(self, kept_project):
        (kept_project / "a.h").write_text("#pragma once\nvoid other(int& x);\nvoid another(const int& x);\nvoid same(int& x);\n")
        (kept_project / "b.h").write_text("#pragma once\nvoid other(const int& x);\nvoid another(int& x);\nvoid same(int& x);\n")
        (kept_project / "m.cpp").write_text(
            '#include "a.h"\n#include "b.h"\nstruct S { int a; int b; int c; };\nvoid use(S& s) {\n  other(s.a);\n  another(s.b);\n  same(s.c);\n}\n'
        )
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        found = [(member, kind) for caller, owner, member, kind, line in _refs(kept_project) if caller == "use"]
        assert ("a", "read") in found and ("b", "read") in found  # declared both ways, in either order: nothing is said
        assert ("c", "readwrite") in found  # `same` is declared the same way twice


class TestTheNonConstReferenceFacts:
    def test_the_parser_records_one_flag_per_parameter_and_drops_overloads_that_differ(self):
        from drspec.parsers.cpp_parser import CppParser

        facts = CppParser().parse(REF_HEADER, "r.h").facts
        assert facts["refparams"] == {"fill": "100", "S::set": "1", "S::look": "0", "both": "1"}  # `mixed` takes it both ways: dropped

    def test_the_argument_position_is_recorded_with_the_call(self):
        from drspec.parsers.cpp_parser import CppParser

        source = "struct S { int a; int b; };\nvoid use(S& s) {\n  fill(s.a, s.b);\n  s.set((s.b));\n  int k = s.a;\n}\n"
        function = next(f for f in CppParser().parse(source, "r.cpp").functions if f.name == "use")
        by_line = {(a.member, a.line): (a.call_receiver, a.call_name, a.arg_index) for a in function.member_accesses}
        assert by_line[("a", 3)] == (None, "fill", 0) and by_line[("b", 3)] == (None, "fill", 1)
        assert by_line[("b", 4)] == ("s", "set", 0)  # inside parentheses it is still the argument
        assert by_line[("a", 5)] == (None, None, -1)  # not an argument
