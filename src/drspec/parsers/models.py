"""Data models for code parsing results."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ExtractedFunction:
    """Represents a function extracted from source code.

    Attributes:
        name: Function name (without parent prefix).
        qualified_name: Full qualified name (e.g., "ClassName.method_name").
        signature: Full function signature line.
        body: Complete function body text.
        start_line: 1-indexed starting line number.
        end_line: 1-indexed ending line number.
        parent: Parent class or function name, if any.
        decorators: List of decorator strings (without @).
        is_method: True if this is a class method.
        is_async: True if this is an async function.
    """

    name: str
    qualified_name: str
    signature: str
    body: str
    start_line: int
    end_line: int
    parent: Optional[str] = None
    decorators: list[str] = field(default_factory=list)
    is_method: bool = False
    is_async: bool = False


@dataclass
class ParseError:
    """Represents a syntax error found during parsing.

    Attributes:
        line: 1-indexed line number of the error.
        column: 0-indexed column number.
        message: Human-readable error message.
    """

    line: int
    column: int
    message: str


@dataclass
class ParseResult:
    """Result of parsing a source file.

    Attributes:
        functions: List of extracted functions.
        errors: List of syntax errors encountered.
        has_errors: True if any syntax errors were found.
        file_path: Optional path to the parsed file.
    """

    functions: list[ExtractedFunction] = field(default_factory=list)
    errors: list[ParseError] = field(default_factory=list)
    has_errors: bool = False
    file_path: Optional[str] = None
