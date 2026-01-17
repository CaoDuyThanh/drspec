"""Code hash computation for change detection.

This module provides normalized SHA-256 hashing for function bodies.
The normalization process removes whitespace variations and comments,
ensuring that semantically identical code produces the same hash.
"""

from __future__ import annotations

import hashlib
import re
from typing import Callable


def compute_hash(body: str, language: str) -> str:
    """Compute SHA-256 hash of normalized function body.

    Args:
        body: The function body text.
        language: The programming language ('python', 'javascript', 'cpp').

    Returns:
        64-character hex string (SHA-256 digest).

    Example:
        >>> compute_hash("def foo():\\n    return 1", "python")
        'a1b2c3...'  # 64 hex characters
    """
    normalized = normalize_code(body, language)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def normalize_code(body: str, language: str) -> str:
    """Normalize code for hashing.

    Normalization process:
    1. Remove comments (language-specific)
    2. Strip leading/trailing whitespace from each line
    3. Collapse multiple whitespace to single space
    4. Remove empty lines
    5. Join with single newline

    Args:
        body: The function body text.
        language: The programming language.

    Returns:
        Normalized code string.
    """
    # Get language-specific comment remover
    remove_comments = _get_comment_remover(language)

    # 1. Remove comments
    code = remove_comments(body)

    # 2. Split into lines
    lines = code.split("\n")

    # 3. Strip whitespace from each line and collapse internal whitespace
    normalized_lines = []
    for line in lines:
        # Strip leading/trailing whitespace
        stripped = line.strip()
        # Collapse multiple whitespace to single space
        stripped = re.sub(r"\s+", " ", stripped)
        normalized_lines.append(stripped)

    # 4. Remove empty lines
    non_empty_lines = [line for line in normalized_lines if line]

    # 5. Join with single newline
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
        # Check for triple-quoted strings (docstrings)
        if code[i:i+3] in ('"""', "'''"):
            quote = code[i:i+3]
            i += 3
            # Skip until closing quote
            while i < n - 2:
                if code[i:i+3] == quote:
                    i += 3
                    break
                i += 1
            else:
                i = n
            continue

        # Check for single-quoted strings (preserve them)
        if code[i] in ('"', "'"):
            quote_char = code[i]
            result.append(code[i])
            i += 1
            while i < n and code[i] != quote_char:
                if code[i] == "\\" and i + 1 < n:
                    # Handle escaped characters
                    result.append(code[i])
                    result.append(code[i + 1])
                    i += 2
                else:
                    result.append(code[i])
                    i += 1
            if i < n:
                result.append(code[i])  # closing quote
                i += 1
            continue

        # Check for line comments
        if code[i] == "#":
            # Skip until end of line
            while i < n and code[i] != "\n":
                i += 1
            continue

        # Regular character
        result.append(code[i])
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
        # Check for single-quoted strings
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

        # Check for double-quoted strings
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

        # Check for template strings (JavaScript)
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

        # Check for block comments /* */
        if i < n - 1 and code[i:i+2] == "/*":
            i += 2
            # Skip until closing */
            while i < n - 1:
                if code[i:i+2] == "*/":
                    i += 2
                    break
                i += 1
            else:
                i = n
            continue

        # Check for line comments //
        if i < n - 1 and code[i:i+2] == "//":
            # Skip until end of line
            while i < n and code[i] != "\n":
                i += 1
            continue

        # Regular character
        result.append(code[i])
        i += 1

    return "".join(result)
