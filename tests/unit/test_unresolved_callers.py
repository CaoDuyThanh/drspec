"""An empty list of callers says so when call sites name the function but could not be resolved to it, and `status`
splits the calls that were not found by what the project defines under the name."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.core import unresolved
from drspec.db import init_schema

runner = CliRunner()

pytestmark = pytest.mark.usefixtures("kept_duckdb", "cached_cli_command")  # the project database is in memory, kept between the commands

CPP_HEADER = """#pragma once
#include <vector>
namespace game {
class Base {
 public:
  virtual bool isReady();
  void baseWork();
};
class Derived : public Base {
 public:
  void run();
  bool isReady() override;
};
class Body { public: void tick(); };
void useAll(std::vector<Body>& bodies);
}
"""

CPP_SOURCE = """#include "more.h"
namespace game {
bool Base::isReady() { return true; }
void Base::baseWork() {}
bool Derived::isReady() { return false; }
void Derived::run() {
  std::map<int, Base> m;
  m[1].baseWork();                 // an element of a map: a type the resolver does not work out
  auto q = unknownFactory();
  q.baseWork();                    // `auto` of a call nothing declares
  Base::isReady();                 // qualified base
}
void Body::tick() {}
void useAll(std::vector<Body>& bodies) {
  bodies.size();                   // a library call
  mystery(1);                      // nothing named mystery
}
}
"""

PYTHON = """class Engine:
    def spin(self):
        return 1


class Other:
    def spin(self):
        return 2


def drive(vehicle):
    return vehicle.spin() + missing_fn()


def main():
    return drive(None)
"""

JAVASCRIPT = """class Engine {
  spin() { return 1; }
}
class Other {
  spin() { return 2; }
}
function drive(vehicle) {
  return vehicle.spin() + missingFn();
}
function main() { return drive(null); }
module.exports = { Engine, Other, drive, main };
"""

RUST = """struct Engine;
impl Engine {
    fn spin(&self) -> i32 { 1 }
}
fn drive(vehicle: Mystery) -> i32 {
    vehicle.spin() + missing_fn()
}
fn main() { drive(Mystery); }
"""


def _project(tmp_path: Path, monkeypatch, files: dict, infer: bool = False) -> Path:
    monkeypatch.chdir(tmp_path)
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    (tmp_path / "_drspec").mkdir(exist_ok=True)  # the project database, in memory (`kept_duckdb`), made as `init` makes it: `init` is not the objective here
    connection = duckdb.connect(str(tmp_path / "_drspec" / "contracts.db"))
    init_schema(connection)
    connection.close()
    if not infer:  # most of these tests are about what the declared types give: the by-name fallback (T-46) has tests of its own
        assert runner.invoke(app, ["config", "set", "resolve.infer_by_unique_name", "false"]).exit_code == 0
    assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
    return tmp_path


def _deps(function_id: str, direction: str = "callers", json_output: bool = True):
    args = ["--json"] if json_output else []
    result = runner.invoke(app, [*args, "deps", "get", function_id, "--direction", direction])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["data"] if json_output else result.output


def _status(key: str) -> dict:
    return json.loads(runner.invoke(app, ["--json", "status"]).output)["data"][key]


class TestTheNoteWithTheByNameRuleOn:
    """The rule is on by default: a name one method defines gets an INFERRED caller (no note); a name two classes define is still the note."""

    @pytest.fixture
    def project(self, tmp_path, monkeypatch):
        header = "#pragma once\nclass A { public: void go(); void solo(); };\nclass B { public: void go(); };\n"
        source = (
            '#include "m.h"\nvoid A::go() {}\nvoid B::go() {}\nvoid A::solo() {}\n'
            "void viaGo() { auto q = unknownFactory(); q.go(); }\nvoid viaSolo() { auto q = unknownFactory(); q.solo(); }\n"
        )
        return _project(tmp_path, monkeypatch, {"m.h": header, "m.cpp": source}, infer=True)

    def test_a_name_defined_in_two_classes_still_says_no_resolved_callers(self, project):
        data = _deps("m.cpp::A::go")
        assert data["callers"] == []
        assert data["unresolved_callers"]["message"] == "no resolved callers; 1 call site in 1 function name go but could not be resolved"
        assert {site["reason"] for site in data["unresolved_callers"]["sites"]} == {unresolved.SEVERAL_DEFINITIONS}
        assert _status("cpp")["unresolved_reasons"][unresolved.SEVERAL_DEFINITIONS] == 1

    def test_a_name_one_method_defines_has_the_inferred_caller_and_no_note(self, project):
        data = _deps("m.cpp::A::solo")
        assert [(entry["function_name"], entry["confidence"]) for entry in data["callers"]] == [("viaSolo", "INFERRED")]
        assert "unresolved_callers" not in data


class TestTheReproduction:
    @pytest.fixture
    def project(self, tmp_path, monkeypatch):
        return _project(tmp_path, monkeypatch, {"src/more.h": CPP_HEADER, "src/more.cpp": CPP_SOURCE})

    def test_a_function_called_only_through_unresolved_calls_says_so(self, project):
        data = _deps("src/more.cpp::game::Base::baseWork")
        assert data["callers"] == []
        found = data["unresolved_callers"]
        assert (found["name"], found["call_sites"], found["functions"]) == ("baseWork", 2, 1)
        assert found["message"] == "no resolved callers; 2 call sites in 1 function name baseWork but could not be resolved"
        assert [(site["call"], site["line"]) for site in found["sites"]] == [("m[1].baseWork()", 8), ("q.baseWork()", 10)]
        assert all(site["function_id"].startswith("src/more.cpp::game::Derived::run#") and site["file_path"] == "src/more.cpp" for site in found["sites"])
        assert {site["reason"] for site in found["sites"]} == {unresolved.RECEIVER_UNKNOWN}
        assert list(data).index("unresolved_callers") == list(data).index("callers") + 1

    def test_the_text_prints_the_note_and_the_sites_under_callers(self, project):
        text = _deps("src/more.cpp::game::Base::baseWork", json_output=False)
        callers = text.split("Callers:")[1].split("Summary:")[0]
        assert "(no resolved callers; 2 call sites in 1 function name baseWork but could not be resolved)" in callers
        assert "src/more.cpp:8  in game::Derived::run#" in callers and "m[1].baseWork()" in callers
        assert "src/more.cpp:10" in callers and "q.baseWork()" in callers

    def test_a_function_with_a_resolved_caller_has_no_note(self, project):
        data = _deps("src/more.cpp::game::Derived::run")
        assert "unresolved_callers" not in data
        # a resolved caller: `Base::isReady()` is called by `run` through the qualified name
        assert _deps("src/more.cpp::game::Base::isReady")["callers"] and "unresolved_callers" not in _deps("src/more.cpp::game::Base::isReady")

    def test_an_entry_point_that_nothing_names_has_no_note(self, project):
        data = _deps("src/more.cpp::game::useAll")
        assert data["callers"] == [] and "unresolved_callers" not in data

    def test_the_note_is_only_for_the_direction_that_asks_for_callers(self, project):
        assert "unresolved_callers" not in _deps("src/more.cpp::game::Base::baseWork", "callees")
        assert "unresolved_callers" in _deps("src/more.cpp::game::Base::baseWork", "both")

    def test_status_splits_what_was_not_found_by_what_the_project_defines(self, project):
        health = _status("cpp")
        reasons = health["unresolved_reasons"]
        assert "not found" not in reasons and sum(reasons.values()) == health["unresolved_calls"]
        # `m[1].baseWork()` and `q.baseWork()`: our own function, receiver unknown; `size()`, `mystery(1)` and `unknownFactory()`: defined nowhere
        assert reasons[unresolved.RECEIVER_UNKNOWN] == 2 and reasons[unresolved.NO_DEFINITION] == 3

    def test_the_mcp_tool_returns_the_same_field(self, project):
        from drspec.mcp_server import _tool_deps_get

        result, failed = _tool_deps_get(project / "_drspec" / "contracts.db", {"function_id": "src/more.cpp::game::Base::baseWork", "direction": "callers"})
        assert not failed and result["callers"] == []
        assert result["unresolved_callers"]["call_sites"] == 2 and list(result).index("unresolved_callers") == list(result).index("callers") + 1
        quiet, failed = _tool_deps_get(project / "_drspec" / "contracts.db", {"function_id": "src/more.cpp::game::useAll", "direction": "callers"})
        assert not failed and "unresolved_callers" not in quiet


class TestTheOtherLanguages:
    def test_python(self, tmp_path, monkeypatch):
        _project(tmp_path, monkeypatch, {"m.py": PYTHON})
        found = _deps("m.py::Engine.spin")["unresolved_callers"]
        assert (found["call_sites"], found["functions"]) == (1, 1) and found["sites"][0]["call"] == "vehicle.spin()" and found["sites"][0]["line"] == 12
        assert found["sites"][0]["reason"] == unresolved.SEVERAL_DEFINITIONS  # two classes define spin
        assert "unresolved_callers" not in _deps("m.py::main")
        reasons = _status("python")["unresolved_reasons"]
        assert reasons[unresolved.NO_DEFINITION] == 1 and "not found" not in reasons  # missing_fn

    def test_javascript(self, tmp_path, monkeypatch):
        _project(tmp_path, monkeypatch, {"m.js": JAVASCRIPT})
        found = _deps("m.js::Engine.spin")["unresolved_callers"]
        assert (found["call_sites"], found["functions"]) == (1, 1) and found["sites"][0]["call"] == "vehicle.spin()" and found["sites"][0]["line"] == 8
        assert "unresolved_callers" not in _deps("m.js::main")
        reasons = _status("javascript")["unresolved_reasons"]
        assert reasons[unresolved.NO_DEFINITION] == 1 and "not found" not in reasons  # missingFn

    def test_rust(self, tmp_path, monkeypatch):
        _project(tmp_path, monkeypatch, {"m.rs": RUST})
        found = _deps("m.rs::Engine::spin")["unresolved_callers"]
        assert (found["call_sites"], found["functions"]) == (1, 1) and found["sites"][0]["call"] == "vehicle.spin()" and found["sites"][0]["line"] == 6
        assert found["sites"][0]["reason"] == unresolved.RECEIVER_UNKNOWN
        assert "unresolved_callers" not in _deps("m.rs::main")
        reasons = _status("rust")["unresolved_reasons"]
        assert reasons[unresolved.NO_DEFINITION] == 1 and "not found" not in reasons  # missing_fn


class TestTheHelpers:
    def test_the_name_a_call_ends_in(self):
        assert [unresolved.called_name(n) for n in ("a", "Base::baseWork", "obj.method", "p->run", "std::vector::size", "")] == ["a", "baseWork", "method", "run", "size", ""]

    def test_the_reason_by_the_number_of_places(self):
        counts = {"one": 1, "two": 2}
        assert unresolved.refine_not_found("none", counts) == unresolved.NO_DEFINITION
        assert unresolved.refine_not_found("Klass::one", counts) == unresolved.RECEIVER_UNKNOWN
        assert unresolved.refine_not_found("x.two", counts) == unresolved.SEVERAL_DEFINITIONS

    def test_a_declaration_and_its_definition_are_one_place_and_an_override_is_another(self, tmp_path, monkeypatch):
        _project(tmp_path, monkeypatch, {"src/more.h": CPP_HEADER, "src/more.cpp": CPP_SOURCE})
        conn = duckdb.connect(str(tmp_path / "_drspec" / "contracts.db"), read_only=True)
        try:
            counts = unresolved.definition_counts(conn)
        finally:
            conn.close()
        assert counts["baseWork"] == 1 and counts["isReady"] == 2 and "mystery" not in counts

    def test_a_site_listed_is_capped_but_the_counts_are_whole(self, tmp_path, monkeypatch):
        calls = "\n".join(f"    vehicle.spin()  # {n}" for n in range(60))
        source = f"class Engine:\n    def spin(self):\n        return 1\n\n\ndef drive(vehicle):\n{calls}\n"
        _project(tmp_path, monkeypatch, {"m.py": source})
        found = _deps("m.py::Engine.spin")["unresolved_callers"]
        assert found["call_sites"] == 60 and len(found["sites"]) == unresolved.MAX_SITES and found["sites_omitted"] == 10
        assert [site["line"] for site in found["sites"]][:3] == [7, 8, 9]
