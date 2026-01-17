"""Tests for the unified function extraction engine."""

import tempfile
from pathlib import Path

import pytest

from drspec.core.scanner import (
    Scanner,
    ScannedFunction,
    ScanProgress,
    ScanResult,
    scan_file,
    scan_directory,
    LANGUAGE_MAP,
    DEFAULT_IGNORES,
)


@pytest.fixture
def scanner():
    """Create a Scanner instance."""
    return Scanner()


@pytest.fixture
def temp_project():
    """Create a temporary project directory with sample files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)

        # Create Python file
        py_file = root / "main.py"
        py_file.write_text('''
def hello():
    return "world"

class Calculator:
    def add(self, a, b):
        return a + b
''')

        # Create JavaScript file
        js_file = root / "app.js"
        js_file.write_text('''
function greet() {
    return "hello";
}

const add = (a, b) => a + b;
''')

        # Create C++ file
        cpp_file = root / "utils.cpp"
        cpp_file.write_text('''
void process() {
    return;
}

class Helper {
public:
    void run() {
        return;
    }
};
''')

        # Create header file
        header_file = root / "utils.h"
        header_file.write_text('''
void process();
''')

        # Create subdirectory with file
        subdir = root / "lib"
        subdir.mkdir()
        (subdir / "helper.py").write_text('''
def helper_func():
    pass
''')

        # Create ignored directory
        gitdir = root / ".git"
        gitdir.mkdir()
        (gitdir / "config").write_text("# git config")

        # Create node_modules (should be ignored)
        node_modules = root / "node_modules"
        node_modules.mkdir()
        (node_modules / "package.js").write_text('''
function ignored() {}
''')

        yield root


class TestLanguageDetection:
    """Tests for language detection."""

    def test_detect_python(self, scanner):
        """Test detecting Python files."""
        assert scanner.detect_language("main.py") == "python"
        assert scanner.detect_language("script.pyw") == "python"

    def test_detect_javascript(self, scanner):
        """Test detecting JavaScript files."""
        assert scanner.detect_language("app.js") == "javascript"
        assert scanner.detect_language("component.jsx") == "javascript"
        assert scanner.detect_language("module.mjs") == "javascript"
        assert scanner.detect_language("common.cjs") == "javascript"

    def test_detect_cpp(self, scanner):
        """Test detecting C++ files."""
        assert scanner.detect_language("main.cpp") == "cpp"
        assert scanner.detect_language("util.cc") == "cpp"
        assert scanner.detect_language("lib.cxx") == "cpp"
        assert scanner.detect_language("legacy.c") == "cpp"
        assert scanner.detect_language("header.h") == "cpp"
        assert scanner.detect_language("header.hpp") == "cpp"

    def test_detect_unsupported(self, scanner):
        """Test unsupported file types."""
        assert scanner.detect_language("readme.md") is None
        assert scanner.detect_language("data.json") is None
        assert scanner.detect_language("image.png") is None

    def test_is_supported(self, scanner):
        """Test is_supported method."""
        assert scanner.is_supported("main.py") is True
        assert scanner.is_supported("readme.md") is False


class TestIgnorePatterns:
    """Tests for ignore pattern handling."""

    def test_default_ignores(self, scanner):
        """Test that default ignores are loaded."""
        assert ".git" in scanner.ignore_patterns
        assert "node_modules" in scanner.ignore_patterns
        assert "__pycache__" in scanner.ignore_patterns

    def test_add_ignore_pattern(self, scanner):
        """Test adding ignore patterns."""
        scanner.add_ignore_pattern("*.log")
        assert "*.log" in scanner.ignore_patterns

    def test_should_ignore_directory(self, scanner, temp_project):
        """Test ignoring directories."""
        git_path = temp_project / ".git" / "config"
        assert scanner.should_ignore(git_path, temp_project) is True

        nm_path = temp_project / "node_modules" / "package.js"
        assert scanner.should_ignore(nm_path, temp_project) is True

    def test_should_not_ignore_regular_file(self, scanner, temp_project):
        """Test that regular files are not ignored."""
        main_path = temp_project / "main.py"
        assert scanner.should_ignore(main_path, temp_project) is False

    def test_custom_ignore_patterns(self):
        """Test custom ignore patterns."""
        scanner = Scanner(ignore_patterns=["*.test.py", "vendor"])
        assert "*.test.py" in scanner.ignore_patterns
        assert ".git" not in scanner.ignore_patterns


class TestScanFile:
    """Tests for scanning individual files."""

    def test_scan_python_file(self, scanner, temp_project):
        """Test scanning a Python file."""
        result = scanner.scan_file(temp_project / "main.py", relative_to=temp_project)

        assert result.files_scanned == 1
        assert len(result.functions) == 2  # hello and Calculator.add

        names = {f.name for f in result.functions}
        assert "hello" in names
        assert "add" in names

        # Check function_id format
        hello_func = next(f for f in result.functions if f.name == "hello")
        assert hello_func.function_id == "main.py::hello"
        assert hello_func.language == "python"

    def test_scan_javascript_file(self, scanner, temp_project):
        """Test scanning a JavaScript file."""
        result = scanner.scan_file(temp_project / "app.js", relative_to=temp_project)

        assert result.files_scanned == 1
        assert len(result.functions) == 2  # greet and add

        names = {f.name for f in result.functions}
        assert "greet" in names
        assert "add" in names

        greet_func = next(f for f in result.functions if f.name == "greet")
        assert greet_func.language == "javascript"

    def test_scan_cpp_file(self, scanner, temp_project):
        """Test scanning a C++ file."""
        result = scanner.scan_file(temp_project / "utils.cpp", relative_to=temp_project)

        assert result.files_scanned == 1
        assert len(result.functions) == 2  # process and Helper::run

        names = {f.name for f in result.functions}
        assert "process" in names
        assert "run" in names

    def test_scan_unsupported_file(self, scanner, temp_project):
        """Test scanning unsupported file type."""
        # Create unsupported file
        readme = temp_project / "README.md"
        readme.write_text("# Test")

        result = scanner.scan_file(readme, relative_to=temp_project)

        assert result.files_scanned == 0
        assert result.files_skipped == 1
        assert len(result.functions) == 0

    def test_scan_nonexistent_file(self, scanner, temp_project):
        """Test scanning a file that doesn't exist."""
        result = scanner.scan_file(temp_project / "nonexistent.py", relative_to=temp_project)

        assert result.files_scanned == 0
        assert len(result.errors) == 1


class TestScanDirectory:
    """Tests for directory scanning."""

    def test_scan_directory_recursive(self, scanner, temp_project):
        """Test recursive directory scanning."""
        result = scanner.scan_directory(temp_project, recursive=True)

        # Should find files in root and lib/ but not .git/ or node_modules/
        assert result.files_scanned >= 4  # main.py, app.js, utils.cpp, utils.h, lib/helper.py

        # Check that functions from different languages were found
        languages = {f.language for f in result.functions}
        assert "python" in languages
        assert "javascript" in languages
        assert "cpp" in languages

    def test_scan_directory_non_recursive(self, scanner, temp_project):
        """Test non-recursive directory scanning."""
        result = scanner.scan_directory(temp_project, recursive=False)

        # Should only find files in root, not lib/
        file_paths = {f.file_path for f in result.functions}
        assert not any("lib" in p for p in file_paths)

    def test_scan_directory_ignores_patterns(self, scanner, temp_project):
        """Test that ignore patterns are respected."""
        result = scanner.scan_directory(temp_project, recursive=True)

        # Should not include functions from ignored directories
        function_ids = {f.function_id for f in result.functions}
        assert not any("node_modules" in fid for fid in function_ids)
        assert not any(".git" in fid for fid in function_ids)

    def test_scan_directory_progress_callback(self, scanner, temp_project):
        """Test progress callback during scanning."""
        progress_updates = []

        def on_progress(progress: ScanProgress):
            progress_updates.append(progress)

        result = scanner.scan_directory(
            temp_project,
            recursive=True,
            progress_callback=on_progress,
        )

        # Should have progress updates
        assert len(progress_updates) > 0

        # First update should have current=1
        assert progress_updates[0].current == 1

        # Last update should have current == total
        last = progress_updates[-1]
        assert last.current == last.total


class TestScanFunctionModel:
    """Tests for ScannedFunction model."""

    def test_scanned_function_from_extracted(self):
        """Test creating ScannedFunction from ExtractedFunction."""
        from drspec.parsers.models import ExtractedFunction

        extracted = ExtractedFunction(
            name="test",
            qualified_name="MyClass.test",
            signature="def test(self):",
            body="def test(self):\n    pass",
            start_line=10,
            end_line=11,
            parent="MyClass",
            decorators=["staticmethod"],
            is_method=True,
            is_async=False,
        )

        scanned = ScannedFunction.from_extracted(
            extracted, "src/module.py", "python"
        )

        assert scanned.function_id == "src/module.py::MyClass.test"
        assert scanned.file_path == "src/module.py"
        assert scanned.name == "test"
        assert scanned.qualified_name == "MyClass.test"
        assert scanned.language == "python"
        assert scanned.is_method is True
        assert "staticmethod" in scanned.decorators


class TestConvenienceFunctions:
    """Tests for convenience functions."""

    def test_scan_file_convenience(self, temp_project):
        """Test the scan_file convenience function."""
        result = scan_file(temp_project / "main.py")

        assert result.files_scanned == 1
        assert len(result.functions) >= 1

    def test_scan_directory_convenience(self, temp_project):
        """Test the scan_directory convenience function."""
        result = scan_directory(temp_project)

        assert result.files_scanned >= 1
        assert len(result.functions) >= 1

    def test_scan_directory_custom_ignores(self, temp_project):
        """Test scan_directory with custom ignore patterns."""
        # Include node_modules by not ignoring it
        result = scan_directory(temp_project, ignore_patterns=[".git"])

        function_ids = {f.function_id for f in result.functions}
        # Now node_modules should be included
        assert any("node_modules" in fid for fid in function_ids)


class TestIterScanDirectory:
    """Tests for streaming directory scanning."""

    def test_iter_scan_directory(self, scanner, temp_project):
        """Test iterating through scan results."""
        results = list(scanner.iter_scan_directory(temp_project))

        assert len(results) >= 4

        for path, result in results:
            assert isinstance(path, Path)
            assert isinstance(result, ScanResult)


class TestEdgeCases:
    """Tests for edge cases."""

    def test_empty_directory(self, scanner):
        """Test scanning an empty directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = scanner.scan_directory(tmpdir)

            assert result.files_scanned == 0
            assert len(result.functions) == 0

    def test_syntax_error_handling(self, scanner):
        """Test handling files with syntax errors."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bad_file = root / "bad.py"
            bad_file.write_text("def broken(:\n    pass")

            result = scanner.scan_file(bad_file)

            # Should report errors but not crash
            # The file is scanned and syntax errors are collected
            assert result.files_scanned == 1
            assert len(result.errors) >= 1  # Should have at least one error

    def test_binary_file_handling(self, scanner):
        """Test handling binary files (should skip)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            binary = root / "data.bin"
            binary.write_bytes(b"\x00\x01\x02\x03")

            result = scanner.scan_file(binary)

            # Should skip unsupported file
            assert result.files_skipped == 1
            assert len(result.functions) == 0
