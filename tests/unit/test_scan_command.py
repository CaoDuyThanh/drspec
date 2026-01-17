"""Tests for the drspec scan command."""

import json
import tempfile
from pathlib import Path

from typer.testing import CliRunner

from drspec.cli.app import app


runner = CliRunner()


class TestScanCommand:
    """Tests for scan command."""

    def test_scan_requires_init(self):
        """Test scan fails if drspec not initialized."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                result = runner.invoke(app, ["scan"])

                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["success"] is False
                assert response["error"]["code"] == "DB_NOT_INITIALIZED"

    def test_scan_path_not_found(self):
        """Test scan fails for non-existent path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                # Initialize first
                runner.invoke(app, ["init"])

                result = runner.invoke(app, ["scan", "/nonexistent/path"])

                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["success"] is False
                assert response["error"]["code"] == "PATH_NOT_FOUND"

    def test_scan_empty_directory(self):
        """Test scanning an empty directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                # Initialize
                runner.invoke(app, ["init"])

                # Create an empty subdirectory to scan
                Path("src").mkdir()

                result = runner.invoke(app, ["scan", "src"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["success"] is True
                assert response["data"]["files_scanned"] == 0
                assert response["data"]["functions_found"] == 0

    def test_scan_python_file(self):
        """Test scanning a Python file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                # Initialize
                runner.invoke(app, ["init"])

                # Create a Python file
                Path("test.py").write_text('''
def hello():
    return "world"

def add(a, b):
    return a + b
''')

                result = runner.invoke(app, ["scan", "test.py"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["success"] is True
                assert response["data"]["files_scanned"] == 1
                assert response["data"]["functions_found"] == 2
                assert response["data"]["functions_new"] == 2

    def test_scan_directory_with_python(self):
        """Test scanning a directory with Python files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                # Initialize
                runner.invoke(app, ["init"])

                # Create source directory with files
                Path("src").mkdir()
                Path("src/module.py").write_text('''
def process():
    pass
''')
                Path("src/utils.py").write_text('''
def helper():
    pass
''')

                result = runner.invoke(app, ["scan", "src"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["success"] is True
                assert response["data"]["files_scanned"] == 2
                assert response["data"]["functions_found"] == 2

    def test_scan_recursive(self):
        """Test recursive directory scanning."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                # Initialize
                runner.invoke(app, ["init"])

                # Create nested structure
                Path("src").mkdir()
                Path("src/core").mkdir()
                Path("src/module.py").write_text('def foo(): pass')
                Path("src/core/engine.py").write_text('def bar(): pass')

                # Options must come before path argument
                result = runner.invoke(app, ["scan", "--recursive", "src"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["success"] is True
                assert response["data"]["files_scanned"] == 2
                assert response["data"]["functions_found"] == 2

    def test_scan_non_recursive(self):
        """Test non-recursive directory scanning."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                # Initialize
                runner.invoke(app, ["init"])

                # Create nested structure
                Path("src").mkdir()
                Path("src/core").mkdir()
                Path("src/module.py").write_text('def foo(): pass')
                Path("src/core/engine.py").write_text('def bar(): pass')

                # Options must come before path argument
                result = runner.invoke(app, ["scan", "--no-recursive", "src"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["success"] is True
                assert response["data"]["files_scanned"] == 1  # Only top-level file
                assert response["data"]["functions_found"] == 1

    def test_scan_javascript_file(self):
        """Test scanning a JavaScript file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                # Initialize
                runner.invoke(app, ["init"])

                # Create a JavaScript file
                Path("app.js").write_text('''
function greet() {
    return "hello";
}

const add = (a, b) => a + b;
''')

                result = runner.invoke(app, ["scan", "app.js"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["success"] is True
                assert response["data"]["files_scanned"] == 1
                assert response["data"]["functions_found"] == 2

    def test_scan_no_queue(self):
        """Test scanning without queueing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                # Initialize
                runner.invoke(app, ["init"])

                # Create a Python file
                Path("test.py").write_text('def hello(): pass')

                # Options must come before path argument
                result = runner.invoke(app, ["scan", "--no-queue", "test.py"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["success"] is True
                assert response["data"]["queue_enabled"] is False

    def test_scan_unchanged_functions(self):
        """Test scanning unchanged functions returns unchanged count."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                # Initialize
                runner.invoke(app, ["init"])

                # Create a Python file
                Path("test.py").write_text('def hello(): pass')

                # First scan
                runner.invoke(app, ["scan", "test.py"])

                # Second scan - same file
                result = runner.invoke(app, ["scan", "test.py"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["success"] is True
                assert response["data"]["functions_unchanged"] == 1
                assert response["data"]["functions_new"] == 0

    def test_scan_returns_json(self):
        """Test that scan command returns valid JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                # Initialize
                runner.invoke(app, ["init"])

                result = runner.invoke(app, ["scan"])

                # Should not raise when parsing
                response = json.loads(result.output)
                assert "success" in response
                assert "data" in response
                assert "error" in response

    def test_scan_default_current_directory(self):
        """Test scan defaults to current directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                # Initialize
                runner.invoke(app, ["init"])

                # Create a file in current directory
                Path("test.py").write_text('def foo(): pass')

                result = runner.invoke(app, ["scan"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["success"] is True
                # Should find the function (excluding _drspec directory)
                assert response["data"]["functions_found"] >= 1
