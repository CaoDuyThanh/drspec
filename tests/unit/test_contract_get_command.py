"""Tests for the drspec contract get command.

The commands run in this process on a database held in memory (`kept_duckdb`): what is checked is what `contract get` answers for the rows
it finds, not that a file reaches a disk. The project folder is the working folder (`project`); no `init` is run (its files are not what
`contract get` reads)."""

import json

import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.cli.commands.contract import find_similar_functions, validate_function_id
from drspec.db import get_connection, init_schema, insert_artifact, insert_contract

runner = CliRunner()


def _function(conn, function_id: str, name: str, status: str = "PENDING", code_hash: str = "hash") -> None:
    """One function row, with the signature and lines every test here has in common."""
    path = function_id.split("::")[0]
    insert_artifact(
        conn,
        function_id=function_id,
        file_path=path,
        function_name=name,
        signature=f"def {name}():",
        body="pass",
        code_hash=code_hash,
        language="python",
        start_line=1,
        end_line=2,
        status=status,
    )


def _contract(signature: str, intent: str, invariants: list, io_examples: list = None) -> str:
    contract = {"function_signature": signature, "intent_summary": intent, "invariants": invariants}
    if io_examples is not None:
        contract["io_examples"] = io_examples
    return json.dumps(contract)


def _get(function_id: str):
    result = runner.invoke(app, ["--json", "contract", "get", function_id])
    return result, json.loads(result.output)


@pytest.fixture
def memory():
    """A database in memory with the schema: what `find_similar_functions` reads."""
    import duckdb

    connection = duckdb.connect(":memory:")
    init_schema(connection)
    yield connection
    connection.close()


@pytest.fixture
def project(tmp_path, monkeypatch, kept_duckdb, cached_cli_command):
    """The working folder of a project whose database is initialised, and its open connection (the commands see the same database)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "_drspec").mkdir()
    connection = get_connection(tmp_path / "_drspec" / "contracts.db")  # pattern-ok: duckdb-file `kept_duckdb` makes this database one in memory (no file)
    init_schema(connection)
    yield connection
    connection.close()


class TestValidateFunctionId:
    def test_valid_function_id(self):
        is_valid, error = validate_function_id("src/utils.py::helper")
        assert is_valid is True
        assert error is None

    def test_valid_function_id_with_path(self):
        is_valid, error = validate_function_id("src/deep/path/module.py::some_function")
        assert is_valid is True
        assert error is None

    def test_invalid_missing_separator(self):
        is_valid, error = validate_function_id("src/utils.py/helper")
        assert is_valid is False
        assert "::" in error

    def test_invalid_empty_filepath(self):
        is_valid, error = validate_function_id("::function_name")
        assert is_valid is False
        assert "filepath" in error.lower()

    def test_invalid_empty_function_name(self):
        is_valid, error = validate_function_id("src/utils.py::")
        assert is_valid is False
        assert "function_name" in error.lower()

    def test_valid_with_multiple_separators(self):
        # splits only on the first :: , so class::method still parses
        is_valid, error = validate_function_id("src/utils.py::ClassName::method")
        assert is_valid is True
        assert error is None


class TestFindSimilarFunctions:
    def test_find_similar_by_function_name(self, memory):
        _function(memory, "src/utils.py::helper_function", "helper_function", code_hash="hash1")
        _function(memory, "src/other.py::helper", "helper", code_hash="hash2")
        _function(memory, "src/main.py::main", "main", code_hash="hash3")

        suggestions = find_similar_functions(memory, "src/missing.py::helper")

        assert len(suggestions) >= 1
        assert any("helper" in s for s in suggestions)

    def test_find_similar_by_path(self, memory):
        _function(memory, "src/payments/process.py::process_payment", "process_payment", code_hash="hash1")

        # no name match ("other_func" is in no function name), so this exercises the path-based fallback: the same file
        suggestions = find_similar_functions(memory, "src/payments/process.py::other_func")

        assert suggestions == ["src/payments/process.py::process_payment"]
        # (the old data asked for `src/payments/other.py`, a path that is in no stored path: the fallback was never reached and the
        # answer, an empty list, satisfied `isinstance(list)`)
        assert find_similar_functions(memory, "src/payments/other.py::other_func") == []

    def test_find_similar_respects_limit(self, memory):
        for i in range(10):
            _function(memory, f"src/module{i}.py::helper{i}", f"helper{i}", code_hash=f"hash{i}")

        suggestions = find_similar_functions(memory, "src/x.py::helper", limit=3)

        assert len(suggestions) == 3  # (ten match; was: at most 3, which an empty answer also satisfies)


class TestContractGetCommand:
    def test_get_contract_not_initialized(self, tmp_path, monkeypatch, kept_duckdb, cached_cli_command):
        monkeypatch.chdir(tmp_path)  # a folder with no `_drspec`
        result, response = _get("src/test.py::func")

        assert result.exit_code == 1
        assert response["success"] is False
        assert response["error"]["code"] == "DB_NOT_INITIALIZED"

    def test_get_contract_invalid_function_id(self, project):
        result, response = _get("invalid_no_separator")

        assert result.exit_code == 1
        assert response["success"] is False
        assert response["error"]["code"] == "INVALID_FUNCTION_ID"

    def test_get_contract_not_found(self, project):
        result, response = _get("src/test.py::nonexistent")

        assert result.exit_code == 1
        assert response["success"] is False
        assert response["error"]["code"] == "CONTRACT_NOT_FOUND"
        assert "suggestions" in response["error"]["details"]

    def test_get_contract_success(self, project):
        _function(project, "src/utils.py::helper", "helper", status="VERIFIED", code_hash="testhash")
        project.execute("UPDATE artifacts SET signature = 'def helper(x: int) -> int:', body = 'return x + 1' WHERE function_id = 'src/utils.py::helper'")
        contract_json = _contract(
            "def helper(x: int) -> int",
            "Adds one to the input and returns the result",
            [{"name": "output_greater", "logic": "Output is always greater than input", "criticality": "HIGH", "on_fail": "error"}],
            [],
        )
        insert_contract(project, "src/utils.py::helper", contract_json, 0.85)

        result, response = _get("src/utils.py::helper")

        assert result.exit_code == 0
        assert response["success"] is True
        assert response["data"]["function_id"] == "src/utils.py::helper"
        assert response["data"]["confidence"]["base"] == 85
        assert response["data"]["confidence"]["adjusted"] == 85  # no findings, no penalty
        assert response["data"]["confidence"]["vision_penalty"] == 0
        assert response["data"]["confidence"]["active_findings"] == 0
        assert response["data"]["status"] == "VERIFIED"
        assert "contract" in response["data"]
        assert response["data"]["contract"]["function_signature"] == "def helper(x: int) -> int"
        assert len(response["data"]["contract"]["invariants"]) == 1

    def test_get_contract_with_suggestions(self, project):
        # artifacts only, no contracts, so lookup must fall back to suggestions
        _function(project, "src/utils.py::process_data", "process_data", code_hash="hash1")
        _function(project, "src/other.py::process", "process", code_hash="hash2")

        result, response = _get("src/missing.py::process")

        assert result.exit_code == 1
        assert response["error"]["code"] == "CONTRACT_NOT_FOUND"
        suggestions = response["error"]["details"]["suggestions"]
        assert any("process" in s for s in suggestions)

    def test_get_contract_empty_filepath_invalid(self, project):
        result, response = _get("::function")

        assert result.exit_code == 1
        assert response["error"]["code"] == "INVALID_FUNCTION_ID"

    def test_get_contract_empty_function_invalid(self, project):
        result, response = _get("src/file.py::")

        assert result.exit_code == 1
        assert response["error"]["code"] == "INVALID_FUNCTION_ID"

    def test_get_contract_with_io_examples(self, project):
        _function(project, "src/math.py::add", "add", code_hash="addhash")
        contract_json = _contract(
            "def add(a: int, b: int) -> int",
            "Adds two integers and returns their sum",
            [{"name": "commutative", "logic": "add(a, b) equals add(b, a)", "criticality": "HIGH", "on_fail": "error"}],
            [{"input": {"a": 1, "b": 2}, "output": 3}, {"input": {"a": -1, "b": 1}, "output": 0}],
        )
        insert_contract(project, "src/math.py::add", contract_json, 0.95)

        result, response = _get("src/math.py::add")

        assert result.exit_code == 0
        assert response["success"] is True
        assert len(response["data"]["contract"]["io_examples"]) == 2

    def test_get_contract_timestamps(self, project):
        _function(project, "src/test.py::func", "func")
        contract_json = _contract(
            "def func()", "A test function that does nothing",
            [{"name": "always_none", "logic": "Always returns None", "criticality": "LOW", "on_fail": "warn"}],
        )
        insert_contract(project, "src/test.py::func", contract_json, 0.5)

        result, response = _get("src/test.py::func")

        assert result.exit_code == 0
        assert "created_at" in response["data"]
        assert "updated_at" in response["data"]
        assert response["data"]["created_at"] is not None

    def test_get_contract_normalizes_legacy_confidence(self, project):
        _function(project, "src/legacy.py::old_func", "old_func", code_hash="legacyhash")
        contract_json = _contract(
            "def old_func()", "Legacy function",
            [{"name": "test", "logic": "Test", "criticality": "LOW", "on_fail": "warn"}],
        )
        # bypass insert_contract to simulate legacy 0-100 scale data (88.0, not 0.88)
        project.execute(
            """INSERT INTO contracts (function_id, contract_json, confidence_score)
                   VALUES (?, ?, 88.0)""",
            ["src/legacy.py::old_func", contract_json],
        )

        result, response = _get("src/legacy.py::old_func")

        assert result.exit_code == 0
        # must normalize to 88, not treat it as already-fractional and show 8800
        assert response["data"]["confidence"]["base"] == 88
        assert response["data"]["confidence"]["adjusted"] == 88

    def test_get_contract_normal_confidence(self, project):
        _function(project, "src/normal.py::func", "func", code_hash="normalhash")
        contract_json = _contract(
            "def func()", "Normal function",
            [{"name": "test", "logic": "Test", "criticality": "LOW", "on_fail": "warn"}],
        )
        insert_contract(project, "src/normal.py::func", contract_json, 0.75)

        result, response = _get("src/normal.py::func")

        assert result.exit_code == 0
        assert response["data"]["confidence"]["base"] == 75
        assert response["data"]["confidence"]["adjusted"] == 75
