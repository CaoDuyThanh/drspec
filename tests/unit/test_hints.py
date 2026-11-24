"""Tests for the hint detection module: which `@invariant` / `@pre` / `@post` / `@requires` lines of a function body are hints, and at which line."""

import pytest

from drspec.core.hints import (
    Hint,
    HintType,
    extract_hints,
    extract_hints_simple,
    hints_to_json,
)

pytestmark = pytest.mark.unit  # text in, hints out


def _found(hints):
    """What was found, in order: (line, type name, text)."""
    return [(h.line, h.type.value, h.text) for h in hints]


class TestHintType:
    """Tests for HintType enum."""

    def test_hint_type_values(self):
        assert HintType.INVARIANT.value == "invariant"
        assert HintType.PRE.value == "pre"
        assert HintType.POST.value == "post"
        assert HintType.REQUIRES.value == "requires"
        assert [t.value for t in HintType] == ["invariant", "pre", "post", "requires"]  # no other kind


class TestHint:
    """Tests for Hint dataclass."""

    def test_hint_creation(self):
        hint = Hint(
            line=10,
            type=HintType.INVARIANT,
            text="value must be positive",
            raw="# @invariant: value must be positive",
        )
        assert hint.line == 10
        assert hint.type == HintType.INVARIANT
        assert hint.text == "value must be positive"
        assert hint.raw == "# @invariant: value must be positive"

    def test_hint_to_dict(self):
        hint = Hint(
            line=5,
            type=HintType.PRE,
            text="x > 0",
            raw="# @pre: x > 0",
        )
        assert hint.to_dict() == {"line": 5, "type": "pre", "text": "x > 0"}  # raw is not included in dict output


class TestExtractHintsPython:
    """Tests for extracting hints from Python code."""

    def test_single_invariant_hash_comment(self):
        body = """def foo(x):
    # @invariant: x must be positive
    return x * 2
"""
        hints = extract_hints(body, start_line=1, language="python")
        assert _found(hints) == [(2, "invariant", "x must be positive")]
        assert hints[0].raw == "# @invariant: x must be positive"  # the line as written, without its indentation

    def test_invariant_with_colon(self):
        body = "# @invariant: result is never None"
        hints = extract_hints(body, start_line=1, language="python")
        assert _found(hints) == [(1, "invariant", "result is never None")]

    def test_invariant_without_colon(self):
        body = "# @invariant result is never None"
        hints = extract_hints(body, start_line=1, language="python")
        assert _found(hints) == [(1, "invariant", "result is never None")]

    def test_pre_condition(self):
        body = "# @pre: x > 0 and y > 0"
        hints = extract_hints(body, start_line=1, language="python")
        assert _found(hints) == [(1, "pre", "x > 0 and y > 0")]

    def test_post_condition(self):
        body = "# @post: len(result) <= len(input)"
        hints = extract_hints(body, start_line=1, language="python")
        assert _found(hints) == [(1, "post", "len(result) <= len(input)")]

    def test_requires_annotation(self):
        body = "# @requires: user.is_authenticated()"
        hints = extract_hints(body, start_line=1, language="python")
        assert _found(hints) == [(1, "requires", "user.is_authenticated()")]

    def test_multiple_hints(self):
        body = """def foo(x, y):
    # @pre: x > 0
    # @pre: y > 0
    # @post: result > 0
    # @invariant: no division by zero
    return x / y
"""
        hints = extract_hints(body, start_line=1, language="python")
        assert _found(hints) == [
            (2, "pre", "x > 0"),
            (3, "pre", "y > 0"),
            (4, "post", "result > 0"),
            (5, "invariant", "no division by zero"),
        ]

    def test_docstring_hints(self):
        body = '''def foo(x):
    """Process a value.

    @invariant: result is always normalized
    @pre: x is not empty
    """
    return normalize(x)
'''
        hints = extract_hints(body, start_line=1, language="python")
        assert _found(hints) == [(4, "invariant", "result is always normalized"), (5, "pre", "x is not empty")]

    def test_case_insensitive(self):
        body = """# @INVARIANT: uppercase
# @Invariant: mixed case
# @invariant: lowercase
"""
        hints = extract_hints(body, start_line=1, language="python")
        assert _found(hints) == [(1, "invariant", "uppercase"), (2, "invariant", "mixed case"), (3, "invariant", "lowercase")]

    def test_start_line_offset(self):
        body = """# line 1
# @invariant: on line 2 of body
# line 3
"""
        hints = extract_hints(body, start_line=100, language="python")
        assert _found(hints) == [(101, "invariant", "on line 2 of body")]  # 100 + 1 (second line of body)


class TestExtractHintsJavaScript:
    """Tests for extracting hints from JavaScript code."""

    def test_single_line_comment(self):
        body = """function foo(x) {
    // @invariant: x must be positive
    return x * 2;
}
"""
        hints = extract_hints(body, start_line=1, language="javascript")
        assert _found(hints) == [(2, "invariant", "x must be positive")]

    def test_block_comment(self):
        body = """/* @invariant: value is normalized */
function normalize(v) { return v; }
"""
        hints = extract_hints(body, start_line=1, language="javascript")
        assert _found(hints) == [(1, "invariant", "value is normalized")]  # the closing `*/` is not part of the text

    def test_jsdoc_style(self):
        body = """/**
 * Process a value.
 * @invariant: result is always positive
 * @requires: x !== null
 */
function process(x) { return x; }
"""
        hints = extract_hints(body, start_line=1, language="javascript")
        assert _found(hints) == [(3, "invariant", "result is always positive"), (4, "requires", "x !== null")]


class TestExtractHintsCpp:
    """Tests for extracting hints from C++ code."""

    def test_single_line_comment(self):
        body = """int foo(int x) {
    // @invariant: x must be positive
    return x * 2;
}
"""
        hints = extract_hints(body, start_line=1, language="cpp")
        assert _found(hints) == [(2, "invariant", "x must be positive")]

    def test_block_comment(self):
        body = """/* @pre: ptr != nullptr */
void process(int* ptr) { }
"""
        hints = extract_hints(body, start_line=1, language="cpp")
        assert _found(hints) == [(1, "pre", "ptr != nullptr")]

    def test_doxygen_style(self):
        body = """/**
 * @brief Process a value
 * @invariant: result > 0
 * @post: state is updated
 */
int process(int x);
"""
        hints = extract_hints(body, start_line=1, language="cpp")
        assert _found(hints) == [(3, "invariant", "result > 0"), (4, "post", "state is updated")]  # `@brief` is not a hint


class TestExtractHintsNoLanguage:
    """Tests for extracting hints without specifying language."""

    def test_auto_detect_python(self):
        body = "# @invariant: works without language"
        hints = extract_hints(body, start_line=1)
        assert _found(hints) == [(1, "invariant", "works without language")]

    def test_auto_detect_javascript(self):
        body = "// @invariant: works without language"
        hints = extract_hints(body, start_line=1)
        assert _found(hints) == [(1, "invariant", "works without language")]

    def test_mixed_styles(self):
        body = """# @invariant: python style
// @invariant: js style
"""
        hints = extract_hints(body, start_line=1)
        assert _found(hints) == [(1, "invariant", "python style"), (2, "invariant", "js style")]


class TestExtractHintsSimple:
    """Tests for extract_hints_simple function."""

    def test_returns_dicts(self):
        body = """# @invariant: test hint
# @pre: another hint
"""
        hints = extract_hints_simple(body, start_line=1)
        assert hints == [{"line": 1, "type": "invariant", "text": "test hint"}, {"line": 2, "type": "pre", "text": "another hint"}]


class TestHintsToJson:
    """Tests for hints_to_json function."""

    def test_converts_hints_to_dicts(self):
        hints = [
            Hint(line=1, type=HintType.INVARIANT, text="hint 1", raw=""),
            Hint(line=2, type=HintType.PRE, text="hint 2", raw=""),
        ]
        result = hints_to_json(hints)
        assert result == [{"line": 1, "type": "invariant", "text": "hint 1"}, {"line": 2, "type": "pre", "text": "hint 2"}]

    def test_empty_list(self):
        result = hints_to_json([])
        assert result == []


class TestEdgeCases:
    """Tests for edge cases in hint detection."""

    def test_no_hints(self):
        body = """def foo(x):
    # Just a regular comment
    return x
"""
        hints = extract_hints(body, start_line=1)
        assert hints == []


    def test_empty_body(self):
        assert extract_hints("", start_line=1) == []

    def test_whitespace_only(self):
        assert extract_hints("   \n   \n   ", start_line=1) == []

    def test_hint_with_extra_whitespace(self):
        body = "#   @invariant:    spaced out hint   "
        assert _found(extract_hints(body, start_line=1)) == [(1, "invariant", "spaced out hint")]  # trimmed on both sides

    def test_one_hint_per_line_the_rest_of_the_line_is_its_text(self):
        body = "# @invariant: test @invariant: test"
        hints = extract_hints(body, start_line=1)
        assert _found(hints) == [(1, "invariant", "test @invariant: test")]

    def test_similar_text_different_lines(self):
        body = """# @invariant: same text
# other comment
# @invariant: same text
"""
        hints = extract_hints(body, start_line=1)
        assert _found(hints) == [(1, "invariant", "same text"), (3, "invariant", "same text")]  # same text on two lines is two hints

    def test_hints_sorted_by_line(self):
        body = """# line 1
# @post: later
# line 3
# @pre: earlier
"""
        hints = extract_hints(body, start_line=1)
        assert _found(hints) == [(2, "post", "later"), (4, "pre", "earlier")]  # in the order of the lines, not of the kinds
