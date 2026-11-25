"""Two files define the same qualified class and alias (`zmp::BehaviorNodeBase`, `zmp::Sync`) differently. The definition a file can see is the
one it uses (the file's includes first, then the nearest folder); when none is nearer than another, the name is ambiguous, and the accesses
that need it are unresolved by the member name, the first member of a chain too. A single definition is unchanged. One scan of a small
folder tree, in memory, made once for the module; no process."""

from __future__ import annotations

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.core.refs import get_refs

runner = CliRunner()

FILES = {
    "ctx.hpp": "#pragma once\nstruct Holder { int available; };\nstruct Context { Holder avoidanceResult; double counter; };\nstruct ContextB { double counter; };\n",
    "pkgA/base.hpp": (
        '#pragma once\n#include "../ctx.hpp"\n#include <memory>\n'
        "namespace zmp {\ntemplate <typename N = ext::Missing>\nclass Base : public N {\npublic:\n    typedef ::Context ContextType;\n"
        "protected:\n    std::shared_ptr<ContextType> context;\n};\nusing Sync = Base<ext::Missing>;\n}\n"
    ),
    "pkgA/node.hpp": '#pragma once\n#include "base.hpp"\nnamespace zmp {\nclass NodeA : public Sync {\npublic:\n    void tick() { context->counter += 11.0; }\n};\n}\n',
    "pkgB/base.hpp": (
        '#pragma once\n#include "../ctx.hpp"\n#include <memory>\n'
        "namespace zmp {\ntemplate <typename N = ext::Missing>\nclass Base : public N {\npublic:\n    typedef ::ContextB ContextType;\n"
        "protected:\n    std::shared_ptr<ContextType> context;\n};\nusing Sync = Base<ext::Missing>;\n}\n"
    ),
    "pkgB/node.hpp": '#pragma once\n#include "base.hpp"\nnamespace zmp {\nclass NodeB : public Sync {\npublic:\n    void tick() { context->counter += 12.0; }\n};\n}\n',
    "one/a.hpp": "#pragma once\n#include <memory>\nnamespace zmp {\ntemplate <class C>\nclass Base2 {\nprotected:\n    std::shared_ptr<C> context;\n};\nusing Sync2 = Base2<::Context>;\n}\n",
    "one/b.hpp": "#pragma once\n#include <memory>\nnamespace zmp {\ntemplate <class C>\nclass Base2 {\nprotected:\n    std::shared_ptr<C> context;\n};\nusing Sync2 = Base2<::ContextB>;\n}\n",
    "one/use.hpp": (
        "#pragma once\nnamespace zmp {\nclass Use : public Sync2 {\npublic:\n"
        "    void tick() { context->counter += 1.0; }\n"
        "    void chain() { context->avoidanceResult.available = true; }\n};\n}\n"
    ),
    "one/solo.hpp": (
        '#pragma once\n#include "a.hpp"\nnamespace zmp {\nusing Solo = Base2<::Context>;\nclass SoloNode : public Solo {\npublic:\n'
        "    void tick() { context->counter += 2.0; }\n};\n}\n"
    ),
}


@pytest.fixture(scope="module")
def scanned(tmp_path_factory):
    from tests.fakes import KeptDuckDB
    from drspec.db import init_schema

    with pytest.MonkeyPatch.context() as patch:
        folder = tmp_path_factory.mktemp("ambiguous")
        KeptDuckDB().install(patch)
        (folder / "_drspec").mkdir()
        for name, text in FILES.items():
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_text(text)
        connection = duckdb.connect(str(folder / "_drspec" / "contracts.db"))  # pattern-ok: duckdb-file kept in memory by `KeptDuckDB`
        init_schema(connection)
        connection.close()
        patch.chdir(folder)
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        connection = duckdb.connect(str(folder / "_drspec" / "contracts.db"), read_only=True)  # pattern-ok: duckdb-file kept in memory by `KeptDuckDB`
        try:
            found = {
                "counter": get_refs(connection, "Context::counter"),
                "counter_b": get_refs(connection, "ContextB::counter"),
                "available": get_refs(connection, "Holder::available"),
                "avoidance": get_refs(connection, "Context::avoidanceResult"),
            }
        finally:
            connection.close()
    return found


def _owned(result: dict, owner: str) -> list:
    return [(function["function_name"], access["kind"]) for block in result["owners"] if block["owner_class"] == owner
            for entry in block["files"] for function in entry["functions"] for access in function["accesses"]]


def _unresolved(result: dict) -> list:
    return [(entry["function_name"], line["kind"], line["site"]) for entry in result["unresolved"]["functions"] for line in entry["lines"]]


class TestTheFileDecidesWhichDefinitionItMeans:
    def test_the_package_a_node_inherits_from_package_a(self, scanned):
        assert ("zmp::NodeA::tick", "readwrite") in _owned(scanned["counter"], "Context")
        assert ("zmp::NodeA::tick", "readwrite") not in _owned(scanned["counter_b"], "ContextB")

    def test_the_package_b_node_inherits_from_package_b(self, scanned):
        assert ("zmp::NodeB::tick", "readwrite") in _owned(scanned["counter_b"], "ContextB")
        assert ("zmp::NodeB::tick", "readwrite") not in _owned(scanned["counter"], "Context")

    def test_nothing_is_unresolved_except_the_ambiguous_class_itself(self, scanned):
        assert [name for name, _kind, _site in _unresolved(scanned["counter"])] == ["zmp::Use::tick"]  # (an unresolved access is listed under its member name)
        assert [name for name, _kind, _site in _unresolved(scanned["counter_b"])] == ["zmp::Use::tick"]


class TestAnAmbiguousNameIsUnresolvedNotSilent:
    def test_a_member_of_an_ambiguous_base_is_unresolved_by_its_name(self, scanned):
        assert ("zmp::Use::tick", "readwrite", "context->counter") in _unresolved(scanned["counter"])

    def test_the_first_member_of_a_chain_on_an_ambiguous_base_is_unresolved_too(self, scanned):
        assert ("zmp::Use::chain", "read", "context->avoidanceResult") in _unresolved(scanned["avoidance"])
        assert ("zmp::Use::chain", "write", "context->avoidanceResult.available") in _unresolved(scanned["available"])

    def test_nothing_of_the_ambiguous_class_is_attributed_to_a_class(self, scanned):
        assert all(name not in {"zmp::Use::tick", "zmp::Use::chain"} for name, _kind in _owned(scanned["counter"], "Context") + _owned(scanned["counter_b"], "ContextB"))


class TestASingleDefinitionIsUnchanged:
    def test_a_name_defined_once_resolves_as_before(self, scanned):
        assert ("zmp::SoloNode::tick", "readwrite") in _owned(scanned["counter"], "Context")
