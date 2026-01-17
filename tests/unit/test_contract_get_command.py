"""Tests for the drspec contract get command."""

import json
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.cli.commands.contract import validate_function_id, find_similar_functions
from drspec.db import get_connection, init_schema, insert_artifact, insert_contract


runner = CliRunner()


class TestValidateFunctionId:
    """Tests for function ID validation."""

    def test_valid_function_id(self):
        """Test valid function ID passes validation."""
        is_valid, error = validate_function_id("src/utils.py::helper")
        assert is_valid is True
        assert error is None

    def test_valid_function_id_with_path(self):
        """Test valid function ID with deep path."""
        is_valid, error = validate_function_id("src/deep/path/module.py::some_function")
        assert is_valid is True
        assert error is None

    def test_invalid_missing_separator(self):
        """Test function ID without :: separator is invalid."""
        is_valid, error = validate_function_id("src/utils.py/helper")
        assert is_valid is False
        assert "::" in error

    def test_invalid_empty_filepath(self):
        """Test function ID with empty filepath is invalid."""
        is_valid, error = validate_function_id("::function_name")
        assert is_valid is False
        assert "filepath" in error.lower()

    def test_invalid_empty_function_name(self):
        """Test function ID with empty function name is invalid."""
        is_valid, error = validate_function_id("src/utils.py::")
        assert is_valid is False
        assert "function_name" in error.lower()

    def test_valid_with_multiple_separators(self):
        """Test function ID with class::method syntax."""
        # This is still valid - we split only on first ::
        is_valid, error = validate_function_id("src/utils.py::ClassName::method")
        assert is_valid is True
        assert error is None


class TestFindSimilarFunctions:
    """Tests for fuzzy function matching."""

    def test_find_similar_by_function_name(self):
        """Test finding similar functions by name."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = get_connection(db_path)
            init_schema(conn)

            # Insert test artifacts
            insert_artifact(
                conn,
                function_id="src/utils.py::helper_function",
                file_path="src/utils.py",
                function_name="helper_function",
                signature="def helper_function():",
                body="pass",
                code_hash="hash1",
                language="python",
                start_line=1,
                end_line=2,
            )
            insert_artifact(
                conn,
                function_id="src/other.py::helper",
                file_path="src/other.py",
                function_name="helper",
                signature="def helper():",
                body="pass",
                code_hash="hash2",
                language="python",
                start_line=1,
                end_line=2,
            )
            insert_artifact(
                conn,
                function_id="src/main.py::main",
                file_path="src/main.py",
                function_name="main",
                signature="def main():",
                body="pass",
                code_hash="hash3",
                language="python",
                start_line=1,
                end_line=2,
            )

            suggestions = find_similar_functions(conn, "src/missing.py::helper")

            assert len(suggestions) >= 1
            # Should find functions with "helper" in name
            assert any("helper" in s for s in suggestions)

            conn.close()

    def test_find_similar_by_path(self):
        """Test finding similar functions by file path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = get_connection(db_path)
            init_schema(conn)

            insert_artifact(
                conn,
                function_id="src/payments/process.py::process_payment",
                file_path="src/payments/process.py",
                function_name="process_payment",
                signature="def process_payment():",
                body="pass",
                code_hash="hash1",
                language="python",
                start_line=1,
                end_line=2,
            )

            # Search for a different function but in similar path
            suggestions = find_similar_functions(conn, "src/payments/other.py::other_func")

            # Since no name match, might find path match
            # This tests the path-based fallback
            assert isinstance(suggestions, list)

            conn.close()

    def test_find_similar_respects_limit(self):
        """Test that suggestions respect limit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = get_connection(db_path)
            init_schema(conn)

            # Insert many artifacts
            for i in range(10):
                insert_artifact(
                    conn,
                    function_id=f"src/module{i}.py::helper{i}",
                    file_path=f"src/module{i}.py",
                    function_name=f"helper{i}",
                    signature=f"def helper{i}():",
                    body="pass",
                    code_hash=f"hash{i}",
                    language="python",
                    start_line=1,
                    end_line=2,
                )

            suggestions = find_similar_functions(conn, "src/x.py::helper", limit=3)

            assert len(suggestions) <= 3

            conn.close()


class TestContractGetCommand:
    """Tests for the contract get CLI command."""

    def test_get_contract_not_initialized(self):
        """Test contract get when DrSpec not initialized."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                result = runner.invoke(app, ["contract", "get", "src/test.py::func"])

                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["success"] is False
                assert response["error"]["code"] == "DB_NOT_INITIALIZED"

    def test_get_contract_invalid_function_id(self):
        """Test contract get with invalid function ID format."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                # Initialize first
                runner.invoke(app, ["init"])

                result = runner.invoke(app, ["contract", "get", "invalid_no_separator"])

                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["success"] is False
                assert response["error"]["code"] == "INVALID_FUNCTION_ID"

    def test_get_contract_not_found(self):
        """Test contract get when contract doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                # Initialize
                runner.invoke(app, ["init"])

                result = runner.invoke(app, ["contract", "get", "src/test.py::nonexistent"])

                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["success"] is False
                assert response["error"]["code"] == "CONTRACT_NOT_FOUND"
                assert "suggestions" in response["error"]["details"]

    def test_get_contract_success(self):
        """Test successful contract get."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                # Initialize
                runner.invoke(app, ["init"])

                # Manually insert artifact and contract
                db_path = Path.cwd() / "_drspec" / "contracts.db"
                conn = get_connection(db_path)

                insert_artifact(
                    conn,
                    function_id="src/utils.py::helper",
                    file_path="src/utils.py",
                    function_name="helper",
                    signature="def helper(x: int) -> int:",
                    body="return x + 1",
                    code_hash="testhash",
                    language="python",
                    start_line=1,
                    end_line=2,
                    status="VERIFIED",
                )

                contract_json = json.dumps({
                    "function_signature": "def helper(x: int) -> int",
                    "intent_summary": "Adds one to the input and returns the result",
                    "invariants": [
                        {
                            "name": "output_greater",
                            "logic": "Output is always greater than input",
                            "criticality": "HIGH",
                            "on_fail": "error",
                        }
                    ],
                    "io_examples": [],
                })
                insert_contract(conn, "src/utils.py::helper", contract_json, 0.85)
                conn.close()

                # Get the contract
                result = runner.invoke(app, ["contract", "get", "src/utils.py::helper"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["success"] is True
                assert response["data"]["function_id"] == "src/utils.py::helper"
                assert response["data"]["confidence"]["base"] == 85
                assert response["data"]["confidence"]["adjusted"] == 85  # No findings, no penalty
                assert response["data"]["confidence"]["vision_penalty"] == 0
                assert response["data"]["confidence"]["active_findings"] == 0
                assert response["data"]["status"] == "VERIFIED"
                assert "contract" in response["data"]
                assert response["data"]["contract"]["function_signature"] == "def helper(x: int) -> int"
                assert len(response["data"]["contract"]["invariants"]) == 1

    def test_get_contract_with_suggestions(self):
        """Test contract not found provides suggestions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                # Initialize
                runner.invoke(app, ["init"])

                # Insert some artifacts (no contracts)
                db_path = Path.cwd() / "_drspec" / "contracts.db"
                conn = get_connection(db_path)

                insert_artifact(
                    conn,
                    function_id="src/utils.py::process_data",
                    file_path="src/utils.py",
                    function_name="process_data",
                    signature="def process_data():",
                    body="pass",
                    code_hash="hash1",
                    language="python",
                    start_line=1,
                    end_line=2,
                )
                insert_artifact(
                    conn,
                    function_id="src/other.py::process",
                    file_path="src/other.py",
                    function_name="process",
                    signature="def process():",
                    body="pass",
                    code_hash="hash2",
                    language="python",
                    start_line=1,
                    end_line=2,
                )
                conn.close()

                # Try to get non-existent contract with similar name
                result = runner.invoke(app, ["contract", "get", "src/missing.py::process"])

                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["error"]["code"] == "CONTRACT_NOT_FOUND"
                suggestions = response["error"]["details"]["suggestions"]
                # Should have suggestions with "process" in the name
                assert any("process" in s for s in suggestions)

    def test_get_contract_empty_filepath_invalid(self):
        """Test that empty filepath in function ID is rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                result = runner.invoke(app, ["contract", "get", "::function"])

                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["error"]["code"] == "INVALID_FUNCTION_ID"

    def test_get_contract_empty_function_invalid(self):
        """Test that empty function name in function ID is rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                result = runner.invoke(app, ["contract", "get", "src/file.py::"])

                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["error"]["code"] == "INVALID_FUNCTION_ID"

    def test_get_contract_with_io_examples(self):
        """Test contract get returns io_examples."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                db_path = Path.cwd() / "_drspec" / "contracts.db"
                conn = get_connection(db_path)

                insert_artifact(
                    conn,
                    function_id="src/math.py::add",
                    file_path="src/math.py",
                    function_name="add",
                    signature="def add(a: int, b: int) -> int:",
                    body="return a + b",
                    code_hash="addhash",
                    language="python",
                    start_line=1,
                    end_line=2,
                )

                contract_json = json.dumps({
                    "function_signature": "def add(a: int, b: int) -> int",
                    "intent_summary": "Adds two integers and returns their sum",
                    "invariants": [
                        {
                            "name": "commutative",
                            "logic": "add(a, b) equals add(b, a)",
                            "criticality": "HIGH",
                            "on_fail": "error",
                        }
                    ],
                    "io_examples": [
                        {"input": {"a": 1, "b": 2}, "output": 3},
                        {"input": {"a": -1, "b": 1}, "output": 0},
                    ],
                })
                insert_contract(conn, "src/math.py::add", contract_json, 0.95)
                conn.close()

                result = runner.invoke(app, ["contract", "get", "src/math.py::add"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["success"] is True
                assert len(response["data"]["contract"]["io_examples"]) == 2

    def test_get_contract_timestamps(self):
        """Test contract get returns timestamps."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                db_path = Path.cwd() / "_drspec" / "contracts.db"
                conn = get_connection(db_path)

                insert_artifact(
                    conn,
                    function_id="src/test.py::func",
                    file_path="src/test.py",
                    function_name="func",
                    signature="def func():",
                    body="pass",
                    code_hash="hash",
                    language="python",
                    start_line=1,
                    end_line=2,
                )

                contract_json = json.dumps({
                    "function_signature": "def func()",
                    "intent_summary": "A test function that does nothing",
                    "invariants": [
                        {
                            "name": "always_none",
                            "logic": "Always returns None",
                            "criticality": "LOW",
                            "on_fail": "warn",
                        }
                    ],
                })
                insert_contract(conn, "src/test.py::func", contract_json, 0.5)
                conn.close()

                result = runner.invoke(app, ["contract", "get", "src/test.py::func"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert "created_at" in response["data"]
                assert "updated_at" in response["data"]
                # Timestamps should be ISO format strings
                assert response["data"]["created_at"] is not None

    def test_get_contract_normalizes_legacy_confidence(self):
        """Test contract get handles legacy data stored as 0-100 instead of 0.0-1.0."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                db_path = Path.cwd() / "_drspec" / "contracts.db"
                conn = get_connection(db_path)

                insert_artifact(
                    conn,
                    function_id="src/legacy.py::old_func",
                    file_path="src/legacy.py",
                    function_name="old_func",
                    signature="def old_func():",
                    body="pass",
                    code_hash="legacyhash",
                    language="python",
                    start_line=1,
                    end_line=2,
                )

                contract_json = json.dumps({
                    "function_signature": "def old_func()",
                    "intent_summary": "Legacy function",
                    "invariants": [
                        {
                            "name": "test",
                            "logic": "Test",
                            "criticality": "LOW",
                            "on_fail": "warn",
                        }
                    ],
                })

                # Directly insert with legacy 0-100 scale (88.0 instead of 0.88)
                conn.execute(
                    """INSERT INTO contracts (function_id, contract_json, confidence_score)
                       VALUES (?, ?, 88.0)""",
                    ["src/legacy.py::old_func", contract_json]
                )
                conn.close()

                result = runner.invoke(app, ["contract", "get", "src/legacy.py::old_func"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                # Should display 88, not 8800
                assert response["data"]["confidence"]["base"] == 88
                assert response["data"]["confidence"]["adjusted"] == 88  # No findings

    def test_get_contract_normal_confidence(self):
        """Test contract get correctly handles normal 0.0-1.0 scale confidence."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                db_path = Path.cwd() / "_drspec" / "contracts.db"
                conn = get_connection(db_path)

                insert_artifact(
                    conn,
                    function_id="src/normal.py::func",
                    file_path="src/normal.py",
                    function_name="func",
                    signature="def func():",
                    body="pass",
                    code_hash="normalhash",
                    language="python",
                    start_line=1,
                    end_line=2,
                )

                contract_json = json.dumps({
                    "function_signature": "def func()",
                    "intent_summary": "Normal function",
                    "invariants": [
                        {
                            "name": "test",
                            "logic": "Test",
                            "criticality": "LOW",
                            "on_fail": "warn",
                        }
                    ],
                })

                # Insert with correct 0.0-1.0 scale (0.75)
                insert_contract(conn, "src/normal.py::func", contract_json, 0.75)
                conn.close()

                result = runner.invoke(app, ["contract", "get", "src/normal.py::func"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                # Should display 75
                assert response["data"]["confidence"]["base"] == 75
                assert response["data"]["confidence"]["adjusted"] == 75  # No findings
