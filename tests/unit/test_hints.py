"""Tests for the hint detection module."""

import pytest

from drspec.core.hints import (
    Hint,
    HintType,
    extract_hints,
    extract_hints_simple,
    hints_to_json,
)


class TestHintType:
    """Tests for HintType enum."""

    def test_hint_type_values(self):
        """Test HintType enum values."""
        assert HintType.INVARIANT.value == "invariant"
        assert HintType.PRE.value == "pre"
        assert HintType.POST.value == "post"
        assert HintType.REQUIRES.value == "requires"


class TestHint:
    """Tests for Hint dataclass."""

    def test_hint_creation(self):
        """Test creating a Hint."""
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
        """Test converting Hint to dict."""
        hint = Hint(
            line=5,
            type=HintType.PRE,
            text="x > 0",
            raw="# @pre: x > 0",
        )
        d = hint.to_dict()
        assert d["line"] == 5
        assert d["type"] == "pre"
        assert d["text"] == "x > 0"
        assert "raw" not in d  # raw is not included in dict output


class TestExtractHintsPython:
    """Tests for extracting hints from Python code."""

    def test_single_invariant_hash_comment(self):
        """Test detecting single @invariant in hash comment."""
        body = """def foo(x):
    # @invariant: x must be positive
    return x * 2
"""
        hints = extract_hints(body, start_line=1, language="python")
        assert len(hints) == 1
        assert hints[0].line == 2
        assert hints[0].type == HintType.INVARIANT
        assert hints[0].text == "x must be positive"

    def test_invariant_with_colon(self):
        """Test @invariant: format."""
        body = "# @invariant: result is never None"
        hints = extract_hints(body, start_line=1, language="python")
        assert len(hints) == 1
        assert hints[0].text == "result is never None"

    def test_invariant_without_colon(self):
        """Test @invariant format without colon."""
        body = "# @invariant result is never None"
        hints = extract_hints(body, start_line=1, language="python")
        assert len(hints) == 1
        assert hints[0].text == "result is never None"

    def test_pre_condition(self):
        """Test @pre annotation."""
        body = "# @pre: x > 0 and y > 0"
        hints = extract_hints(body, start_line=1, language="python")
        assert len(hints) == 1
        assert hints[0].type == HintType.PRE
        assert hints[0].text == "x > 0 and y > 0"

    def test_post_condition(self):
        """Test @post annotation."""
        body = "# @post: len(result) <= len(input)"
        hints = extract_hints(body, start_line=1, language="python")
        assert len(hints) == 1
        assert hints[0].type == HintType.POST
        assert hints[0].text == "len(result) <= len(input)"

    def test_requires_annotation(self):
        """Test @requires annotation."""
        body = "# @requires: user.is_authenticated()"
        hints = extract_hints(body, start_line=1, language="python")
        assert len(hints) == 1
        assert hints[0].type == HintType.REQUIRES
        assert hints[0].text == "user.is_authenticated()"

    def test_multiple_hints(self):
        """Test detecting multiple hints."""
        body = """def foo(x, y):
    # @pre: x > 0
    # @pre: y > 0
    # @post: result > 0
    # @invariant: no division by zero
    return x / y
"""
        hints = extract_hints(body, start_line=1, language="python")
        assert len(hints) == 4
        assert hints[0].type == HintType.PRE
        assert hints[1].type == HintType.PRE
        assert hints[2].type == HintType.POST
        assert hints[3].type == HintType.INVARIANT

    def test_docstring_hints(self):
        """Test hints inside docstrings."""
        body = '''def foo(x):
    """Process a value.

    @invariant: result is always normalized
    @pre: x is not empty
    """
    return normalize(x)
'''
        hints = extract_hints(body, start_line=1, language="python")
        assert len(hints) == 2
        assert hints[0].type == HintType.INVARIANT
        assert hints[1].type == HintType.PRE

    def test_case_insensitive(self):
        """Test that hint detection is case-insensitive."""
        body = """# @INVARIANT: uppercase
# @Invariant: mixed case
# @invariant: lowercase
"""
        hints = extract_hints(body, start_line=1, language="python")
        assert len(hints) == 3
        for hint in hints:
            assert hint.type == HintType.INVARIANT

    def test_start_line_offset(self):
        """Test that line numbers use start_line offset."""
        body = """# line 1
# @invariant: on line 2 of body
# line 3
"""
        hints = extract_hints(body, start_line=100, language="python")
        assert len(hints) == 1
        assert hints[0].line == 101  # 100 + 1 (second line of body)


class TestExtractHintsJavaScript:
    """Tests for extracting hints from JavaScript code."""

    def test_single_line_comment(self):
        """Test detecting @invariant in // comment."""
        body = """function foo(x) {
    // @invariant: x must be positive
    return x * 2;
}
"""
        hints = extract_hints(body, start_line=1, language="javascript")
        assert len(hints) == 1
        assert hints[0].line == 2
        assert hints[0].type == HintType.INVARIANT
        assert hints[0].text == "x must be positive"

    def test_block_comment(self):
        """Test detecting @invariant in block comment."""
        body = """/* @invariant: value is normalized */
function normalize(v) { return v; }
"""
        hints = extract_hints(body, start_line=1, language="javascript")
        assert len(hints) == 1
        assert hints[0].text == "value is normalized"

    def test_jsdoc_style(self):
        """Test detecting @invariant in JSDoc style."""
        body = """/**
 * Process a value.
 * @invariant: result is always positive
 * @requires: x !== null
 */
function process(x) { return x; }
"""
        hints = extract_hints(body, start_line=1, language="javascript")
        assert len(hints) == 2
        assert hints[0].type == HintType.INVARIANT
        assert hints[1].type == HintType.REQUIRES


class TestExtractHintsCpp:
    """Tests for extracting hints from C++ code."""

    def test_single_line_comment(self):
        """Test detecting @invariant in // comment."""
        body = """int foo(int x) {
    // @invariant: x must be positive
    return x * 2;
}
"""
        hints = extract_hints(body, start_line=1, language="cpp")
        assert len(hints) == 1
        assert hints[0].type == HintType.INVARIANT

    def test_block_comment(self):
        """Test detecting @invariant in block comment."""
        body = """/* @pre: ptr != nullptr */
void process(int* ptr) { }
"""
        hints = extract_hints(body, start_line=1, language="cpp")
        assert len(hints) == 1
        assert hints[0].type == HintType.PRE

    def test_doxygen_style(self):
        """Test detecting @invariant in Doxygen style."""
        body = """/**
 * @brief Process a value
 * @invariant: result > 0
 * @post: state is updated
 */
int process(int x);
"""
        hints = extract_hints(body, start_line=1, language="cpp")
        assert len(hints) == 2
        assert hints[0].type == HintType.INVARIANT
        assert hints[1].type == HintType.POST


class TestExtractHintsNoLanguage:
    """Tests for extracting hints without specifying language."""

    def test_auto_detect_python(self):
        """Test that Python-style hints work without language."""
        body = "# @invariant: works without language"
        hints = extract_hints(body, start_line=1)
        assert len(hints) == 1
        assert hints[0].text == "works without language"

    def test_auto_detect_javascript(self):
        """Test that JS-style hints work without language."""
        body = "// @invariant: works without language"
        hints = extract_hints(body, start_line=1)
        assert len(hints) == 1
        assert hints[0].text == "works without language"

    def test_mixed_styles(self):
        """Test both Python and JS styles work together."""
        body = """# @invariant: python style
// @invariant: js style
"""
        hints = extract_hints(body, start_line=1)
        assert len(hints) == 2


class TestExtractHintsSimple:
    """Tests for extract_hints_simple function."""

    def test_returns_dicts(self):
        """Test that extract_hints_simple returns dicts."""
        body = """# @invariant: test hint
# @pre: another hint
"""
        hints = extract_hints_simple(body, start_line=1)
        assert len(hints) == 2
        assert isinstance(hints[0], dict)
        assert hints[0]["line"] == 1
        assert hints[0]["type"] == "invariant"
        assert hints[0]["text"] == "test hint"


class TestHintsToJson:
    """Tests for hints_to_json function."""

    def test_converts_hints_to_dicts(self):
        """Test converting Hint objects to JSON-serializable dicts."""
        hints = [
            Hint(line=1, type=HintType.INVARIANT, text="hint 1", raw=""),
            Hint(line=2, type=HintType.PRE, text="hint 2", raw=""),
        ]
        result = hints_to_json(hints)
        assert len(result) == 2
        assert result[0]["type"] == "invariant"
        assert result[1]["type"] == "pre"

    def test_empty_list(self):
        """Test converting empty list."""
        result = hints_to_json([])
        assert result == []


class TestEdgeCases:
    """Tests for edge cases in hint detection."""

    def test_no_hints(self):
        """Test body with no hints."""
        body = """def foo(x):
    # Just a regular comment
    return x
"""
        hints = extract_hints(body, start_line=1)
        assert len(hints) == 0

    def test_empty_body(self):
        """Test empty body."""
        hints = extract_hints("", start_line=1)
        assert len(hints) == 0

    def test_whitespace_only(self):
        """Test whitespace-only body."""
        hints = extract_hints("   \n   \n   ", start_line=1)
        assert len(hints) == 0

    def test_hint_with_extra_whitespace(self):
        """Test hint with extra whitespace."""
        body = "#   @invariant:    spaced out hint   "
        hints = extract_hints(body, start_line=1)
        assert len(hints) == 1
        assert hints[0].text == "spaced out hint"

    def test_duplicate_hints_removed(self):
        """Test that duplicate hints on same line are removed."""
        # This could happen if multiple patterns match the same text
        body = "# @invariant: test @invariant: test"
        hints = extract_hints(body, start_line=1)
        # Should have at most 2 (one for each match) but not more due to dedup
        assert len(hints) <= 2

    def test_similar_text_different_lines(self):
        """Test that same text on different lines is preserved."""
        body = """# @invariant: same text
# other comment
# @invariant: same text
"""
        hints = extract_hints(body, start_line=1)
        assert len(hints) == 2
        assert hints[0].line == 1
        assert hints[1].line == 3

    def test_hints_sorted_by_line(self):
        """Test that hints are sorted by line number."""
        body = """# line 1
# @post: later
# line 3
# @pre: earlier
"""
        hints = extract_hints(body, start_line=1)
        assert len(hints) == 2
        assert hints[0].line < hints[1].line
