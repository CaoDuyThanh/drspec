"""C++ receivers typed from what is declared: the base classes, the fields, the return types, `auto`, the elements of
containers and arrays, the parameters of lambdas and the arguments of templates. Nothing that resolved before changes,
and nothing is guessed."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.db import init_schema
from drspec.parsers.cpp_parser import CppParser
from drspec.parsers.cpp_types import normalize_type, parse_type, split_chain

runner = CliRunner()

pytestmark = pytest.mark.usefixtures("kept_duckdb", "cached_cli_command")  # the project database is in memory, kept between the commands

# The reviewer's reproduction project; each case is a comment in it.
AGENT_H = '#pragma once\nclass Context {\n public:\n  bool avoid = false;\n  int speed();\n  void stop();\n};\nclass Agent {\n public:\n  void update(Context* context);\n  bool isDoAvoidance(Context* context);\n  void helper();\n  int inlineThing() { return helperInline(); }\n  int helperInline() { return 1; }\n  static int make(int v);\n private:\n  int x = 0;\n  Context ctx;\n};\nclass Pilot : public Agent {\n public:\n  void fly(Context& c);\n  void baseCall();\n};\n'
AGENT_CPP = '#include "agent.h"\nint Context::speed() { return 3; }\nvoid Context::stop() {}\nbool Agent::isDoAvoidance(Context* context) { return context->avoid; }\nvoid Agent::helper() {}\nint Agent::make(int v) { return v; }\nvoid Agent::update(Context* context) {\n  if (isDoAvoidance(context)) {      // implicit this\n    helper();                         // implicit this\n  }\n  this->helper();                     // this->\n  (*this).helper();                   // (*this).\n  context->speed();                   // pointer param of known type\n  ctx.stop();                         // member object of known type\n  Context local;\n  local.stop();                       // local of known type\n  Context* p = context;\n  p->stop();                          // local pointer\n  Agent::make(1);                     // qualified static\n  make(2);                            // unqualified static\n}\nvoid Pilot::fly(Context& c) { c.stop(); helper(); }   // reference param; base-class method\nvoid Pilot::baseCall() { Agent::helper(); this->isDoAvoidance(nullptr); }\nvoid freeFn(Agent* a, Agent& b) { a->helper(); b.helper(); a->update(nullptr); }\n'
MORE_H = '#pragma once\n#include <vector>\nnamespace game {\nclass Steering {\n public:\n  bool wantsAvoid() const;\n  void apply();\n};\nclass Body {\n public:\n  Steering* getSteering();\n  Steering& steeringRef();\n  void tick();\n};\nclass Base {\n public:\n  virtual bool isReady();\n  void baseWork();\n};\nclass Derived : public Base {\n public:\n  void run();\n  bool isReady() override;\n};\ntemplate <typename T> class Holder { public: T* get(); void each(); };\n}\n'
MORE_CPP = '#include "more.h"\nnamespace game {\nbool Steering::wantsAvoid() const { return true; }\nvoid Steering::apply() {}\nSteering* Body::getSteering() { return nullptr; }\nSteering& Body::steeringRef() { static Steering s; return s; }\nvoid Body::tick() {\n  getSteering()->apply();          // call result then ->\n  steeringRef().apply();           // reference result\n  auto* s = getSteering();\n  s->wantsAvoid();                 // auto\n}\nbool Base::isReady() { return true; }\nvoid Base::baseWork() {}\nbool Derived::isReady() { return false; }\nvoid Derived::run() {\n  baseWork();                      // inherited, implicit this\n  this->baseWork();                // inherited, this->\n  Base::isReady();                 // qualified base\n  isReady();                       // own override\n}\ntemplate <typename T> T* Holder<T>::get() { return nullptr; }\ntemplate <typename T> void Holder<T>::each() { get(); }\nvoid useAll(std::vector<Body>& bodies, Holder<Body>& h) {\n  for (auto& b : bodies) b.tick();         // range-for auto\n  bodies[0].tick();                          // subscript\n  h.get()->tick();                           // template result\n  for (std::size_t i = 0; i < bodies.size(); ++i) bodies[i].tick();\n  auto fn = [&](Body& x) { x.tick(); };      // lambda\n  fn(bodies[0]);\n}\n}\n'


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


def _edges(project: Path) -> dict:
    """`{(caller, callee): {confidence}}` with the file and the hash left out of the names."""
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


@pytest.fixture
def repro(tmp_path, monkeypatch):
    return _project(tmp_path, monkeypatch, {"src/agent.h": AGENT_H, "src/agent.cpp": AGENT_CPP, "src/more.h": MORE_H, "src/more.cpp": MORE_CPP})


class TestEveryShapeOfTheReproduction:
    @pytest.mark.parametrize(
        "caller, callee",
        [
            ("Pilot::fly", "Agent::helper"),  # (a) a method of the base class, called by its bare name
            ("Pilot::baseCall", "Agent::isDoAvoidance"),  # (a) `this->` and a method of the base class
            ("game::Derived::run", "game::Base::baseWork"),  # (a) both forms, in a namespace
            ("Agent::update", "Context::stop"),  # (b) `ctx.stop()`, a member object of declared type
            ("game::Body::tick", "game::Steering::apply"),  # (c) `getSteering()->apply()` and `steeringRef().apply()`
            ("game::Body::tick", "game::Steering::wantsAvoid"),  # (d) `auto* s = getSteering(); s->wantsAvoid()`
            ("game::useAll", "game::Body::tick"),  # (e) range-for and subscripts, (f) a lambda, (g) `h.get()->tick()`
        ],
    )
    def test_the_call_resolves_with_the_type_known(self, repro, caller, callee):
        assert _edges(repro).get((caller, callee)) == {"EXTRACTED"}

    def test_each_call_site_of_each_shape_is_in_the_edge(self, repro):
        conn = duckdb.connect(str(repro / "_drspec" / "contracts.db"), read_only=True)
        try:
            rows = conn.execute("SELECT caller_id, callee_id, call_site FROM dependencies").fetchall()
        finally:
            conn.close()
        sites = {(c.split("::", 1)[1].rsplit("#", 1)[0], d.split("::", 1)[1].rsplit("#", 1)[0]): site for c, d, site in rows}
        assert set(sites[("Agent::update", "Context::stop")].split("; ")) >= {"ctx.stop()", "local.stop()", "p->stop()"}
        assert set(sites[("game::Body::tick", "game::Steering::apply")].split("; ")) == {"getSteering()->apply()", "steeringRef().apply()"}
        assert set(sites[("game::useAll", "game::Body::tick")].split("; ")) >= {"b.tick()", "bodies[0].tick()", "h.get()->tick()", "x.tick()"}
        assert set(sites[("game::Derived::run", "game::Base::baseWork")].split("; ")) == {"baseWork()", "this->baseWork()"}

    def test_nothing_that_resolved_before_is_gone(self, repro):
        edges = _edges(repro)
        for pair in [
            ("Agent::update", "Agent::isDoAvoidance"), ("Agent::update", "Agent::helper"), ("Agent::update", "Context::speed"), ("Agent::update", "Agent::make"),
            ("Pilot::fly", "Context::stop"), ("Pilot::baseCall", "Agent::helper"), ("freeFn", "Agent::helper"), ("freeFn", "Agent::update"),
            ("game::Body::tick", "game::Body::getSteering"), ("game::Body::tick", "game::Body::steeringRef"), ("game::Derived::run", "game::Base::isReady"),
            ("game::Derived::run", "game::Derived::isReady"), ("game::Holder::each", "game::Holder::get"),
        ]:
            assert edges.get(pair) == {"EXTRACTED"}, pair

    def test_status_counts_fewer_calls_unresolved(self, repro):
        health = json.loads(runner.invoke(app, ["--json", "status"]).output)["data"]["cpp"]
        assert health["unresolved_calls"] <= 2 and health["calls"] >= 30  # only `bodies.size()` and the lambda `fn(...)` are left


class TestNothingIsGuessed:
    @pytest.fixture
    def project(self, tmp_path, monkeypatch):
        header = (
            "#pragma once\n#include <vector>\n"
            "class Base { public: virtual int ready(); int work(int a); int work(double a); void tick(); };\n"
            "class Derived : public Base { public: int ready() override; void own(); };\n"
            "class Body { public: void tick(); void go(); };\n"
            "class Other { public: void tick(); };\n"
        )
        source = (
            '#include "m.h"\n'
            "int Base::ready() { return 1; }\nint Derived::ready() { return 2; }\nint Base::work(int a) { return a; }\nint Base::work(double a) { return 1; }\n"
            "void Base::tick() {}\nvoid Derived::own() {}\nvoid Body::tick() {}\nvoid Body::go() {}\nvoid Other::tick() {}\n"
            "void virtualCall(Base& b) { b.ready(); }\n"
            "void overloaded(Derived& d) { d.work(1); }\n"
            "void unknownType() { auto z = mystery(); z.tick(); }\n"
            "void notAContainer(Body body) { body[0].go(); }\n"
            "void shadowed(Body x) { auto f = [&](Other& x) { x.tick(); }; x.go(); }\n"
            "void unknownReceiver(Mystery m) { m.go(); }\n"
        )
        return _project(tmp_path, monkeypatch, {"m.h": header, "m.cpp": source})

    def test_a_virtual_method_resolves_to_the_declared_one_not_the_overrides(self, project):
        edges = _edges(project)
        assert edges.get(("virtualCall", "Base::ready")) == {"EXTRACTED"} and ("virtualCall", "Derived::ready") not in edges

    def test_an_overload_set_the_arguments_cannot_tell_apart_is_never_one_guess(self, project):
        conn = duckdb.connect(str(project / "_drspec" / "contracts.db"), read_only=True)
        try:
            rows = conn.execute("SELECT callee_id, confidence FROM dependencies WHERE caller_id LIKE '%::overloaded#%'").fetchall()
        finally:
            conn.close()
        assert len(rows) == 2 and {confidence for _callee, confidence in rows} == {"AMBIGUOUS"}  # both `work`, found through the base

    def test_what_has_no_declared_type_stays_unresolved(self, project):
        edges = _edges(project)
        assert not [pair for pair in edges if pair[0] in ("unknownType", "notAContainer", "unknownReceiver")]

    def test_a_name_a_lambda_parameter_clashes_with_keeps_the_functions_type(self, project):
        edges = _edges(project)
        assert edges.get(("shadowed", "Body::go")) == {"EXTRACTED"}
        assert ("shadowed", "Other::tick") not in edges  # the clash is not guessed at


class TestShapesTheResolverStillCannotType:
    """What a declared type does not give is not guessed: each call below names `rare`, which only `Body` defines."""

    @pytest.fixture
    def project(self, tmp_path, monkeypatch):
        header = "#pragma once\nclass Body { public: void rare(); };\n"
        source = (
            '#include "b.h"\nvoid Body::rare() {}\n'
            "void onAMap(std::map<int, Body>& m) { m[1].rare(); }\n"
            "void onPointers(Body** arr) { arr[0]->rare(); }\n"
            "void onAPair(std::pair<int, Body>& pr) { pr.first.rare(); }\n"
            "void onAFactory() { auto q = unknownFactory(); q.rare(); }\n"
            "void onAnIterator(std::vector<Body>& v) { for (auto it = v.begin(); it != v.end(); ++it) it->rare(); }\n"
        )
        return _project(tmp_path, monkeypatch, {"b.h": header, "b.cpp": source})

    @pytest.mark.parametrize("caller", ["onAMap", "onPointers", "onAPair", "onAFactory", "onAnIterator"])
    def test_the_call_stays_unresolved(self, project, caller):
        assert not [pair for pair in _edges(project) if pair[0] == caller and pair[1] == "Body::rare"]

    def test_the_note_of_the_unresolved_callers_names_them(self, project):
        result = runner.invoke(app, ["--json", "deps", "get", "b.cpp::Body::rare", "--direction", "callers"])
        found = json.loads(result.output)["data"]["unresolved_callers"]
        assert found["call_sites"] == 5 and found["functions"] == 5


class TestTheParserSaysWhatItReads:
    def parse(self, source: str):
        return CppParser().parse(source, "a.cpp")

    def test_declared_types_keep_template_arguments_arrays_auto_and_loops(self):
        function = self.parse(
            "void f(std::vector<Body>& bodies, Holder<Body>& h, const Context* c) {\n"
            "  auto* s = getSteering(); auto copy = other; Body local[3]; Body one;\n"
            "  for (auto& b : bodies) b.tick();\n  for (Body& d : bodies) d.tick();\n"
            "  auto fn = [&](Body& x, int k) { x.tick(); };\n}\n"
        ).functions[0]
        assert function.declared_param_types == {
            "bodies": "std::vector<Body>", "h": "Holder<Body>", "c": "Context", "s": "=getSteering()", "copy": "=other",
            "local": "Body[]", "one": "Body", "b": "@bodies", "d": "Body", "x": "Body",
        }

    def test_receivers_are_short_chains(self):
        calls = {(call.receiver, call.name) for call in self.parse(
            "void f() { getSteering()->apply(); bodies[0].tick(); h.get()->tick(); (*this).helper(); this->ctx.stop(); a.b.c(); new X()->y(); }\n"
        ).functions[0].calls}
        assert {("getSteering()", "apply"), ("bodies[]", "tick"), ("h.get()", "tick"), ("this", "helper"), ("this->ctx", "stop"), ("a.b", "c")} <= calls
        assert not [call for call in calls if call[1] == "y"]  # a receiver that is not a chain is skipped, as before

    def test_a_lambda_param_that_clashes_with_a_function_one_does_not_replace_it(self):
        function = self.parse("void f(Body x) { auto g = [&](Other& x, Third y) { x.tick(); }; }\n").functions[0]
        assert function.declared_param_types == {"x": "Body", "y": "Third"}

    def test_classes_and_return_types_are_recorded(self):
        facts = self.parse(
            "namespace game {\nclass Derived : public Base, private Mixin<int> {\n public:\n  Context ctx;\n  std::vector<Body> items, more;\n  Body arr[8];\n"
            "  Steering* getSteering();\n  int plain();\n};\ntemplate <typename T, int N> class Holder : Base { public: T* get(); T value; };\n}\n"
            "namespace game { Steering* Body::getSteering() { return nullptr; } template <typename T> T* Holder<T>::get() { return nullptr; } }\n"
        ).facts
        classes = {fact["name"]: fact for fact in facts["classes"]}
        assert classes["game::Derived"]["bases"] == ["Base", "Mixin<int>"]
        assert classes["game::Derived"]["fields"] == {"ctx": "Context", "items": "std::vector<Body>", "more": "std::vector<Body>", "arr": "Body[]"}
        assert classes["game::Holder"]["template"] == ["T", "N"] and classes["game::Holder"]["fields"] == {"value": "T"}
        assert facts["returns"] == {"game::Derived::getSteering": "Steering", "game::Body::getSteering": "Steering", "game::Holder::get": "T"}  # `int` is no class

    def test_overloads_with_different_return_types_say_nothing(self):
        facts = self.parse("class A { public: Foo* make(int); Bar* make(double); Foo* once(); };\n").facts
        assert facts["returns"] == {"A::once": "Foo"}

    def test_the_type_helpers(self):
        assert normalize_type("const std::vector<game::Body>&") == "std::vector<game::Body>"
        assert normalize_type("Body", array=True) == "Body[]" and normalize_type("unsigned int") is None and normalize_type("auto") is None
        assert normalize_type("std::vector<int>::iterator") is None and normalize_type("std::array<Body, 4>") == "std::array<Body,4>"
        assert parse_type("std::map<int,std::vector<Body>>") == ("std::map", ["int", "std::vector<Body>"], False) and parse_type("Body[]") == ("Body", [], True)
        assert split_chain("h.get()") == [("h", []), ("get", ["()"])] and split_chain("bodies[]") == [("bodies", ["[]"])] and split_chain("a..b") is None


class TestTheIndexAfterAnEdit:
    def test_a_change_of_the_base_class_in_a_header_changes_the_edge_of_a_file_nobody_touched(self, tmp_path, monkeypatch):
        files = {
            "d.h": "#pragma once\nclass Base { public: void baseWork(); };\nclass Derived : public Base { public: void run(); };\n",
            "d.cpp": '#include "d.h"\nvoid Base::baseWork() {}\nvoid Derived::run() { baseWork(); }\n',
        }
        project = _project(tmp_path, monkeypatch, files)
        assert _edges(project).get(("Derived::run", "Base::baseWork")) == {"EXTRACTED"}
        (project / "d.h").write_text("#pragma once\nclass Base { public: void baseWork(); };\nclass Derived { public: void run(); };\n")  # no longer derives
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        assert ("Derived::run", "Base::baseWork") not in _edges(project)  # d.cpp was not edited: the header's change reached it
        (project / "d.h").write_text(files["d.h"])
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        assert _edges(project).get(("Derived::run", "Base::baseWork")) == {"EXTRACTED"}

    def test_a_change_of_a_return_type_reaches_the_callers(self, tmp_path, monkeypatch):
        files = {
            "r.h": "#pragma once\nclass A { public: void go(); };\nclass B { public: void go(); };\nclass Maker { public: A* make(); };\n",
            "r.cpp": '#include "r.h"\nvoid A::go() {}\nvoid B::go() {}\nA* Maker::make() { return nullptr; }\nvoid use(Maker& m) { m.make()->go(); }\n',
        }
        project = _project(tmp_path, monkeypatch, files)
        assert ("use", "A::go") in _edges(project) and ("use", "B::go") not in _edges(project)
        (project / "r.h").write_text(files["r.h"].replace("A* make();", "B* make();"))
        (project / "r.cpp").write_text(files["r.cpp"].replace("A* Maker::make", "B* Maker::make"))
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        assert ("use", "B::go") in _edges(project) and ("use", "A::go") not in _edges(project)

    def test_the_files_read_before_the_facts_existed_are_queued_once(self, tmp_path, monkeypatch):
        from drspec.core.facts_version import FACTS_VERSION, requeue_for_new_facts
        from drspec.db.queries import list_staleness_queue

        project = _project(tmp_path, monkeypatch, {"a.cpp": "void f() {}\n", "a.py": "def g():\n    return 1\n"})
        assert FACTS_VERSION["cpp"]
        conn = duckdb.connect(str(project / "_drspec" / "contracts.db"))
        try:
            conn.execute("DELETE FROM config WHERE key = 'scan:facts:cpp'")
            assert requeue_for_new_facts(conn) >= 1 and "a.cpp" in list_staleness_queue(conn) and "a.py" not in list_staleness_queue(conn)
            conn.execute("DELETE FROM staleness_queue")
            assert requeue_for_new_facts(conn) == 0
        finally:
            conn.close()

    def test_an_index_made_by_the_parser_of_version_8_is_read_again_and_one_of_version_9_is_not(self):
        """Raised to 9 when the type aliases of a file (`using X = Base<Arg>;`, `typedef Base<Arg> X;`) are recorded, so a base or a variable
        that is an alias resolves. Raised to 8 when a struct named by a scope or template arguments (`template<> struct Node<X>::Impl`) is recorded with its
        members, and the files that lost functions to a destructor named `A<X>::~A<X>` or an argument alone on its line are read whole: the C++
        files of an index made before are queued once at the worker's start, and not again. In memory, no scan."""
        from drspec.core import facts_version
        from drspec.db import init_schema
        from drspec.db.queries import list_staleness_queue

        conn = duckdb.connect(":memory:")
        try:
            init_schema(conn)
            conn.execute("INSERT INTO scanned_files (file_path, mtime, size) VALUES ('a.cpp', 1.0, 10), ('b.h', 1.0, 10), ('c.py', 1.0, 10)")
            assert facts_version.FACTS_VERSION["cpp"] == "9"
            conn.execute("INSERT INTO config (key, value) VALUES ('scan:facts:cpp', '8')")  # what a worker of the earlier parser left
            assert facts_version.requeue_for_new_facts(conn) >= 2
            assert {"a.cpp", "b.h"} <= set(list_staleness_queue(conn)) and "c.py" not in list_staleness_queue(conn)
            assert conn.execute("SELECT value FROM config WHERE key = 'scan:facts:cpp'").fetchone()[0] == "9"
            conn.execute("DELETE FROM staleness_queue")
            assert facts_version.requeue_for_new_facts(conn) == 0 and list_staleness_queue(conn) == []  # version 9 is not read again
        finally:
            conn.close()

    def test_mentions_cover_the_names_inside_declared_types_and_receivers(self, tmp_path, monkeypatch):
        from drspec.core.graph_sync import CPP_TYPES_CHANGED, _may_be_affected

        project = _project(tmp_path, monkeypatch, {"a.cpp": "void f(std::vector<Body>& bodies) { auto* s = getSteering(); s->go(); bodies[0].tick(); }\n"})
        conn = duckdb.connect(str(project / "_drspec" / "contracts.db"), read_only=True)
        try:
            stored = conn.execute("SELECT value FROM config WHERE key = 'scan:mentions:a.cpp'").fetchone()[0]
        finally:
            conn.close()
        mentioned = json.loads(stored)
        assert {"getSteering", "Body", "bodies", "vector"} <= set(mentioned["n"]) and mentioned["c"] is True
        assert _may_be_affected(stored, {"getSteering"}, set()) and not _may_be_affected(stored, {"unrelated"}, set())
        assert _may_be_affected(stored, {CPP_TYPES_CHANGED}, set())


class TestADefinitionInSeveralPlacesGetsNothingInEitherMode:
    """The by-name rule is on by default: a name that two classes define is no edge with it on, and none with it off."""

    @pytest.mark.parametrize("infer", [True, False])
    def test_a_receiver_that_cannot_be_typed_and_a_name_two_classes_define_gets_no_edge(self, tmp_path, monkeypatch, infer):
        header = "#pragma once\nclass Body { public: void tick(); };\nclass Other { public: void tick(); };\n"
        source = '#include "m.h"\nvoid Body::tick() {}\nvoid Other::tick() {}\nvoid unknownType() { auto z = mystery(); z.tick(); }\n'
        project = _project(tmp_path, monkeypatch, {"m.h": header, "m.cpp": source}, infer=infer)
        assert not [pair for pair in _edges(project) if pair[0] == "unknownType"]
