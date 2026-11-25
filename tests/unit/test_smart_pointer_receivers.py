"""A receiver that is a smart pointer is the class it points to (`std::shared_ptr<T>` and `std::unique_ptr<T>`, const references to them,
a member of that type, an `auto` local from `std::make_shared<T>()`), and a receiver whose type is not known is stored as unresolved by the
member name it used, for the first member of a chain too. One scan of a small source, in memory, made once for the module; no process."""

from __future__ import annotations

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.db.queries import get_member_references
from drspec.parsers.cpp_parser import CppParser

runner = CliRunner()

SOURCE = """#include <memory>
struct Inner { int val; };
struct Ctx { int n; Inner inner; void run(); };
struct Owner { std::shared_ptr<Ctx> member; void go(); };
void viaShared(const std::shared_ptr<Ctx>& c) { c->n = 1; }
void viaUnique(std::unique_ptr<Ctx> u) { u->n = 2; }
void viaMade() { auto m = std::make_shared<Ctx>(); m->n = 3; }
void viaMakeUnique() { auto u = std::make_unique<Ctx>(); u->n = 4; }
void Owner::go() { member->n = 5; }
void viaChain(const std::shared_ptr<Ctx>& c) { c->inner.val = 6; }
void unknownFirst() { auto z = unknownFactory(); z->inner.val = 7; }
"""


@pytest.fixture(scope="module")
def scanned(tmp_path_factory):
    from tests.fakes import KeptDuckDB
    from drspec.db import init_schema

    with pytest.MonkeyPatch.context() as patch:
        folder = tmp_path_factory.mktemp("smart")
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
            rows = get_member_references(connection)
        finally:
            connection.close()
    return [(r["caller_id"].split("::", 1)[1].rsplit("#", 1)[0], r["owner_class"], r["member"], r["kind"], r["line"]) for r in rows]


def _in(rows, caller):
    return [(owner, member, kind) for name, owner, member, kind, _line in rows if name == caller]


class TestSmartPointersAreTheirClass:
    def test_a_const_shared_pointer_reference_parameter(self, scanned):
        assert _in(scanned, "viaShared") == [("Ctx", "n", "write")]

    def test_a_unique_pointer_parameter(self, scanned):
        assert _in(scanned, "viaUnique") == [("Ctx", "n", "write")]

    def test_an_auto_local_from_make_shared(self, scanned):
        assert _in(scanned, "viaMade") == [("Ctx", "n", "write")]

    def test_an_auto_local_from_make_unique(self, scanned):
        assert _in(scanned, "viaMakeUnique") == [("Ctx", "n", "write")]

    def test_a_member_of_type_shared_pointer(self, scanned):
        assert sorted(_in(scanned, "Owner::go")) == [("Ctx", "n", "write"), ("Owner", "member", "read")]  # `member` is read to reach `n`

    def test_a_chain_through_a_shared_pointer_resolves_each_member(self, scanned):
        assert sorted(_in(scanned, "viaChain")) == [("Ctx", "inner", "read"), ("Inner", "val", "write")]


class TestAnUnknownReceiverIsUnresolvedForEveryMember:
    def test_the_first_member_of_a_chain_on_an_unknown_receiver_is_unresolved(self, scanned):
        unresolved = [(member, kind) for name, owner, member, kind, _line in scanned if name == "unknownFirst" and owner == ""]
        assert ("inner", "read") in unresolved  # `z->inner`: the first member, counted under its own name

    def test_a_later_member_of_the_same_chain_is_unresolved_too(self, scanned):
        unresolved = [(member, kind) for name, owner, member, kind, _line in scanned if name == "unknownFirst" and owner == ""]
        assert ("val", "write") in unresolved

    def test_nothing_of_the_unknown_chain_is_attributed_to_a_class(self, scanned):
        assert all(owner == "" for owner, _member, _kind in _in(scanned, "unknownFirst"))


class TestTheParserKeepsTheReceiverOfMakeShared:
    def test_the_call_of_make_shared_is_a_receiver_the_resolver_can_read(self):
        source = "struct Ctx { int n; };\nvoid f() { auto m = std::make_shared<Ctx>(); m->n = 3; }\n"
        found = CppParser().parse(source, "x.cpp").functions
        function = next(f for f in found if f.name == "f")
        assert function.declared_param_types == {"m": "=std::make_shared<Ctx>()"}
