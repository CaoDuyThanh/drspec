"""Reads and writes of members and fields (C++ first): what the parser finds in a function's body, and the table that holds them
(`member_references`, apart from the call and reference tables, with no foreign key: a function's rows go with it)."""

from __future__ import annotations

import duckdb
import pytest

from drspec.db import init_schema
from drspec.db.queries import (
    delete_references_for_function,
    get_member_references,
    replace_member_references,
)
from drspec.parsers.cpp_parser import CppParser

SOURCE = """#include <vector>
struct AvoidanceResult { std::vector<int> data; bool ok; };
struct Holder {
  std::vector<int> data;
  int count;
  void fill(AvoidanceResult& r, AvoidanceResult* p) {
    r.data.empty();
    r.data.push_back(1);
    r.ok = true;
    if (r.ok) { count++; }
    int x = p->data[0];
    this->data.clear();
    data.push_back(2);
    count += 3;
    auto f = [&]() { return r.ok; };
    int local = 0;
    local = count;
  }
};
"""


def _accesses(source: str, name: str = "", path: str = "m.hpp") -> list:
    functions = CppParser().parse(source, path).functions
    function = next(f for f in functions if f.name == name) if name else functions[0]
    return [(a.line, a.receiver, a.member, a.kind) for a in function.member_accesses]


class TestWhatTheParserFinds:
    def test_every_shape_of_an_access_with_its_kind_and_line(self):
        assert _accesses(SOURCE) == [
            (7, "r", "data", "read"),  # r.data.empty(): a read of the field (empty() is not in the list of methods that change)
            (8, "r", "data", "write"),  # r.data.push_back(1): a write through a call
            (9, "r", "ok", "write"),  # r.ok = true
            (10, "r", "ok", "read"),  # if (r.ok)
            (10, None, "count", "readwrite"),  # count++ (a bare name: the resolver says it is the field of the class)
            (11, "p", "data", "read"),  # p->data[0]
            (12, "this", "data", "write"),  # this->data.clear()
            (13, None, "data", "write"),  # data.push_back(2)
            (14, None, "count", "readwrite"),  # count += 3
            (15, "r", "ok", "read"),  # in a lambda: the enclosing function's
            (17, None, "count", "read"),  # local = count: `local` is declared here, not a field
        ]

    def test_a_method_call_is_a_call_and_not_an_access(self):
        source = "struct A { int n; int get(); };\nint f(A& a, A* p) { return a.get() + p->get(); }\n"
        assert _accesses(source) == []

    @pytest.mark.parametrize(
        "statement, expected",
        [
            ("r.n = 1;", "write"), ("r.n += 1;", "readwrite"), ("r.n -= 1;", "readwrite"), ("r.n++;", "readwrite"), ("--r.n;", "readwrite"),
            ("r.v[0] = 1;", "write"), ("(r.n) = 1;", "write"), ("int k = r.n;", "read"), ("use(r.n);", "read"), ("r.v.size();", "read"),
            ("r.v.clear();", "write"), ("r.v.insert(r.v.begin(), 1);", "write"), ("if (r.n == 1) {}", "read"), ("int k = r.v[1];", "read"),
        ],
    )
    def test_the_kind_follows_the_statement(self, statement, expected):
        source = f"struct R {{ int n; int v[2]; }};\nvoid f(R& r) {{\n  {statement}\n}}\n"
        accesses = _accesses(source, "f")
        assert accesses and accesses[0][3] == expected, accesses

    def test_a_receiver_too_complex_to_write_down_is_kept_with_an_empty_receiver(self):
        source = "struct R { int n; };\nint f(R a, R b, bool c) { return (c ? a : b).n; }\n"
        assert _accesses(source, "f") == [(2, "", "n", "read")]

    def test_a_chain_of_accesses_is_one_per_member(self):
        source = "struct In { int z; };\nstruct R { In inner; };\nint f(R& r) { r.inner.z = 3; return 0; }\n"
        assert _accesses(source, "f") == [(3, "r.inner", "z", "write"), (3, "r", "inner", "read")]  # the outer one first

    def test_a_bare_name_is_only_collected_inside_a_method(self):
        source = "int g;\nvoid free_function() { g = 1; }\nstruct S { int g; void m() { g = 2; } };\n"
        assert _accesses(source, "free_function") == []
        assert _accesses(source, "m") == [(3, None, "g", "write")]

    def test_locals_parameters_and_loop_variables_are_not_collected_as_bare_names(self):
        source = (
            "struct S { int total; int step;\n"
            "  void m(int step) {\n"
            "    for (int i = 0; i < 3; ++i) { total += i; }\n"
            "    for (auto& e : list) { total += e; }\n"
            "    auto g = [](int total) { return total; };\n"
            "    step = 1;\n"
            "  }\n"
            "};\n"
        )
        found = [(member, kind) for _line, _receiver, member, kind in _accesses(source, "m")]
        assert ("total", "readwrite") in found and ("step", "write") not in found and ("i", "read") not in found and ("e", "read") not in found

    def test_a_nested_function_has_its_own_accesses(self):
        source = "struct S { int a; void m() { struct L { int b; void n() { b = 1; } }; a = 1; } };\n"
        assert _accesses(source, "m") == [(1, None, "a", "write")]

    def test_a_c_file_is_not_read_for_members(self):
        assert CppParser().parse("struct S { int a; };\nint f(struct S* s) { return s->a; }\n", "m.c").functions[0].member_accesses == []

    def test_a_definition_outside_the_class_is_a_method_too(self):
        source = "struct S { int a; void m(); };\nvoid S::m() { a = 1; this->a = 2; }\n"
        assert _accesses(source, "m", "m.cpp") == [(2, None, "a", "write"), (2, "this", "a", "write")]


class TestAConditionIsNotADeclaration:
    """`if (flag)` holds an expression, not a declaration: a field used alone as a condition used to be taken for a local variable of that
    name, and every access to it in the function was dropped (a 4,500-line file lost 6 accesses of `pendingPathRequest` in one method)."""

    FIELD = "struct S {\n  bool pending = false;\n  void update() {\n    %s\n    pending = false;\n  }\n};\n"

    @pytest.mark.parametrize(
        "condition",
        ["if (pending) { work(); }", "while (pending) { work(); }", "if (!pending) { work(); }", "switch (pending) { default: break; }", "if (work(), pending) { work(); }"],
        ids=["if", "while", "not", "switch", "comma"],
    )
    def test_a_field_used_alone_in_a_condition_keeps_every_access_in_the_function(self, condition):
        found = _accesses(self.FIELD % condition, "update")
        assert (5, None, "pending", "write") in found and any(kind == "read" for _line, _receiver, _member, kind in found), found

    def test_the_read_in_the_condition_is_recorded_too(self):
        assert _accesses(self.FIELD % "if (pending) { work(); }", "update") == [(4, None, "pending", "read"), (5, None, "pending", "write")]

    @pytest.mark.parametrize("condition", ["if (int pending = get()) { work(); }", "if (auto pending = get(); pending > 1) { work(); }"], ids=["declaration", "init_statement"])
    def test_a_variable_declared_in_a_condition_is_still_a_local(self, condition):
        assert not [a for a in _accesses(self.FIELD % condition, "update") if a[2] == "pending"]


class TestTheTable:
    @pytest.fixture
    def conn(self):
        connection = duckdb.connect(":memory:")
        init_schema(connection)
        yield connection
        connection.close()

    def _rows(self):
        return [
            ("Holder", "data", "write", "m.hpp", 8, "r.data", ""),
            ("Holder", "data", "read", "m.hpp", 7, "r.data", ""),
            ("", "n", "read", "m.hpp", 9, "(c ? a : b).n", ""),
            ("Holder", "data", "write", "m.hpp", 8, "r.data", ""),  # the same row twice is one
        ]

    def test_the_rows_of_a_function_are_replaced_not_added_to(self, conn):
        replace_member_references(conn, "m.hpp::fill#1", self._rows())
        replace_member_references(conn, "m.hpp::fill#1", self._rows()[:1])
        assert [(r["owner_class"], r["member"], r["kind"], r["line"]) for r in get_member_references(conn)] == [("Holder", "data", "write", 8)]

    def test_two_functions_keep_their_own_rows_and_a_query_by_class_and_member(self, conn):
        replace_member_references(conn, "m.hpp::a#1", self._rows())
        replace_member_references(conn, "m.hpp::b#1", self._rows()[:1])
        found = get_member_references(conn, "Holder", "data")
        assert [(r["caller_id"], r["kind"], r["line"]) for r in found] == [("m.hpp::a#1", "read", 7), ("m.hpp::a#1", "write", 8), ("m.hpp::b#1", "write", 8)]
        assert [r["member"] for r in get_member_references(conn, "")] == ["n"]  # an unresolved access has no class

    def test_a_removed_function_takes_its_rows_with_it_and_not_the_others(self, conn):
        replace_member_references(conn, "m.hpp::a#1", self._rows())
        replace_member_references(conn, "m.hpp::b#1", self._rows()[:1])
        delete_references_for_function(conn, "m.hpp::a#1")
        assert {r["caller_id"] for r in get_member_references(conn)} == {"m.hpp::b#1"}

    def test_an_existing_database_without_the_table_gets_it(self):
        connection = duckdb.connect(":memory:")
        init_schema(connection)
        connection.execute("DROP TABLE member_references")
        init_schema(connection)
        assert get_member_references(connection) == []
        connection.close()

    def test_the_call_and_reference_tables_are_untouched(self, conn):
        replace_member_references(conn, "m.hpp::a#1", self._rows())
        assert conn.execute("SELECT COUNT(*) FROM dependencies").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM code_references").fetchone()[0] == 0


def _through(source: str, name: str = "") -> list:
    """(line, receiver, member, kind, written_through) of a function's accesses, as the parser records them."""
    functions = CppParser().parse(source, "m.cpp").functions
    function = next(f for f in functions if f.name == name) if name else functions[0]
    return [(a.line, a.receiver, a.member, a.kind, a.written_through) for a in function.member_accesses]


class TestTheBaseOfAWriteChainKnowsWhatIsWrittenThroughIt:
    """A base of a chain whose last member is written carries the members the write goes through; the base keeps its kind (a read)."""

    def test_a_write_of_a_member_of_the_base_marks_the_base(self):
        source = "struct H { int available; };\nstruct C { H avoid; };\nvoid f(C* c) { c->avoid.available = true; }\n"
        assert _through(source, "f") == [(3, "c->avoid", "available", "write", ""), (3, "c", "avoid", "read", "available")]

    def test_an_increment_and_a_compound_assignment_mark_the_base_too(self):
        source = "struct H { int n; };\nstruct C { H avoid; };\nvoid f(C& c) { c.avoid.n += 1; c.avoid.n++; }\n"
        marks = [m for m in _through(source, "f") if m[1] == "c" and m[2] == "avoid"]
        assert marks == [(3, "c", "avoid", "read", "n"), (3, "c", "avoid", "read", "n")]

    def test_a_chain_of_depth_three_marks_every_base_and_the_last_member_stays_the_write(self):
        source = "struct D { int d; };\nstruct C { D c; };\nstruct B { C b; };\nstruct A { B a; };\nvoid f(A& a) { a.a.b.c.d = 1; }\n"
        found = _through(source, "f")
        assert (5, "a.a.b", "c", "read", "d") in found  # `c` is the base of `d`: written through `d`
        assert (5, "a.a", "b", "read", "c.d") in found  # `b` is written through `c.d`
        assert (5, "a", "a", "read", "b.c.d") in found  # `a` is written through `b.c.d`
        assert [m for m in found if m[2] == "d"] == [(5, "a.a.b.c", "d", "write", "")]  # the written member is a write with no mark

    def test_a_chain_that_is_only_read_has_no_mark(self):
        source = "struct H { int n; };\nstruct C { H avoid; };\nint f(C& c) { return c.avoid.n; }\n"
        assert _through(source, "f") == [(3, "c.avoid", "n", "read", ""), (3, "c", "avoid", "read", "")]  # the outer member first

    def test_a_subscript_between_the_members_is_passed_through(self):
        source = "struct H { int n; };\nstruct C { H items[4]; };\nvoid f(C& c) { c.items[0].n = 2; }\n"
        assert (3, "c", "items", "read", "n") in _through(source, "f")

    def test_a_call_in_the_way_is_not_a_write_of_the_base(self):
        source = "struct H { int n; H* get(); };\nstruct C { H avoid; };\nvoid f(C& c) { c.avoid.get()->n = 1; }\n"
        assert [m for m in _through(source, "f") if m[2] == "avoid"] == [(3, "c", "avoid", "read", "")]

    def test_a_base_that_is_itself_the_written_member_has_no_mark(self):
        source = "struct C { int n; };\nvoid f(C& c) { c.n = 1; }\n"
        assert _through(source, "f") == [(2, "c", "n", "write", "")]
