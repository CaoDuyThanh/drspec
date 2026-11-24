"""A member inherited from a template base class is typed by the arguments the derived class gives the base (`class N : public Base<Context>`
makes `Base`'s `Ctx` the `Context`): its accesses resolve. An argument that stays unbound (a dependent type of the derived class) makes the
member unknown, and its accesses are unresolved by the member name they use, for the first member of a chain too; a plain base still works.
One scan of a small source, in memory, made once for the module; no process."""

from __future__ import annotations

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.core.refs import get_refs

runner = CliRunner()

SOURCE = """#include <memory>
struct Holder { int available; };
struct Context { Holder avoidanceResult; double counter; };
template <class Ctx>
class NodeBase {
protected:
    std::shared_ptr<Ctx> context;
};
class TickNode : public NodeBase<Context> {
public:
    using NodeBase<Context>::NodeBase;
    void tick() {
        context->counter += 2.0;
        context->avoidanceResult.available = false;
    }
};
class UsingNode : public NodeBase<Context> {
public:
    void tick() { context->counter += 4.0; }
};
class Mid : public NodeBase<Context> {};
class Leaf : public Mid {
public:
    void tick() { context->counter = 1.0; }
};
class PlainBase {
protected:
    std::shared_ptr<Context> context;
};
class PlainNode : public PlainBase {
public:
    void tick() { context->counter += 3.0; }
};
template <class T>
class Dependent : public NodeBase<T> {
public:
    void tick() { context->counter += 5.0; }
};
template <class ContextType>
class DependentLong : public NodeBase<ContextType> {
public:
    void tick() { context->avoidanceResult.available = true; }
};
"""


@pytest.fixture(scope="module")
def scanned(tmp_path_factory):
    from tests.fakes import KeptDuckDB
    from drspec.db import init_schema

    with pytest.MonkeyPatch.context() as patch:
        folder = tmp_path_factory.mktemp("template_base")
        KeptDuckDB().install(patch)
        (folder / "_drspec").mkdir()
        (folder / "m.hpp").write_text(SOURCE)
        connection = duckdb.connect(str(folder / "_drspec" / "contracts.db"))  # pattern-ok: duckdb-file kept in memory by `KeptDuckDB`
        init_schema(connection)
        connection.close()
        patch.chdir(folder)
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        connection = duckdb.connect(str(folder / "_drspec" / "contracts.db"), read_only=True)  # pattern-ok: duckdb-file kept in memory by `KeptDuckDB`
        try:
            found = {
                "counter": get_refs(connection, "Context::counter"),
                "available": get_refs(connection, "Holder::available"),
                "avoidance": get_refs(connection, "Context::avoidanceResult"),
            }
        finally:
            connection.close()
    return found


def _by_caller(result: dict, owner: str) -> list:
    """`(caller, kind)` of the accesses that the result puts under `owner`."""
    return [(function["function_name"], access["kind"]) for block in result["owners"] if block["owner_class"] == owner
            for entry in block["files"] for function in entry["functions"] for access in function["accesses"]]


class TestAMemberOfATemplateBaseIsResolved:
    def test_a_bound_template_base_resolves_the_member_it_declares(self, scanned):
        assert ("TickNode::tick", "readwrite") in _by_caller(scanned["counter"], "Context")

    def test_the_write_through_the_member_of_the_bound_base_is_under_its_class(self, scanned):
        assert ("TickNode::tick", "write") in _by_caller(scanned["available"], "Holder")

    def test_using_the_base_s_constructor_does_not_change_it(self, scanned):
        assert ("TickNode::tick", "readwrite") in _by_caller(scanned["counter"], "Context")  # the `using` is on the same class

    def test_a_base_of_a_base_is_followed_with_its_bindings(self, scanned):
        assert ("Leaf::tick", "write") in _by_caller(scanned["counter"], "Context")

    def test_a_class_that_uses_the_member_of_its_template_base_directly_resolves_it(self, scanned):
        assert ("UsingNode::tick", "readwrite") in _by_caller(scanned["counter"], "Context")

    def test_a_plain_base_still_works(self, scanned):
        assert ("PlainNode::tick", "readwrite") in _by_caller(scanned["counter"], "Context")


class TestAnUnboundArgumentIsUnresolvedNotSilent:
    def test_a_dependent_argument_leaves_the_member_unresolved_by_its_name(self, scanned):
        assert ("Dependent::tick", "readwrite") in _unresolved_callers_of(scanned["counter"])

    def test_the_first_member_of_a_chain_on_an_unbound_member_is_unresolved_too(self, scanned):
        assert ("DependentLong::tick", "read", "context->avoidanceResult") in _unresolved_callers_of_site(scanned["avoidance"])  # `context->avoidanceResult`
        assert ("DependentLong::tick", "write", "context->avoidanceResult.available") in _unresolved_callers_of_site(scanned["available"])

    def test_nothing_of_the_dependent_classes_is_attributed_to_a_class(self, scanned):
        assert all(function != "Dependent::tick" for block in scanned["counter"]["owners"] for entry in block["files"]
                   for function in (f["function_name"] for f in entry["functions"]))


def _unresolved_callers_of(result: dict) -> list:
    return [(entry["function_name"], line["kind"]) for entry in result["unresolved"]["functions"] for line in entry["lines"]]


def _unresolved_callers_of_site(result: dict) -> list:
    return [(entry["function_name"], line["kind"], line["site"]) for entry in result["unresolved"]["functions"] for line in entry["lines"]]
