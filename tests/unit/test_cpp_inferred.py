"""A C++ member call on a receiver whose type cannot be worked out gets an INFERRED edge to the one method of that name the
project defines, and to nothing else: a name defined by several methods, an overload set, a free function or an operator
gets none, and no EXTRACTED edge changes."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app

runner = CliRunner()

HEADER = """#pragma once
#include <map>
class Body { public: void rare(); void both(); void over(int a); void over(double a); };
class A { public: void dup(); };
class B { public: void dup(); };
class Typed { public: void own(); };
class Functor { public: void operator()(); };
void freeOnly();
void both();
namespace util { void helper(); }
"""

SOURCE = """#include "b.h"
void Body::rare() {}
void Body::both() {}
void Body::over(int a) {}
void Body::over(double a) {}
void A::dup() {}
void B::dup() {}
void Typed::own() {}
void Functor::operator()() {}
void freeOnly() {}
void both() {}
void util::helper() {}
void viaMap(std::map<int, Body>& m) { m[1].rare(); }
void viaPtrArray(Body** arr) { arr[0]->rare(); }
void viaPair(std::pair<int, Body>& pr) { pr.first.rare(); }
void viaFactory() { auto q = unknownFactory(); q.rare(); }
void viaDup() { auto q = unknownFactory(); q.dup(); }
void viaFreeName() { auto q = unknownFactory(); q.freeOnly(); }
void viaNamespaceName() { auto q = unknownFactory(); q.helper(); }
void viaBoth() { auto q = unknownFactory(); q.both(); }
void viaOverloads() { auto q = unknownFactory(); q.over(1); }
void viaOperator() { auto q = unknownFactory(); q.operator()(); }
void viaNothing() { auto q = unknownFactory(); q.nobody_defines_this(); }
struct Holder { ExtApi* api; };
static Ext* g_ext;
void viaExternalParam(std::string* s) { s->rare(); }
void viaExternalField(Holder* h) { h->api->rare(); }
void viaGlobal() { g_ext->rare(); }
void viaStrings(std::vector<std::string>& v) { v[0].rare(); }
void typed(Typed& t) { t.rare(); t.own(); }
void Typed::selfCall() { this->rare(); }
"""


@pytest.fixture
def project(kept_project):
    tmp_path = kept_project  # the working folder, with an empty project database in memory: `init` is not the objective here
    (tmp_path / "b.h").write_text(HEADER.replace("void own();", "void own(); void selfCall();"))
    (tmp_path / "b.cpp").write_text(SOURCE)
    assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
    return tmp_path


def _edges(project: Path) -> dict:
    conn = duckdb.connect(str(project / "_drspec" / "contracts.db"), read_only=True)
    try:
        rows = conn.execute("SELECT caller_id, callee_id, confidence FROM dependencies").fetchall()
    finally:
        conn.close()
    short = lambda function_id: function_id.split("::", 1)[1].rsplit("#", 1)[0]  # noqa: E731
    result: dict = {}
    for caller, callee, confidence in rows:
        result.setdefault((short(caller), short(callee)), set()).add(confidence)
    return result


def _status(key: str = "cpp") -> dict:
    return json.loads(runner.invoke(app, ["--json", "status"]).output)["data"][key]


class TestTheUniqueMethodName:
    @pytest.mark.parametrize("caller", ["viaMap", "viaPtrArray", "viaPair", "viaFactory"])
    def test_a_call_on_an_untyped_receiver_gets_an_inferred_edge_to_the_one_method(self, project, caller):
        assert _edges(project).get((caller, "Body::rare")) == {"INFERRED"}

    def test_a_method_defined_in_two_classes_gets_no_edge(self, project):
        edges = _edges(project)
        assert not [pair for pair in edges if pair[0] == "viaDup"]

    def test_the_call_it_leaves_is_counted_as_defined_in_several_places(self, project):
        assert _status()["unresolved_reasons"]["defined in several places"] >= 1

    def test_a_free_function_or_a_namespace_function_is_not_a_candidate_for_a_member_call(self, project):
        edges = _edges(project)
        assert not [pair for pair in edges if pair[0] in ("viaFreeName", "viaNamespaceName")]

    def test_a_free_function_of_the_same_name_does_not_make_the_method_ambiguous(self, project):
        assert _edges(project).get(("viaBoth", "Body::both")) == {"INFERRED"}

    def test_an_overload_set_and_an_operator_and_a_name_nobody_defines_get_none(self, project):
        edges = _edges(project)
        assert not [pair for pair in edges if pair[0] in ("viaOverloads", "viaOperator", "viaNothing")]

    @pytest.mark.parametrize("caller", ["viaExternalParam", "viaExternalField", "viaGlobal", "viaStrings"])
    def test_a_receiver_declared_as_a_type_the_project_does_not_define_is_not_a_method_of_the_project(self, project, caller):
        # `std::string*`, a field of a library type, a variable declared at file level, the elements of a `std::vector<std::string>`
        assert not [pair for pair in _edges(project) if pair[0] == caller]

    def test_a_receiver_whose_type_is_known_is_never_guessed_at(self, project):
        edges = _edges(project)
        assert ("typed", "Typed::own") in edges and edges[("typed", "Typed::own")] == {"EXTRACTED"}
        assert ("typed", "Body::rare") not in edges  # `Typed` is known and has no `rare`
        assert ("Typed::selfCall", "Body::rare") not in edges  # nor is `this`


class TestAClashingClassName:
    """A class name defined twice with different fields is dropped from the type facts (`_read_type_facts`); its method is still a method
    of the project: the one ordinary method of that name elsewhere is not "the one method" the call names."""

    def test_a_name_with_one_method_in_a_dropped_class_and_one_ordinary_method_gets_no_edge(self, kept_project):
        (kept_project / "a.h").write_text("#pragma once\nclass Plain { public: void shared(); };\nclass Twin { public: Plain* one; void shared(); };\n")
        (kept_project / "b.h").write_text("#pragma once\nclass Twin { public: Plain* two; void shared(); };\n")
        (kept_project / "m.cpp").write_text(
            '#include "a.h"\n#include "b.h"\nvoid Twin::shared() {}\nvoid Plain::shared() {}\n'
            "void caller() { auto q = unknownFactory(); q.shared(); }\n"
        )
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        assert not [pair for pair in _edges(kept_project) if pair[0] == "caller"]


class TestWhatTheUserSees:
    def test_status_counts_the_inferred_edges_apart(self, project):
        by_confidence = json.loads(runner.invoke(app, ["--json", "status"]).output)["data"]["edges"]["calls"]["by_confidence"]
        assert by_confidence["INFERRED"] == 5 and by_confidence["EXTRACTED"] >= 1  # the four of `rare` and `viaBoth`
        output = runner.invoke(app, ["status"]).output  # the default: the calls on one line, with the inferred ones apart from the extracted
        assert "Edges Calls: 6 (extracted 1, inferred 5, ambiguous 0)" in output or "inferred 5" in output, output
        details = runner.invoke(app, ["status", "--details"]).output
        calls = details.split("  Calls:")[1].split("  References:")[0]  # the section of the calls (the references have their own)
        assert [line.strip() for line in calls.splitlines() if "Inferred" in line] == ["Inferred: 5"], calls

    def test_deps_get_shows_the_confidence_of_each_edge(self, project):
        data = json.loads(runner.invoke(app, ["--json", "deps", "get", "b.cpp::Body::rare", "--direction", "callers"]).output)["data"]
        assert {entry["function_name"]: entry["confidence"] for entry in data["callers"]} == {name: "INFERRED" for name in ("viaMap", "viaPtrArray", "viaPair", "viaFactory")}
        typed = json.loads(runner.invoke(app, ["--json", "deps", "get", "b.cpp::typed", "--direction", "callees"]).output)["data"]
        assert {entry["function_name"]: entry["confidence"] for entry in typed["callees"]} == {"own": "EXTRACTED"}
        assert "Confidence: INFERRED" in runner.invoke(app, ["deps", "get", "b.cpp::Body::rare", "--direction", "callers"]).output

    def test_the_mcp_tool_shows_it_too(self, project):
        from drspec.mcp_server import _tool_deps_get

        result, failed = _tool_deps_get(project / "_drspec" / "contracts.db", {"function_id": "b.cpp::Body::rare", "direction": "callers"})
        assert not failed and {entry["confidence"] for entry in result["callers"]} == {"INFERRED"}


class TestTheSetting:
    def test_it_is_on_by_default(self, project):
        out = json.loads(runner.invoke(app, ["--json", "config", "get", "resolve.infer_by_unique_name"]).output)
        assert "true" in json.dumps(out).lower()

    def test_off_leaves_the_graph_as_it_was_without_the_fallback(self, project):
        on = _edges(project)
        assert runner.invoke(app, ["config", "set", "resolve.infer_by_unique_name", "false"]).exit_code == 0
        assert runner.invoke(app, ["--json", "scan", ".", "--full"]).exit_code == 0
        off = _edges(project)
        assert not [pair for pair, tiers in off.items() if "INFERRED" in tiers]
        # nothing but the INFERRED edges is gone, and nothing else changed
        assert off == {pair: tiers for pair, tiers in on.items() if tiers != {"INFERRED"}}
        assert runner.invoke(app, ["config", "set", "resolve.infer_by_unique_name", "true"]).exit_code == 0
        assert runner.invoke(app, ["--json", "scan", ".", "--full"]).exit_code == 0
        assert _edges(project) == on  # and back

    def test_turning_it_off_re_resolves_at_the_next_publish_without_a_full_scan(self, project):
        assert runner.invoke(app, ["config", "set", "resolve.infer_by_unique_name", "false"]).exit_code == 0
        (project / "b.cpp").write_text(SOURCE + "void another() {}\n")  # any change publishes
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        assert not [pair for pair, tiers in _edges(project).items() if "INFERRED" in tiers]


class TestAfterAnEdit:
    def test_a_second_method_of_the_name_takes_the_edge_away_from_a_file_nobody_touched(self, project):
        assert _edges(project).get(("viaMap", "Body::rare")) == {"INFERRED"}
        (project / "other.h").write_text("#pragma once\nclass Other { public: void rare(); };\n")
        (project / "other.cpp").write_text('#include "other.h"\nvoid Other::rare() {}\n')
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        assert not [pair for pair in _edges(project) if pair[0] == "viaMap"]  # b.cpp was not edited
        (project / "other.cpp").unlink()
        (project / "other.h").unlink()
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        assert _edges(project).get(("viaMap", "Body::rare")) == {"INFERRED"}
