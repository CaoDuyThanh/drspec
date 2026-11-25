"""Unified function extraction engine for multi-language codebases.

This module provides a unified interface for scanning source files and directories,
coordinating the appropriate language parsers (Python, JavaScript, C++).
"""

from __future__ import annotations

import fnmatch
import os
import re
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional, Protocol, NamedTuple

from drspec.core.identity import function_identity
from drspec.parsers import CppParser, JavaScriptParser, ParseResult, PythonParser, RustParser
from drspec.parsers.models import (
    ExtractedConstant,
    ExtractedFunction,
    RawCall,
    RawMemberAccess,
    RawModuleImport,
    RawNameImport,
    RawPropertyRead,
    RawSymbolRead,
)

logger = logging.getLogger(__name__)


LANGUAGE_MAP: dict[str, str] = {
    ".py": "python",
    ".pyw": "python",
    # JS parser also handles TS basics
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".rs": "rust",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".c": "c",  # read by the C++ parser (same family); stored as `c`
    ".h": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".mm": "cpp",  # Objective-C++: the C++ parts are read; a message send is an error, so such a file is partial
    ".hh": "cpp",
    ".H": "cpp",
}

DEFAULT_IGNORES: list[str] = [
    ".git",
    "node_modules",
    "__pycache__",
    # Glob, not exact name: a venv isn't always named exactly "venv"/
    # ".venv" (e.g. ".venv-review"), and missing one means scanning its
    # entire site-packages as project source.
    "venv*",
    ".venv*",
    "dist",
    "build",
    "target",
    "Cargo.lock",
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

    function_id: str  # filepath::qualified_name#parameter-hash (see core.identity)
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
    is_property: bool = False
    calls: list[RawCall] = field(default_factory=list)
    property_reads: list[RawPropertyRead] = field(default_factory=list)
    symbol_reads: list[RawSymbolRead] = field(default_factory=list)
    member_accesses: list[RawMemberAccess] = field(default_factory=list)
    first_param_name: Optional[str] = None
    declared_param_types: dict[str, str] = field(default_factory=dict)
    parameter_tokens: list[str] = field(default_factory=list)
    discriminator: str = ""

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
        function_id = function_identity(file_path, extracted.qualified_name, extracted.parameter_tokens)

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
            is_property=extracted.is_property,
            calls=list(extracted.calls),
            property_reads=list(extracted.property_reads),
            symbol_reads=list(extracted.symbol_reads),
            member_accesses=list(extracted.member_accesses),
            first_param_name=extracted.first_param_name,
            declared_param_types=dict(extracted.declared_param_types),
            parameter_tokens=list(extracted.parameter_tokens),
            discriminator=extracted.discriminator,
        )


@dataclass
class ScannedConstant:
    """A module-level constant extracted from a scan, with scanner-level
    metadata — the `constants` counterpart of `ScannedFunction`."""

    function_id: str  # filepath::NAME — same identity shape as a function
    file_path: str
    name: str
    value_text: str
    start_line: int
    end_line: int
    language: str

    @classmethod
    def from_extracted(cls, extracted: ExtractedConstant, file_path: str, language: str) -> "ScannedConstant":
        return cls(
            function_id=f"{file_path}::{extracted.name}",
            file_path=file_path,
            name=extracted.name,
            value_text=extracted.value_text,
            start_line=extracted.start_line,
            end_line=extracted.end_line,
            language=language,
        )


@dataclass
class ScanProgress:
    """Progress information during a directory scan."""

    current: int  # 1-based
    total: int
    current_file: str
    functions_found: int


@dataclass
class FileImports:
    """A file's own import statements, extracted from the AST — never
    resolved to function_ids here since that needs DB access the parser
    doesn't have (see `drspec.core.project_facts`)."""

    name_imports: list[RawNameImport] = field(default_factory=list)
    module_imports: list[RawModuleImport] = field(default_factory=list)
    includes: list[str] = field(default_factory=list)
    """C++ `#include "file"` targets as written (system headers in angle brackets are left out)."""
    facts: dict = field(default_factory=dict)
    """Module facts of a JavaScript, TypeScript or Rust file (bindings, exports, classes, impls), see `ParseResult.facts`."""


class ScanDiagnostic(NamedTuple):
    file: str
    message: str
    line: Optional[int] = None
    column: Optional[int] = None


@dataclass
class ScanResult:
    """Result of scanning a file or directory."""

    functions: list[ScannedFunction] = field(default_factory=list)
    files_scanned: int = 0
    files_skipped: int = 0
    errors: list[ScanDiagnostic] = field(default_factory=list)
    file_imports: dict[str, FileImports] = field(default_factory=dict)
    parse_status: str = "failed"  # successful, partial (some of it parsed), failed
    skipped_functions: list[dict] = field(default_factory=list)
    """Functions left out of a partial file because their own text contains a syntax error:
    `{"file", "name", "line"}` each."""
    skipped_constructs: int = 0
    """Valid constructs the grammar cannot read, skipped without being counted as errors."""
    partial_functions: list[dict] = field(default_factory=list)
    """Functions kept although their own text holds a syntax error (their head was read): `{"file", "name", "line",
    "function_id"}` each. They are in `functions` too; this says which of them were read in part."""


def _distinct_ids(functions: list) -> None:
    """Two definitions of one file with the same qualified name and parameters would share one identifier and become one
    function: the branches of an `if` or a `try`, a name defined again, `cfg`-selected variants, and the same method in
    several `impl` blocks of one type (`impl Trait<A> for T` and `impl Trait<B> for T`).

    Where every definition has a discriminator of its own (the header of its `impl` block) that tells them apart, the
    first keeps the identifier it has and each other one gets its header mixed into its parameter tokens. Otherwise the
    last keeps its identifier (the one a single definition had, and the one that runs) and each earlier one gets its
    position among them. Either way every definition is a function, and keeps its identifier while unrelated code
    changes."""
    positions: dict = {}
    for function in functions:
        positions.setdefault(function.function_id, []).append(function)
    for group in positions.values():
        if len(group) == 1:
            continue
        headers = [function.discriminator for function in group]
        by_header = all(headers) and len(set(headers)) == len(headers)
        keeper = group[0] if by_header else group[-1]
        for number, function in enumerate(group, start=1):
            if function is keeper:
                continue
            marker = f"#impl-{function.discriminator}" if by_header else f"#definition-{number}"
            function.parameter_tokens = list(function.parameter_tokens) + [marker]
            function.function_id = function_identity(function.file_path, function.qualified_name, function.parameter_tokens)


def _head_is_whole(signature: str) -> bool:
    """Whether a function's signature has its brackets paired: the sign that its head was read and only the body is
    damaged. (`def two():` and `int bad(int x) {` are; `def f(:` and `void g(int a,` are not.)"""
    head = signature.split("{", 1)[0]  # a one-line function has its body in the signature
    pairs = {")": "(", "]": "["}
    stack = []
    for char in head:
        if char in "([":
            stack.append(char)
        elif char in pairs:
            if not stack or stack.pop() != pairs[char]:
                return False
    return not stack


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

        self._parsers: dict[str, Parser] = {
            "python": PythonParser(),
            "javascript": JavaScriptParser(),
            "typescript": JavaScriptParser("typescript"),
            "tsx": JavaScriptParser("tsx"),
            "cpp": CppParser(),
            "rust": RustParser(),
        }
        self._parsers["c"] = self._parsers["cpp"]  # one parser for the family
        self._folder_labels: dict[tuple, str] = {}

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
        if path.suffix == ".C":  # (by Unix convention a capital `.C` is C++)
            return "cpp"
        suffix = path.suffix.lower()
        return LANGUAGE_MAP.get(suffix)

    #: Suffixes of C++ sources and of C sources, for telling what a header belongs with.
    _CPP_SOURCES = frozenset({".cpp", ".cc", ".cxx", ".hpp", ".hxx", ".hh", ".mm", ".C", ".H"})

    def label_of(self, file_path: str | Path, root: Optional[Path] = None) -> Optional[str]:
        """The `language` stored for the functions of a file: what `detect_language` says, except for a header, which is
        `c` when the sources next to it are C and no C++ file is near, and `cpp` otherwise (see `_header_label`). Only
        folders inside `root` (the project) are looked at, when it is given."""
        language = self.detect_language(file_path)
        if language == "cpp" and Path(file_path).suffix == ".h":
            return self._header_label(Path(file_path).resolve().parent, Path(root).resolve() if root is not None else None)
        return language

    def _header_label(self, folder: Path, root: Optional[Path] = None) -> str:
        """The label a `.h` in `folder` gets: the nearest folder, this one and then its parents (a header often sits in
        an `include` folder beside the sources), that has C or C++ sources decides; C++ wins when it has both. The folders
        above the project (`root`) are not looked at: sources that are not the project's say nothing about its header.
        Decided at the scan of the header, so it changes when the neighbours do and the header is read again."""
        if root is not None and root != folder and root not in folder.parents:
            root = None  # (a file outside the project it is scanned for: nothing to hold the lookup to)
        key = (folder, root)
        cached = self._folder_labels.get(key)
        if cached is not None:
            return cached
        label = "cpp"
        current = folder
        for _ in range(5):
            kinds = self._source_kinds(current)
            if kinds:
                label = "cpp" if "cpp" in kinds else "c"
                break
            if current.parent == current or (root is not None and current == root):
                break
            current = current.parent
        self._folder_labels[key] = label
        return label

    def _source_kinds(self, folder: Path) -> set:
        """`{"c"}`, `{"cpp"}` or both, for the source files directly in `folder` and, when it has none, in its immediate
        subfolders (a header folder beside `src/`)."""
        kinds: set = set()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            return kinds
        for entry in entries:
            if entry.is_file(follow_symlinks=False):
                suffix = os.path.splitext(entry.name)[1]
                if suffix == ".c":
                    kinds.add("c")
                elif suffix in self._CPP_SOURCES:
                    kinds.add("cpp")
        if kinds:
            return kinds
        for entry in entries:
            if entry.is_dir(follow_symlinks=False) and not entry.name.startswith(".") and entry.name not in ("node_modules", "target", "build"):
                try:
                    for inner in os.scandir(entry.path):
                        suffix = os.path.splitext(inner.name)[1]
                        if suffix == ".c":
                            kinds.add("c")
                        elif suffix in self._CPP_SOURCES:
                            kinds.add("cpp")
                except OSError:
                    continue
        return kinds

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
        rel_path = path.relative_to(root) if root else path
        name = path.name

        if os.name == "nt":  # (the shell-style match is case-insensitive there: leave it to `fnmatch`)
            for pattern in self._ignore_patterns:
                if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(str(rel_path), pattern):
                    return True
                for parent in rel_path.parents:
                    if fnmatch.fnmatch(parent.name, pattern):
                        return True
            return False
        regex = self._ignore_regex()
        if regex is None:
            return False
        if regex.match(name) or regex.match(str(rel_path)):
            return True
        return any(regex.match(parent.name) for parent in rel_path.parents)

    def _ignore_regex(self):
        """The ignore patterns as one regular expression (compiled once; again when the list is changed)."""
        patterns = tuple(self._ignore_patterns)
        if getattr(self, "_ignore_key", None) != patterns:
            self._ignore_key = patterns
            self._ignore_compiled = re.compile("|".join(fnmatch.translate(pattern) for pattern in patterns)) if patterns else None
        return self._ignore_compiled

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

        parser_language = self.detect_language(path)
        if parser_language is None:
            logger.debug(f"Skipping unsupported file: {path}")
            result.files_skipped = 1
            return result
        language = self.label_of(path, relative_to)  # what is stored for the file's functions

        parser = self._parsers.get(parser_language)
        if parser is None:
            logger.warning(f"No parser for language '{language}': {path}")
            result.files_skipped = 1
            return result

        if relative_to:
            try:
                rel_path = str(path.relative_to(relative_to))
            except ValueError:
                rel_path = str(path)
        else:
            rel_path = str(path)

        try:
            if isinstance(parser, CppParser):
                parse_result = parser.parse_file(str(path), language=language)  # `c` also turns on what only C needs
            else:
                parse_result = parser.parse_file(str(path))
            result.files_scanned = 1
            result.parse_status = "partial" if parse_result.has_errors else "successful"

            result.skipped_constructs = parse_result.skipped_constructs
            partial_kept: list = []
            for line in parse_result.suspected_losses:  # swallowed by an error: the name is what the head's text gives
                name = parse_result.suspected_names.get(line)
                result.skipped_functions.append(
                    {"file": rel_path, "name": name or "(a definition the parser could not read)", "line": line}
                )
            for extracted in parse_result.functions:
                if extracted.has_error and not _head_is_whole(extracted.signature):
                    # The head itself is damaged (`def f(:`): there is nothing reliable to name or hash, so the function
                    # is left out and counted, as before.
                    result.skipped_functions.append({"file": rel_path, "name": extracted.qualified_name, "line": extracted.start_line})
                    continue
                scanned = ScannedFunction.from_extracted(extracted, rel_path, language)
                result.functions.append(scanned)
                if extracted.has_error:  # the head was read: kept, with what could be read of the rest, and marked partial
                    partial_kept.append(
                        (scanned, {"file": rel_path, "name": extracted.qualified_name, "line": extracted.start_line})
                    )
            _distinct_ids(result.functions)
            for scanned, info in partial_kept:
                result.partial_functions.append({**info, "function_id": scanned.function_id})

            result.file_imports[rel_path] = FileImports(
                name_imports=parse_result.name_imports,
                module_imports=parse_result.module_imports,
                includes=list(parse_result.includes),
                facts=parse_result.facts,
            )

            if parse_result.has_errors:
                result.errors.extend(ScanDiagnostic(rel_path, e.message, e.line, e.column)
                                     for e in parse_result.errors)

        except FileNotFoundError:
            logger.error(f"File not found: {path}")
            result.errors.append(ScanDiagnostic(rel_path, "File not found"))
        except IOError as e:
            logger.error(f"Error reading file {path}: {e}")
            result.errors.append(ScanDiagnostic(rel_path, str(e)))
        except Exception as e:
            logger.error(f"Error parsing file {path}: {e}")
            result.errors.append(ScanDiagnostic(rel_path, f"Parse error: {e}"))
            result.files_skipped = 1

        return result

    def scan_directory(
        self,
        directory: str | Path,
        recursive: bool = True,
        progress_callback: Optional[ProgressCallback] = None,
        relative_to: Optional[Path] = None,
    ) -> ScanResult:
        """Scan a directory for functions.

        Args:
            directory: Path to the directory to scan.
            recursive: If True, scan subdirectories recursively.
            progress_callback: Optional callback for progress updates.
            relative_to: Base path for computing each file's relative path
                        (and therefore its `function_id`). Defaults to
                        `directory` itself, so callers that care about a
                        stable `function_id` regardless of which directory
                        argument was scanned (e.g. the CLI, anchored to the
                        project root) should pass it explicitly.

        Returns:
            ScanResult with all extracted functions.
        """
        root = Path(directory).resolve()
        if not root.is_dir():
            raise ValueError(f"Not a directory: {root}")

        files_to_scan: list[Path] = []
        for path in self._iter_files(root, recursive):
            if self.is_supported(path):
                files_to_scan.append(path)

        total_files = len(files_to_scan)
        result = ScanResult()

        for i, file_path in enumerate(files_to_scan, 1):
            file_result = self.scan_file(file_path, relative_to=relative_to or root)

            result.functions.extend(file_result.functions)
            result.files_scanned += file_result.files_scanned
            result.files_skipped += file_result.files_skipped
            result.errors.extend(file_result.errors)
            result.file_imports.update(file_result.file_imports)
            result.skipped_functions.extend(file_result.skipped_functions)
            result.partial_functions.extend(file_result.partial_functions)
            result.skipped_constructs += file_result.skipped_constructs

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
        relative_to: Optional[Path] = None,
    ) -> Iterator[tuple[Path, ScanResult]]:
        """Iterate through directory, yielding scan results for each file.

        This is useful for streaming results without loading all into memory.

        Args:
            directory: Path to the directory to scan.
            recursive: If True, scan subdirectories recursively.
            relative_to: Base path for computing each file's relative path
                        (and therefore its `function_id`). Defaults to
                        `directory` itself; see `scan_directory`'s docstring
                        for why a caller may want to pass this explicitly.

        Yields:
            Tuples of (file_path, scan_result) for each scanned file.
        """
        root = Path(directory).resolve()
        if not root.is_dir():
            raise ValueError(f"Not a directory: {root}")

        for path in self._iter_files(root, recursive):
            if self.is_supported(path):
                result = self.scan_file(path, relative_to=relative_to or root)
                yield path, result

    def iter_supported_files(self, directory: str | Path, recursive: bool = True) -> Iterator[Path]:
        """Every file under `directory` that a scan would process: a
        supported language, not matching an ignore pattern."""
        root = Path(directory).resolve()
        for path in self._iter_files(root, recursive):
            if self.is_supported(path):
                yield path

    def _iter_files(self, root: Path, recursive: bool) -> Iterator[Path]:
        """Iterate through files in directory, respecting ignore patterns.

        Args:
            root: Root directory to scan.
            recursive: If True, include subdirectories.

        Yields:
            Path objects for each file.
        """
        import os

        def raise_error(error):
            raise error

        if recursive:
            for directory, directories, filenames in os.walk(root, onerror=raise_error):
                base = Path(directory)
                directories[:] = [name for name in directories if not self.should_ignore(base / name, root)]
                for name in filenames:
                    path = base / name
                    if not self.should_ignore(path, root):
                        yield path
        else:
            for path in root.iterdir():
                if path.is_file() and not self.should_ignore(path, root):
                    yield path


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
