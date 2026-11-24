"""C++ that the grammar reads wrongly or not at all: a member comparison, a conversion operator, a macro beside a
declaration, a compiler probe. Every function is found, at the position it has in the text, and a loss that leaves no
error is still reported."""

from __future__ import annotations

import json
import shutil

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.core.language_health import classify_cpp_call, language_health
from drspec.core.partial import (
    partial_summary,
    record_partial_files,
    totals,
    worst_files,
)
from drspec.core.scan_service import new_run, partial_report, summarize
from drspec.core.scanner import Scanner
from drspec.db import init_schema
from drspec.parsers.cpp_parser import CppParser
from tests.helpers import folder_listing, table_counts

runner = CliRunner()


def _parse(text: str):
    return CppParser().parse(text)


def _names(text: str) -> list:
    return sorted(f.name for f in _parse(text).functions)


COMPARISONS = [
    "x.m < B", "x->m < B", "x.m < 3", "x.m < y.n", "a.b.c < 1", "x.m < B ? 1 : 2", "(x.m) < B", "x.m <= B", "x.m > B",
]


class TestAMemberComparison:
    @pytest.mark.parametrize("expression", COMPARISONS)
    def test_a_function_with_the_comparison_is_found_with_its_body(self, expression):
        source = f"struct S {{ int m; int n; struct {{ struct {{ int c; }} b; }} a; }};\nbool before(S x) {{ return true; }}\nbool f(S x, S y, int B) {{ return {expression}; }}\nbool after(S x) {{ return true; }}\n"
        result = _parse(source)
        assert sorted(f.name for f in result.functions) == ["after", "before", "f"]
        function = next(f for f in result.functions if f.name == "f")
        assert expression in function.body and function.start_line == 3 and function.end_line == 3  # the stored text is the code

    def test_the_if_form_and_the_reporters_two_examples(self):
        source = (
            "float headingResolution_deg; int expansionOptions_historyIdx;\n"
            "void g(S s) { s.headingResolution_deg < EPSILON ? run() : stop(); }\n"
            "void h(S s) { if (s.expansionOptions.historyIdx < START_EXPANSION_IDX) { go(); } }\n"
            "void i(S x, S y) { if (x.m < y.n) { go(); } }\nvoid last() {}\n"
        )
        assert _names(source) == ["g", "h", "i", "last"]

    def test_two_comparisons_in_one_expression_do_not_swallow_the_rest_of_the_file(self):
        source = "".join(f"void before{i}() {{}}\n" for i in range(2)) + (
            "bool inside(P pos, C* c) { return (pos.x < c->x) || (pos.y < c->y); }\n"
        ) + "".join(f"void after{i}() {{}}\n" for i in range(4))
        result = _parse(source)
        assert len(result.functions) == 7 and not result.suspected_losses

    def test_real_templates_are_left_alone(self):
        source = (
            "void a(S x) { auto v = x.get<int>(); auto w = x.template f<T>(); std::vector<int> q; }\n"
            "template <typename T> T tmpl(T v) { return v; }\n#include <vector>\nbool cmp(S x) { return x.m < 3; }\nvoid z() {}\n"
        )
        assert _names(source) == ["a", "cmp", "tmpl", "z"]

    def test_the_text_handed_back_is_the_original(self):
        source = "bool f(S x, int B) { return x.m < B ? true : false; }\n"
        function = _parse(source).functions[0]
        assert function.body == source.strip() and "-" not in function.body.replace("true", "")

    def test_a_correct_file_is_never_rewritten(self):
        parser = CppParser()
        calls = []
        original = parser._rewrite_for_parsing
        parser._rewrite_for_parsing = lambda text: calls.append(1) or original(text)
        parser.parse("bool f(S x) { return x.get<int>() < 3; }\n")
        assert calls == []

    def test_positions_are_those_of_the_original_text(self):
        source = "void a() {}\n\nbool f(S x, S y) {\n    return (x.m < y.m) &&\n           (x.n < y.n);\n}\n\nvoid b() {}\n"
        by_name = {f.name: (f.start_line, f.end_line) for f in _parse(source).functions}
        assert by_name == {"a": (1, 1), "f": (3, 6), "b": (8, 8)}

    def test_a_real_syntax_error_is_still_reported(self):
        result = _parse("void ok() {}\nint broken( { return ;\nvoid fine() {}\n")
        assert result.has_errors and result.errors[0].line == 2 and "ok" in [f.name for f in result.functions]


class TestOtherConstructsTheGrammarReadsWrongly:
    def test_a_compiler_probe_in_a_condition(self):
        source = '#if __has_warning("-Wunknown")\n#pragma clang diagnostic ignored "-Wunknown"\n#endif\nvoid f() {}\n'
        result = _parse(source)
        assert _names(source) == ["f"] and not result.has_errors and result.skipped_constructs == 1


    @pytest.mark.parametrize(
        "source, expected",
        [
            ("void f(va_list a) { const char* s = va_arg(a, const char*); }\nvoid g() {}\n", ["f", "g"]),
            ('void f(long long v) { printf("%" PRId64, v); snprintf(b, 9, "%" IM_PRId64 " x", v); }\nvoid g() {}\n', ["f", "g"]),
            ("void f() {\n#if CLIP\n  while (c.Step()) {\n#else\n  {\n#endif\n    work();\n  }\n}\nvoid g() {}\n", ["f", "g"]),
            ("static int IMGUI_CDECL Cmp(const void* a, const void* b) { return 0; }\nvoid after() {}\n", ["Cmp", "after"]),
            ("struct W {\n  int x;\n  IM_NODEBUGSTEP constexpr inline W() : x(0) { }\n  IM_MSVC_RUNTIME_CHECKS_OFF\n  static inline int g() { return 1; }\n  void Z() {}\n};\n", ["W", "Z", "g"]),
        ],
        ids=["va_arg_with_a_type", "macro_between_string_literals", "braces_split_by_an_if", "calling_convention_macro", "macro_before_a_constructor"],
    )
    def test_each_construct_is_read_as_its_functions(self, source, expected):
        assert _names(source) == expected


BRACED_DEFAULT = """#include <vector>
struct Pose { double x; double y; };

class Planner {
 public:
  // a defaulted parameter written as a braced list: `= {}` (artos: 16 files, one function each)
  void addPath(const std::vector<int>& cuts = {});
  explicit Planner(Pose start = {0.0, 1.0}, int n = 1) : n_(n) {}
  int limit(const std::vector<int>& skip = {1,
                                            2}) const {
    return n_;
  }
  int other() { return 2; }

 private:
  int n_;
};

inline double area(const Pose& p = {}) { return p.x * p.y; }
int after() { return 3; }
"""


class TestABracedDefaultArgument:
    def test_the_functions_with_a_braced_default_are_found_whole(self):
        result = _parse(BRACED_DEFAULT)
        assert sorted(f.qualified_name for f in result.functions) == ["Planner::Planner", "Planner::addPath", "Planner::limit", "Planner::other", "after", "area"]
        assert [f.name for f in result.functions if f.has_error] == [] and result.suspected_losses == [] and not result.has_errors

    def test_the_signature_is_kept_as_the_user_wrote_it(self):
        signatures = {f.name: f.signature for f in _parse(BRACED_DEFAULT).functions}
        assert "= {}" in signatures["area"] and "= {0.0, 1.0}" in signatures["Planner"]
        assert signatures["limit"].endswith("skip = {1,")  # a list across two lines: the signature is its first line, as written

    def test_a_function_keeps_its_line(self):
        lines = {f.name: f.start_line for f in _parse(BRACED_DEFAULT).functions}
        assert lines["addPath"] == 7 and lines["other"] == 13 and lines["after"] == 20  # blanked text has the size of the text it replaced

    def test_a_braced_list_that_is_not_a_default_argument_is_left_alone(self):
        source = "std::vector<int> v = {1, 2};\nint f(int a) { int x[] = {1, 2}; auto g = [](int z = 0) { return z; }; return x[0] == 1; }\nint h() { return 1; }\n"
        assert _names(source) == ["f", "h"] and not _parse(source).has_errors

    def test_a_braced_default_in_a_comment_or_a_string_is_not_touched(self):
        source = 'int f(int a = 1) { const char* s = "p = {1}, q"; /* r = {2}) */ return a; }\nint g() { return 1; }\n'
        assert _names(source) == ["f", "g"] and not _parse(source).has_errors

    def test_through_the_scanner_nothing_is_skipped_or_partial(self, tmp_path):
        path = tmp_path / "planner.hpp"
        path.write_text(BRACED_DEFAULT)
        result = Scanner().scan_file(path, tmp_path)
        assert result.skipped_functions == [] and result.partial_functions == [] and len(result.functions) == 6


class TestWhatTheArtosFilesLost:
    """Three constructs of two real files (a destructor named with its template arguments, an argument alone on its line, a
    struct that an explicit specialization defines): each lost the functions around it, now none."""

    def test_a_destructor_named_with_its_template_arguments(self):
        source = "template<>\nNodeImpl<X>::~NodeImpl<X>() = default;\nint after() { return 1; }\n"
        result = _parse(source)
        assert [f.qualified_name for f in result.functions] == ["NodeImpl::~NodeImpl", "after"]
        assert not result.has_errors and result.suspected_losses == []

    def test_a_destructor_with_a_body_and_nested_arguments(self):
        source = "template<class T>\nNodeImpl<std::vector<T>>::~NodeImpl<std::vector<T>>() { cleanup(); }\nint after() { return 1; }\n"
        result = _parse(source)
        assert [f.qualified_name for f in result.functions] == ["NodeImpl::~NodeImpl", "after"] and not result.has_errors

    def test_a_destructor_written_without_arguments_is_as_before(self):
        source = "template<class T>\nNodeImpl<T>::~NodeImpl() = default;\nint after() { return 1; }\n"
        assert [f.qualified_name for f in _parse(source).functions] == ["NodeImpl::~NodeImpl", "after"]

    def test_a_destructor_shown_in_a_comment_or_a_string_is_not_touched(self):
        source = 'int f() { const char* s = "A<X>::~A<X>("; /* B<Y>::~B<Y>() */ return 1; }\nint g() { return 2; }\n'
        assert _names(source) == ["f", "g"] and not _parse(source).has_errors

    def test_an_argument_alone_on_its_line_is_not_a_macro(self):
        source = "void f() {\n    param(\n        \"name\",\n        value,\n        DEFAULT_SPEED\n    );\n}\nint after() { return 1; }\n"
        result = _parse(source)
        assert _names(source) == ["after", "f"] and not result.has_errors and result.suspected_losses == []

    def test_an_element_alone_on_its_line_after_a_comma_is_not_a_macro(self):
        source = "int f() {\n    int x = g(1,\n        LIMIT_MAX);\n    return x;\n}\nint after() { return 1; }\n"
        assert _names(source) == ["after", "f"] and not _parse(source).has_errors

    def test_a_macro_alone_on_its_line_between_declarations_is_still_blanked(self):
        source = "IM_RUNTIME_CHECKS_OFF\nint f() { return 1; }\nIM_RUNTIME_CHECKS_ON\nint g() { return 2; }\n"
        result = _parse(source)
        assert _names(source) == ["f", "g"] and not result.has_errors and result.suspected_losses == []

    @pytest.mark.parametrize(
        "head, owner",
        [
            ("template<>\nstruct Node<X>::Impl", "Node::Impl"),
            ("template<class T>\nstruct Node<T>::Impl", "Node::Impl"),
            ("template<>\nstruct Impl<X>", "Impl"),
            ("struct Node::Impl", "Node::Impl"),
            ("template<>\nclass Node::Impl<X>", "Node::Impl"),
        ],
    )
    def test_a_struct_named_by_a_scope_or_template_arguments_keeps_its_methods(self, head, owner):
        source = head + "\n{\n    Impl(int c) : wc({ 1, -1 }) { }\n    bool reset(int a) { return true; }\n};\nint after() { return 1; }\n"
        result = _parse(source)
        assert [f.qualified_name for f in result.functions] == [f"{owner}::Impl", f"{owner}::reset", "after"]
        assert not result.has_errors and result.suspected_losses == []

    def test_a_struct_named_in_a_namespace_has_its_namespace(self):
        source = "namespace zmp {\ntemplate<>\nstruct Node<X>::Impl { void run() { } };\n}\n"
        assert [f.qualified_name for f in _parse(source).functions] == ["zmp::Node::Impl::run"]

    def test_the_classes_a_scope_names_are_recorded_for_the_resolver(self):
        source = "template<>\nstruct Node<X>::Impl { int count; void run() { } };\n"
        classes = {fact["name"]: fact for fact in _parse(source).facts["classes"]}
        assert classes["Node::Impl"]["members"] == ["count"]


class TestSilentLossesAreGone:
    def test_an_attribute_macro_after_the_parameters(self):
        declaration = "struct B {\n  void AppendF(const char* fmt, ...) IM_FMTARGS(2);\n  void Other() { }\n};\n"
        assert "Other" in _names(declaration)
        definition = "struct B {\n  void AddLog(const char* fmt, ...) IM_FMTARGS(2)\n  { int x; }\n  void Other() { }\n};\n"
        assert _names(definition) == ["AddLog", "Other"]

    def test_a_control_statement_ending_in_a_macro_is_not_mistaken_for_one(self):
        result = _parse("void f() { if (x) IM_ASSERT(x); while (y) STEP(1); }\nvoid g() {}\n")
        assert [f.name for f in result.functions] == ["f", "g"] and not result.has_errors
        assert [c.name for c in result.functions[0].calls] == ["IM_ASSERT", "STEP"]

    def test_conversion_operators_are_functions_with_their_own_names(self):
        result = _parse("struct C {\n float v;\n operator int() const { return 1; }\n operator int() { return 2; }\n operator float*() { return &v; }\n operator unsigned int() const { return 3; }\n void Other() {}\n};\n")
        listed = sorted((f.qualified_name, tuple(f.parameter_tokens)) for f in result.functions)
        assert listed == [
            ("C::Other", ()), ("C::operator float*", ()), ("C::operator int", ()), ("C::operator int", ("const",)), ("C::operator unsigned int", ("const",)),
        ]
        assert len({f.body for f in result.functions}) == 5

    def test_a_definition_dropped_without_an_error_is_reported(self):
        parser = CppParser()
        text = "int one() { return 1; }\nint two() { return 2; }\n"
        tree_functions = parser.parse(text).functions
        parser2 = CppParser()
        parser2._definitions_by_scan = lambda source: [1, 2, 9]  # the brace scan sees a definition at line 9 that no function covers
        result = parser2.parse(text)
        assert len(tree_functions) == 2 and result.suspected_losses == [9]
        assert result.has_errors and result.errors[0].message == "Possibly missed definition"

    def test_the_brace_scan_agrees_with_a_clean_file(self):
        parser = CppParser()
        text = "namespace n { struct S { S() : a(1) {} int a; int get() const { return a; } }; }\nvoid f() { if (x) { y(); } }\n"
        assert parser._missed_definitions(text, parser.parse(text).functions) == []


_WORST = {"wa": 1, "wb": 3, "wc": 2, "wd": 0, "we": 4, "wf": 5, "wg": 6}


@pytest.fixture(scope="module")
def _scanned_cpp(tmp_path_factory, initialised_template):
    """One project with the C++ files the CLI checks need, initialised and scanned once for the module."""
    root = tmp_path_factory.mktemp("cpp_project")
    (root / "t1.cpp").write_text("int ok1() { return 1; }\nint bad(int x) { return (x +; }\nint ok2() { return 2; }\n")
    for name, broken in _WORST.items():
        body = "".join(f"int ok{name}{i}() {{ return {i}; }}\n" for i in range(2))
        body += "".join(f"int bad{name}{i}(int x) {{ return (x +; }}\n" for i in range(broken))
        (root / f"{name}.cpp").write_text(body)
    (root / "decl.h").write_text("int add(int a, int b);\nint sub(int a, int b);\n")
    (root / "decl.cpp").write_text('#include "decl.h"\nint add(int a, int b) { return a + b; }\n')
    (root / "macro.cpp").write_text(
        "int helper(int v) { return v; }\n"
        "int main_fn(int* p) { IM_ASSERT(p); memset(p, 0, 4); std::sort(p, p + 2); unknown_call(1); return helper(1); }\n"
    )
    from drspec.core import project_files

    shutil.copytree(initialised_template, root, dirs_exist_ok=True)  # the initialised project (`init` is the session's, not this module's)
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(root)
        patch.setattr(project_files, "inside_git", lambda folder: False)  # the files are listed from disk: no git runs
        assert runner.invoke(app, ["--json", "scan"]).exit_code == 0
    before = (table_counts(root / "_drspec" / "contracts.db"), folder_listing(root))
    yield root
    # the project is shared by the tests of the module and they only read it: what it holds at the end is what it held when it was built
    assert (table_counts(root / "_drspec" / "contracts.db"), folder_listing(root)) == before, "a test wrote into the project the module shares"


class TestPartialFunctions:
    def test_a_function_with_an_error_in_its_body_is_kept_and_marked(self, tmp_path):
        source = tmp_path / "a.cpp"
        source.write_text("int ok1() { return 1; }\nint bad(int x) { return (x +; }\nint ok2() { return 2; }\n")
        result = Scanner().scan_file(source, relative_to=tmp_path)
        assert [f.name for f in result.functions] == ["ok1", "bad", "ok2"]
        assert [p["name"] for p in result.partial_functions] == ["bad"]

    def test_the_search_marks_the_broken_function_only(self, _scanned_cpp, monkeypatch):
        monkeypatch.chdir(_scanned_cpp)
        matches = json.loads(runner.invoke(app, ["--json", "search", "-E", "bad|ok1", "--code-only"], catch_exceptions=False).output)["data"]["matches"]  # (one call for both)
        assert any(m["function_name"] == "bad" and m["partial"] is True for m in matches)
        ok = [m for m in matches if m["function_name"] == "ok1"]
        assert ok and "partial" not in ok[0]
        assert "Partial: Yes" in runner.invoke(app, ["search", "bad", "--code-only"]).output

    def test_the_status_lists_the_files_that_lose_most_worst_first(self, _scanned_cpp, monkeypatch):
        monkeypatch.chdir(_scanned_cpp)
        status = json.loads(runner.invoke(app, ["--json", "status"]).output)["data"]["partially_parsed"]
        assert status["files"] == 1 + sum(1 for broken in _WORST.values() if broken)  # wd has no broken function
        assert status["examples"][0]["file"] == "wg.cpp" and len(status["examples"]) == 5
        losses = [e["functions_skipped"] + e["functions_partial"] for e in status["examples"]]
        assert losses == sorted(losses, reverse=True)


class TestTheWorstFilesAndTheirTotals:
    """The pure functions behind the partial-file report: no scan, no database."""

    @staticmethod
    def _infos():
        return [
            {"file": f"{name}.cpp", "first_error_line": 3, "skipped": ["x"] * broken, "partial": []}
            for name, broken in (("a", 1), ("b", 3), ("c", 2), ("d", 0), ("e", 4), ("f", 5), ("g", 6))
        ]

    def test_the_worst_files_come_first_and_only_five_are_listed(self):
        worst = worst_files(self._infos(), 5)
        assert [w["file"] for w in worst] == ["g.cpp", "f.cpp", "e.cpp", "b.cpp", "c.cpp"]
        assert all(w["file"] != "d.cpp" for w in worst)  # a file that lost nothing is not listed

    def test_the_totals_add_up_over_every_file(self):
        infos = self._infos()
        totals_ = totals(infos)
        assert totals_["files"] == 6 and totals_["files_read_with_warnings"] == 1
        assert totals_["functions_skipped"] == sum(len(i["skipped"]) for i in infos)

    def test_a_scan_summary_gives_the_same_totals(self):
        partial = {info["file"]: info for info in self._infos()}
        report = partial_report(partial, 0)
        assert len(report["partial_examples"]) == 5 and report["partial_examples"][0]["file"] == "g.cpp"
        assert report["functions_skipped"] == totals(self._infos())["functions_skipped"]

    def test_the_status_counts_come_from_the_recorded_files(self):
        conn = duckdb.connect(":memory:")
        init_schema(conn)
        record_partial_files(conn, [], {info["file"]: info for info in self._infos()})
        summary = partial_summary(conn)
        assert summary["files"] == 6 and len(summary["examples"]) == 5
        conn.close()


class TestWhatTheSummaryAndStatusCount:
    def test_declarations_are_told_apart_from_definitions(self):
        run = new_run("x", 1)
        run["functions_found"] = 3
        run["declarations_found"] = 1
        data = summarize({"queued": 1, "scan_id": "x"}, run, "done")
        assert data["definitions_found"] + data["declarations_found"] == data["functions_found"] == 3

    def test_a_declaration_is_read_as_one(self):
        functions = CppParser().parse("int add(int a,int b);\n", "a.h").functions  # declarations are read in headers
        assert functions and "declaration" in functions[0].decorators

    def test_macros_and_library_calls_are_counted_apart_from_unresolved_calls(self, _scanned_cpp):
        conn = duckdb.connect(str(_scanned_cpp / "_drspec" / "contracts.db"), read_only=True)  # pattern-ok: duckdb-file reads the shared project's database FILE the CLI made
        try:
            health = language_health(conn, "cpp")
        finally:
            conn.close()
        assert health["macro_invocations"] == 1 and health["library_calls"] == 2
        assert health["calls"] == 2 and health["unresolved_calls"] == 1
        assert health["unresolved_reasons"] == {"no definition in the project": 1}
        assert sum(health["unresolved_reasons"].values()) == health["unresolved_calls"]

    @pytest.mark.parametrize(
        "call, reason",
        [
            ({"name": "IM_ASSERT", "receiver": None}, "macro"),
            ({"name": "GL_CALL", "receiver": None}, "macro"),
            ({"name": "memcpy", "receiver": None}, "external"),
            ({"name": "sort", "receiver": "std"}, "external"),
            ({"name": "SameLine", "receiver": "ImGui"}, "not found"),
            ({"name": "memcpy", "receiver": "obj"}, "not found"),
        ],
    )
    def test_the_classification(self, call, reason):
        assert classify_cpp_call(call) == reason


class TestObjectiveCPlusPlus:
    def test_mm_files_are_scanned_as_c_plus_plus(self, tmp_path):
        assert Scanner().detect_language("b.mm") == "cpp"
        source = tmp_path / "b.mm"
        source.write_text("int plain() { return 1; }\n")
        assert len(Scanner().scan_file(source, relative_to=tmp_path).functions) == 1
