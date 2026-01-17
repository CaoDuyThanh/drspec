"""Tests for the drspec source get command."""

import json
import tempfile
from pathlib import Path

from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.db import get_connection, insert_artifact


runner = CliRunner()


class TestSourceGetCommand:
    """Tests for the source get CLI command."""

    def test_get_requires_init(self):
        """Test source get fails without initialization."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                result = runner.invoke(app, ["source", "get", "src/test.py::func"])

                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["success"] is False
                assert response["error"]["code"] == "DB_NOT_INITIALIZED"

    def test_get_invalid_function_id_no_separator(self):
        """Test source get with invalid function ID (no ::)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                result = runner.invoke(app, ["source", "get", "invalid_id"])

                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["success"] is False
                assert response["error"]["code"] == "INVALID_FUNCTION_ID"
                assert "::" in response["error"]["message"]

    def test_get_invalid_function_id_empty_parts(self):
        """Test source get with invalid function ID (empty parts)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                # Empty filepath
                result = runner.invoke(app, ["source", "get", "::func"])
                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["error"]["code"] == "INVALID_FUNCTION_ID"

                # Empty function name
                result = runner.invoke(app, ["source", "get", "src/test.py::"])
                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["error"]["code"] == "INVALID_FUNCTION_ID"

    def test_get_function_not_found(self):
        """Test source get for non-existent function."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                result = runner.invoke(app, ["source", "get", "src/test.py::nonexistent"])

                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["success"] is False
                assert response["error"]["code"] == "FUNCTION_NOT_FOUND"
                assert "suggestions" in response["error"]["details"]

    def test_get_function_not_found_with_suggestions(self):
        """Test source get shows suggestions when function not found."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                db_path = Path.cwd() / "_drspec" / "contracts.db"
                conn = get_connection(db_path)

                # Insert a function with similar name
                insert_artifact(
                    conn,
                    function_id="src/utils.py::helper_func",
                    file_path="src/utils.py",
                    function_name="helper_func",
                    signature="def helper_func(x: int) -> int:",
                    body="def helper_func(x: int) -> int:\n    return x + 1",
                    code_hash="hash1",
                    language="python",
                    start_line=10,
                    end_line=12,
                )
                conn.close()

                # Search for similar function
                result = runner.invoke(app, ["source", "get", "src/other.py::helper"])

                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["error"]["code"] == "FUNCTION_NOT_FOUND"
                # Should suggest the similar function
                assert "src/utils.py::helper_func" in response["error"]["details"]["suggestions"]

    def test_get_success(self):
        """Test successful source get."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                db_path = Path.cwd() / "_drspec" / "contracts.db"
                conn = get_connection(db_path)

                body = """def calculate_total(items: list) -> float:
    \"\"\"Calculate the total price of items.\"\"\"
    total = 0.0
    for item in items:
        total += item.price
    return total"""

                insert_artifact(
                    conn,
                    function_id="src/payments/calc.py::calculate_total",
                    file_path="src/payments/calc.py",
                    function_name="calculate_total",
                    signature="def calculate_total(items: list) -> float:",
                    body=body,
                    code_hash="hash123",
                    language="python",
                    start_line=45,
                    end_line=51,
                )
                conn.close()

                result = runner.invoke(app, ["source", "get", "src/payments/calc.py::calculate_total"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["success"] is True
                data = response["data"]
                assert data["function_id"] == "src/payments/calc.py::calculate_total"
                assert data["file_path"] == "src/payments/calc.py"
                assert data["function_name"] == "calculate_total"
                assert data["language"] == "python"
                assert data["start_line"] == 45
                assert data["end_line"] == 51
                assert data["signature"] == "def calculate_total(items: list) -> float:"
                assert "calculate_total" in data["body"]
                assert data["hints"] == []

    def test_get_with_invariant_hints_python(self):
        """Test source get extracts @invariant hints from Python comments."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                db_path = Path.cwd() / "_drspec" / "contracts.db"
                conn = get_connection(db_path)

                body = """def reconcile(pending, posted):
    \"\"\"Reconcile transactions.\"\"\"
    # @invariant: No duplicate transaction IDs in output
    result = []
    seen = set()
    # @invariant: Sum of amounts must equal input sum
    for tx in pending:
        if tx.id not in seen:
            result.append(tx)
            seen.add(tx.id)
    return result"""

                insert_artifact(
                    conn,
                    function_id="src/reconcile.py::reconcile",
                    file_path="src/reconcile.py",
                    function_name="reconcile",
                    signature="def reconcile(pending, posted):",
                    body=body,
                    code_hash="hash456",
                    language="python",
                    start_line=10,
                    end_line=21,
                )
                conn.close()

                result = runner.invoke(app, ["source", "get", "src/reconcile.py::reconcile"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["success"] is True
                hints = response["data"]["hints"]
                assert len(hints) == 2
                assert hints[0]["line"] == 12  # start_line (10) + line index (2)
                assert hints[0]["text"] == "No duplicate transaction IDs in output"
                assert hints[1]["line"] == 15  # start_line (10) + line index (5)
                assert hints[1]["text"] == "Sum of amounts must equal input sum"

    def test_get_with_invariant_hints_javascript(self):
        """Test source get extracts @invariant hints from JavaScript comments."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                db_path = Path.cwd() / "_drspec" / "contracts.db"
                conn = get_connection(db_path)

                body = """function validate(input) {
    // @invariant: Input must be non-null
    if (!input) throw new Error('Invalid');
    /* @invariant: Return value is always positive */
    return Math.abs(input);
}"""

                insert_artifact(
                    conn,
                    function_id="src/utils.js::validate",
                    file_path="src/utils.js",
                    function_name="validate",
                    signature="function validate(input)",
                    body=body,
                    code_hash="hash789",
                    language="javascript",
                    start_line=1,
                    end_line=6,
                )
                conn.close()

                result = runner.invoke(app, ["source", "get", "src/utils.js::validate"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                hints = response["data"]["hints"]
                assert len(hints) == 2
                assert hints[0]["text"] == "Input must be non-null"
                assert hints[1]["text"] == "Return value is always positive"

    def test_get_with_invariant_hints_cpp(self):
        """Test source get extracts @invariant hints from C++ comments."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                db_path = Path.cwd() / "_drspec" / "contracts.db"
                conn = get_connection(db_path)

                body = """int process(int* arr, int size) {
    // @invariant: arr must not be null
    if (!arr) return -1;
    // @invariant: size must be positive
    int sum = 0;
    for (int i = 0; i < size; i++) {
        sum += arr[i];
    }
    return sum;
}"""

                insert_artifact(
                    conn,
                    function_id="src/processor.cpp::process",
                    file_path="src/processor.cpp",
                    function_name="process",
                    signature="int process(int* arr, int size)",
                    body=body,
                    code_hash="hash_cpp",
                    language="cpp",
                    start_line=20,
                    end_line=29,
                )
                conn.close()

                result = runner.invoke(app, ["source", "get", "src/processor.cpp::process"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                hints = response["data"]["hints"]
                assert len(hints) == 2
                assert hints[0]["text"] == "arr must not be null"
                assert hints[1]["text"] == "size must be positive"

    def test_get_no_hints_when_none_present(self):
        """Test source get returns empty hints list when no @invariant comments."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                db_path = Path.cwd() / "_drspec" / "contracts.db"
                conn = get_connection(db_path)

                body = """def simple_func():
    # This is a regular comment
    \"\"\"Just a docstring.\"\"\"
    return 42"""

                insert_artifact(
                    conn,
                    function_id="src/simple.py::simple_func",
                    file_path="src/simple.py",
                    function_name="simple_func",
                    signature="def simple_func():",
                    body=body,
                    code_hash="hash_simple",
                    language="python",
                    start_line=1,
                    end_line=4,
                )
                conn.close()

                result = runner.invoke(app, ["source", "get", "src/simple.py::simple_func"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["data"]["hints"] == []

    def test_get_case_insensitive_invariant(self):
        """Test @invariant detection is case-insensitive."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                db_path = Path.cwd() / "_drspec" / "contracts.db"
                conn = get_connection(db_path)

                body = """def test():
    # @INVARIANT: uppercase hint
    # @Invariant: mixed case hint
    pass"""

                insert_artifact(
                    conn,
                    function_id="src/test.py::test",
                    file_path="src/test.py",
                    function_name="test",
                    signature="def test():",
                    body=body,
                    code_hash="hash_case",
                    language="python",
                    start_line=1,
                    end_line=4,
                )
                conn.close()

                result = runner.invoke(app, ["source", "get", "src/test.py::test"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                hints = response["data"]["hints"]
                assert len(hints) == 2
                assert hints[0]["text"] == "uppercase hint"
                assert hints[1]["text"] == "mixed case hint"

    def test_get_help(self):
        """Test source get help displays options."""
        result = runner.invoke(app, ["source", "get", "--help"])
        assert result.exit_code == 0
        assert "function_id" in result.stdout.lower()
        assert "filepath::function_name" in result.stdout.lower()
