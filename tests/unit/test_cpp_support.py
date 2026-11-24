"""C++ at the level of Python: names, one entry per method, and call edges that cross files."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.core.cpp_resolver import resolve_include
from drspec.parsers import CppParser
from drspec.parsers.cpp_macros import TEST_MACROS, macro_test_case
from tests.helpers import strip_identity_hash as _strip

runner = CliRunner()

HEADER = """#pragma once
#include <string>

namespace math {

int helper(int a);
int identity_default(int v);
template <typename T> T identity(T v) { return v; }

class Calc {
public:
    explicit Calc(int base);
    int mul(int a, int b) const;
    int twice(int v) const;
    static int twice_static(int v);
    bool operator==(const Calc& other) const;
    int base() const { return base_; }
private:
    int base_;
};

} // namespace math
"""

CALC = """#include "calc.h"

namespace math {

static int local_util(int x) { return x + 1; }

int helper(int a) { return local_util(a) * 2; }

Calc::Calc(int base) : base_(base) {}

int Calc::mul(int a, int b) const { return helper(a) * b + base_; }

int Calc::twice(int v) const { return Calc::twice_static(v); }

int Calc::twice_static(int v) { return v * 2; }

bool Calc::operator==(const Calc& other) const { return base_ == other.base_; }

template <>
int identity<int>(int v) { return v + 0; }

} // namespace math
"""

MAIN = """#include "calc.h"

int run(int r) {
    math::Calc c(3);
    int a = math::helper(r);
    int b = c.mul(2, 3);
    return a + b + math::Calc::twice_static(r);
}

int main() { return run(1); }
"""

TESTS = """#include "calc.h"

TEST(CalcSuite, Multiplies) {
    math::Calc c(2);
    EXPECT_EQ(c.mul(2, 3), 6);
}

TEST(CalcSuite, Doubles) {
    EXPECT_EQ(math::Calc::twice_static(2), 4);
}
"""

FILES = {"include/calc.h": HEADER, "src/calc.cpp": CALC, "src/main.cpp": MAIN, "src/test_calc.cpp": TESTS}


def _write_the_files(root: Path) -> None:
    for name, text in FILES.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)


@pytest.fixture
def project(kept_project):
    _write_the_files(kept_project)  # the project database is in memory, kept between the commands: `init` is not what these tests are about
    assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
    return kept_project


def _db(project: Path):
    return duckdb.connect(str(project / "_drspec" / "contracts.db"), read_only=True)


def _short(function_id: str) -> str:
    return function_id.split("#")[0]


def _edges(project: Path):
    connection = _db(project)
    try:
        return sorted((_short(a), _short(b), c) for a, b, c in connection.execute("SELECT caller_id, callee_id, confidence FROM dependencies").fetchall())
    finally:
        connection.close()


class TestNames:
    def test_an_out_of_class_operator_keeps_its_name(self):
        result = CppParser().parse(CALC, file_path="src/calc.cpp")
        names = [f.qualified_name for f in result.functions]
        assert "math::Calc::operator==" in names and "math::Calc" not in names

    def test_an_explicit_specialisation_with_a_body_is_indexed_with_its_arguments(self):
        names = [f.qualified_name for f in CppParser().parse(CALC, file_path="src/calc.cpp").functions]
        assert "math::identity<int>" in names

    def test_test_macros_are_named_after_the_test(self):
        functions = CppParser().parse(TESTS, file_path="src/test_calc.cpp").functions
        assert [(f.qualified_name, f.name, f.parameter_tokens) for f in functions] == [
            ("CalcSuite.Multiplies", "Multiplies", []), ("CalcSuite.Doubles", "Doubles", []),
        ]

    @pytest.mark.parametrize(
        "macro,arguments,expected",
        [
            ("TEST_F", ["Fixture", "Works"], ("Fixture", "Works")),
            ("TEST_P", ["Suite", "Param"], ("Suite", "Param")),
            ("BOOST_AUTO_TEST_CASE", ["adds"], (None, "adds")),
            ("TEST", ["Suite"], None),
            ("TEST", ["Suite", "1bad"], None),
            ("NOT_A_TEST", ["a", "b"], None),
        ],
    )
    def test_the_macro_list_reads_each_macros_arguments(self, macro, arguments, expected):
        assert macro_test_case(macro, arguments) == expected

    def test_the_list_is_one_place_that_can_be_extended(self, monkeypatch):
        monkeypatch.setitem(TEST_MACROS, "MY_TEST", 2)
        functions = CppParser().parse("MY_TEST(Group, Case) { int x = 1; }\n", file_path="t.cpp").functions
        assert [f.qualified_name for f in functions] == ["Group.Case"]


class TestOneEntryPerMethod:
    def test_the_header_declarations_are_not_entries_of_their_own(self, project):
        connection = _db(project)
        try:
            ids = sorted(_short(row[0]) for row in connection.execute("SELECT function_id FROM artifacts").fetchall())
        finally:
            connection.close()
        assert ids.count("src/calc.cpp::math::Calc::mul") == 1 and ids.count("src/calc.cpp::math::helper") == 1
        assert not [i for i in ids if i.startswith("include/calc.h") and i.endswith("::mul")]
        assert "src/calc.cpp::math::Calc::operator==" in ids and "src/calc.cpp::math::identity<int>" in ids
        assert "src/test_calc.cpp::CalcSuite.Multiplies" in ids and "src/test_calc.cpp::CalcSuite.Doubles" in ids
        assert not [i for i in ids if i.endswith("::TEST")]

    def test_a_header_inline_definition_and_a_template_are_entries(self, project):
        connection = _db(project)
        try:
            files = {_short(row[0]): row[1] for row in connection.execute("SELECT function_id, file_path FROM artifacts").fetchall()}
        finally:
            connection.close()
        assert files["include/calc.h::math::Calc::base"] == "include/calc.h" and files["include/calc.h::math::identity"] == "include/calc.h"

    def test_looking_a_method_up_through_its_header_finds_the_definition(self, project):
        result = runner.invoke(app, ["--json", "source", "get", "include/calc.h::math::Calc::mul"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["file_path"] == "src/calc.cpp" and data["declared_in"] == ["include/calc.h"]

    def test_a_contract_saved_through_the_header_is_found_through_either_path(self, project):
        from drspec.contracts import Contract

        contract = Contract.from_dict(dict(
            function_signature="int mul(int, int)", intent_summary="Multiply two numbers",
            invariants=[dict(name="rule0", logic="Output is an integer", criticality="HIGH", on_fail="error")],
        )).to_json()
        assert runner.invoke(app, ["contract", "save", "include/calc.h::math::Calc::mul", "--confidence", "80"], input=contract).exit_code == 0
        via_header = runner.invoke(app, ["--json", "contract", "get", "include/calc.h::math::Calc::mul"])
        via_source = runner.invoke(app, ["--json", "contract", "get", "src/calc.cpp::math::Calc::mul"])
        assert via_header.exit_code == 0 and via_source.exit_code == 0

    def test_a_declaration_without_any_definition_stays_an_entry_and_is_not_dead_code(self, project):
        connection = _db(project)
        try:
            assert [r[0].split("#")[0] for r in connection.execute("SELECT function_id FROM artifacts WHERE function_name = 'identity_default'").fetchall()] == ["include/calc.h::math::identity_default"]
        finally:
            connection.close()
        unused = json.loads(runner.invoke(app, ["--json", "deps", "unused"]).output)["data"]["functions"]
        assert "identity_default" not in {f["function_name"] for f in unused}
        source = json.loads(runner.invoke(app, ["--json", "source", "get", "include/calc.h::math::identity_default"]).output)["data"]
        assert source["declaration_only"] is True

    def test_a_definition_scanned_later_turns_the_waiting_declaration_into_an_alias(self, project):
        (project / "src" / "extra.cpp").write_text('#include "calc.h"\nint math::identity_default(int v) { return v; }\n')
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        connection = _db(project)
        try:
            names = [r[0].split("#")[0] for r in connection.execute("SELECT function_id FROM artifacts WHERE function_name = 'identity_default'").fetchall()]
            alias = connection.execute("SELECT definition_id FROM function_declarations WHERE definition_id IS NOT NULL AND function_name = 'identity_default'").fetchall()
        finally:
            connection.close()
        assert len(names) <= 1 and (names == [] or names[0].startswith("src/")) and len(alias) <= 1

    def test_removing_the_definition_makes_the_declaration_an_entry_again(self, project):
        (project / "src" / "calc.cpp").write_text(CALC.replace("int helper(int a) { return local_util(a) * 2; }\n", ""))
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        connection = _db(project)
        try:
            rows = connection.execute("SELECT function_id FROM artifacts WHERE function_name = 'helper'").fetchall()
        finally:
            connection.close()
        assert [r[0].split("#")[0] for r in rows] == ["include/calc.h::math::helper"]


class TestCallEdges:
    def test_the_edges_the_plan_lists_exist_with_the_right_confidence(self, project):
        edges = _edges(project)
        wanted = {
            ("src/calc.cpp::math::Calc::mul", "src/calc.cpp::math::helper"),
            ("src/main.cpp::run", "src/calc.cpp::math::helper"),
            ("src/main.cpp::run", "src/calc.cpp::math::Calc::mul"),
            ("src/main.cpp::run", "src/calc.cpp::math::Calc::twice_static"),
        }
        found = {(a, b) for a, b, _ in edges}
        assert wanted <= found
        assert all(confidence == "EXTRACTED" for _, _, confidence in edges)

    def test_the_edge_count_rose_from_one_and_none_goes_to_a_wrong_function(self, project):
        edges = _edges(project)
        assert len(edges) >= 5
        callees = {b for _, b, _ in edges}
        assert callees <= {
            "src/calc.cpp::math::helper", "src/calc.cpp::math::Calc::mul", "src/calc.cpp::math::Calc::twice_static",
            "src/calc.cpp::math::local_util", "src/main.cpp::run",
        }

    def test_a_qualified_call_inside_a_class_reaches_the_static_member(self, project):
        assert ("src/calc.cpp::math::Calc::twice", "src/calc.cpp::math::Calc::twice_static", "EXTRACTED") in _edges(project)

    def test_a_member_call_on_a_variable_uses_its_declared_type(self, project):
        assert ("src/test_calc.cpp::CalcSuite.Multiplies", "src/calc.cpp::math::Calc::mul", "EXTRACTED") in _edges(project)

    def test_an_auto_variable_is_not_guessed(self, project):
        # an `auto` whose initialiser has no declared type is not given a type: the only edge it can get is the INFERRED one of the
        # by-name fallback (T-46: `mul` has one method of that name here), never an EXTRACTED or AMBIGUOUS one
        (project / "src" / "auto.cpp").write_text('#include "calc.h"\nint viaAuto() { auto c = unknownFactory(); return c.mul(1, 2); }\n')
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        edges = [e for e in _edges(project) if e[0] == "src/auto.cpp::viaAuto" and e[1].endswith("::mul")]
        assert all(confidence == "INFERRED" for _caller, _callee, confidence in edges)

    def test_an_auto_variable_takes_the_type_its_initialiser_declares(self, project):
        # `auto c = math::Calc(3)` is a Calc by what is written (T-45)
        (project / "src" / "auto.cpp").write_text('#include "calc.h"\nint viaAuto() { auto c = math::Calc(3); return c.mul(1, 2); }\n')
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        assert ("src/auto.cpp::viaAuto", "src/calc.cpp::math::Calc::mul", "EXTRACTED") in _edges(project)

    def test_a_function_that_is_not_included_is_not_reached(self, project):
        (project / "src" / "lonely.cpp").write_text("int lonely() { return math::helper(1); }\n")  # includes nothing
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        assert not [e for e in _edges(project) if e[0] == "src/lonely.cpp::lonely"]

    def test_an_include_two_levels_away_is_seen(self, project):
        (project / "include" / "outer.h").write_text('#pragma once\n#include "calc.h"\n')
        (project / "src" / "far.cpp").write_text('#include "../include/outer.h"\nint far() { return math::helper(1); }\n')
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        assert ("src/far.cpp::far", "src/calc.cpp::math::helper", "EXTRACTED") in _edges(project)

    def test_the_innermost_scope_wins(self, project):
        (project / "src" / "scopes.cpp").write_text(
            "int work(int a) { return a; }\nnamespace outer {\nint work(int a) { return a + 1; }\nint use() { return work(1); }\n}\n"
        )
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        assert [e for e in _edges(project) if e[0] == "src/scopes.cpp::outer::use"] == [("src/scopes.cpp::outer::use", "src/scopes.cpp::outer::work", "EXTRACTED")]

    def test_overloaded_callees_are_ambiguous_one_edge_each(self, project):
        (project / "src" / "over.cpp").write_text("int add(int a, int b) { return a + b; }\ndouble add(double a, double b) { return a + b; }\nint use() { return add(1, 2); }\n")
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        rows = [e for e in _edges(project) if e[0] == "src/over.cpp::use"]
        assert len(rows) == 2 and {e[2] for e in rows} == {"AMBIGUOUS"}


class TestIncludes:
    KNOWN = {"include/calc.h", "src/calc.cpp", "src/util/calc.h", "other/readme.h"}

    def test_relative_to_the_including_file_first(self):
        assert resolve_include("src/main.cpp", "util/calc.h", self.KNOWN) == "src/util/calc.h"
        assert resolve_include("src/main.cpp", "../include/calc.h", self.KNOWN) == "include/calc.h"

    def test_a_unique_path_ending_is_found_anywhere(self):
        assert resolve_include("src/main.cpp", "readme.h", self.KNOWN) == "other/readme.h"

    def test_a_target_two_files_could_be_is_the_nearer_one_or_nothing(self):
        assert resolve_include("src/main.cpp", "calc.h", self.KNOWN) == "src/util/calc.h" or resolve_include("src/main.cpp", "calc.h", self.KNOWN) is None
        assert resolve_include("elsewhere/x.cpp", "calc.h", self.KNOWN) is None

    def test_a_missing_header_is_none(self):
        assert resolve_include("src/main.cpp", "nowhere.h", self.KNOWN) is None

    def test_the_parser_collects_quoted_includes_only(self):
        result = CppParser().parse('#include "a.h"\n#include <vector>\n#include "sub/b.h"\nint f() { return 0; }\n', file_path="x.cpp")
        assert result.includes == ["a.h", "sub/b.h"]


class TestMeasuringProgress:
    def test_status_reports_the_cpp_files_skipped_constructs_and_unresolved_calls(self, project):
        (project / "src" / "spec.cpp").write_text("template<typename T> struct N { ~N() = default; };\ntemplate<> N<int>::~N<int>() = default;\nint z() { return 1; }\n")
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        cpp = json.loads(runner.invoke(app, ["--json", "status"]).output)["data"]["cpp"]
        assert cpp["files"] == 5 and cpp["skipped_constructs"] >= 1 and cpp["files_partially_parsed"] == 0
        assert cpp["calls"] > 0 and 0 <= cpp["unresolved_share"] <= 100
        assert "Cpp" in runner.invoke(app, ["status"]).output

    def test_a_project_without_cpp_has_no_such_section(self, kept_project):
        (kept_project / "a.py").write_text("def f():\n    return 1\n")
        runner.invoke(app, ["--json", "scan", "."])
        assert "cpp" not in json.loads(runner.invoke(app, ["--json", "status"]).output)["data"]


class TestMigrationOnACppDatabase:
    @pytest.fixture
    def project(self, tmp_path, monkeypatch):
        """The one test here whose objective is a database FILE (copied, rewritten as an old one, migrated): a real file, made by the schema and a scan."""
        from drspec.db import init_schema

        monkeypatch.chdir(tmp_path)
        _write_the_files(tmp_path)
        (tmp_path / "_drspec").mkdir()
        connection = duckdb.connect(str(tmp_path / "_drspec" / "contracts.db"))  # pattern-ok: duckdb-file the objective of the test is a database file
        init_schema(connection)
        connection.close()
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        return tmp_path

    def test_a_database_from_before_the_hash_gets_the_identifiers_a_fresh_parse_gives(self, project, tmp_path):
        import shutil

        from drspec.db.identity_migration import ensure_identity_layout

        database = project / "_drspec" / "contracts.db"
        connection = duckdb.connect(str(database), read_only=True)
        try:
            expected = sorted(row[0] for row in connection.execute("SELECT function_id FROM artifacts").fetchall())
            expected_aliases = sorted(connection.execute("SELECT function_id, definition_id FROM function_declarations").fetchall(), key=str)
        finally:
            connection.close()
        copy = tmp_path / "scanned-copy.db"
        shutil.copy(database, copy)
        old = tmp_path / "old" / "contracts.db"
        old.parent.mkdir()
        _strip(copy, old)
        assert ensure_identity_layout(old) == "migrated"
        migrated = duckdb.connect(str(old), read_only=True)
        try:
            assert sorted(row[0] for row in migrated.execute("SELECT function_id FROM artifacts").fetchall()) == expected
            assert sorted(migrated.execute("SELECT function_id, definition_id FROM function_declarations").fetchall(), key=str) == expected_aliases
        finally:
            migrated.close()


class TestWhatTheGrammarCannotReadDoesNotTakeTheRestWithIt:
    SOURCE = (
        "template<typename T> struct N { ~N() = default; };\n"
        "template<> N<int>::~N<int>() = default;\n"
        "EXPORT int exported(int a) { return a; }\n"
        "int next_one() { return 1; }\n"
    )

    def test_the_code_after_a_defaulted_specialisation_is_still_indexed(self):
        result = CppParser().parse(self.SOURCE, file_path="x.cpp")
        names = [f.qualified_name for f in result.functions]
        assert "exported" in names and "next_one" in names
        assert result.errors == [] and result.skipped_constructs == 1

    @pytest.mark.parametrize(
        "decorated",
        [
            "EXPORT int f() { return 1; }",
            "MYLIB_API int f() { return 1; }",
            "Q_DECL_EXPORT int f() { return 1; }",
            "__declspec(dllexport) int f() { return 1; }",
            "__attribute__((visibility(\"default\"))) int f() { return 1; }",
        ],
    )
    def test_export_macros_before_a_definition_are_blanked_out(self, decorated):
        result = CppParser().parse(decorated + "\nint g() { return 2; }\n", file_path="x.cpp")
        assert [f.qualified_name for f in result.functions] == ["f", "g"] and result.errors == []

    def test_a_decorated_class_keeps_its_methods(self):
        result = CppParser().parse("class MYLIB_API Widget {\npublic:\n    int size() const { return 1; }\n    int area() { return 2; }\n};\n", file_path="w.cpp")
        assert [f.qualified_name for f in result.functions] == ["Widget::size", "Widget::area"]

    def test_the_project_can_list_its_own_macros(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "_drspec").mkdir()
        source = "class OURLIB_PUBLIC_THING Gadget {\npublic:\n    int g() { return 1; }\n};\n"
        assert [f.qualified_name for f in CppParser().parse(source, file_path="g.cpp").functions] == []
        (tmp_path / "_drspec" / "cpp_macros.txt").write_text("# our export macro\nOURLIB_PUBLIC_THING\n")
        assert [f.qualified_name for f in CppParser().parse(source, file_path="g.cpp").functions] == ["Gadget::g"]

    def test_what_the_partial_rule_drops_is_reported(self, project):
        (project / "src" / "weird.cpp").write_text("int before() { return 0; }\nclass UNKNOWN_DECORATION Gadget {\npublic:\n    int g() { return 1; }\n};\nint after() { return 2; }\n")
        data = json.loads(runner.invoke(app, ["--json", "scan", "."]).output)["data"]
        assert data["files_partially_parsed"] == 1 and data["functions_skipped"] >= 1
        assert data["partial_examples"][0]["file"] == "src/weird.cpp"
        connection = _db(project)
        try:
            names = {r[0] for r in connection.execute("SELECT function_name FROM artifacts WHERE file_path = 'src/weird.cpp'").fetchall()}
        finally:
            connection.close()
        assert {"before", "after"} <= names


class TestAnonymousNamespaces:
    def test_a_call_into_an_anonymous_namespace_of_the_same_file_has_an_edge(self, project):
        (project / "src" / "anon.cpp").write_text(
            "namespace { int helper() { return 3; } }\nnamespace outer { namespace { int inner() { return 4; } } int use2() { return inner(); } }\nint use() { return helper(); }\n"
        )
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        edges = {(a, b) for a, b, _ in _edges(project)}
        assert ("src/anon.cpp::use", "src/anon.cpp::<anonymous>::helper") in edges
        assert ("src/anon.cpp::outer::use2", "src/anon.cpp::outer::<anonymous>::inner") in edges

    def test_another_file_cannot_call_into_it(self, project):
        (project / "src" / "anon.cpp").write_text("namespace { int hidden() { return 3; } }\n")
        (project / "src" / "other.cpp").write_text('#include "anon.cpp"\nint use() { return hidden(); }\n')
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        assert not [e for e in _edges(project) if e[0] == "src/other.cpp::use"]
