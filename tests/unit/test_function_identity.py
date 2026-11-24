"""One identity rule for every language: a readable name plus a hash of the parameters."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.core.call_resolver import CallSite, ResolutionInput, resolve_calls
from tests.helpers import publish as sync_scan_results
from drspec.core.identity import display_name, function_identity, parameter_hash, split_function_id
from drspec.core.scanner import Scanner
from drspec.db import AmbiguousFunctionId, init_schema, resolve_function_id
from drspec.parsers import CppParser, JavaScriptParser
from tests.helpers import fid

CPP = """
int add(int a, int b) { return a + b; }
double add(double a, double b) { return a + b; }
float add(float a, float b, float c) { return a; }
class Calc { public: int mul(int a) { return a; } int mul(int a, int b) { return a * b; } };
struct Calc2 { int div(int a, int b) { return a / b; } double div(double a, double b) { return a / b; } };
int use() { return add(1, 2); }
"""


def _scan(root: Path, name: str, source: str):
    (root / name).write_text(source)
    result = Scanner().scan_file(str(root / name), relative_to=root)
    assert result and result.parse_status == "successful"
    return result


def _ids(result):
    return [f.function_id for f in result.functions]


class TestTheRule:
    def test_the_identifier_is_file_name_and_eight_hex_characters(self):
        identifier = function_identity("src/db/queries.py", "insert_artifact", ["str", "int"])
        assert identifier.startswith("src/db/queries.py::insert_artifact#")
        file_path, name, digest = split_function_id(identifier)
        assert (file_path, name) == ("src/db/queries.py", "insert_artifact") and len(digest) == 8

    def test_a_function_without_parameters_still_has_a_suffix(self):
        assert function_identity("a.py", "f", []).endswith("#" + parameter_hash([]))

    def test_the_hash_is_stable_across_runs(self):
        assert parameter_hash(["int", "str"]) == "45ad1e17"  # pinned: the same on every machine and run
        assert parameter_hash(["int"]) != parameter_hash(["str"]) != parameter_hash([])
        assert parameter_hash(["a", "b"]) != parameter_hash(["b", "a"])

    def test_display_name_is_the_last_segment_without_the_hash(self):
        assert display_name("calc.cpp::ns::Calc::mul#12345678") == "mul"
        assert display_name("a.py::K.run#12345678") == "run"
        assert display_name("a.py::K.run#12345678", "run") == "run"
        assert display_name("a.py::plain") == "plain"


class TestSameNameDefinitionsAreKept:
    def test_every_cpp_overload_is_indexed_with_its_own_identifier_and_body(self, tmp_path):
        result = _scan(tmp_path, "calc.cpp", CPP)
        by_name = {}
        for f in result.functions:
            by_name.setdefault(f.qualified_name, []).append(f)
        assert {name: len(items) for name, items in by_name.items()} == {"add": 3, "Calc::mul": 2, "Calc2::div": 2, "use": 1}
        assert len(set(_ids(result))) == 8
        assert len({f.body for f in result.functions}) == 8

    def test_a_property_and_its_setter_are_two_functions(self, tmp_path):
        result = _scan(tmp_path, "m.py", "class C:\n    @property\n    def v(self):\n        return 1\n\n    @v.setter\n    def v(self, value):\n        pass\n")
        assert [(f.qualified_name, f.parameter_tokens, f.is_property) for f in result.functions] == [("C.v", [], True), ("C.v", ["value"], False)]
        assert len(set(_ids(result))) == 2

    def test_python_keeps_both_of_two_identical_definitions_and_the_last_has_the_usual_identifier(self, tmp_path):
        result = _scan(tmp_path, "m.py", "def f(a):\n    return 1\n\n\ndef f(a):\n    return 2\n")
        assert [f.body for f in result.functions] == ["def f(a):\n    return 1", "def f(a):\n    return 2"]
        single = _scan(tmp_path, "s.py", "def f(a):\n    return 2\n")
        assert result.functions[1].function_id.split("::", 1)[1] == single.functions[0].function_id.split("::", 1)[1]
        assert result.functions[0].function_id != result.functions[1].function_id

    def test_javascript_functions_with_other_parameters_are_kept(self):
        result = JavaScriptParser().parse("function f(a) { return a; }\nfunction f(a, b) { return b; }\n")
        assert [f.parameter_tokens for f in result.functions] == [["a"], ["a", "b"]]

    def test_a_cpp_declaration_is_dropped_only_when_the_same_function_is_defined_in_the_file(self):
        result = CppParser().parse(
            "int twice(int a);\nint twice(double a);\nint twice(int a) { return a * 2; }\n", file_path="t.h"
        )
        shapes = sorted((f.parameter_tokens, "declaration" in f.decorators) for f in result.functions)
        assert shapes == [(["double"], True), (["int"], False)]


class TestWhatChangesTheIdentifier:
    def _id(self, tmp_path, source, name="f"):
        result = _scan(tmp_path, "m.py", source)
        return next(f.function_id for f in result.functions if f.qualified_name == name)

    def test_defaults_return_types_and_decorators_do_not_change_it(self, tmp_path):
        base = self._id(tmp_path, "def f(a: int, b: str):\n    return 1\n")
        assert self._id(tmp_path, "def f(a: int, b: str = 'x'):\n    return 1\n") == base
        assert self._id(tmp_path, "def f(a: int, b: str) -> bool:\n    return 1\n") == base
        assert self._id(tmp_path, "@decorator\ndef f(a: int, b: str):\n    return 1\n") == base
        assert self._id(tmp_path, "def f(a:int,b:  str):\n    return 1\n") == base

    def test_a_parameter_type_a_new_parameter_or_an_untyped_rename_does(self, tmp_path):
        base = self._id(tmp_path, "def f(a: int, b):\n    return 1\n")
        assert self._id(tmp_path, "def f(a: float, b):\n    return 1\n") != base
        assert self._id(tmp_path, "def f(a: int, b, c):\n    return 1\n") != base
        assert self._id(tmp_path, "def f(a: int, renamed):\n    return 1\n") != base
        assert self._id(tmp_path, "def f(other: int, b):\n    return 1\n") == base  # a typed parameter is known by its type

    def test_self_and_cls_are_not_parameters(self, tmp_path):
        with_self = self._id(tmp_path, "class K:\n    def f(self, a):\n        return a\n", "K.f")
        as_function = self._id(tmp_path, "class K:\n    @staticmethod\n    def f(a):\n        return a\n", "K.f")
        assert with_self.split("#")[1] == as_function.split("#")[1]

    def test_the_same_source_gives_the_same_identifiers(self, tmp_path):
        first = _ids(_scan(tmp_path, "calc.cpp", CPP))
        second = _ids(Scanner().scan_file(str(tmp_path / "calc.cpp"), relative_to=tmp_path))
        assert first == second


class TestCallsReachEveryOverload:
    def _fact(self, **kwargs):
        return ResolutionInput(caller_id="a.cpp::use#00000000", call_sites=[CallSite(receiver=None, name="add", text="add(1, 2)")], **kwargs)

    def test_a_call_to_overloaded_free_functions_has_an_ambiguous_edge_to_each(self):
        ids = ["a.cpp::add#11111111", "a.cpp::add#22222222", "a.cpp::add#33333333"]
        resolved = resolve_calls(self._fact(same_file_functions={"add": ids}))
        assert sorted((r.callee_id, r.confidence) for r in resolved) == sorted((i, "AMBIGUOUS") for i in ids)

    def test_a_call_to_a_single_definition_is_still_extracted(self):
        resolved = resolve_calls(self._fact(same_file_functions={"add": ["a.cpp::add#11111111"]}))
        assert [(r.callee_id, r.confidence) for r in resolved] == [("a.cpp::add#11111111", "EXTRACTED")]

    def test_same_named_functions_of_different_qualified_names_stay_unresolved(self):
        resolved = resolve_calls(self._fact(same_file_functions={"add": ["a.cpp::ns::add#11111111", "a.cpp::other::add#22222222"]}))
        assert resolved == []

    def test_overloaded_methods_reached_through_self_are_ambiguous(self):
        methods = ["a.cpp::Calc::mul#11111111", "a.cpp::Calc::mul#22222222"]
        fact = ResolutionInput(
            caller_id="a.cpp::Calc::go#00000000", call_sites=[CallSite(receiver="this", name="mul", text="this->mul(2)")],
            caller_parent="a.cpp::Calc", self_aliases=["this"], classes_by_name={"a.cpp::Calc": {"mul": methods}},
        )
        assert sorted((r.callee_id, r.confidence) for r in resolve_calls(fact)) == sorted((m, "AMBIGUOUS") for m in methods)

    def test_overloads_imported_by_name_or_module_are_ambiguous(self):
        both = ["u.py::f#11111111", "u.py::f#22222222"]
        by_name = ResolutionInput(caller_id="c.py::g#0", call_sites=[CallSite(None, "f", "f(1)")], imported_names={"f": both})
        by_module = ResolutionInput(caller_id="c.py::g#0", call_sites=[CallSite("u", "f", "u.f(1)")], imported_modules={"u": {"f": both}})
        for fact in (by_name, by_module):
            assert sorted((r.callee_id, r.confidence) for r in resolve_calls(fact)) == sorted((i, "AMBIGUOUS") for i in both)


class TestEndToEndWithAScan:
    @pytest.fixture
    def project(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "calc.cpp").write_text(CPP)
        connection = duckdb.connect(":memory:")
        init_schema(connection)
        result = Scanner().scan_file(str(tmp_path / "calc.cpp"), relative_to=tmp_path)
        sync_scan_results(connection, functions=result.functions, scanned_file_paths={"calc.cpp"}, file_imports=result.file_imports)
        yield tmp_path, connection
        connection.close()

    def test_a_call_gets_an_ambiguous_edge_to_each_overload_and_no_others(self, project):
        _root, connection = project
        rows = connection.execute("SELECT callee_id, confidence FROM dependencies WHERE caller_id LIKE 'calc.cpp::use#%'").fetchall()
        assert len(rows) == 3 and {confidence for _, confidence in rows} == {"AMBIGUOUS"}
        assert {callee.split("#")[0] for callee, _ in rows} == {"calc.cpp::add"}

    def test_editing_one_overload_marks_only_it_changed(self, project):
        root, connection = project
        (root / "calc.cpp").write_text(CPP.replace("return a + b; }\ndouble", "return a + b + 1; }\ndouble", 1))
        result = Scanner().scan_file(str(root / "calc.cpp"), relative_to=root)
        stats = sync_scan_results(connection, functions=result.functions, scanned_file_paths={"calc.cpp"}, file_imports=result.file_imports)
        assert (stats.functions_changed, stats.functions_new, stats.functions_removed) == (1, 0, 0)

    def test_a_plain_scan_keeps_the_ambiguous_overload_edges_up_to_date(self, project):
        root, connection = project
        (root / "calc.cpp").write_text(CPP.replace("int use() { return add(1, 2); }", "int use() { return 7; }"))
        result = Scanner().scan_file(str(root / "calc.cpp"), relative_to=root)
        sync_scan_results(connection, functions=result.functions, scanned_file_paths={"calc.cpp"}, file_imports=result.file_imports)
        assert connection.execute("SELECT COUNT(*) FROM dependencies").fetchone() == (0,)


class TestLookups:
    @pytest.fixture
    def stored(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "calc.cpp").write_text(CPP)
        connection = duckdb.connect(":memory:")
        init_schema(connection)
        result = Scanner().scan_file(str(tmp_path / "calc.cpp"), relative_to=tmp_path)
        sync_scan_results(connection, functions=result.functions, scanned_file_paths={"calc.cpp"}, file_imports=result.file_imports)
        yield connection
        connection.close()

    def test_a_name_with_one_definition_resolves(self, stored):
        assert resolve_function_id(stored, "calc.cpp::use") == fid("calc.cpp", "use")

    def test_a_name_with_several_lists_the_candidates(self, stored):
        with pytest.raises(AmbiguousFunctionId) as raised:
            resolve_function_id(stored, "calc.cpp::add")
        assert len(raised.value.candidates) == 3
        assert all(signature for _, signature in raised.value.candidates)

    def test_the_full_identifier_is_exact(self, stored):
        identifier = fid("calc.cpp", "add", "int", "int")
        assert resolve_function_id(stored, identifier) == identifier

    def test_an_unknown_name_is_returned_for_the_caller_to_report(self, stored):
        assert resolve_function_id(stored, "calc.cpp::nope") == "calc.cpp::nope"


class TestTheCommandLine:
    def test_a_short_name_with_overloads_fails_with_the_candidates_and_a_full_identifier_works(self, kept_project):
        runner = CliRunner()
        (kept_project / "calc.cpp").write_text(CPP)
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0

        ambiguous = runner.invoke(app, ["--json", "source", "get", "calc.cpp::add"])
        assert ambiguous.exit_code == 1
        error = json.loads(ambiguous.output)["error"]
        assert len(error["details"]["candidates"]) == 3 and "full identifier" in error["message"]

        exact = runner.invoke(app, ["--json", "source", "get", fid("calc.cpp", "add", "double", "double")])
        assert exact.exit_code == 0 and "double add" in json.loads(exact.output)["data"]["body"]

        unique = runner.invoke(app, ["--json", "source", "get", "calc.cpp::use"])
        assert unique.exit_code == 0

    def test_deps_get_shows_the_method_name_not_the_hash(self, kept_project):
        runner = CliRunner()
        (kept_project / "calc.cpp").write_text(CPP)
        runner.invoke(app, ["--json", "scan", "."])
        result = runner.invoke(app, ["--json", "deps", "get", fid("calc.cpp", "use"), "--direction", "callees"])
        assert result.exit_code == 0
        names = {c["function_name"] for c in json.loads(result.output)["data"]["callees"]}
        assert names == {"add"}
