"""Tests for `drspec source get`.

The commands run in this process on a database held in memory (`kept_duckdb`): what is checked is what `source get` answers for the rows it
finds, not that a file reaches a disk. The project folder is the working folder (`project`); no `init` is run."""

import json

import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.db import get_connection, init_schema, insert_artifact

runner = CliRunner()


def _store(conn, function_id: str, signature: str, body: str, language: str, start_line: int, end_line: int, code_hash: str) -> None:
    path, name = function_id.split("::")
    insert_artifact(
        conn, function_id=function_id, file_path=path, function_name=name, signature=signature, body=body,
        code_hash=code_hash, language=language, start_line=start_line, end_line=end_line,
    )


def _get(function_id: str):
    result = runner.invoke(app, ["--json", "source", "get", function_id])
    return result, json.loads(result.output)


@pytest.fixture
def project(tmp_path, monkeypatch, kept_duckdb, cached_cli_command):
    """The working folder of a project whose database is initialised, and its open connection (the commands see the same database)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "_drspec").mkdir()
    connection = get_connection(tmp_path / "_drspec" / "contracts.db")
    init_schema(connection)
    yield connection
    connection.close()


class TestSourceGetCommand:
    def test_get_requires_init(self, tmp_path, monkeypatch, kept_duckdb, cached_cli_command):
        monkeypatch.chdir(tmp_path)  # a folder with no `_drspec`
        result, response = _get("src/test.py::func")

        assert result.exit_code == 1
        assert response["success"] is False
        assert response["error"]["code"] == "DB_NOT_INITIALIZED"

    def test_get_invalid_function_id_no_separator(self, project):
        result, response = _get("invalid_id")

        assert result.exit_code == 1
        assert response["success"] is False
        assert response["error"]["code"] == "INVALID_FUNCTION_ID"
        assert "::" in response["error"]["message"]

    def test_get_invalid_function_id_empty_parts(self, project):
        for function_id in ("::func", "src/test.py::"):
            result, response = _get(function_id)
            assert result.exit_code == 1
            assert response["error"]["code"] == "INVALID_FUNCTION_ID"

    def test_get_function_not_found(self, project):
        result, response = _get("src/test.py::nonexistent")

        assert result.exit_code == 1
        assert response["success"] is False
        assert response["error"]["code"] == "FUNCTION_NOT_FOUND"
        assert "suggestions" in response["error"]["details"]

    def test_get_function_not_found_with_suggestions(self, project):
        _store(
            project, "src/utils.py::helper_func", "def helper_func(x: int) -> int:",
            "def helper_func(x: int) -> int:\n    return x + 1", "python", 10, 12, "hash1",
        )

        result, response = _get("src/other.py::helper")

        assert result.exit_code == 1
        assert response["error"]["code"] == "FUNCTION_NOT_FOUND"
        assert "src/utils.py::helper_func" in response["error"]["details"]["suggestions"]

    def test_get_success(self, project):
        body = """def calculate_total(items: list) -> float:
    \"\"\"Calculate the total price of items.\"\"\"
    total = 0.0
    for item in items:
        total += item.price
    return total"""
        _store(
            project, "src/payments/calc.py::calculate_total", "def calculate_total(items: list) -> float:",
            body, "python", 45, 51, "hash123",
        )

        result, response = _get("src/payments/calc.py::calculate_total")

        assert result.exit_code == 0
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

    def test_get_with_invariant_hints_python(self, project):
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
        _store(project, "src/reconcile.py::reconcile", "def reconcile(pending, posted):", body, "python", 10, 21, "hash456")

        result, response = _get("src/reconcile.py::reconcile")

        assert result.exit_code == 0
        assert response["success"] is True
        hints = response["data"]["hints"]
        assert len(hints) == 2
        assert hints[0]["line"] == 12  # start_line (10) + line index (2)
        assert hints[0]["text"] == "No duplicate transaction IDs in output"
        assert hints[1]["line"] == 15  # start_line (10) + line index (5)
        assert hints[1]["text"] == "Sum of amounts must equal input sum"

    def test_get_with_invariant_hints_javascript(self, project):
        body = """function validate(input) {
    // @invariant: Input must be non-null
    if (!input) throw new Error('Invalid');
    /* @invariant: Return value is always positive */
    return Math.abs(input);
}"""
        _store(project, "src/utils.js::validate", "function validate(input)", body, "javascript", 1, 6, "hash789")

        result, response = _get("src/utils.js::validate")

        assert result.exit_code == 0
        hints = response["data"]["hints"]
        assert len(hints) == 2
        assert hints[0]["text"] == "Input must be non-null"
        assert hints[1]["text"] == "Return value is always positive"

    def test_get_with_invariant_hints_cpp(self, project):
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
        _store(project, "src/processor.cpp::process", "int process(int* arr, int size)", body, "cpp", 20, 29, "hash_cpp")

        result, response = _get("src/processor.cpp::process")

        assert result.exit_code == 0
        hints = response["data"]["hints"]
        assert len(hints) == 2
        assert hints[0]["text"] == "arr must not be null"
        assert hints[1]["text"] == "size must be positive"

    def test_get_no_hints_when_none_present(self, project):
        body = """def simple_func():
    # This is a regular comment
    \"\"\"Just a docstring.\"\"\"
    return 42"""
        _store(project, "src/simple.py::simple_func", "def simple_func():", body, "python", 1, 4, "hash_simple")

        result, response = _get("src/simple.py::simple_func")

        assert result.exit_code == 0
        assert response["data"]["hints"] == []

    def test_get_case_insensitive_invariant(self, project):
        body = """def test():
    # @INVARIANT: uppercase hint
    # @Invariant: mixed case hint
    pass"""
        _store(project, "src/test.py::test", "def test():", body, "python", 1, 4, "hash_case")

        result, response = _get("src/test.py::test")

        assert result.exit_code == 0
        hints = response["data"]["hints"]
        assert len(hints) == 2
        assert hints[0]["text"] == "uppercase hint"
        assert hints[1]["text"] == "mixed case hint"

    def test_get_help(self):
        result = runner.invoke(app, ["source", "get", "--help"])
        assert result.exit_code == 0
        assert "function_id" in result.stdout.lower()
        assert "filepath::name#hash" in result.stdout.lower()
