"""`drspec verify run` and `verify script` through the command runner. The database is in memory (`kept_duckdb`: one DuckDB kept between the
commands, no file), the command tree is built once, and the verification script runs in this process (`scripts_in_process_unless_integration`)."""

from __future__ import annotations

import dataclasses
import json

import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.cli.commands import verify as verify_module
from drspec.db import get_connection, init_schema, insert_artifact, insert_contract

pytestmark = [
    pytest.mark.component,
    pytest.mark.usefixtures("scripts_in_process_unless_integration", "kept_duckdb", "cached_cli_command"),  # no test here starts a Python process, file or tool
]


runner = CliRunner()


# =============================================================================
# Fixtures
# =============================================================================


NON_EMPTY = {"name": "non_empty", "logic": "Output is not empty", "criticality": "HIGH", "on_fail": "error"}
ALL_POSITIVE = {"name": "all_positive", "logic": "All values in output are positive", "criticality": "MEDIUM", "on_fail": "warn"}


def _database(tmp_path, function_name=None, invariants=()):
    """A project database (in memory, kept between the commands) with one function and, when `invariants` are given, its contract."""
    db_path = tmp_path / "_drspec" / "drspec.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(str(db_path))
    init_schema(conn)
    if function_name:
        function_id = f"src/test.py::{function_name}"
        insert_artifact(
            conn,
            function_id=function_id,
            file_path="src/test.py",
            function_name=function_name,
            signature=f"def {function_name}() -> list",
            body=f"def {function_name}() -> list:\n    return [1, 2, 3]",
            code_hash="abc123",
            language="python",
            start_line=1,
            end_line=2,
        )
        if invariants:
            contract_json = json.dumps({
                "function_signature": f"def {function_name}() -> list",
                "intent_summary": "Returns a list of items",
                "invariants": list(invariants),
                "io_examples": [],
            })
            insert_contract(conn, function_id=function_id, contract_json=contract_json, confidence_score=0.85)
    conn.close()
    return str(db_path)


@pytest.fixture
def db_with_contract(tmp_path):
    return _database(tmp_path, "get_items", [NON_EMPTY])


@pytest.fixture
def db_without_contract(tmp_path):
    return _database(tmp_path)


@pytest.fixture
def executor_calls(monkeypatch):
    """The calls of `verify run` to the executor: it still runs the script (in this process), and the time it took is set to 0.1234 s."""
    calls = []
    real = verify_module.execute_verification

    def spy(**kwargs):
        calls.append(kwargs)
        return dataclasses.replace(real(**kwargs), execution_time=0.1234)

    monkeypatch.setattr(verify_module, "execute_verification", spy)
    return calls


# =============================================================================
# verify run Tests
# =============================================================================


class TestVerifyRun:
    def test_run_passing_verification(self, db_with_contract):
        test_data = json.dumps({"input": {}, "output": [1, 2, 3]})

        result = runner.invoke(
            app,
            ["--json", "--db", db_with_contract, "verify", "run", "src/test.py::get_items"],
            input=test_data,
        )

        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["success"] is True
        assert data["data"]["passed"] is True
        assert data["data"]["function_id"] == "src/test.py::get_items"

    def test_run_failing_verification(self, db_with_contract):
        # Empty list should fail the "non_empty" invariant
        test_data = json.dumps({"input": {}, "output": []})

        result = runner.invoke(
            app,
            ["--json", "--db", db_with_contract, "verify", "run", "src/test.py::get_items"],
            input=test_data,
        )

        assert result.exit_code == 1
        data = json.loads(result.stdout)
        assert data["success"] is True  # Command succeeded but verification failed
        assert data["data"]["passed"] is False
        assert "non_empty" in data["data"]["message"]

    def test_run_with_failed_invariant_details(self, db_with_contract):
        test_data = json.dumps({"input": {}, "output": []})

        result = runner.invoke(
            app,
            ["--json", "--db", db_with_contract, "verify", "run", "src/test.py::get_items"],
            input=test_data,
        )

        data = json.loads(result.stdout)
        assert "failed_invariant" in data["data"]
        assert data["data"]["failed_invariant"]["name"] == "non_empty"
        assert data["data"]["failed_invariant"]["criticality"] == "HIGH"

    def test_run_contract_not_found(self, db_without_contract):
        test_data = json.dumps({"input": {}, "output": []})

        result = runner.invoke(
            app,
            ["--json", "--db", db_without_contract, "verify", "run", "src/missing.py::func"],
            input=test_data,
        )

        assert result.exit_code == 1
        data = json.loads(result.stdout)
        assert data["success"] is False
        assert data["error"]["code"] == "CONTRACT_NOT_FOUND"

    def test_run_invalid_function_id(self, db_with_contract):
        test_data = json.dumps({"input": {}, "output": []})

        result = runner.invoke(
            app,
            ["--json", "--db", db_with_contract, "verify", "run", "invalid_id"],
            input=test_data,
        )

        assert result.exit_code == 1
        data = json.loads(result.stdout)
        assert data["success"] is False
        assert data["error"]["code"] == "INVALID_FUNCTION_ID"

    def test_run_no_stdin_data(self, db_with_contract):
        result = runner.invoke(
            app,
            ["--json", "--db", db_with_contract, "verify", "run", "src/test.py::get_items"],
            input="",
        )

        assert result.exit_code == 1
        assert result.output.strip() != ""
        data = json.loads(result.output)
        assert data["success"] is False
        assert data["error"]["code"] == "INVALID_JSON"

    def test_run_invalid_json_stdin(self, db_with_contract):
        result = runner.invoke(
            app,
            ["--json", "--db", db_with_contract, "verify", "run", "src/test.py::get_items"],
            input="not valid json",
        )

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["success"] is False
        assert data["error"]["code"] == "INVALID_JSON"

    def test_run_missing_input_key(self, db_with_contract):
        test_data = json.dumps({"output": []})

        result = runner.invoke(
            app,
            ["--json", "--db", db_with_contract, "verify", "run", "src/test.py::get_items"],
            input=test_data,
        )

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["success"] is False
        assert "input" in data["error"]["message"].lower()

    def test_run_missing_output_key(self, db_with_contract):
        test_data = json.dumps({"input": {}})

        result = runner.invoke(
            app,
            ["--json", "--db", db_with_contract, "verify", "run", "src/test.py::get_items"],
            input=test_data,
        )

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["success"] is False
        assert "output" in data["error"]["message"].lower()

    def test_run_reports_the_time_of_the_script_in_whole_milliseconds(self, db_with_contract, executor_calls):
        result = runner.invoke(
            app,
            ["--json", "--db", db_with_contract, "verify", "run", "src/test.py::get_items"],
            input=json.dumps({"input": {}, "output": [1, 2, 3]}),
        )

        assert result.exit_code == 0
        assert json.loads(result.stdout)["data"]["execution_time_ms"] == 123  # the executor said 0.1234 s

    def test_run_hands_the_timeout_to_the_executor(self, db_with_contract, executor_calls):
        test_data = json.dumps({"input": {}, "output": [1, 2, 3]})
        base = ["--json", "--db", db_with_contract, "verify", "run", "src/test.py::get_items"]

        assert runner.invoke(app, base + ["--timeout", "2.0"], input=test_data).exit_code == 0
        assert runner.invoke(app, base, input=test_data).exit_code == 0

        assert [call["timeout"] for call in executor_calls] == [2.0, verify_module.DEFAULT_TIMEOUT]
        assert executor_calls[0]["input_data"] == {} and executor_calls[0]["output_data"] == [1, 2, 3]


# =============================================================================
# verify script Tests
# =============================================================================


class TestVerifyScript:
    def test_script_returns_verification_code(self, db_with_contract):
        result = runner.invoke(
            app,
            ["--json", "--db", db_with_contract, "verify", "script", "src/test.py::get_items"],
        )

        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["success"] is True
        assert "script" in data["data"]
        assert "def verify(" in data["data"]["script"]
        assert "non_empty" in data["data"]["script"]

    def test_script_contract_not_found(self, db_without_contract):
        result = runner.invoke(
            app,
            ["--json", "--db", db_without_contract, "verify", "script", "src/missing.py::func"],
        )

        assert result.exit_code == 1
        data = json.loads(result.stdout)
        assert data["success"] is False
        assert data["error"]["code"] == "CONTRACT_NOT_FOUND"

    def test_script_invalid_function_id(self, db_with_contract):
        result = runner.invoke(
            app,
            ["--json", "--db", db_with_contract, "verify", "script", "invalid"],
        )

        assert result.exit_code == 1
        data = json.loads(result.stdout)
        assert data["success"] is False
        assert data["error"]["code"] == "INVALID_FUNCTION_ID"


# =============================================================================
# Integration Tests
# =============================================================================


class TestVerifyIntegration:
    def test_verify_multiple_invariants(self, tmp_path):
        db_path = _database(tmp_path, "process", [NON_EMPTY, ALL_POSITIVE])
        base = ["--json", "--db", db_path, "verify", "run", "src/test.py::process"]

        result = runner.invoke(app, base, input=json.dumps({"input": {}, "output": [1, 2, 3]}))
        assert result.exit_code == 0
        assert json.loads(result.stdout)["data"]["passed"] is True

        result = runner.invoke(app, base, input=json.dumps({"input": {}, "output": []}))
        assert result.exit_code == 1
        data = json.loads(result.stdout)["data"]
        assert data["passed"] is False
        assert data["failed_invariant"]["name"] == "non_empty"  # the first invariant that fails is named
