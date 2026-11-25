"""Macros the project defines to decorate declarations: told apart by their definition, not by how the name looks."""

from __future__ import annotations

from pathlib import Path

import pytest

from drspec.parsers.cpp_defined_macros import (
    MAX_INCLUDE_DEPTH,
    blank_decorations,
    decoration_macros,
)
from drspec.parsers.cpp_parser import CppParser

pytestmark = pytest.mark.unit  # small source files in tmp_path, read by the real C++ parser


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _parse(root: Path, text: str, name: str = "t.cpp"):
    (root / name).write_text(text)
    return CppParser().parse_file(name)


def _names(result) -> list:
    return sorted(f.name for f in result.functions)


class TestADefinedMacroDoesNotCostTheFunctionsAroundIt:
    @pytest.mark.parametrize(
        "source, expected",
        [
            # a format attribute after the parameter list, the macro defined in the file
            ('#define IM_FMTARGS(x) __attribute__((format(printf, x, x+1)))\nstruct T { void AppendF(const char* fmt, ...) IM_FMTARGS(2); void Other(){ } };\n', ["AppendF", "Other"]),
            # the same under a short name that looks like nothing in particular
            ('#define FMT(x) __attribute__((format(printf, x, x+1)))\nstruct T { void AppendF(const char* fmt, ...) FMT(2); void Other(){ } };\n', ["AppendF", "Other"]),
            ("#define API\nclass API Cls { public: int m(){ return 1; } };\n", ["m"]),
            ("#define API\nstruct API Cls { int m(){ return 1; } };\n", ["m"]),
            ('#define EXPORT __attribute__((visibility("default")))\nclass EXPORT Widget { public: int size() const { return 1; } };\n', ["size"]),
            ("#define MY_API\nclass MY_API Cls3 { public: int m(){ return 1; } };\n", ["m"]),
            ("#define IMGUI_API __declspec(dllexport)\nstruct IMGUI_API S { int n(){ return 2; } };\n", ["n"]),
            # as a qualifier of a function definition
            ("#define HOT __attribute__((hot))\nHOT int fast(int x) { return x; }\n", ["fast"]),
        ],
    )
    def test_the_method_is_found_and_the_file_reads_cleanly(self, root, source, expected):
        result = _parse(root, source + "int after() { return 1; }\n")
        assert _names(result) == sorted(expected + ["after"])
        assert not result.has_errors, result.errors

    @pytest.mark.parametrize(
        "source, expected",
        [
            ("class DLLEXPORT Cls4 { public: int m(){ return 1; } };\n", ["m"]),
            ("struct IMGUI_API ImGuiStorage { int n(){ return 2; } };\n", ["n"]),
        ],
    )
    def test_an_unknown_name_keeps_its_old_handling(self, root, source, expected):
        result = _parse(root, source + "int after() { return 1; }\n")
        assert _names(result) == sorted(expected + ["after"]) and not result.has_errors


    def test_the_stored_signature_keeps_the_macro(self, root):
        result = _parse(root, "#define FASTCALLISH __attribute__((hot))\nFASTCALLISH int fast(int x) { return x + 1; }\n")
        (function,) = result.functions
        assert function.signature == function.body == "FASTCALLISH int fast(int x) { return x + 1; }"  # the text as written, the macro included
        assert (function.name, function.start_line, function.end_line) == ("fast", 2, 2)

    def test_the_macro_stays_in_a_body_that_mentions_it(self, root):
        result = _parse(root, '#define API\nclass API C { public: const char* name() { return "API"; } // API\n};\n')
        (function,) = result.functions
        assert function.body == 'const char* name() { return "API"; }'  # the string "API" is not blanked
        assert function.qualified_name == "C::name"

    def test_a_macro_that_expands_to_code_is_not_blanked(self, root):
        source = "#define SQUARE(x) ((x) * (x))\n#define LIMIT 10\nint area() { return SQUARE(LIMIT); }\n"
        result = _parse(root, source)
        (function,) = result.functions
        assert function.body == "int area() { return SQUARE(LIMIT); }" and not result.has_errors

    def test_a_macro_defined_in_an_included_header_counts(self, root):
        (root / "lib.h").write_text("#define LIB\n")
        result = _parse(root, '#include "lib.h"\nclass LIB Cls { public: int m(){ return 1; } };\nint after(){ return 1; }\n')
        assert _names(result) == ["after", "m"] and not result.has_errors

    def test_a_macro_defined_in_a_header_of_a_header_counts(self, root):
        (root / "base.h").write_text("#define LIB\n")
        (root / "mid.h").write_text('#include "base.h"\n')
        result = _parse(root, '#include "mid.h"\nclass LIB Cls { public: int m(){ return 1; } };\n')
        assert _names(result) == ["m"] and not result.has_errors

    def test_a_header_in_the_folder_of_the_file_is_found(self, root):
        (root / "sub").mkdir()
        (root / "sub" / "lib.h").write_text("#define LIB\n")
        (root / "sub" / "a.cpp").write_text('#include "lib.h"\nclass LIB Cls { public: int m(){ return 1; } };\n')
        assert _names(CppParser().parse_file("sub/a.cpp")) == ["m"]

    def test_the_define_line_itself_is_left_alone(self, root):
        # blanking the name being defined once left a bare `#define`, and the next line was read as part of it
        result = _parse(root, "#define EXPORT __attribute__((visibility(\"default\")))\nint first() { return 1; }\nint second() { return 2; }\n")
        assert _names(result) == ["first", "second"] and not result.has_errors

    def test_a_missing_header_is_not_an_error(self, root):
        result = _parse(root, '#include "nowhere.h"\n#include <vector>\nint f() { return 1; }\n')
        assert _names(result) == ["f"] and not result.has_errors


class TestWhichDefinitionsDecorate:
    def _found(self, root: Path, text: str) -> dict:
        (root / "d.cpp").write_text(text)
        return decoration_macros(text, "d.cpp")

    def test_empty_and_attribute_macros(self, root):
        found = self._found(root, '#define A\n#define B __attribute__((x))\n#define C __declspec(dllexport)\n#define D(x) __attribute__((format(printf, x, 0)))\n')
        assert found == {"A": False, "B": False, "C": False, "D": True}

    def test_calling_conventions_and_inline_spellings(self, root):
        found = self._found(root, "#define CC __cdecl\n#define FI __forceinline\n#define NR __attribute__((noreturn))\n")
        assert found == {"CC": False, "FI": False, "NR": False}  # none of them takes arguments

    def test_a_macro_of_code_or_a_value_does_not(self, root):
        found = self._found(root, '#define V 10\n#define S "text"\n#define M(a, b) ((a) < (b) ? (a) : (b))\n#define BLOCK { x = 1; }\n#define TYPE int\n')
        assert found == {}


    def test_an_empty_function_like_macro_is_a_statement_in_disguise_and_stays(self, root):
        assert self._found(root, "#define LOG(x)\n#define TRACE(...)\n") == {}

    def test_every_definition_has_to_decorate(self, root):
        text = "#ifdef A\n#define MIXED __declspec(dllexport)\n#else\n#define MIXED 1\n#endif\n#ifdef A\n#define BOTH __declspec(dllexport)\n#else\n#define BOTH\n#endif\n"
        assert self._found(root, text) == {"BOTH": False}

    def test_one_that_is_object_like_in_one_place_and_function_like_in_another_is_left_out(self, root):
        assert self._found(root, "#define X\n#define X(a) __attribute__((a))\n") == {}

    def test_a_decoration_made_of_another(self, root):
        found = self._found(root, "#define BASE __declspec(dllexport)\n#define OUTER BASE\n")
        assert found == {"BASE": False, "OUTER": False}

    def test_a_keyword_is_never_taken(self, root):
        assert self._found(root, "#define const\n#define class\n") == {}

    def test_a_continued_definition(self, root):
        assert self._found(root, "#define LONG \\\n    __attribute__((visibility(\"default\"))) \\\n    __attribute__((used))\n") == {"LONG": False}

    def test_the_include_depth_is_bounded(self, root):
        names = [f"h{i}.h" for i in range(MAX_INCLUDE_DEPTH + 1)]  # one header more than is read
        for index, name in enumerate(names):
            nxt = f'#include "{names[index + 1]}"\n' if index + 1 < len(names) else "#define DEEP\n"
            (root / name).write_text(nxt)

        assert decoration_macros(f'#include "{names[0]}"\n', "d.cpp") == {}


class TestBlanking:
    def test_the_length_and_every_line_stay(self):
        text = "#define API\nclass API C {\n  API int f();\n};\n"
        out = blank_decorations(text, {"API": False})
        assert len(out) == len(text) and out.count("\n") == text.count("\n")
        assert "API" in out.split("\n")[0] and "API" not in "".join(out.split("\n")[1:])

    def test_strings_comments_and_directives_are_left_alone(self):
        text = '#ifdef API\n#endif\nconst char* s = "API"; // API\n/* API */ int API_x;\nint y = API_count;\n'
        assert blank_decorations(text, {"API": False}) == text

    def test_a_function_like_macro_goes_with_its_arguments_only_when_called(self):
        text = "void f() FMT(2);\nint FMT;\nint g = FMT + 1;\n"
        out = blank_decorations(text, {"FMT": True})
        lines = out.split("\n")
        assert "FMT" not in lines[0]
        assert "FMT" in lines[1] and "FMT" in lines[2]

    def test_an_object_like_macro_followed_by_a_parenthesis_is_not_a_decoration(self):
        text = "int a = API(3);\n"
        assert blank_decorations(text, {"API": False}) == text

    def test_nothing_defined_nothing_changed(self):
        assert blank_decorations("class API C {};\n", {}) == "class API C {};\n"
