"""Tests for the code hasher module.

Change detection hashes the EXACT source (`exact-source:v1`): every edit, even of a comment or of the whitespace, changes the hash, so that no change
can be missed. The format of the hash is a contract (a different format makes every stored hash and every contract look stale), so it is pinned here
against a hash computed by hand, not by the product."""

import hashlib

import pytest

from drspec.core.hasher import (
    compute_hash,
    normalize_code,
)

pytestmark = pytest.mark.unit  # strings in, strings out


def _by_hand(body: str, language: str) -> str:
    """What `compute_hash` must return, written out: SHA-256 of `drspec:exact-source:v1`, NUL, the language, NUL, the source (UTF-8)."""
    return hashlib.sha256(("drspec:exact-source:v1\0" + language + "\0" + body).encode("utf-8")).hexdigest()


class TestComputeHash:
    """Tests for hash computation."""


    def test_hash_is_64_lowercase_hex_characters(self):
        result = compute_hash("def foo(): return 1", "python")

        assert len(result) == 64
        assert set(result) <= set("0123456789abcdef")

    def test_same_code_same_hash(self):
        code = "def foo(): return 1"

        assert compute_hash(code, "python") == compute_hash(code, "python")

    def test_different_code_different_hash(self):
        assert compute_hash("def foo(): return 1", "python") != compute_hash("def foo(): return 2", "python")


#: (language, one text, the same text edited in a way a normalising hasher would ignore). Every pair must hash differently.
EDITS = [
    pytest.param("python", "def foo(): return 1", "  def foo(): return 1  ", id="python-leading-and-trailing-whitespace"),
    pytest.param("python", "def foo():\n    return 1", "def foo():\n        return 1", id="python-indentation"),
    pytest.param("python", "def foo():\n    return 1", "def foo():\n\n    return 1\n\n", id="python-blank-lines"),
    pytest.param("python", "def foo(a, b): return a + b", "def foo(a,  b):   return  a +  b", id="python-repeated-spaces"),
    pytest.param("python", "def foo():\n    return 1", "def foo():\n\treturn 1", id="python-tab-for-spaces"),
    pytest.param("python", "def foo():\n    return 1", "def foo():\r\n    return 1", id="python-crlf-for-lf"),
    pytest.param("python", "def foo(): return 1", "def foo(): return 1  # this is a comment", id="python-trailing-comment"),
    pytest.param("python", "def foo():\n    return 1", "def foo():\n    # a comment\n    return 1", id="python-full-line-comment"),
    pytest.param("python", "def foo():\n    return 1", 'def foo():\n    """This is a docstring."""\n    return 1', id="python-docstring"),
    pytest.param("python", "def foo():\n    return 1", 'def foo():\n    """\n    This is a multiline\n    docstring.\n    """\n    return 1', id="python-multiline-docstring"),
    pytest.param("python", "def foo():\n    return 1", "def foo():\n    '''Single quote docstring'''\n    return 1", id="python-single-quote-docstring"),
    pytest.param("python", 'def foo(): return "# not a comment"', 'def foo(): return "# not a comment"  # this is', id="python-comment-after-a-hash-in-a-string"),
    pytest.param("python", "def calculate(a, b):\n    return a + b", "def calculate(a, b):\n    return a - b", id="python-operator-changed"),
    pytest.param("python", "def calculate(a, b):\n    return a + b", "def calculate(x, y):\n    return x + y", id="python-variables-renamed"),
    pytest.param("javascript", "function foo() { return 1; }", "function foo() { return 1; } // comment", id="javascript-line-comment"),
    pytest.param("javascript", "function foo() { return 1; }", "function foo() { /* comment */ return 1; }", id="javascript-block-comment"),
    pytest.param("javascript", "function foo() {\n    return 1;\n}", "function foo() {\n    /*\n     * Multiline\n     * comment\n     */\n    return 1;\n}", id="javascript-multiline-comment"),
    pytest.param("javascript", 'function foo() { return "// not a comment"; }', 'function foo() { return "// not a comment"; } // real comment', id="javascript-comment-after-slashes-in-a-string"),
    pytest.param("javascript", "const x = `template // string`;", "const x = `template // string`; // comment", id="javascript-comment-after-a-template-string"),
    pytest.param("javascript", "function add(a, b) {\n    return a + b;\n}", "function add(a,b){\nreturn a+b;\n}", id="javascript-spacing-around-operators"),
    pytest.param("cpp", "void foo() { return; }", "void foo() { return; } // comment", id="cpp-line-comment"),
    pytest.param("cpp", "void foo() { return; }", "void foo() { /* comment */ return; }", id="cpp-block-comment"),
    pytest.param("cpp", 'void foo() { const char* s = "// not a comment"; }', 'void foo() { const char* s = "// not a comment"; } // real', id="cpp-comment-after-slashes-in-a-string"),
    pytest.param("cpp", "void process() {\n    doSomething();\n}", "void process() {\n    /* Process data */\n    doSomething(); // call handler\n}", id="cpp-block-and-line-comments"),
]


class TestEveryEditChangesTheHash:
    """Whatever the edit (a comment, a blank line, an indent, a rename), a changed source is a changed hash: change detection never normalises."""

    @pytest.mark.parametrize("language, before, after", EDITS)
    def test_the_edit_is_seen(self, language, before, after):
        assert compute_hash(before, language) != compute_hash(after, language)

    @pytest.mark.parametrize("language, code", [
        pytest.param("python", 'def foo(): return "say \\"hello\\""', id="python-escaped-quote"),
        pytest.param("cpp", "void foo() { char c = '/'; }", id="cpp-char-literal"),
    ])
    def test_source_with_quotes_is_hashed_as_it_is(self, language, code):
        assert compute_hash(code, language) == _by_hand(code, language)


class TestNormalizeCode:
    """`normalize_code` (comments removed, whitespace collapsed, blank lines dropped) is no longer what is hashed; it is kept for display."""

    def test_normalize_python(self):
        code = """
def foo():
    # comment
    return 1
"""

        assert normalize_code(code, "python") == "def foo():\nreturn 1"

    def test_python_docstrings_go_too_but_a_hash_in_a_string_stays(self):
        assert normalize_code("def foo():\n    '''doc'''\n    return 1", "python") == "def foo():\nreturn 1"
        assert normalize_code('def foo(): return "# not a comment"  # this is', "python") == 'def foo(): return "# not a comment"'

    def test_normalize_javascript(self):
        code = """
function foo() {
    // comment
    return 1;
}
"""

        assert normalize_code(code, "javascript") == "function foo() {\nreturn 1;\n}"


    def test_normalize_unknown_language(self):
        # unrecognized language: whitespace is normalized, but no comment syntax is stripped
        assert normalize_code("some   code # with comment", "rust") == "some code # with comment"
