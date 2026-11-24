"""`refs get` / the MCP tool `refs_get`: who reads and writes a member, grouped by owner class, file and function, each access with its
kind, line and the line of code; a bare name lists every owner; the accesses left unresolved are counted and named."""

from __future__ import annotations

import json

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.mcp_server import _tool_refs_get
from tests.unit.test_member_references import HEADER, SOURCE

runner = CliRunner()

TWO_CPP = """struct P { int n; int only_p; };
struct Q { int n; };
void writeP(P& p) {
  p.n = 1;
  p.only_p = 3;
}
int readQ(Q& q) {
  int x = q.n;
  return x;
}
void lost() {
  auto z = unknownFactory();
  z.n = 2;
  z.only_p += 1;
}
"""


GATE_CPP = """struct Gate {
  bool pending = false;
  void init();
  void update();
};
void Gate::init() {
  pending = true;
}
void Gate::update() {
  if (pending) {
    log();
  }
  pending = false;
}
"""

NS_CPP = """namespace zmp {
struct Ctx { int result; void run(); };
void Ctx::run() { result = 1; }
}
namespace alpha {
struct Dup { int level; void a(); };
void Dup::a() { level = 1; }
}
namespace beta {
struct Dup { int level; void b(); };
void Dup::b() { level = 2; }
}
"""

DECLARED_CPP = """struct Context { double counter = 0.0; };
struct ContextB { double counter = 0.0; };
struct ContextChild : ContextB { int extra; };
struct Plain { int other; };
void bump(Context& c) { c.counter += 1.0; }
"""

BROKEN_CPP = """struct Cog {
  int teeth;
  template <class T>
  static int toMap(
    const T& input
  ) = delete;
  void spin();
};
void Cog::spin() {
  teeth = 1;
}
"""


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    """The member-reference project of T-49 and a second file, scanned once (an in-memory database, no `init`, no file): the database
    stays in place for the tests of this module, which only read it."""
    from drspec.db import init_schema
    from tests.fakes import KeptDuckDB

    folder = tmp_path_factory.mktemp("refs")
    with pytest.MonkeyPatch.context() as patch:
        KeptDuckDB().install(patch)
        (folder / "_drspec").mkdir()
        (folder / "m.h").write_text(HEADER)
        (folder / "m.cpp").write_text(SOURCE)
        (folder / "two.cpp").write_text(TWO_CPP)
        (folder / "gate.cpp").write_text(GATE_CPP)
        (folder / "broken.cpp").write_text(BROKEN_CPP)
        (folder / "ns.cpp").write_text(NS_CPP)
        (folder / "declared.cpp").write_text(DECLARED_CPP)
        connection = duckdb.connect(str(folder / "_drspec" / "contracts.db"))  # pattern-ok: duckdb-file kept in memory by `KeptDuckDB`
        init_schema(connection)
        connection.close()
        patch.chdir(folder)
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        yield folder


def _refs(*args, exit_code=0):
    result = runner.invoke(app, ["--json", "refs", "get", *args])
    assert result.exit_code == exit_code, result.output
    return json.loads(result.output)["data" if exit_code == 0 else "error"]


def _accesses(data):
    return [
        (owner["owner_class"], function["function_name"], access["kind"], access["line"], access["source"])
        for owner in data["owners"] for file in owner["files"] for function in file["functions"] for access in function["accesses"]
    ]


class TestEveryAccessOfAMember:
    def test_a_qualified_member_lists_each_function_with_kind_line_and_the_line_of_code(self, project):
        data = _refs("AvoidanceResult::data")
        assert [owner["owner_class"] for owner in data["owners"]] == ["AvoidanceResult"]
        assert _accesses(data) == [
            ("AvoidanceResult", "Holder::run", "read", 3, "r.data.empty();"),
            ("AvoidanceResult", "Holder::run", "write", 4, "r.data.push_back(1);"),
            ("AvoidanceResult", "Holder::run", "read", 7, "int x = p->data[0];"),
        ]
        file = data["owners"][0]["files"][0]
        assert file["file_path"] == "m.cpp" and file["functions"][0]["function_id"].startswith("m.cpp::Holder::run#")
        assert data["total_accesses"] == 3 and not data["truncated"]

    def test_kind_read_and_write_each_keep_their_accesses_and_a_read_modify_write_is_both(self, project):
        reads = _accesses(_refs("Holder::count", "--kind", "read"))
        writes = _accesses(_refs("Holder::count", "--kind", "write"))
        everything = _accesses(_refs("Holder::count"))
        assert [kind for _o, _f, kind, _l, _s in everything] == ["readwrite", "write", "read"]  # count++ ; this->count = 2 ; local = count
        assert [kind for _o, _f, kind, _l, _s in reads] == ["readwrite", "read"] and [kind for _o, _f, kind, _l, _s in writes] == ["readwrite", "write"]

    def test_context_adds_the_lines_around_each_access_inside_its_function(self, project):
        data = _refs("AvoidanceResult::data", "--kind", "read", "--context", "3")
        accesses = [a for owner in data["owners"] for f in owner["files"] for fn in f["functions"] for a in fn["accesses"]]
        first, second = accesses
        assert [line["text"].strip() for line in first["before"]] == ["void Holder::run(AvoidanceResult& r, AvoidanceResult* p) {"]  # the function starts at line 2
        assert [line["line"] for line in first["after"]] == [4, 5, 6] and first["after"][0]["text"].strip() == "r.data.push_back(1);"
        assert [line["line"] for line in second["before"]] == [4, 5, 6]

    def test_a_limit_says_how_many_there_were(self, project):
        data = _refs("AvoidanceResult::ok", "--limit", "2")
        assert data["returned"] == 2 and data["total_accesses"] == 5 and data["truncated"]


class TestAMemberGivenByNameOnly:
    def test_every_owner_that_has_it_is_listed_apart(self, project):
        data = _refs("n")
        assert [owner["owner_class"] for owner in data["owners"]] == ["P", "Q"]
        assert _accesses(data) == [("P", "writeP", "write", 4, "p.n = 1;"), ("Q", "readQ", "read", 8, "int x = q.n;")]
        assert [owner["accesses"] for owner in data["owners"]] == [1, 1]


class TestWhatWasLeftUnresolved:
    def test_the_accesses_whose_receiver_type_is_unknown_are_counted_and_named(self, project):
        data = _refs("P::n", "--unresolved", "full")  # (the default lists them by file: `full` names each one)
        unresolved = data["unresolved"]
        assert unresolved["count"] == 1 and unresolved["functions_total"] == 1
        function = unresolved["functions"][0]
        assert function["function_name"] == "lost" and function["lines"] == [{"line": 13, "kind": "write", "site": "z.n", "receiver": "z"}]
        assert "unresolved" in unresolved["note"] and "may read or write" in unresolved["note"]

    def test_a_member_with_no_resolved_access_says_so_and_does_not_look_unused(self, project):
        data = _refs("P::only_p", "--kind", "read")
        assert data["owners"] == [] and data["unresolved"]["count"] == 1  # `p.only_p = 3` only writes it; `z.only_p += 1` reads it, and nobody knows what `z` is
        assert "No resolved access" in data["note"]

    def test_a_member_nothing_records_says_so_and_a_wrong_owner_names_the_right_ones(self, project):
        assert "No access to a member named 'nothing_like_it'" in _refs("nothing_like_it")["note"]
        # Two classes have the member: nothing is guessed, both are named
        data = _refs("Zed::n")
        assert data["owners"] == [] and data["other_owners_of_the_name"] == ["P", "Q"] and "owner_resolved" not in data


class TestTheCommandAndTheTool:
    def test_the_text_output_is_labelled_and_the_format_option_gives_json(self, project):
        text = runner.invoke(app, ["refs", "get", "AvoidanceResult::data"]).output
        assert "Owner Class: AvoidanceResult" in text and "Name: Holder::run" in text and "Source: r.data.empty();" in text and "Kind: write" in text
        as_json = runner.invoke(app, ["refs", "get", "AvoidanceResult::data", "--format", "json"]).output
        assert json.loads(as_json)["data"]["total_accesses"] == 3

    def test_a_kind_that_is_neither_read_nor_write_is_an_error(self, project):
        error = _refs("data", "--kind", "both", exit_code=1)
        assert error["code"] == "INVALID_INPUT"

    def test_no_project_is_an_error_not_a_crash(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert _refs("data", exit_code=1)["code"] == "DB_NOT_INITIALIZED"

    def test_the_mcp_tool_gives_the_same_answer(self, project):
        db_path = project / "_drspec" / "contracts.db"
        result, failed = _tool_refs_get(db_path, {"symbol": "AvoidanceResult::data", "kind": "read", "context": 1})
        assert not failed and result["total_accesses"] == 2 and result["owners"][0]["files"][0]["functions"][0]["accesses"][0]["before"]
        assert _tool_refs_get(db_path, {})[1] and _tool_refs_get(db_path, {"symbol": "x", "kind": "both"})[1]


class TestARecoveredFileSaysSo:
    """"No access recorded" is not "never accessed" when the file that defines the class had parse problems: `refs get` names the file."""

    def test_a_condition_on_a_field_does_not_hide_its_other_accesses(self, project):
        data = _refs("Gate::pending")
        assert _accesses(data) == [
            ("Gate", "Gate::init", "write", 7, "pending = true;"),
            ("Gate", "Gate::update", "read", 10, "if (pending) {"),
            ("Gate", "Gate::update", "write", 13, "pending = false;"),
        ]

    def test_a_clean_file_has_no_parse_problem_line(self, project):
        assert "parse_problems" not in _refs("Gate::pending")

    def test_a_file_that_had_parse_problems_is_named_with_its_counts(self, project):
        data = _refs("Cog::teeth")
        (problem,) = data["parse_problems"]
        assert problem["file"] == "broken.cpp" and problem["first_error_line"] == 6
        assert problem["functions_skipped"] == 1 and problem["functions_partial"] == 0 and problem["skipped_constructs"] == 0
        assert "broken.cpp had parse problems" in problem["note"] and "no access recorded is not proof of none" in problem["note"]

    def test_the_line_is_in_the_text_and_follows_the_class_when_nothing_was_recorded(self, project):
        text = runner.invoke(app, ["refs", "get", "Cog::nothing_here"]).output
        assert "broken.cpp had parse problems" in text  # the class is defined in that file: an empty answer says why it may be empty
        assert "parse problems" not in runner.invoke(app, ["refs", "get", "Gate::nothing_here"]).output


class TestAnOwnerWrittenWithoutItsNamespaceOrWrong:
    """One candidate class is used and named; several are listed; the full name is taken as it is."""

    def test_the_one_class_that_has_the_member_is_used_when_the_owner_is_wrong(self, project):
        data = _refs("Q::only_p")
        assert [owner["owner_class"] for owner in data["owners"]] == ["P"]
        assert data["owner_resolved"] == {"given": "Q", "used": ["P"]} and data["owner_note"] == "'Q' does not declare a member 'only_p'; the only class with recorded accesses to a member of that name is P: used it"
        assert data["symbol"] == "Q::only_p"

    def test_a_class_written_without_its_namespace_is_used_and_named(self, project):
        data = _refs("Ctx::result")
        assert [owner["owner_class"] for owner in data["owners"]] == ["zmp::Ctx"]
        assert data["owner_resolved"] == {"given": "Ctx", "used": ["zmp::Ctx"]} and data["owner_note"] == "'Ctx' is not a full class name: used zmp::Ctx"

    def test_two_classes_of_that_name_are_both_listed_apart(self, project):
        data = _refs("Dup::level")
        assert [owner["owner_class"] for owner in data["owners"]] == ["alpha::Dup", "beta::Dup"]
        assert data["owner_note"] == "'Dup' matches 2 classes: alpha::Dup, beta::Dup; the accesses of each are listed apart"

    def test_the_full_name_is_taken_as_it_is(self, project):
        assert "owner_resolved" not in _refs("zmp::Ctx::result") and "owner_resolved" not in _refs("Gate::pending")

    def test_the_text_says_which_one_was_used(self, project):
        text = runner.invoke(app, ["refs", "get", "Q::only_p"]).output
        assert "Owner Note:" in text and "'Q' does not declare a member 'only_p'" in text and "is P: used it" in text


class TestAClassThatDeclaresTheMemberIsNeverReplaced:
    """`ContextB` declares `counter` and nothing accesses it: the answer is about `ContextB`, not about the `Context` that has accesses."""

    def test_a_class_that_declares_the_member_without_an_access_is_not_redirected(self, project):
        data = _refs("ContextB::counter")
        assert data["owners"] == [] and data["total_accesses"] == 0
        assert "owner_resolved" not in data and "owner_note" not in data
        assert data["note"] == "No access to ContextB::counter is recorded ('ContextB' declares 'counter'; C++ only, a scan records the accesses)."
        assert data["other_owners_of_the_name"] == ["Context"]  # (said, not used)

    def test_a_member_a_base_declares_counts_as_declared(self, project):
        data = _refs("ContextChild::counter")
        assert data["owners"] == [] and "owner_resolved" not in data and "ContextChild" in data["note"]

    def test_the_class_that_has_the_accesses_is_still_answered(self, project):
        data = _refs("Context::counter")
        assert [owner["owner_class"] for owner in data["owners"]] == ["Context"] and "owner_resolved" not in data

    def test_a_class_without_the_member_is_redirected_with_the_true_wording(self, project):
        data = _refs("Plain::counter")
        assert [owner["owner_class"] for owner in data["owners"]] == ["Context"]
        assert data["owner_resolved"] == {"given": "Plain", "used": ["Context"]}
        assert data["owner_note"] == "'Plain' does not declare a member 'counter'; the only class with recorded accesses to a member of that name is Context: used it"

    def test_an_unknown_class_is_redirected_and_says_it_is_unknown(self, project):
        data = _refs("Nope::counter")
        assert data["owner_resolved"] == {"given": "Nope", "used": ["Context"]}
        assert data["owner_note"] == "'Nope' is not a class the index knows; the only class with recorded accesses to a member of that name is Context: used it"

    def test_two_candidates_are_listed_not_used(self, project):
        data = _refs("Nope::level")
        assert data["owners"] == [] and "owner_resolved" not in data and data["other_owners_of_the_name"] == ["alpha::Dup", "beta::Dup"]


TWO_CANDIDATES = """struct Context { double counter; };
struct ContextB { double counter; };
struct Holder { int level; };
void a(Context& c) { c.counter += 1.0; }
void b(ContextB& c) { c.counter += 2.0; }
"""


@pytest.fixture(scope="module")
def two_candidates(tmp_path_factory):
    """One scan of the source above, in memory: `Holder` does not declare `counter`, and two other classes have accesses of that name."""
    from tests.fakes import KeptDuckDB
    from drspec.db import init_schema

    with pytest.MonkeyPatch.context() as patch:
        folder = tmp_path_factory.mktemp("two_candidates")
        KeptDuckDB().install(patch)
        (folder / "_drspec").mkdir()
        (folder / "m.hpp").write_text(TWO_CANDIDATES)
        connection = duckdb.connect(str(folder / "_drspec" / "contracts.db"))  # pattern-ok: duckdb-file kept in memory by `KeptDuckDB`
        init_schema(connection)
        connection.close()
        patch.chdir(folder)
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        text = {symbol: runner.invoke(app, ["refs", "get", symbol]).output for symbol in ("Holder::counter", "Nope::counter")}
        connection = duckdb.connect(str(folder / "_drspec" / "contracts.db"), read_only=True)  # pattern-ok: duckdb-file kept in memory by `KeptDuckDB`
        try:
            found = {symbol: _tool_refs_get(folder / "_drspec" / "contracts.db", {"symbol": symbol})[0] for symbol in ("Holder::counter", "Nope::counter")}
        finally:
            connection.close()
    return {"data": found, "text": text}


class TestTwoCandidatesSaySomethingTrueOfTheNamedClass:
    NOTE_HOLDER = "'Holder' does not declare a member 'counter'; classes with recorded accesses to a member of that name: Context, ContextB"
    NOTE_NOPE = "'Nope' is not a class the index knows; classes with recorded accesses to a member of that name: Context, ContextB"

    def test_an_existing_class_without_the_member_says_it_does_not_declare_it_and_names_the_candidates(self, two_candidates):
        data = two_candidates["data"]["Holder::counter"]
        assert data["note"] == self.NOTE_HOLDER
        assert data["owners"] == [] and "owner_resolved" not in data and data["other_owners_of_the_name"] == ["Context", "ContextB"]

    def test_an_unknown_class_says_it_is_unknown_in_the_same_style(self, two_candidates):
        data = two_candidates["data"]["Nope::counter"]
        assert data["note"] == self.NOTE_NOPE
        assert data["owners"] == [] and "owner_resolved" not in data

    def test_the_text_form_shows_the_same_note(self, two_candidates):
        assert f"Note: {self.NOTE_HOLDER}" in two_candidates["text"]["Holder::counter"]
        assert f"Note: {self.NOTE_NOPE}" in two_candidates["text"]["Nope::counter"]


UNRESOLVED_MANY = {
    "m.cpp": (
        "struct Context { double counter; int x; };\n"
        "void known(Context& c) { c.counter += 1.0; }\n"
        "void u1() { auto c = unknownFactory(); c->counter += 0.0; }\n"
        "void u2() { auto c = unknownFactory(); c->counter += 0.0; }\n"
        "void u3() { auto c = unknownFactory(); c->counter += 0.0; }\n"
    ),
    "other.cpp": "void u4() { auto c = unknownFactory(); c->counter = 1.0; }\n",
}


@pytest.fixture(scope="module")
def unresolved_many(tmp_path_factory):
    """One scan, in memory: `Context::counter` has one resolved access and four unresolved ones (three in m.cpp, one in other.cpp);
    `Context::x` has none unresolved. The CLI is called in process."""
    from tests.fakes import KeptDuckDB
    from drspec.db import init_schema

    with pytest.MonkeyPatch.context() as patch:
        folder = tmp_path_factory.mktemp("unresolved_modes")
        KeptDuckDB().install(patch)
        (folder / "_drspec").mkdir()
        for name, text in UNRESOLVED_MANY.items():
            (folder / name).write_text(text)
        connection = duckdb.connect(str(folder / "_drspec" / "contracts.db"))  # pattern-ok: duckdb-file kept in memory by `KeptDuckDB`
        init_schema(connection)
        connection.close()
        patch.chdir(folder)
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        connection = duckdb.connect(str(folder / "_drspec" / "contracts.db"), read_only=True)  # pattern-ok: duckdb-file kept in memory by `KeptDuckDB`
        try:
            from drspec.core.refs import get_refs
            found = {
                "default": _refs("Context::counter"),
                "full_limit_2": _refs("Context::counter", "--unresolved", "full", "--limit", "2"),
                "count": _refs("Context::counter", "--unresolved", "count"),
                "no_unresolved": _refs("Context::x"),
            }
            with pytest.raises(ValueError):
                get_refs(connection, "Context::counter", unresolved="sometimes")
            refused = True
        finally:
            connection.close()
    found["refused"] = refused
    return found


class TestTheUnresolvedAccessesInThreeModes:
    def test_by_default_they_are_counted_by_file_and_no_function_is_listed(self, unresolved_many):
        unresolved = unresolved_many["default"]["unresolved"]
        assert unresolved["count"] == 4 and unresolved["functions_total"] == 4
        assert unresolved["files"] == [
            {"file_path": "m.cpp", "accesses": 3, "functions": 3},
            {"file_path": "other.cpp", "accesses": 1, "functions": 1},
        ]
        assert "functions" not in unresolved and "functions_omitted" not in unresolved
        assert unresolved["note"].endswith("Counted by file under `files`; `unresolved: full` lists each one.")

    def test_full_lists_each_function_and_the_limit_caps_it_and_says_how_many_were_left_out(self, unresolved_many):
        unresolved = unresolved_many["full_limit_2"]["unresolved"]
        assert [f["function_name"] for f in unresolved["functions"]] == ["u1", "u2"]
        assert unresolved["functions_omitted"] == 2 and unresolved["functions_total"] == 4 and unresolved["count"] == 4
        assert unresolved["note"].endswith("Listed under `functions`.")

    def test_count_gives_only_the_numbers_and_the_note(self, unresolved_many):
        unresolved = unresolved_many["count"]["unresolved"]
        assert set(unresolved) == {"count", "functions_total", "note"} and unresolved["count"] == 4
        assert "Counted by file" not in unresolved["note"] and "Listed under" not in unresolved["note"]

    def test_a_result_with_nothing_unresolved_has_no_list_and_no_note(self, unresolved_many):
        data = unresolved_many["no_unresolved"]
        assert data["unresolved"] == {"count": 0, "functions_total": 0, "files": []} and data["owners"] == []  # nothing listed, no note

    def test_an_unknown_mode_is_refused(self, unresolved_many):
        assert unresolved_many["refused"]


class TestTheMcpToolHasTheSameDefault:
    """The MCP tool passes the `unresolved` argument on, and `files` is what it does without one (the connection and the query are replaced:
    what is checked is the argument)."""

    def _calls(self, monkeypatch, arguments, tmp_path):
        from drspec import mcp_server
        from drspec.core import refs

        seen = []
        monkeypatch.setattr(refs, "get_refs", lambda conn, symbol, **kwargs: seen.append(kwargs) or {"symbol": symbol})
        monkeypatch.setattr(mcp_server, "_connect_readonly", lambda db_path: type("C", (), {"close": lambda self: None})())
        result = _tool_refs_get(tmp_path / "_drspec" / "contracts.db", {"symbol": "Context::counter", **arguments})
        return seen, result

    def test_without_the_argument_the_unresolved_accesses_are_counted_by_file(self, monkeypatch, tmp_path):
        seen, _result = self._calls(monkeypatch, {}, tmp_path)
        assert seen[0]["unresolved"] == "files"

    def test_the_argument_is_passed_on(self, monkeypatch, tmp_path):
        seen, _result = self._calls(monkeypatch, {"unresolved": "count"}, tmp_path)
        assert seen[0]["unresolved"] == "count"
