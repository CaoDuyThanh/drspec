"""Tests for drspec verify command."""

from __future__ import annotations

import json
from io import StringIO
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.db import get_connection, init_schema, insert_artifact, insert_contract


runner = CliRunner()


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def db_with_contract(tmp_path):
    """Create a database with a contract for testing."""
    db_path = tmp_path / "_drspec" / "drspec.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = get_connection(str(db_path))
    init_schema(conn)

    # Insert an artifact
    insert_artifact(
        conn,
        function_id="src/test.py::get_items",
        file_path="src/test.py",
        function_name="get_items",
        signature="def get_items() -> list",
        body="def get_items() -> list:\n    return [1, 2, 3]",
        code_hash="abc123",
        language="python",
        start_line=1,
        end_line=2,
    )

    # Insert a contract
    contract_json = json.dumps({
        "function_signature": "def get_items() -> list",
        "intent_summary": "Returns a list of items",
        "invariants": [
            {
                "name": "non_empty",
                "logic": "Output is not empty",
                "criticality": "HIGH",
                "on_fail": "error",
            }
        ],
        "io_examples": [],
    })

    insert_contract(
        conn,
        function_id="src/test.py::get_items",
        contract_json=contract_json,
        confidence_score=0.85,
    )

    return str(db_path)


@pytest.fixture
def db_without_contract(tmp_path):
    """Create a database without any contracts."""
    db_path = tmp_path / "_drspec" / "drspec.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = get_connection(str(db_path))
    init_schema(conn)

    return str(db_path)


# =============================================================================
# verify run Tests
# =============================================================================


class TestVerifyRun:
    """Tests for verify run command."""

    def test_run_passing_verification(self, db_with_contract):
        """Should pass when verification succeeds."""
        test_data = json.dumps({"input": {}, "output": [1, 2, 3]})

        result = runner.invoke(
            app,
            ["--db", db_with_contract, "verify", "run", "src/test.py::get_items"],
            input=test_data,
        )

        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["success"] is True
        assert data["data"]["passed"] is True
        assert data["data"]["function_id"] == "src/test.py::get_items"

    def test_run_failing_verification(self, db_with_contract):
        """Should fail when verification fails."""
        # Empty list should fail the "non_empty" invariant
        test_data = json.dumps({"input": {}, "output": []})

        result = runner.invoke(
            app,
            ["--db", db_with_contract, "verify", "run", "src/test.py::get_items"],
            input=test_data,
        )

        assert result.exit_code == 1
        data = json.loads(result.stdout)
        assert data["success"] is True  # Command succeeded but verification failed
        assert data["data"]["passed"] is False
        assert "non_empty" in data["data"]["message"]

    def test_run_with_failed_invariant_details(self, db_with_contract):
        """Should include failed invariant details in response."""
        test_data = json.dumps({"input": {}, "output": []})

        result = runner.invoke(
            app,
            ["--db", db_with_contract, "verify", "run", "src/test.py::get_items"],
            input=test_data,
        )

        data = json.loads(result.stdout)
        assert "failed_invariant" in data["data"]
        assert data["data"]["failed_invariant"]["name"] == "non_empty"
        assert data["data"]["failed_invariant"]["criticality"] == "HIGH"

    def test_run_contract_not_found(self, db_without_contract):
        """Should return error when contract doesn't exist."""
        test_data = json.dumps({"input": {}, "output": []})

        result = runner.invoke(
            app,
            ["--db", db_without_contract, "verify", "run", "src/missing.py::func"],
            input=test_data,
        )

        assert result.exit_code == 1
        data = json.loads(result.stdout)
        assert data["success"] is False
        assert data["error"]["code"] == "CONTRACT_NOT_FOUND"

    def test_run_invalid_function_id(self, db_with_contract):
        """Should return error for invalid function ID."""
        test_data = json.dumps({"input": {}, "output": []})

        result = runner.invoke(
            app,
            ["--db", db_with_contract, "verify", "run", "invalid_id"],
            input=test_data,
        )

        assert result.exit_code == 1
        data = json.loads(result.stdout)
        assert data["success"] is False
        assert data["error"]["code"] == "INVALID_FUNCTION_ID"

    def test_run_no_stdin_data(self, db_with_contract):
        """Should return error when no test data provided."""
        result = runner.invoke(
            app,
            ["--db", db_with_contract, "verify", "run", "src/test.py::get_items"],
            input="",
        )

        assert result.exit_code == 1
        # Check output contains error
        assert result.output.strip() != ""
        data = json.loads(result.output)
        assert data["success"] is False
        assert data["error"]["code"] == "INVALID_JSON"

    def test_run_invalid_json_stdin(self, db_with_contract):
        """Should return error for invalid JSON input."""
        result = runner.invoke(
            app,
            ["--db", db_with_contract, "verify", "run", "src/test.py::get_items"],
            input="not valid json",
        )

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["success"] is False
        assert data["error"]["code"] == "INVALID_JSON"

    def test_run_missing_input_key(self, db_with_contract):
        """Should return error when input key is missing."""
        test_data = json.dumps({"output": []})

        result = runner.invoke(
            app,
            ["--db", db_with_contract, "verify", "run", "src/test.py::get_items"],
            input=test_data,
        )

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["success"] is False
        assert "input" in data["error"]["message"].lower()

    def test_run_missing_output_key(self, db_with_contract):
        """Should return error when output key is missing."""
        test_data = json.dumps({"input": {}})

        result = runner.invoke(
            app,
            ["--db", db_with_contract, "verify", "run", "src/test.py::get_items"],
            input=test_data,
        )

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["success"] is False
        assert "output" in data["error"]["message"].lower()

    def test_run_includes_execution_time(self, db_with_contract):
        """Should include execution time in response."""
        test_data = json.dumps({"input": {}, "output": [1, 2, 3]})

        result = runner.invoke(
            app,
            ["--db", db_with_contract, "verify", "run", "src/test.py::get_items"],
            input=test_data,
        )

        data = json.loads(result.stdout)
        assert "execution_time_ms" in data["data"]
        assert isinstance(data["data"]["execution_time_ms"], int)
        assert data["data"]["execution_time_ms"] >= 0

    def test_run_with_custom_timeout(self, db_with_contract):
        """Should respect custom timeout."""
        test_data = json.dumps({"input": {}, "output": [1, 2, 3]})

        result = runner.invoke(
            app,
            ["--db", db_with_contract, "verify", "run", "src/test.py::get_items", "--timeout", "2.0"],
            input=test_data,
        )

        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["data"]["passed"] is True


# =============================================================================
# verify script Tests
# =============================================================================


class TestVerifyScript:
    """Tests for verify script command."""

    def test_script_returns_verification_code(self, db_with_contract):
        """Should return the verification script."""
        result = runner.invoke(
            app,
            ["--db", db_with_contract, "verify", "script", "src/test.py::get_items"],
        )

        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["success"] is True
        assert "script" in data["data"]
        assert "def verify(" in data["data"]["script"]
        assert "non_empty" in data["data"]["script"]

    def test_script_contract_not_found(self, db_without_contract):
        """Should return error when contract doesn't exist."""
        result = runner.invoke(
            app,
            ["--db", db_without_contract, "verify", "script", "src/missing.py::func"],
        )

        assert result.exit_code == 1
        data = json.loads(result.stdout)
        assert data["success"] is False
        assert data["error"]["code"] == "CONTRACT_NOT_FOUND"

    def test_script_invalid_function_id(self, db_with_contract):
        """Should return error for invalid function ID."""
        result = runner.invoke(
            app,
            ["--db", db_with_contract, "verify", "script", "invalid"],
        )

        assert result.exit_code == 1
        data = json.loads(result.stdout)
        assert data["success"] is False
        assert data["error"]["code"] == "INVALID_FUNCTION_ID"


# =============================================================================
# Integration Tests
# =============================================================================


class TestVerifyIntegration:
    """Integration tests for verify command with complex contracts."""

    def test_verify_multiple_invariants(self, tmp_path):
        """Should handle contracts with multiple invariants."""
        db_path = tmp_path / "_drspec" / "drspec.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)

        conn = get_connection(str(db_path))
        init_schema(conn)

        # Insert artifact
        insert_artifact(
            conn,
            function_id="src/test.py::process",
            file_path="src/test.py",
            function_name="process",
            signature="def process(items: list) -> list",
            body="def process(items: list) -> list:\n    return items",
            code_hash="xyz789",
            language="python",
            start_line=1,
            end_line=2,
        )

        # Contract with multiple invariants
        contract_json = json.dumps({
            "function_signature": "def process(items: list) -> list",
            "intent_summary": "Processes items",
            "invariants": [
                {
                    "name": "non_empty",
                    "logic": "Output is not empty",
                    "criticality": "HIGH",
                    "on_fail": "error",
                },
                {
                    "name": "all_positive",
                    "logic": "All values in output are positive",
                    "criticality": "MEDIUM",
                    "on_fail": "warn",
                },
            ],
            "io_examples": [],
        })

        insert_contract(
            conn,
            function_id="src/test.py::process",
            contract_json=contract_json,
            confidence_score=0.75,
        )

        # Test with passing data
        test_data = json.dumps({"input": {}, "output": [1, 2, 3]})
        result = runner.invoke(
            app,
            ["--db", str(db_path), "verify", "run", "src/test.py::process"],
            input=test_data,
        )

        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["data"]["passed"] is True

        # Test with failing data (empty)
        test_data = json.dumps({"input": {}, "output": []})
        result = runner.invoke(
            app,
            ["--db", str(db_path), "verify", "run", "src/test.py::process"],
            input=test_data,
        )

        assert result.exit_code == 1
        data = json.loads(result.stdout)
        assert data["data"]["passed"] is False
        # First invariant should fail first
        assert "non_empty" in data["data"]["message"]
