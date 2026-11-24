"""The two scripts that check the parsers against real clones (`scripts/corpus_check_c.py`, `scripts/corpus_check_cpp.py`).

No test reads a clone: the scripts are run by hand when a parser changes. What is tested here is their own logic, on made-up
input and on a small folder made in `tmp_path`: the rule that finds a function swallowing others or starting in the wrong place, the
filter that picks the definitions with a body, the share of the compiler's functions found, and the recorded answers they read.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "c"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


corpus_check_c = _load("corpus_check_c")
corpus_check_cpp = _load("corpus_check_cpp")


class TestTheSweepRule:
    def test_a_clean_file_has_no_function_swallowing_others_or_misplaced(self):
        from drspec.core.scanner import Scanner

        text = (FIXTURES / "constructs.c").read_text()
        result = Scanner().scan_file(str(FIXTURES / "constructs.c"), relative_to=FIXTURES)
        assert corpus_check_c.swallowing_and_misplaced(result.functions, text.split("\n")) == ([], [])

    def test_a_function_that_holds_four_others_over_three_hundred_lines_is_swallowing(self):
        big = SimpleNamespace(name="big", start_line=1, end_line=400, body="int big() {")
        inner = [SimpleNamespace(name=f"f{i}", start_line=10 + i, end_line=11 + i, body=f"int f{i}() {{") for i in range(4)]
        lines = ["int big() {"] + [f"int f{i}() {{" for i in range(4)] + [""] * 400
        swallowing, _ = corpus_check_c.swallowing_and_misplaced([big, *inner], lines)
        assert swallowing == [("big", 4)]

    def test_three_others_or_three_hundred_lines_are_not_enough(self):
        few = SimpleNamespace(name="few", start_line=1, end_line=400, body="int few() {")
        short = SimpleNamespace(name="short", start_line=1, end_line=100, body="int short() {")
        inner = [SimpleNamespace(name=f"f{i}", start_line=10 + i, end_line=11 + i, body=f"int f{i}() {{") for i in range(4)]
        lines = ["int few() {"] + [f"int f{i}() {{" for i in range(4)] + [""] * 400
        assert corpus_check_c.swallowing_and_misplaced([few, *inner[:3]], lines)[0] == []  # three others
        assert corpus_check_c.swallowing_and_misplaced([short, *inner], lines)[0] == []  # a hundred lines

    def test_a_function_whose_text_is_not_where_it_says_is_misplaced(self):
        function = SimpleNamespace(name="g", start_line=10, end_line=12, body="int g() {\n}")
        assert corpus_check_c.swallowing_and_misplaced([function], ["unrelated"] * 20)[1] == [("g", 10)]

    def test_the_text_may_start_up_to_three_lines_earlier_with_the_macros_in_front(self):
        function = SimpleNamespace(name="g", start_line=10, end_line=12, body="int g() {\n}")
        lines = ["unrelated"] * 6 + ["int g() {"] + ["unrelated"] * 13  # the first line of the body is on line 7, three above 10
        assert corpus_check_c.swallowing_and_misplaced([function], lines)[1] == []


class TestTheDefinitionFilterOfTheCppCheck:
    SOURCE = (
        "// void commented() { }\n"
        "int plain(int a) { return a; }\n"
        "struct S { int method() const { return 1; } void decl(); };\n"
        "void S::decl() { }\n"
        "int only_declared(int);\n"
        "static int helper(int v) noexcept { return v; }\n"
        "int calls() { return helper(1) + plain(2); }\n"
    )
    NAMES = ("plain", "method", "decl", "only_declared", "helper", "calls", "commented", "missing", "operator()", "")

    def test_the_indexed_filter_gives_the_same_names_as_the_search_per_name(self):
        indexed = corpus_check_cpp.explicit_definitions(self.SOURCE, self.NAMES)
        assert indexed == corpus_check_cpp.explicit_definitions_one_search_per_name(self.SOURCE, self.NAMES)

    def test_a_name_that_is_only_declared_or_commented_is_not_a_definition(self):
        found = corpus_check_cpp.explicit_definitions(self.SOURCE, self.NAMES)
        assert {"plain", "method", "decl", "helper", "calls"} <= found
        assert not ({"only_declared", "commented", "missing", "operator()", ""} & found)


@pytest.fixture
def clone(tmp_path):
    """A made-up clone of raylib: `src/a.c` with two functions, `src/big.h` with a function that swallows others, and a header with one
    function the parser cannot read."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.c").write_text("int f(void) { return 1; }\nint g(void) { return 2; }\n")
    return tmp_path


class TestTheCompilerShare:
    def test_the_share_counts_what_the_recorded_answer_defines_and_drspec_finds(self, clone):
        recorded = {
            "src": {"files": ["src/a.c"], "defined": [["a.c", "f"], ["a.c", "g"], ["a.c", "missing"]]},
            "examples": {"files": [], "defined": []},
        }
        share, missed = corpus_check_c.compiler_share(clone, recorded)
        assert share == pytest.approx(2 / 3) and missed == [("a.c", "missing")]

    def test_the_sweep_lists_a_file_with_nothing_wrong_as_clean(self, clone):
        result = corpus_check_c.sweep(clone)
        assert result == {"swallowing": [], "misplaced": [], "skipped": {}}

    def test_the_exit_status_is_one_when_the_share_is_under_the_minimum(self, clone, tmp_path, monkeypatch, capsys):
        recorded = {
            "src": {"files": ["src/a.c"], "defined": [["a.c", "f"], ["a.c", "missing"]]},
            "examples": {"files": [], "defined": []},
        }
        answer = tmp_path / "answer.json"
        answer.write_text(json.dumps(recorded))
        monkeypatch.setattr(corpus_check_c, "RECORDED", answer)
        assert corpus_check_c.main([str(clone), "--skip-sweep"]) == 1
        assert "lost  a.c  missing" in capsys.readouterr().out


class TestTheRecordedAnswers:
    def test_the_raylib_record_has_the_files_and_pairs_the_check_reads(self):
        recorded = json.loads(corpus_check_c.RECORDED.read_text())
        for folder in ("src", "examples"):
            assert recorded[folder]["files"] and all(len(pair) == 2 for pair in recorded[folder]["defined"])

    def test_the_imgui_record_has_the_defined_names_of_the_five_files(self):
        recorded = json.loads(corpus_check_cpp.RECORDED.read_text())
        assert set(corpus_check_cpp.IMGUI_FILES) <= set(recorded)
        assert all(recorded[name]["defined"] and set(recorded[name]["explicit"]) <= set(recorded[name]["defined"]) for name in corpus_check_cpp.IMGUI_FILES)
