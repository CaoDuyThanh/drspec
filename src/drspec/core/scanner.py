"""Unified function extraction engine for multi-language codebases.

This module provides a unified interface for scanning source files and directories,
coordinating the appropriate language parsers (Python, JavaScript, C++).
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional, Protocol

from drspec.parsers import CppParser, JavaScriptParser, ParseResult, PythonParser
from drspec.parsers.models import ExtractedFunction

logger = logging.getLogger(__name__)


# Language detection by file extension
LANGUAGE_MAP: dict[str, str] = {
    # Python
    ".py": "python",
    ".pyw": "python",
    # JavaScript/TypeScript (JS parser handles TS basics)
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "javascript",
    ".tsx": "javascript",
    # C/C++
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".c": "cpp",  # C uses C++ parser (subset)
    ".h": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".hh": "cpp",
    ".H": "cpp",
}

# Default directories and patterns to ignore during scanning
DEFAULT_IGNORES: list[str] = [
    ".git",
    "node_modules",
    "__pycache__",
    "venv",
    ".venv",
    "dist",
    "build",
    ".pytest_cache",
    ".mypy_cache",
    "*.egg-info",
    ".tox",
    ".nox",
    ".coverage",
    "htmlcov",
    ".eggs",
]


class Parser(Protocol):
    """Protocol for language parsers."""

    def parse(self, source_code: str, file_path: Optional[str] = None) -> ParseResult:
        """Parse source code and extract functions."""
        ...

    def parse_file(self, file_path: str) -> ParseResult:
        """Parse a file and extract functions."""
        ...


@dataclass
class ScannedFunction:
    """A function extracted from a scan with additional metadata.

    Extends ExtractedFunction with scanner-specific fields like
    function_id, file_path (relative), and language.
    """

    function_id: str  # filepath::qualified_name
    file_path: str  # Relative path from project root
    name: str
    qualified_name: str
    signature: str
    body: str
    start_line: int
    end_line: int
    language: str
    parent: Optional[str] = None
    decorators: list[str] = field(default_factory=list)
    is_method: bool = False
    is_async: bool = False

    @classmethod
    def from_extracted(
        cls,
        extracted: ExtractedFunction,
        file_path: str,
        language: str,
    ) -> "ScannedFunction":
        """Create ScannedFunction from ExtractedFunction.

        Args:
            extracted: The extracted function from parser.
            file_path: Relative path to the source file.
            language: The language of the source file.

        Returns:
            ScannedFunction with function_id and language set.
        """
        # Build function ID: filepath::qualified_name
        function_id = f"{file_path}::{extracted.qualified_name}"

        return cls(
            function_id=function_id,
            file_path=file_path,
            name=extracted.name,
            qualified_name=extracted.qualified_name,
            signature=extracted.signature,
            body=extracted.body,
            start_line=extracted.start_line,
            end_line=extracted.end_line,
            language=language,
            parent=extracted.parent,
            decorators=extracted.decorators.copy(),
            is_method=extracted.is_method,
            is_async=extracted.is_async,
        )


@dataclass
class ScanProgress:
    """Progress information during a directory scan."""

    current: int  # Current file index (1-based)
    total: int  # Total files to scan
    current_file: str  # Path being scanned
    functions_found: int  # Total functions found so far


@dataclass
class ScanResult:
    """Result of scanning a file or directory."""

    functions: list[ScannedFunction] = field(default_factory=list)
    files_scanned: int = 0
    files_skipped: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)  # (file_path, error_message)


# Progress callback type
ProgressCallback = Callable[[ScanProgress], None]


class Scanner:
    """Unified function extraction engine.

    Coordinates language-specific parsers to scan mixed-language codebases.
    """

    def __init__(self, ignore_patterns: Optional[list[str]] = None) -> None:
        """Initialize the scanner.

        Args:
            ignore_patterns: Patterns to ignore during scanning.
                            Defaults to DEFAULT_IGNORES.
        """
        self._ignore_patterns = ignore_patterns if ignore_patterns is not None else DEFAULT_IGNORES.copy()

        # Initialize parsers (lazy loading could be added if needed)
        self._parsers: dict[str, Parser] = {
            "python": PythonParser(),
            "javascript": JavaScriptParser(),
            "cpp": CppParser(),
        }

    @property
    def ignore_patterns(self) -> list[str]:
        """Get current ignore patterns."""
        return self._ignore_patterns.copy()

    def add_ignore_pattern(self, pattern: str) -> None:
        """Add an ignore pattern."""
        if pattern not in self._ignore_patterns:
            self._ignore_patterns.append(pattern)

    def detect_language(self, file_path: str | Path) -> Optional[str]:
        """Detect language from file extension.

        Args:
            file_path: Path to the file.

        Returns:
            Language name or None if unsupported.
        """
        path = Path(file_path)
        suffix = path.suffix.lower()
        return LANGUAGE_MAP.get(suffix)

    def is_supported(self, file_path: str | Path) -> bool:
        """Check if a file type is supported.

        Args:
            file_path: Path to the file.

        Returns:
            True if the file extension is supported.
        """
        return self.detect_language(file_path) is not None

    def should_ignore(self, path: Path, root: Optional[Path] = None) -> bool:
        """Check if a path should be ignored.

        Args:
            path: Path to check.
            root: Root directory for relative path matching.

        Returns:
            True if the path matches any ignore pattern.
        """
        # Get relative path for matching
        rel_path = path.relative_to(root) if root else path
        name = path.name

        for pattern in self._ignore_patterns:
            # Check against name
            if fnmatch.fnmatch(name, pattern):
                return True
            # Check against full relative path
            if fnmatch.fnmatch(str(rel_path), pattern):
                return True
            # Check if any parent directory matches
            for parent in rel_path.parents:
                if fnmatch.fnmatch(parent.name, pattern):
                    return True

        return False

    def scan_file(
        self,
        file_path: str | Path,
        relative_to: Optional[Path] = None,
    ) -> ScanResult:
        """Scan a single file and extract functions.

        Args:
            file_path: Path to the file to scan.
            relative_to: Base path for computing relative file paths.
                        If None, uses the file's directory.

        Returns:
            ScanResult with extracted functions.
        """
        path = Path(file_path).resolve()
        result = ScanResult()

        # Detect language
        language = self.detect_language(path)
        if language is None:
            logger.debug(f"Skipping unsupported file: {path}")
            result.files_skipped = 1
            return result

        # Get parser
        parser = self._parsers.get(language)
        if parser is None:
            logger.warning(f"No parser for language '{language}': {path}")
            result.files_skipped = 1
            return result

        # Compute relative path
        if relative_to:
            try:
                rel_path = str(path.relative_to(relative_to))
            except ValueError:
                rel_path = str(path)
        else:
            rel_path = str(path)

        # Parse file
        try:
            parse_result = parser.parse_file(str(path))
            result.files_scanned = 1

            # Convert to ScannedFunction
            for extracted in parse_result.functions:
                scanned = ScannedFunction.from_extracted(extracted, rel_path, language)
                result.functions.append(scanned)

            if parse_result.has_errors:
                error_msgs = [f"{e.line}:{e.column}: {e.message}" for e in parse_result.errors]
                result.errors.append((rel_path, "; ".join(error_msgs)))

        except FileNotFoundError:
            logger.error(f"File not found: {path}")
            result.errors.append((rel_path, "File not found"))
        except IOError as e:
            logger.error(f"Error reading file {path}: {e}")
            result.errors.append((rel_path, str(e)))
        except Exception as e:
            logger.error(f"Error parsing file {path}: {e}")
            result.errors.append((rel_path, f"Parse error: {e}"))
            result.files_skipped = 1

        return result

    def scan_directory(
        self,
        directory: str | Path,
        recursive: bool = True,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> ScanResult:
        """Scan a directory for functions.

        Args:
            directory: Path to the directory to scan.
            recursive: If True, scan subdirectories recursively.
            progress_callback: Optional callback for progress updates.

        Returns:
            ScanResult with all extracted functions.
        """
        root = Path(directory).resolve()
        if not root.is_dir():
            raise ValueError(f"Not a directory: {root}")

        # Collect all files to scan
        files_to_scan: list[Path] = []
        for path in self._iter_files(root, recursive):
            if self.is_supported(path):
                files_to_scan.append(path)

        total_files = len(files_to_scan)
        result = ScanResult()

        # Scan each file
        for i, file_path in enumerate(files_to_scan, 1):
            file_result = self.scan_file(file_path, relative_to=root)

            # Aggregate results
            result.functions.extend(file_result.functions)
            result.files_scanned += file_result.files_scanned
            result.files_skipped += file_result.files_skipped
            result.errors.extend(file_result.errors)

            # Report progress
            if progress_callback:
                progress = ScanProgress(
                    current=i,
                    total=total_files,
                    current_file=str(file_path.relative_to(root)),
                    functions_found=len(result.functions),
                )
                progress_callback(progress)

        return result

    def iter_scan_directory(
        self,
        directory: str | Path,
        recursive: bool = True,
    ) -> Iterator[tuple[Path, ScanResult]]:
        """Iterate through directory, yielding scan results for each file.

        This is useful for streaming results without loading all into memory.

        Args:
            directory: Path to the directory to scan.
            recursive: If True, scan subdirectories recursively.

        Yields:
            Tuples of (file_path, scan_result) for each scanned file.
        """
        root = Path(directory).resolve()
        if not root.is_dir():
            raise ValueError(f"Not a directory: {root}")

        for path in self._iter_files(root, recursive):
            if self.is_supported(path):
                result = self.scan_file(path, relative_to=root)
                yield path, result

    def _iter_files(self, root: Path, recursive: bool) -> Iterator[Path]:
        """Iterate through files in directory, respecting ignore patterns.

        Args:
            root: Root directory to scan.
            recursive: If True, include subdirectories.

        Yields:
            Path objects for each file.
        """
        if recursive:
            for path in root.rglob("*"):
                if path.is_file() and not self.should_ignore(path, root):
                    yield path
        else:
            for path in root.iterdir():
                if path.is_file() and not self.should_ignore(path, root):
                    yield path


# Convenience functions for simple usage
def scan_file(file_path: str | Path) -> ScanResult:
    """Scan a single file.

    Args:
        file_path: Path to the file.

    Returns:
        ScanResult with extracted functions.
    """
    scanner = Scanner()
    return scanner.scan_file(file_path)


def scan_directory(
    directory: str | Path,
    recursive: bool = True,
    ignore_patterns: Optional[list[str]] = None,
) -> ScanResult:
    """Scan a directory for functions.

    Args:
        directory: Path to the directory.
        recursive: If True, scan subdirectories.
        ignore_patterns: Patterns to ignore.

    Returns:
        ScanResult with extracted functions.
    """
    scanner = Scanner(ignore_patterns=ignore_patterns)
    return scanner.scan_directory(directory, recursive=recursive)
