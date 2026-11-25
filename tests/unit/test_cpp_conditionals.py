"""C++ under the preprocessor: members and functions inside `#if` / `#else` blocks, the same function defined in both
branches, raw string literals, a macro that stands for a storage class, and the stored text of a function being the text
the user wrote."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.core.scanner import Scanner
from drspec.parsers.cpp_parser import CppParser

runner = CliRunner()


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _scan(root: Path, name: str, text: str):
    (root / name).write_text(text)
    return Scanner().scan_file(name, relative_to=root)


def _lines(result) -> list:
    return sorted((f.qualified_name, f.start_line) for f in result.functions)


CLASS_WITH_BLOCKS = """struct ImTextureRef
{
    ImTextureRef()                          { _TexData = NULL; }
    ImTextureRef(int tex_id)                { _TexData = NULL; }
#if !defined(IMGUI_DISABLE_OBSOLETE_FUNCTIONS) && !defined(ImTextureID)
    ImTextureRef(void* tex_id)              { _TexData = NULL; }  // For legacy backends
#endif

    inline int  GetTexID() const;
};
struct F {
    bool Draw(const char* label = "Filter");
#ifndef IMGUI_DISABLE_OBSOLETE_FUNCTIONS
    inline bool         Draw(const char* label, float width)            { if (width != 0.0f) ImGui::SetNextItemWidth(width); return Draw(label); }
#else
    inline bool         Draw(const char* label, float width)            { return false; }
#endif
};
int after() { return 1; }
"""


class TestMembersUnderThePreprocessor:
    def test_members_in_an_if_block_of_a_class_body_are_found(self, root):
        result = _scan(root, "o.h", CLASS_WITH_BLOCKS)
        assert result.parse_status == "successful" and not result.skipped_functions
        assert ("ImTextureRef::ImTextureRef", 6) in _lines(result) and ("after", 19) in _lines(result)

    def test_both_branches_of_an_if_else_in_a_class_body_are_functions(self, root):
        result = _scan(root, "o.h", CLASS_WITH_BLOCKS)
        draws = [f for f in result.functions if f.qualified_name == "F::Draw" and f.start_line in (14, 16)]
        assert len(draws) == 2 and len({f.function_id for f in draws}) == 2

    def test_access_carries_into_a_block(self, root):
        result = _scan(root, "a.h", "class K {\npublic:\n#ifdef X\n    int pub() { return 1; }\n#endif\nprivate:\n    int priv() { return 2; }\n};\n")
        access = {f.name: f.decorators for f in result.functions}
        assert "public" in access["pub"] and "private" in access["priv"]


TWO_BRANCHES = """inline double ImSign(double x)           { return 1.0; }
#ifdef IMGUI_ENABLE_SSE
inline float  ImRsqrt(float x)           { return _mm_cvtss_f32(_mm_rsqrt_ss(_mm_set_ss(x))); }
#else
inline float  ImRsqrt(float x)           { return 1.0f / sqrtf(x); }
#endif
int after() { return 1; }
"""


class TestTheSameFunctionInBothBranches:
    def test_each_is_a_function_with_an_identifier_of_its_own(self, root):
        result = _scan(root, "e.h", TWO_BRANCHES)
        assert result.parse_status == "successful" and not result.skipped_functions
        rsqrt = [f for f in result.functions if f.name == "ImRsqrt"]
        assert len(rsqrt) == 2 and len({f.function_id for f in rsqrt}) == 2
        assert sorted(f.start_line for f in rsqrt) == [3, 5]

    def test_the_last_keeps_the_identifier_a_single_definition_has(self, root):
        single = _scan(root, "s.h", "inline float  ImRsqrt(float x)           { return 1.0f / sqrtf(x); }\n")
        both = _scan(root, "e.h", TWO_BRANCHES)
        plain = single.functions[0].function_id.split("#")[1]
        last = max((f for f in both.functions if f.name == "ImRsqrt"), key=lambda f: f.start_line)
        assert last.function_id == f"e.h::ImRsqrt#{plain}"

    def test_a_declaration_is_still_dropped_when_the_definition_is_in_the_file(self, root):
        result = CppParser().parse("int twice(int a);\nint twice(int a) { return a * 2; }\nint twice(double a);\n", file_path="t.h")
        shapes = sorted((f.parameter_tokens, "declaration" in f.decorators) for f in result.functions)
        assert shapes == [(["double"], True), (["int"], False)]


class TestRawStrings:
    def test_a_shader_in_a_raw_string_is_not_a_function(self, root):
        text = 'const char* shader = R"(\nfn main(in: VertexInput) -> VertexOutput {\n    return out;\n}\n)";\nint real() { return 1; }\n'
        result = _scan(root, "w.cpp", text)
        assert [f.name for f in result.functions] == ["real"] and not result.skipped_functions and result.parse_status == "successful"

    def test_a_delimiter_is_honoured(self, root):
        text = 'const char* sql = R"sql(\nvoid fake() { }\n)" still inside )sql";\nint real() { return 1; }\n'
        assert not _scan(root, "w.cpp", text).skipped_functions


class TestAMacroForAStorageClass:
    def test_a_function_after_it_is_found(self, root):
        text = "#ifdef STB_STATIC\n#define STBTT_DEF static\n#else\n#define STBTT_DEF extern\n#endif\n\nSTBTT_DEF unsigned char *find_doc(const void *info, int gl)\n{\n   return 0;\n}\nint after() { return 1; }\n"
        result = _scan(root, "t.h", text)
        assert sorted(f.name for f in result.functions) == ["after", "find_doc"] and not result.skipped_functions


class TestTheStoredTextIsTheUsersText:
    TEXT = "#define FMT(x) __attribute__((format(printf, x, x+1)))\n#define API\nstruct S {\n    void A(const char* fmt, ...) FMT(2);\n    void B() { }\n};\nAPI int B2(int a) FMT(1) { return a; }\n"

    def test_a_trailing_macro_is_in_the_signature_and_the_body(self, root):
        result = _scan(root, "m.cpp", self.TEXT)
        a = next(f for f in result.functions if f.name == "A")
        assert a.signature == "void A(const char* fmt, ...) FMT(2);" and a.body == a.signature

    def test_a_leading_macro_is_in_the_body_too(self, root):
        b2 = next(f for f in _scan(root, "m.cpp", self.TEXT).functions if f.name == "B2")
        assert b2.signature == "API int B2(int a) FMT(1) { return a; }" and b2.body == b2.signature

    def test_a_default_decorator_name_is_kept_too(self, root):
        result = _scan(root, "d.cpp", "class DLLEXPORT Cls { public: int m() { return 1; } };\nIMGUI_API void f() { }\n")
        f = next(f for f in result.functions if f.name == "f")
        assert f.body == "IMGUI_API void f() { }"

    def test_a_search_for_the_macro_finds_the_body(self, kept_project):
        (kept_project / "m.cpp").write_text(self.TEXT)  # the project database is in memory (`kept_project`): `init` is not the objective
        assert runner.invoke(app, ["--json", "scan"]).exit_code == 0
        data = json.loads(runner.invoke(app, ["--json", "search", "FMT(1)", "--code-only"]).output)["data"]
        assert data["total_matches"] >= 1


class TestObjectiveCppIsSaidToBeSkipped:
    def test_status_counts_what_a_message_send_costs(self, kept_project):
        (kept_project / "v.mm").write_text("@implementation V\n- (void)m:(int)x { [self n:x]; }\n- (void)o { }\n@end\nint after() { return 1; }\n")
        assert runner.invoke(app, ["--json", "scan"]).exit_code == 0
        block = json.loads(runner.invoke(app, ["--json", "status"]).output)["data"]["cpp"]
        assert block.get("objective_c_functions_skipped", 0) >= 1


class TestTheOtherBranchesWhenOnlyTheFirstWasRead:
    """When braces cannot be read across the branches of an `#if`, the tree is made from the first branch of each. What the
    `#else` branches define is read from texts that keep one of them each, and is not lost without a word."""

    TEXT = (
        "#if defined(W)\nstatic int one(int a)\n{\n    #if defined(X)\n    { a++; }\n    #else\n    { a--; }\n    #endif\n    return a;\n}\n"
        "#elif defined(V)\nstatic int two(int a) { return a; }\n"
        "#else\nstatic int three(int a) { return a; }\nstatic int four(int a) { return a; }\n#endif\n"
        "int tail(void) { return 1; }\nstatic int broken( {\n"
    )

    @staticmethod
    def _first_branches_read(parser):
        def forced(prepared, tree):
            single, _dropped = parser._first_branches_only(prepared)
            return parser._parser.parse(bytes(single, "utf8")), single, prepared

        parser._better_tree = forced
        return parser

    def test_the_functions_of_every_branch_are_found(self):
        result = self._first_branches_read(CppParser()).parse(self.TEXT, "x.cpp")
        assert {"one", "two", "three", "four", "tail"} <= {f.name for f in result.functions}

    def test_without_the_other_branches_only_the_first_is(self):
        parser = self._first_branches_read(CppParser())
        parser._MAX_BRANCHES_READ = 1
        names = {f.name for f in parser.parse(self.TEXT, "x.cpp").functions}
        assert "one" in names and not names & {"two", "three", "four"}

    def test_a_function_is_not_listed_twice_and_keeps_its_own_lines(self):
        result = self._first_branches_read(CppParser()).parse(self.TEXT, "x.cpp")
        lines = [(f.start_line, f.name) for f in result.functions]
        assert len(lines) == len(set(lines))
        assert dict((name, line) for line, name in lines)["three"] == 14

    def test_the_variant_texts_keep_one_branch_each_and_the_same_length(self):
        parser = CppParser()
        texts = parser._other_branch_texts(self.TEXT)
        assert len(texts) == 2 and all(len(text) == len(self.TEXT) and text.count("\n") == self.TEXT.count("\n") for text in texts)
        assert "two(" in texts[0] and "one(" not in texts[0] and "three(" not in texts[0]
        assert "three(" in texts[1] and "two(" not in texts[1] and "one(" not in texts[1]
        assert "#if" not in texts[0] and "#else" not in texts[1]

    def test_a_text_without_a_conditional_has_no_other_texts(self):
        assert CppParser()._other_branch_texts("int a(void) { return 1; }\n") == []


class TestWhatIsKeptFromTheOtherBranches:
    @staticmethod
    def _function(name="f", start=3, end=5):
        from drspec.parsers.models import ExtractedFunction

        return ExtractedFunction(name=name, qualified_name=name, signature="", body="", start_line=start, end_line=end, parent=None)

    LINES = ["", "", "int f(void)", "{", "}", "x"]

    def test_a_function_that_has_its_name_and_swallows_nothing_is_kept(self):
        assert CppParser._is_a_function_of_its_own(self._function(), [3, 10], self.LINES)

    def test_one_in_which_another_function_starts_is_not_a_function_of_its_own(self):
        assert not CppParser._is_a_function_of_its_own(self._function(end=9), [3, 6, 10], self.LINES)

    def test_one_whose_name_is_not_where_it_starts_is_not(self):
        assert not CppParser._is_a_function_of_its_own(self._function(name="g"), [3], self.LINES)

    def test_one_that_starts_inside_a_macro_that_goes_on_is_not(self):
        lines = ["#define M(a) \\", "int f(void)", "{", "}"]
        assert not CppParser._is_a_function_of_its_own(self._function(start=2, end=4), [2], lines)

    def test_the_text_of_a_function_does_not_start_in_the_define_before_it(self):
        text = (
            "#define XADD(a) \\\n    __asm__ __volatile__(   \\\n        \"lock\" : \"=a\"(a))\n"
            "static int cas(int *d)\n{\n    return *d;\n}\n"
        )
        result = CppParser().parse(text, "x.c", language="c")
        assert [f.body.split("\n")[0] for f in result.functions] == ["static int cas(int *d)"]

    def test_the_text_of_a_function_does_not_start_in_the_use_of_a_macro_that_defines_another(self):
        text = (
            "#define DEFINE_ONE(t) \\\nstatic t* make_ ## t (int a) \\\n{ \\\n    return 0; \\\n}\n"
            "DEFINE_ONE(int)\nint plain(int a) { return a; }\n"
        )
        result = CppParser().parse(text, "x.c", language="c")
        assert [f.body.split("\n")[0] for f in result.functions] == ["int plain(int a) { return a; }"]

    C_TEXT = (
        "/* éè comment */\n#if defined(W)\nstatic int one(int a)\n{\n    #if defined(X)\n    { a++; }\n    #else\n    { a--; }\n    #endif\n    return a;\n}\n"
        "#else\n/* ü */ static int two(int a) { return a; }\nstatic int three(int a) { return a; }\n#endif\nstatic int broken( {\n"
    )

    def test_a_c_file_reads_only_the_lines_a_branch_adds_and_finds_what_they_define(self):
        parser = TestTheOtherBranchesWhenOnlyTheFirstWasRead._first_branches_read(CppParser())
        names = {f.name for f in parser.parse(self.C_TEXT, "x.c", language="c").functions}
        assert {"one", "two", "three"} <= names

    def test_lines_with_text_above_ascii_do_not_move_what_follows(self):
        parser = TestTheOtherBranchesWhenOnlyTheFirstWasRead._first_branches_read(CppParser())
        result = parser.parse(self.C_TEXT, "x.c", language="c")
        lines = self.C_TEXT.split("\n")
        for function in result.functions:
            assert function.body.split("\n")[0].strip() in lines[function.start_line - 1]

    def test_a_branch_that_adds_no_definition_is_not_parsed(self):
        text = "#if W\nint one(void) { return 1; }\n#else\nextern int counter;\nstruct S { int x; };\n#endif\nint broken( {\n"
        parser = TestTheOtherBranchesWhenOnlyTheFirstWasRead._first_branches_read(CppParser())
        real = parser._parser.parse
        parses = []
        parser._parser = type("Counting", (), {"parse": lambda self, data, *a, **k: (parses.append(1), real(data, *a, **k))[1]})()
        parser.parse(text, "x.c", language="c")
        assert len(parses) == 2  # the file, and the first-branch text the forced fallback makes: not a third for the #else

    def test_what_a_branch_adds_is_what_the_first_reading_does_not_have(self):
        added = CppParser._lines_only_in("a\nb\n \nd", "a\n \n \n ")
        assert added == [(2, "b"), (4, "d")]

    def test_a_blanked_line_has_the_bytes_of_the_line(self):
        from drspec.parsers.cpp_parser import _blank

        assert len(_blank("café").encode()) == len("café".encode())


class TestDeclarationsOfTheOtherBranchesOfAC:
    TEXT = "#if W\nint one(void) { return 1; }\n#else\nextern __inline int two(int);\n#endif\nint broken( {\n"

    def test_a_c_file_reads_the_declaration_an_else_branch_adds(self):
        parser = TestTheOtherBranchesWhenOnlyTheFirstWasRead._first_branches_read(CppParser())
        found = {(f.name, f.start_line) for f in parser.parse(self.TEXT, "x.h", language="c").functions}  # (a header: its declarations are functions)
        assert ("one", 2) in found and ("two", 4) in found

    def test_a_branch_with_only_a_variable_or_a_type_is_not_read(self):
        parser = CppParser()
        added = [(4, "extern int counter;"), (5, "struct S { int x; };")]
        assert parser._may_define(added, [], declarations=True) is False
        assert parser._may_define([(4, "extern int two(int);")], [], declarations=True) is True
        assert parser._may_define([(4, "extern int two(int);")], [], declarations=False) is False


HEADER_ONLY_LIBRARY = """#ifndef LIB_H
#define LIB_H
#define LIBDEF extern

LIBDEF unsigned char *lib_load(const char *name, int *w);
LIBDEF void lib_free(void *data);
LIBDEF void lib_flip_on_load(int flag);
// only with thread-local storage
LIBDEF void lib_flip_on_load_thread(int flag);
LIBDEF int lib_only_declared(int a);

#endif

#ifdef LIB_IMPLEMENTATION
LIBDEF unsigned char *lib_load(const char *name, int *w) { return 0; }
LIBDEF void lib_free(void *data) { }

#ifndef LIB_THREAD_LOCAL
void lib_flip_on_load(int flag) { }
#else
void lib_flip_on_load(int flag) { }
LIBDEF void lib_flip_on_load_thread(int flag) { }
#endif
#endif
"""


class TestAHeaderOnlyLibraryWithMacroPrefixedPrototypes:
    """`STBIDEF ...;` of `stb_image.h`: the functions are indexed, as the definitions that follow, and a prototype with no
    definition stays a declaration (§70.5 of the design notes asked whether they are lost)."""

    def test_every_function_of_the_library_is_found(self, root):
        result = _scan(root, "lib.h", HEADER_ONLY_LIBRARY)
        names = {f.name for f in result.functions}
        assert {"lib_load", "lib_free", "lib_flip_on_load", "lib_flip_on_load_thread", "lib_only_declared"} <= names
        assert result.skipped_functions == []

    def test_one_with_a_definition_is_that_definition_and_one_without_is_a_declaration(self, root):
        result = _scan(root, "lib.h", HEADER_ONLY_LIBRARY)
        kinds = {}
        for f in result.functions:
            kinds.setdefault(f.name, set()).add("declaration" if "declaration" in f.decorators else "definition")
        assert kinds["lib_load"] == {"definition"} and kinds["lib_flip_on_load_thread"] == {"definition"}
        assert kinds["lib_only_declared"] == {"declaration"}

    def test_the_definition_in_the_other_branch_is_the_one_found_for_the_thread_variant(self, root):
        result = _scan(root, "lib.h", HEADER_ONLY_LIBRARY)
        (found,) = [f for f in result.functions if f.name == "lib_flip_on_load_thread" and "declaration" not in f.decorators]
        assert found.start_line == 22 and "{ }" in found.body
