"""C read with the C++ grammar: its own label, the constructs only C has, the vendored single-header libraries, and the
filters and counts that name it.

All of it is the parser on a string, a committed excerpt, or a database in memory loaded from strings. The real corpora (raylib,
Dear ImGui) are not read by any test: `scripts/corpus_check_c.py` and `scripts/corpus_check_cpp.py` check them by hand, and what
they found over time is pinned here and in `test_cpp_conditionals.py` and `test_cpp_recovery.py` by small excerpts (the list is in
`tests/README.md`)."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from drspec.core.hasher import compute_hash
from drspec.core.language_health import language_health
from drspec.core.languages import C_FAMILY, family_of, languages_matching
from drspec.core.live_search import known_languages
from drspec.core.scanner import ScannedFunction, Scanner
from drspec.db import init_schema
from drspec.db.queries import language_filter, search_artifacts, upsert_scanned_file
from drspec.parsers.cpp_parser import CppParser
from drspec.parsers.python_parser import PythonParser
from tests.helpers import table_counts
from tests.helpers import publish

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "c"


def _functions(text: str, path: str, language: str):
    """The functions of `text` as a scan stores them (the parser reads a string: no file, no label lookup)."""
    parser = PythonParser() if language == "python" else CppParser()
    result = parser.parse(text, path) if language == "python" else parser.parse(text, path, language=language)
    return [ScannedFunction.from_extracted(function, path, language) for function in result.functions]


class TestTheLabel:
    @pytest.mark.parametrize(
        "files,target,expected",
        [
            pytest.param(["a.c"], "a.c", "c", id="a-c-file-is-c"),
            pytest.param(["b.cpp"], "b.cpp", "cpp", id="a-cpp-file-is-cpp"),
            pytest.param(["Legacy.C"], "Legacy.C", "cpp", id="a-capital-dot-c-is-cpp-by-convention"),
            pytest.param(["lib.h", "lib.c"], "lib.h", "c", id="a-header-beside-c-sources-is-c"),
            pytest.param(["lib.h", "lib.cpp"], "lib.h", "cpp", id="a-header-beside-a-cpp-file-is-cpp"),
            pytest.param(["x.h", "a.c", "b.cpp"], "x.h", "cpp", id="c-and-cpp-in-one-folder-make-a-header-cpp"),
            pytest.param(["src/include/api.h", "src/m.c"], "src/include/api.h", "c", id="a-header-in-an-include-folder-follows-the-sources-beside-it"),
            pytest.param(["lonely/only.h"], "lonely/only.h", "cpp", id="a-header-with-nothing-near-is-cpp"),
        ],
    )
    def test_the_label_follows_the_name_and_the_neighbours(self, tmp_path, files, target, expected):
        # the rule reads the folders around a file, so the files exist (empty)
        for name in files:
            (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / name).touch()
        assert Scanner().label_of(tmp_path / target, tmp_path) == expected

    def test_the_label_reaches_the_functions_of_a_scan(self, tmp_path):
        # one real scan of real files: the wiring from the label to `function.language`
        (tmp_path / "a.c").write_text("int f(void) { return 1; }\n")
        (tmp_path / "b.cpp").write_text("int g() { return 1; }\n")
        (tmp_path / "clib").mkdir()
        (tmp_path / "clib" / "lib.c").write_text("int h(void) { return 1; }\n")
        (tmp_path / "clib" / "lib.h").write_text("static inline int twice(int a) { return a * 2; }\n")
        scanner = Scanner()
        languages = {name: {f.language for f in scanner.scan_file(str(tmp_path / name), relative_to=tmp_path).functions} for name in ("a.c", "b.cpp", "clib/lib.h")}
        assert languages == {"a.c": {"c"}, "b.cpp": {"cpp"}, "clib/lib.h": {"c"}}

    def test_sources_above_the_project_do_not_decide_the_label_of_its_header(self, tmp_path):
        (tmp_path / "c_sources_outside").mkdir()
        (tmp_path / "c_sources_outside" / "m.c").touch()
        project = tmp_path / "project"
        project.mkdir()
        (project / "only.h").touch()
        assert Scanner().label_of(project / "only.h", project) == "cpp"
        # (without a project to hold the lookup to, the folders above are looked at, as before)
        assert Scanner().label_of(project / "only.h") == "c"

    def test_the_label_is_not_part_of_the_identifier_or_the_hash(self):
        (as_c,) = _functions("int f(int x) { return x; }\n", "a.c", "c")
        (as_cpp,) = _functions("int f(int x) { return x; }\n", "b.cpp", "cpp")
        assert as_c.function_id.split("::", 1)[1] == as_cpp.function_id.split("::", 1)[1]
        assert compute_hash("int f() {}", "c") == compute_hash("int f() {}", "cpp")

    def test_the_family(self):
        assert C_FAMILY == ("c", "cpp") and family_of("c") == "cpp"
        assert languages_matching("cpp") == ("c", "cpp") and languages_matching("c") == ("c",) and languages_matching("python") == ("python",)


@pytest.fixture(scope="module")
def project_db():
    """A project database in memory with three files loaded from strings: two C functions, one C++ and one Python."""
    connection = duckdb.connect(":memory:")
    init_schema(connection)
    functions = (
        _functions("int c_one(void) { return 1; }\nint c_two(void) { return 2; }\n", "a.c", "c")
        + _functions("int cpp_one() { return 1; }\n", "b.cpp", "cpp")
        + _functions("def py_one():\n    return 1\n", "c.py", "python")
    )
    publish(connection, functions=functions, scanned_file_paths={"a.c", "b.cpp", "c.py"}, file_imports={})
    for name in ("a.c", "b.cpp", "c.py"):
        upsert_scanned_file(connection, name, 1.0, 10)
    before = table_counts(connection)
    yield connection
    assert table_counts(connection) == before, "a test wrote into the database the module shares"
    connection.close()


class TestFiltersAndStatus:
    def test_the_filter_names_the_stored_languages(self):
        assert language_filter("c") == ("language IN (?)", ["c"])
        assert language_filter("cpp") == ("language IN (?, ?)", ["c", "cpp"])

    def test_include_c_is_only_c_and_include_cpp_is_both(self, project_db):
        assert [a.function_name for a in search_artifacts(project_db, "one", language="c")] == ["c_one"]
        assert sorted(a.function_name for a in search_artifacts(project_db, "one", language="cpp")) == ["c_one", "cpp_one"]

    def test_status_counts_the_c_inside_the_cpp_block(self, project_db):
        block = language_health(project_db, "cpp")
        assert block["files"] == 2 and block["c_files"] == 1 and block["c_functions"] == 2

    def test_an_unknown_language_is_refused_and_c_is_known(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner

        from drspec.cli.app import app

        assert "c" in known_languages() and "nosuch" not in known_languages()
        monkeypatch.chdir(tmp_path)  # (the check is the first thing the command does: no project is needed)
        result = CliRunner().invoke(app, ["--json", "search", "x", "--include", "nosuch"])
        assert result.exit_code != 0 and json.loads(result.output)["error"]["code"] == "INVALID_INPUT"


class TestAnOlderDatabaseIsRescanned:
    def test_a_rescan_relabels_the_c_functions_and_keeps_their_identity_hash_and_contract(self):
        connection = duckdb.connect(":memory:")
        init_schema(connection)
        sources = {"a.c": "int f(void) { return 1; }\n", "b.h": "int g(void) { return 2; }\n"}
        new = [fn for path, text in sources.items() for fn in _functions(text, path, "c")]
        old = [fn for path, text in sources.items() for fn in _functions(text, path, "cpp")]  # as a scan before C had its own label stored them
        publish(connection, functions=old, scanned_file_paths=set(sources), file_imports={})
        connection.execute("INSERT INTO contracts (function_id, contract_json) VALUES (?, '{}')", [old[0].function_id])
        before = connection.execute("SELECT function_id, code_hash FROM artifacts ORDER BY 1").fetchall()
        assert {row[0] for row in connection.execute("SELECT language FROM artifacts").fetchall()} == {"cpp"}

        publish(connection, functions=new, scanned_file_paths=set(sources), file_imports={})

        assert connection.execute("SELECT DISTINCT language FROM artifacts").fetchall() == [("c",)]
        assert connection.execute("SELECT function_id, code_hash FROM artifacts ORDER BY 1").fetchall() == before
        assert connection.execute("SELECT COUNT(*) FROM contracts").fetchone() == (1,)
        connection.close()

    def test_nothing_reads_a_relabel_marker_any_more(self):
        source = Path(__file__).resolve().parents[2] / "src" / "drspec"
        assert not (source / "db" / "c_label.py").exists()
        assert not any("schema:c_label" in path.read_text() for path in source.rglob("*.py"))


MANIFEST = json.loads((FIXTURES / "constructs.json").read_text())
CONSTRUCTS = (FIXTURES / "constructs.c").read_text()


@pytest.fixture(scope="module")
def constructs_result():
    """`constructs.c` scanned once for the whole conformance class."""
    result = Scanner().scan_file(str(FIXTURES / "constructs.c"), relative_to=FIXTURES)
    before = [(f.name, f.start_line, f.end_line, f.body, f.language) for f in result.functions]
    yield result
    assert [(f.name, f.start_line, f.end_line, f.body, f.language) for f in result.functions] == before, "a test changed the result the module shares"


class TestTheConformanceFile:
    def test_every_function_is_found_and_the_file_reads_cleanly(self, constructs_result):
        found = {f.name for f in constructs_result.functions}
        assert not (set(MANIFEST["functions"]) - found), sorted(set(MANIFEST["functions"]) - found)
        assert constructs_result.parse_status == "successful" and not constructs_result.errors
        # (the one thing listed is a function that a macro defines: reported by the use that makes it, not lost)
        assert all(s["name"].endswith("(a function defined by a macro)") for s in constructs_result.skipped_functions), constructs_result.skipped_functions

    @pytest.mark.parametrize("case", MANIFEST["cases"])
    def test_each_construct_between_its_sentinels(self, case):
        block = CONSTRUCTS.split(f"/* ---- {case} ---- */")[1].split("/* ----")[0]
        result = CppParser().parse(block, "x.c", language="c")
        wanted = {n for n in MANIFEST["functions"] if n.startswith(("before_" + case, "after_" + case))}
        found = {f.name for f in result.functions}
        assert wanted <= found, (case, sorted(wanted - found))
        assert not result.has_errors, [e.line for e in result.errors]

    def test_positions_are_those_of_the_source(self, constructs_result):
        lines = CONSTRUCTS.split("\n")
        for function in constructs_result.functions:
            assert function.name in lines[function.start_line - 1], function.name  # the name is on the line it says the function starts at
            assert function.body == "\n".join(lines[function.start_line - 1 : function.end_line]).strip(), function.name


class TestTheCOnlyConstructs:
    def test_a_kr_definition_is_a_function_with_its_text_unchanged(self):
        text = "int before(void) { return 0; }\nint kr(a, b)\n    int a;\n    int b;\n{\n    return a + b;\n}\nint after(void) { return 1; }\n"
        result = CppParser().parse(text, "k.c", language="c")
        kr = next(f for f in result.functions if f.name == "kr")
        assert (kr.start_line, kr.end_line) == (2, 7) and "int a;" in kr.body and not result.has_errors

    def test_a_function_returning_a_function_pointer_has_its_own_parameters(self):
        result = CppParser().parse("int (*pick(int kind, void* data))(int) { return 0; }\n", "p.c", language="c")
        (pick,) = result.functions
        assert pick.name == "pick" and pick.parameter_tokens == ["int", "void*"]

    def test_cpp_keywords_used_as_names_keep_their_spelling(self):
        text = "int new_of(int new, int delete) { int this = new + delete; return this; }\nint delete(int class) { return class; }\nint call(void) { return delete(new_of(1, 2)); }\n"
        result = CppParser().parse(text, "n.c", language="c")
        assert not result.has_errors
        assert sorted(f.name for f in result.functions) == ["call", "delete", "new_of"]
        call = next(f for f in result.functions if f.name == "call")
        assert [c.name for c in call.calls] == ["delete", "new_of"]
        delete = next(f for f in result.functions if f.name == "delete")
        assert delete.parameter_tokens == ["int"] and "class" in delete.body  # the parameter named with a C++ keyword is read as a C name

    def test_the_same_words_are_untouched_in_cpp(self):
        text = "int f(int* p) { int* q = new int[3]; delete[] q; return *p; }\nclass K { public: int g() { return this->h(); } int h() { return 1; } };\n"
        result = CppParser().parse(text, "x.cpp", language="cpp")
        assert sorted(f.name for f in result.functions) == ["f", "g", "h"] and not result.has_errors

    def test_a_statement_expression_and_asm_and_array_qualifiers(self):
        text = (
            "int maxi(int a, int b) { return ({ int _a = a, _b = b; _a > _b ? _a : _b; }); }\n"
            "static inline void barrier(void) { __asm__ __volatile__(\"\" ::: \"memory\"); }\n"
            "int first(int a[static 1]) { return a[0]; }\n"
        )
        result = CppParser().parse(text, "g.c", language="c")
        assert sorted(f.name for f in result.functions) == ["barrier", "first", "maxi"] and not result.has_errors

    def test_a_call_in_a_function_after_a_statement_expression_is_still_found(self):
        text = "int g(int a) { return a; }\nint maxi(int a, int b) { int x = ({ a; }); return g(x); }\n"
        result = CppParser().parse(text, "g.c", language="c")
        assert [c.name for c in next(f for f in result.functions if f.name == "maxi").calls] == ["g"]


class TestVendoredLibraries:
    def test_an_x_macro_list_and_a_function_defining_macro(self):
        text = (
            "#define COLORS X(RED) X(GREEN) X(BLUE)\nenum Color {\n#define X(c) c,\n    COLORS\n#undef X\n};\n"
            "int color_count(void) {\n#define X(c) +1\n    return 0 COLORS;\n#undef X\n}\n"
            "#define DEFINE_GETTER(name, field) int get_##name(struct G *g) { return g->field; }\nstruct G { int w; };\nDEFINE_GETTER(w, w)\nint after_getter(void) { return 1; }\n"
        )
        result = CppParser().parse(text, "x.c", language="c")
        assert sorted(f.name for f in result.functions) == ["after_getter", "color_count"] and not result.has_errors

    def test_an_identity_macro_makes_a_compound_literal(self):
        text = (
            "#ifdef __cplusplus\n#define LITERAL(name) name\n#else\n#define LITERAL(name) (name)\n#endif\n"
            "typedef struct { float x, y; } Rectangle;\nvoid draw(Rectangle r);\n"
            "void box(float x, float y) { draw(LITERAL(Rectangle){ x, y }); draw(LITERAL(Rectangle){ y, x }); }\n"
        )
        result = CppParser().parse(text, "g.h", language="c")
        assert not result.has_errors and [f.name for f in result.functions if "declaration" not in f.decorators] == ["box"]

    def test_a_macro_that_ends_in_enum_makes_a_typedef_of_an_enum(self):
        text = "#define MY_ENUM(type, name) type name; enum\ntypedef MY_ENUM(unsigned char, kind) {\n    A, B\n} kind;\nint use_kind(void) { return A; }\n"
        result = CppParser().parse(text, "g.h", language="c")
        assert [f.name for f in result.functions] == ["use_kind"] and not result.has_errors and not result.suspected_losses

    def test_a_control_statement_in_an_unreadable_stretch_is_not_a_lost_function(self):
        text = "int ok(void) { return 1; }\n/* if (x) { y; } */\nint broken(void) { if (a) { b(( ; } }\nint after(void) { return 2; }\n"
        result = CppParser().parse(text, "x.c", language="c")
        assert all(line == 3 for line in result.suspected_losses)

    def test_code_shown_in_a_comment_is_not_counted_as_lost(self):
        text = "/*\n    if (ma_device_init(NULL, &config, &device) != MA_SUCCESS) {\n        return -1;\n    }\n*/\nint broken(int a { return a; }\nint after(void) { return 2; }\n"
        result = CppParser().parse(text, "x.c", language="c")
        assert 2 not in result.suspected_losses and 3 not in result.suspected_losses


class TestSkippedEntriesHaveNames:
    TEXT = "int ok(void) { return 1; }\nMA_API MA_ATOMIC(4, int) mine(int a) { b(( ; } }\nint after(void) { return 2; }\n"
    MACRO = (
        "#define DEFINE_READER(extension, type) \\\n"
        "static type* read_all_ ## extension (int handle, unsigned int* count) \\\n"
        "{ \\\n"
        "    return 0; \\\n"
        "}\n"
        "DEFINE_READER(s32, int)\n"
        "DEFINE_READER(f32, float)\n"
        "int plain(void) { return 1; }\n"
    )

    def test_a_lost_definition_is_named_by_the_text_before_its_parenthesis_not_by_a_macro_in_front(self):
        result = CppParser().parse(self.TEXT, "x.c", language="c")
        assert result.suspected_names[2] == "mine"

    @pytest.mark.parametrize(
        "text,expected",
        [
            pytest.param(TEXT, [{"file": "x.c", "name": "mine", "line": 2}], id="a-name-and-a-name-less-loss"),
            pytest.param(
                MACRO,
                [
                    {"file": "x.c", "name": "DEFINE_READER(s32, int) (a function defined by a macro)", "line": 6},
                    {"file": "x.c", "name": "DEFINE_READER(f32, float) (a function defined by a macro)", "line": 7},
                ],
                id="functions-a-macro-defines",
            ),
        ],
    )
    def test_the_scanner_lists_what_was_lost_with_its_names(self, tmp_path, text, expected):
        # one real file each: that `suspected_names` becomes `skipped_functions` is the scanner's own step
        (tmp_path / "x.c").write_text(text)
        skipped = Scanner().scan_file(str(tmp_path / "x.c"), relative_to=tmp_path).skipped_functions
        assert all(entry in skipped for entry in expected), skipped

    def test_names_of_heads_that_are_split_qualified_or_operators(self):
        parser = CppParser()
        text = "static ma_result\nns::Thing::run(int a,\n    int b)\n{\n}\nbool operator==(const A& a, const A& b) { return 1; }\nTing::~Ting() {}\n"
        names = parser._names_of_losses(text, [1, 6, 7])
        assert names == {1: "ns::Thing::run", 6: "operator==", 7: "Ting::~Ting"}

    def test_a_struct_head_is_named_as_a_struct_and_a_text_without_a_head_has_no_name(self):
        parser = CppParser()
        names = parser._names_of_losses("struct ma_job\n{\n    int a;\n}\n// nothing here\n", [1, 5])
        assert names == {1: "struct ma_job"}


class TestWhatTheCompilerSeesAndWhatItDoesNot:
    def test_a_function_that_a_macro_defines_is_reported_by_the_use_that_makes_it(self):
        result = CppParser().parse(TestSkippedEntriesHaveNames.MACRO, "m.c", language="c")
        assert [f.name for f in result.functions] == ["plain"]
        assert result.suspected_names == {
            6: "DEFINE_READER(s32, int) (a function defined by a macro)",
            7: "DEFINE_READER(f32, float) (a function defined by a macro)",
        }
        assert result.suspected_losses == [6, 7]

    CPP_BLOCK = (
        "int c_one(void) { return 1; }\n"
        "#ifdef __cplusplus\n"
        "extern \"C\" {\n"
        "#endif\n"
        "#ifdef __cplusplus\n"
        "struct Wrapper { int broken( ; };\n"
        "#else\n"
        "int c_two(void) { return 2; }\n"
        "#endif\n"
        "#ifndef __cplusplus\n"
        "int c_three(void) { return 3; }\n"
        "#else\n"
        "int cpp_only(int a) { return a +; }\n"
        "#endif\n"
    )

    def test_the_lines_only_a_cpp_compiler_reads_are_known(self):
        hidden = CppParser()._cplusplus_only_lines(self.CPP_BLOCK)
        assert {3, 6, 13} <= hidden and not hidden & {1, 8, 11}

    def test_a_loss_in_a_cplusplus_block_of_a_c_file_is_not_counted_but_one_in_c_code_is(self):
        text = (
            "int ok(void) { return 1; }\n#ifdef __cplusplus\nMA_API MA_ATOMIC(4, int) wrapper(int a) { b(( ; } }\n#endif\n"
            "MA_API MA_ATOMIC(4, int) broken_c(int a) { b(( ; } }\nint after(void) { return 2; }\n"
        )
        as_c = CppParser().parse(text, "x.c", language="c")
        as_cpp = CppParser().parse(text, "x.cpp", language="cpp")
        assert as_c.suspected_losses == [5, 6]  # the loss in C code is counted (and the line it swallowed); the one the C compiler never reads is not
        assert as_cpp.suspected_losses == [3, 5, 6]  # a C++ compiler reads the block too


class TestADirectiveInsideAnEnum:
    def test_it_does_not_cost_the_functions_after_it(self):
        text = "enum Flags {\n    A = 1,\n#ifdef EXTRA\n    B = 2,\n#endif\n#define FLAG_MAX 4\n    C = FLAG_MAX,\n};\nint after(void) { return 1; }\n"
        result = CppParser().parse(text, "e.h")
        assert [f.name for f in result.functions] == ["after"] and not result.has_errors
