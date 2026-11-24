"""Code hash computation for change detection.

Change detection hashes exact source, including literals and indentation.
Legacy normalization helpers remain available for display only.
"""

from __future__ import annotations

import hashlib
import re
from typing import Callable


HASH_STRATEGY = "exact-source:v1"


def compute_hash(body: str, language: str) -> str:
    """Compute a versioned SHA-256 hash of exact function source.

    Args:
        body: The function body text.
        language: The programming language ('python', 'javascript', 'cpp').

    Returns:
        64-character hex string (SHA-256 digest).

    Example:
        >>> compute_hash("def foo():\\n    return 1", "python")
        'a1b2c3...'  # 64 hex characters
    """
    # Domain separation deliberately invalidates every legacy normalized hash. C is hashed as C++ (the language it was
    # stored as before it had a label of its own), so no hash changes, and a header whose label flips (see
    # `Scanner.label_of`) keeps its hash and its contracts.
    language = "cpp" if language == "c" else language
    return hashlib.sha256(("drspec:" + HASH_STRATEGY + "\0" + language + "\0" + body).encode("utf-8")).hexdigest()



def compute_edge_hash(caller_hash: str, callee_hash: str, call_site: str) -> str:
    """Compute a dependency edge's hash.

    `edge_hash = sha256(caller.code_hash + callee.code_hash + call_site)` —
    deliberately *not* normalized like `compute_hash`: `call_site` is the
    literal call-expression text, and a different call (e.g. different
    arguments) should change the hash even if neither function's body did.

    Args:
        caller_hash: The caller artifact's current `code_hash`.
        callee_hash: The callee artifact's current `code_hash`.
        call_site: The call-site text (possibly concatenated across
            multiple call sites for the same caller/callee pair).

    Returns:
        64-character hex string (SHA-256 digest).
    """
    return hashlib.sha256((caller_hash + callee_hash + call_site).encode("utf-8")).hexdigest()


def normalize_code(body: str, language: str) -> str:
    """Normalize code for hashing: strip comments, collapse whitespace, drop empty lines.

    Args:
        body: The function body text.
        language: The programming language.

    Returns:
        Normalized code string.
    """
    remove_comments = _get_comment_remover(language)
    code = remove_comments(body)
    lines = code.split("\n")

    normalized_lines = []
    for line in lines:
        stripped = line.strip()
        stripped = re.sub(r"\s+", " ", stripped)
        normalized_lines.append(stripped)

    non_empty_lines = [line for line in normalized_lines if line]

    return "\n".join(non_empty_lines)


def _get_comment_remover(language: str) -> Callable[[str], str]:
    """Get the comment removal function for a language.

    Args:
        language: The programming language.

    Returns:
        Function that removes comments from code.
    """
    removers = {
        "python": _remove_python_comments,
        "javascript": _remove_c_style_comments,
        "cpp": _remove_c_style_comments,
        "c": _remove_c_style_comments,
        "rust": _remove_rust_comments,
    }
    return removers.get(language, _no_op_remover)


def _no_op_remover(code: str) -> str:
    """Default remover that does nothing."""
    return code


def _remove_python_comments(code: str) -> str:
    """Remove Python comments and docstrings.

    Handles:
    - # line comments
    - Triple-quoted docstrings (''' and \""")
    - Preserves # inside strings

    Args:
        code: Python source code.

    Returns:
        Code with comments and docstrings removed.
    """
    result = []
    i = 0
    n = len(code)

    while i < n:
        if code[i:i+3] in ('"""', "'''"):
            quote = code[i:i+3]
            i += 3
            while i < n - 2:
                if code[i:i+3] == quote:
                    i += 3
                    break
                i += 1
            else:
                i = n
            continue

        if code[i] in ('"', "'"):
            quote_char = code[i]
            result.append(code[i])
            i += 1
            while i < n and code[i] != quote_char:
                if code[i] == "\\" and i + 1 < n:
                    result.append(code[i])
                    result.append(code[i + 1])
                    i += 2
                else:
                    result.append(code[i])
                    i += 1
            if i < n:
                result.append(code[i])
                i += 1
            continue

        if code[i] == "#":
            while i < n and code[i] != "\n":
                i += 1
            continue

        result.append(code[i])
        i += 1

    return "".join(result)


def _remove_rust_comments(code: str) -> str:
    """Remove Rust comments: `//` (including `///` and `//!` doc comments) and `/* */`, which nest.

    Strings are kept as they are: `"..."` with escapes and raw strings `r"..."`, `r#"..."#` (a `//` inside
    them is text). A `'` starts a character literal only when it closes within a few characters; otherwise
    it is a lifetime (`&'a str`) and is left alone.
    """
    result = []
    i = 0
    n = len(code)
    while i < n:
        char = code[i]
        if char == "/" and code[i + 1:i + 2] == "/":
            while i < n and code[i] != "\n":
                i += 1
            continue
        if char == "/" and code[i + 1:i + 2] == "*":
            depth = 1
            i += 2
            while i < n and depth:
                if code[i:i + 2] == "/*":
                    depth += 1
                    i += 2
                elif code[i:i + 2] == "*/":
                    depth -= 1
                    i += 2
                else:
                    i += 1
            continue
        if char == "r" and (i == 0 or not (code[i - 1].isalnum() or code[i - 1] == "_")):
            j = i + 1
            while j < n and code[j] == "#":
                j += 1
            if j < n and code[j] == '"':
                hashes = j - i - 1
                end = code.find('"' + "#" * hashes, j + 1)
                end = n if end == -1 else end + 1 + hashes
                result.append(code[i:end])
                i = end
                continue
        if char == '"':
            j = i + 1
            while j < n and code[j] != '"':
                j += 2 if code[j] == "\\" else 1
            result.append(code[i:j + 1])
            i = j + 1
            continue
        if char == "'":
            closing = code.find("'", i + 2)
            literal = code[i:closing + 1] if closing != -1 and closing - i <= 4 else ""
            if literal and (literal[1] != "\\" or len(literal) > 3) and "\n" not in literal:
                result.append(literal)
                i += len(literal)
                continue
        result.append(char)
        i += 1
    return "".join(result)


def _remove_c_style_comments(code: str) -> str:
    """Remove C-style comments (// and /* */).

    Handles:
    - // line comments
    - /* */ block comments
    - Preserves // and /* inside strings

    Args:
        code: JavaScript or C++ source code.

    Returns:
        Code with comments removed.
    """
    result = []
    i = 0
    n = len(code)

    while i < n:
        if code[i] == "'":
            result.append(code[i])
            i += 1
            while i < n and code[i] != "'":
                if code[i] == "\\" and i + 1 < n:
                    result.append(code[i])
                    result.append(code[i + 1])
                    i += 2
                else:
                    result.append(code[i])
                    i += 1
            if i < n:
                result.append(code[i])
                i += 1
            continue

        if code[i] == '"':
            result.append(code[i])
            i += 1
            while i < n and code[i] != '"':
                if code[i] == "\\" and i + 1 < n:
                    result.append(code[i])
                    result.append(code[i + 1])
                    i += 2
                else:
                    result.append(code[i])
                    i += 1
            if i < n:
                result.append(code[i])
                i += 1
            continue

        if code[i] == "`":
            result.append(code[i])
            i += 1
            while i < n and code[i] != "`":
                if code[i] == "\\" and i + 1 < n:
                    result.append(code[i])
                    result.append(code[i + 1])
                    i += 2
                else:
                    result.append(code[i])
                    i += 1
            if i < n:
                result.append(code[i])
                i += 1
            continue

        if i < n - 1 and code[i:i+2] == "/*":
            i += 2
            while i < n - 1:
                if code[i:i+2] == "*/":
                    i += 2
                    break
                i += 1
            else:
                i = n
            continue

        if i < n - 1 and code[i:i+2] == "//":
            while i < n and code[i] != "\n":
                i += 1
            continue

        result.append(code[i])
        i += 1

    return "".join(result)
