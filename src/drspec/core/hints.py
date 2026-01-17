"""Hint detection module for extracting @invariant and related annotations.

This module provides comprehensive hint detection for Python, JavaScript, and C++ code,
supporting various annotation formats like @invariant, @pre, @post, @requires.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class HintType(str, Enum):
    """Types of hints that can be detected."""

    INVARIANT = "invariant"
    PRE = "pre"
    POST = "post"
    REQUIRES = "requires"


@dataclass
class Hint:
    """Represents a detected hint annotation.

    Attributes:
        line: 1-indexed line number where hint was found.
        type: Type of hint (invariant, pre, post, requires).
        text: Extracted hint text (the description).
        raw: Original comment text containing the hint.
    """

    line: int
    type: HintType
    text: str
    raw: str

    def to_dict(self) -> dict:
        """Convert hint to dictionary format.

        Returns:
            Dictionary with line, type, and text keys.
        """
        return {
            "line": self.line,
            "type": self.type.value,
            "text": self.text,
        }


# Pattern components
_HINT_TYPES = r"(?:invariant|pre|post|requires)"

# Python patterns: # @invariant: text
PYTHON_PATTERNS = [
    # Single-line comment: # @invariant: text
    re.compile(
        rf"#\s*@({_HINT_TYPES})[:\s]+(.+?)(?:\n|$)",
        re.IGNORECASE,
    ),
    # Docstring style: @invariant: text (inside """)
    re.compile(
        rf"@({_HINT_TYPES})[:\s]+(.+?)(?:\n|$)",
        re.IGNORECASE,
    ),
]

# JavaScript/TypeScript patterns
JS_PATTERNS = [
    # Single-line comment: // @invariant: text
    re.compile(
        rf"//\s*@({_HINT_TYPES})[:\s]+(.+?)(?:\n|$)",
        re.IGNORECASE,
    ),
    # Block comment: /* @invariant: text */
    re.compile(
        rf"/\*\s*@({_HINT_TYPES})[:\s]+(.+?)\s*\*/",
        re.IGNORECASE,
    ),
    # JSDoc style: * @invariant text
    re.compile(
        rf"\*\s*@({_HINT_TYPES})[:\s]+(.+?)(?:\n|$)",
        re.IGNORECASE,
    ),
]

# C++ patterns (same as JS plus some variations)
CPP_PATTERNS = [
    # Single-line comment: // @invariant: text
    re.compile(
        rf"//\s*@({_HINT_TYPES})[:\s]+(.+?)(?:\n|$)",
        re.IGNORECASE,
    ),
    # Block comment: /* @invariant: text */
    re.compile(
        rf"/\*\s*@({_HINT_TYPES})[:\s]+(.+?)\s*\*/",
        re.IGNORECASE,
    ),
    # Doxygen style: * @invariant text
    re.compile(
        rf"\*\s*@({_HINT_TYPES})[:\s]+(.+?)(?:\n|$)",
        re.IGNORECASE,
    ),
]

# Language to pattern mapping
LANGUAGE_PATTERNS = {
    "python": PYTHON_PATTERNS,
    "javascript": JS_PATTERNS,
    "typescript": JS_PATTERNS,
    "cpp": CPP_PATTERNS,
    "c": CPP_PATTERNS,
}


def _normalize_hint_type(type_str: str) -> HintType:
    """Normalize hint type string to HintType enum.

    Args:
        type_str: Raw type string from regex match.

    Returns:
        Corresponding HintType enum value.
    """
    type_lower = type_str.lower()
    if type_lower == "invariant":
        return HintType.INVARIANT
    elif type_lower == "pre":
        return HintType.PRE
    elif type_lower == "post":
        return HintType.POST
    elif type_lower == "requires":
        return HintType.REQUIRES
    else:
        return HintType.INVARIANT  # Default fallback


def extract_hints(
    body: str,
    start_line: int = 1,
    language: Optional[str] = None,
) -> list[Hint]:
    """Extract all hints from function body.

    Detects @invariant, @pre, @post, and @requires annotations in comments.
    Supports Python, JavaScript, TypeScript, and C++ comment styles.

    Args:
        body: Function source code.
        start_line: 1-indexed start line of function in file.
        language: Programming language (python, javascript, cpp). If None,
            uses all patterns.

    Returns:
        List of Hint objects, sorted by line number.
    """
    hints = []
    lines = body.split("\n")

    # Get patterns for language (or all if not specified)
    if language and language.lower() in LANGUAGE_PATTERNS:
        patterns = LANGUAGE_PATTERNS[language.lower()]
    else:
        # Use all patterns when language not specified
        patterns = PYTHON_PATTERNS + JS_PATTERNS

    for i, line in enumerate(lines):
        line_num = start_line + i

        for pattern in patterns:
            for match in pattern.finditer(line):
                hint_type = _normalize_hint_type(match.group(1))
                hint_text = match.group(2).strip()

                hints.append(
                    Hint(
                        line=line_num,
                        type=hint_type,
                        text=hint_text,
                        raw=line.strip(),
                    )
                )

    # Remove duplicates (same line and normalized text)
    # The text may differ slightly due to pattern matching, so we normalize
    seen = set()
    unique_hints = []
    for hint in hints:
        # Normalize text by removing trailing */ and extra whitespace
        normalized_text = hint.text.rstrip().rstrip("*/").rstrip()
        key = (hint.line, normalized_text)
        if key not in seen:
            seen.add(key)
            # Use the hint with cleaner text
            if hint.text != normalized_text:
                hint = Hint(
                    line=hint.line,
                    type=hint.type,
                    text=normalized_text,
                    raw=hint.raw,
                )
            unique_hints.append(hint)

    return sorted(unique_hints, key=lambda h: h.line)


def extract_hints_simple(
    body: str,
    start_line: int = 1,
) -> list[dict]:
    """Extract hints and return as simple dictionaries.

    This is a convenience function for CLI output that returns hints
    in dictionary format.

    Args:
        body: Function source code.
        start_line: 1-indexed start line of function in file.

    Returns:
        List of hint dictionaries with line, type, and text keys.
    """
    hints = extract_hints(body, start_line)
    return [hint.to_dict() for hint in hints]


def hints_to_json(hints: list[Hint]) -> list[dict]:
    """Convert list of Hint objects to JSON-serializable dictionaries.

    Args:
        hints: List of Hint objects.

    Returns:
        List of dictionaries suitable for JSON serialization.
    """
    return [hint.to_dict() for hint in hints]
