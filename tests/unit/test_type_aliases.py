"""A type alias (`using X = Base<Arg>;`, `typedef Base<Arg> X;`, also in a namespace, and an alias of an alias) names the class it stands for:
a class whose base is such an alias inherits the members of the target (with its template arguments bound), and a variable declared with
the alias is of that type. An alias cycle, or an alias of a type that is not a project class, stays unresolved by the member name, and
nothing loops. One scan of a small source, in memory, made once for the module; no process."""

from __future__ import annotations

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.core.refs import get_refs

runner = CliRunner()

SOURCE = """#include <memory>
#include <vector>
struct Context { double counter; };
template <class Ctx>
class NodeBase {
protected:
    std::shared_ptr<Ctx> context;
};
class PlainBase {
protected:
    std::shared_ptr<Context> context;
};
using AliasBase = NodeBase<Context>;
typedef NodeBase<Context> TypedefBase;
using PlainAlias = PlainBase;
using Outer = AliasBase;
using Cycle1 = Cycle2;
using Cycle2 = Cycle1;
using Lib = std::vector<int>;
namespace zmp {
using ZmpBase = NodeBase<Context>;
class InNamespace : public ZmpBase {
public:
    void tick() { context->counter += 1.0; }
};
}
class AliasNode : public AliasBase {
public:
    void tick() { context->counter += 2.0; }
};
class TypedefNode : public TypedefBase {
public:
    void tick() { context->counter += 3.0; }
};
class PlainAliasNode : public PlainAlias {
public:
    void tick() { context->counter += 4.0; }
};
class AliasOfAlias : public Outer {
public:
    void tick() { context->counter += 5.0; }
};
class OnCycle : public Cycle1 {
public:
    void tick() { context->counter += 6.0; }
};
class OnLibrary : public Lib {
public:
    void tick() { context->counter += 7.0; }
};
void viaAlias(TypedefBase& b) { b.context->counter = 8.0; }
"""


@pytest.fixture(scope="module")
def scanned(tmp_path_factory):
    from tests.fakes import KeptDuckDB
    from drspec.db import init_schema

    with pytest.MonkeyPatch.context() as patch:
        folder = tmp_path_factory.mktemp("aliases")
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
            found = get_refs(connection, "Context::counter")
        finally:
            connection.close()
    return found


def _owned(result: dict) -> list:
    """`(function name, kind)` of the accesses the result puts under Context."""
    return [(function["function_name"], access["kind"]) for block in result["owners"] if block["owner_class"] == "Context"
            for entry in block["files"] for function in entry["functions"] for access in function["accesses"]]


def _unresolved(result: dict) -> list:
    """`(function name, kind, site)` of the accesses that stay unresolved."""
    return [(entry["function_name"], line["kind"], line["site"]) for entry in result["unresolved"]["functions"] for line in entry["lines"]]


class TestAnAliasOfABaseIsFollowed:
    def test_a_using_alias_of_a_template_specialisation(self, scanned):
        assert ("AliasNode::tick", "readwrite") in _owned(scanned)

    def test_a_typedef_of_a_template_specialisation(self, scanned):
        assert ("TypedefNode::tick", "readwrite") in _owned(scanned)

    def test_an_alias_inside_a_namespace(self, scanned):
        assert ("zmp::InNamespace::tick", "readwrite") in _owned(scanned)

    def test_an_alias_of_an_alias(self, scanned):
        assert ("AliasOfAlias::tick", "readwrite") in _owned(scanned)

    def test_a_plain_class_alias(self, scanned):
        assert ("PlainAliasNode::tick", "readwrite") in _owned(scanned)

    def test_a_variable_declared_with_the_alias_is_of_its_type(self, scanned):
        assert ("viaAlias", "write") in _owned(scanned)


class TestWhatIsNotAClassStaysUnresolved:
    def test_an_alias_cycle_is_unresolved_by_the_member_name_and_does_not_loop(self, scanned):
        assert ("OnCycle::tick", "readwrite", "context->counter") in _unresolved(scanned)

    def test_an_alias_of_a_type_the_project_does_not_define_is_unresolved_by_the_member_name(self, scanned):
        assert ("OnLibrary::tick", "readwrite", "context->counter") in _unresolved(scanned)

    def test_nothing_of_those_is_attributed_to_a_class(self, scanned):
        names = {name for name, _kind in _owned(scanned)}
        assert "OnCycle::tick" not in names and "OnLibrary::tick" not in names
