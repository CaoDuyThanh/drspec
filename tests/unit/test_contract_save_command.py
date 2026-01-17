"""Tests for the drspec contract save command."""

import json
import tempfile
from pathlib import Path

from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.db import (
    get_artifact,
    get_connection,
    get_contract,
    get_reasoning_traces,
    insert_artifact,
)


runner = CliRunner()


def make_valid_contract(
    function_signature: str = "def helper(x: int) -> int",
    intent_summary: str = "A helper function that processes input",
) -> str:
    """Create a valid contract JSON string for testing."""
    return json.dumps({
        "function_signature": function_signature,
        "intent_summary": intent_summary,
        "invariants": [
            {
                "name": "valid_output",
                "logic": "Output is always valid",
                "criticality": "HIGH",
                "on_fail": "error",
            }
        ],
        "io_examples": [],
    })


class TestContractSaveCommand:
    """Tests for the contract save CLI command."""

    def test_save_requires_init(self):
        """Test contract save fails without initialization."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                result = runner.invoke(
                    app,
                    ["contract", "save", "test.py::foo", "--confidence", "80"],
                    input=make_valid_contract(),
                )

                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["success"] is False
                assert response["error"]["code"] == "DB_NOT_INITIALIZED"

    def test_save_invalid_function_id(self):
        """Test contract save with invalid function ID format."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                result = runner.invoke(
                    app,
                    ["contract", "save", "invalid_no_separator", "--confidence", "80"],
                    input=make_valid_contract(),
                )

                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["error"]["code"] == "INVALID_FUNCTION_ID"

    def test_save_artifact_not_found(self):
        """Test contract save when artifact doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                result = runner.invoke(
                    app,
                    ["contract", "save", "src/test.py::nonexistent", "--confidence", "80"],
                    input=make_valid_contract(),
                )

                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["error"]["code"] == "FUNCTION_NOT_FOUND"

    def test_save_invalid_contract_schema(self):
        """Test contract save with invalid contract JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                # Create an artifact first
                db_path = Path.cwd() / "_drspec" / "contracts.db"
                conn = get_connection(db_path)
                insert_artifact(
                    conn,
                    function_id="src/test.py::func",
                    file_path="src/test.py",
                    function_name="func",
                    signature="def func():",
                    body="pass",
                    code_hash="hash1",
                    language="python",
                    start_line=1,
                    end_line=2,
                )
                conn.close()

                # Invalid contract - missing required fields
                invalid_contract = json.dumps({"function_signature": "def func()"})

                result = runner.invoke(
                    app,
                    ["contract", "save", "src/test.py::func", "--confidence", "80"],
                    input=invalid_contract,
                )

                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["error"]["code"] == "INVALID_SCHEMA"

    def test_save_success_verified(self):
        """Test successful contract save with high confidence (VERIFIED)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                # Create an artifact
                db_path = Path.cwd() / "_drspec" / "contracts.db"
                conn = get_connection(db_path)
                insert_artifact(
                    conn,
                    function_id="src/utils.py::helper",
                    file_path="src/utils.py",
                    function_name="helper",
                    signature="def helper(x: int) -> int:",
                    body="return x + 1",
                    code_hash="hash1",
                    language="python",
                    start_line=1,
                    end_line=2,
                )
                conn.close()

                result = runner.invoke(
                    app,
                    ["contract", "save", "src/utils.py::helper", "--confidence", "85"],
                    input=make_valid_contract(),
                )

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["success"] is True
                assert response["data"]["function_id"] == "src/utils.py::helper"
                assert response["data"]["confidence_score"] == 85
                assert response["data"]["status"] == "VERIFIED"
                assert "Contract saved successfully" in response["data"]["message"]

                # Verify contract is stored
                conn = get_connection(db_path)
                contract = get_contract(conn, "src/utils.py::helper")
                assert contract is not None
                # Use approximate comparison due to float precision
                assert abs(contract["confidence_score"] - 0.85) < 0.001

                # Verify artifact status updated
                artifact = get_artifact(conn, "src/utils.py::helper")
                assert artifact.status == "VERIFIED"
                conn.close()

    def test_save_success_needs_review(self):
        """Test successful contract save with low confidence (NEEDS_REVIEW)."""
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
                    code_hash="hash1",
                    language="python",
                    start_line=1,
                    end_line=2,
                )
                conn.close()

                result = runner.invoke(
                    app,
                    ["contract", "save", "src/test.py::func", "--confidence", "50"],
                    input=make_valid_contract("def func()"),
                )

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["data"]["status"] == "NEEDS_REVIEW"

                # Verify artifact status
                conn = get_connection(db_path)
                artifact = get_artifact(conn, "src/test.py::func")
                assert artifact.status == "NEEDS_REVIEW"
                conn.close()

    def test_save_boundary_confidence_70(self):
        """Test confidence boundary at 70 (should be VERIFIED)."""
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
                    code_hash="hash1",
                    language="python",
                    start_line=1,
                    end_line=2,
                )
                conn.close()

                result = runner.invoke(
                    app,
                    ["contract", "save", "src/test.py::func", "--confidence", "70"],
                    input=make_valid_contract("def func()"),
                )

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["data"]["status"] == "VERIFIED"

    def test_save_boundary_confidence_69(self):
        """Test confidence boundary at 69 (should be NEEDS_REVIEW)."""
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
                    code_hash="hash1",
                    language="python",
                    start_line=1,
                    end_line=2,
                )
                conn.close()

                result = runner.invoke(
                    app,
                    ["contract", "save", "src/test.py::func", "--confidence", "69"],
                    input=make_valid_contract("def func()"),
                )

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["data"]["status"] == "NEEDS_REVIEW"

    def test_save_with_reasoning_trace(self):
        """Test contract save with reasoning trace."""
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
                    code_hash="hash1",
                    language="python",
                    start_line=1,
                    end_line=2,
                )
                conn.close()

                trace_json = json.dumps({"reasoning": "This function clearly does X"})

                result = runner.invoke(
                    app,
                    [
                        "contract", "save", "src/test.py::func",
                        "--confidence", "80",
                        "--trace", trace_json,
                    ],
                    input=make_valid_contract("def func()"),
                )

                assert result.exit_code == 0

                # Verify trace is stored
                conn = get_connection(db_path)
                traces = get_reasoning_traces(conn, "src/test.py::func")
                assert len(traces) == 1
                assert traces[0]["agent"] == "judge"
                conn.close()

    def test_save_with_custom_agent(self):
        """Test contract save with custom agent name."""
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
                    code_hash="hash1",
                    language="python",
                    start_line=1,
                    end_line=2,
                )
                conn.close()

                trace_json = json.dumps({"step": "analysis"})

                result = runner.invoke(
                    app,
                    [
                        "contract", "save", "src/test.py::func",
                        "--confidence", "80",
                        "--agent", "proposer",
                        "--trace", trace_json,
                    ],
                    input=make_valid_contract("def func()"),
                )

                assert result.exit_code == 0

                conn = get_connection(db_path)
                traces = get_reasoning_traces(conn, "src/test.py::func", agent="proposer")
                assert len(traces) == 1
                conn.close()

    def test_save_updates_existing_contract(self):
        """Test that saving a contract updates an existing one."""
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
                    code_hash="hash1",
                    language="python",
                    start_line=1,
                    end_line=2,
                )
                conn.close()

                # First save
                result1 = runner.invoke(
                    app,
                    ["contract", "save", "src/test.py::func", "--confidence", "50"],
                    input=make_valid_contract("def func()", "First intent"),
                )
                assert result1.exit_code == 0

                # Second save with higher confidence
                result2 = runner.invoke(
                    app,
                    ["contract", "save", "src/test.py::func", "--confidence", "90"],
                    input=make_valid_contract("def func()", "Updated intent"),
                )
                assert result2.exit_code == 0

                # Verify updated contract
                conn = get_connection(db_path)
                contract = get_contract(conn, "src/test.py::func")
                contract_data = json.loads(contract["contract_json"])
                assert contract_data["intent_summary"] == "Updated intent"
                # Use approximate comparison due to float precision
                assert abs(contract["confidence_score"] - 0.90) < 0.001

                artifact = get_artifact(conn, "src/test.py::func")
                assert artifact.status == "VERIFIED"
                conn.close()

    def test_save_invalid_json_syntax(self):
        """Test contract save with invalid JSON syntax."""
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
                    code_hash="hash1",
                    language="python",
                    start_line=1,
                    end_line=2,
                )
                conn.close()

                result = runner.invoke(
                    app,
                    ["contract", "save", "src/test.py::func", "--confidence", "80"],
                    input="not valid json {",
                )

                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["error"]["code"] == "INVALID_SCHEMA"

    def test_save_help(self):
        """Test contract save help displays correctly."""
        result = runner.invoke(app, ["contract", "save", "--help"])
        assert result.exit_code == 0
        assert "confidence" in result.stdout.lower()
        assert "function_id" in result.stdout.lower()

    def test_save_confidence_min_boundary(self):
        """Test confidence minimum boundary (0)."""
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
                    code_hash="hash1",
                    language="python",
                    start_line=1,
                    end_line=2,
                )
                conn.close()

                result = runner.invoke(
                    app,
                    ["contract", "save", "src/test.py::func", "--confidence", "0"],
                    input=make_valid_contract("def func()"),
                )

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["data"]["confidence_score"] == 0
                assert response["data"]["status"] == "NEEDS_REVIEW"

    def test_save_confidence_max_boundary(self):
        """Test confidence maximum boundary (100)."""
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
                    code_hash="hash1",
                    language="python",
                    start_line=1,
                    end_line=2,
                )
                conn.close()

                result = runner.invoke(
                    app,
                    ["contract", "save", "src/test.py::func", "--confidence", "100"],
                    input=make_valid_contract("def func()"),
                )

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["data"]["confidence_score"] == 100
                assert response["data"]["status"] == "VERIFIED"

    def test_save_with_queue_item_fk_workaround(self):
        """Test contract save works when function is in the queue (FK constraint fix)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                db_path = Path.cwd() / "_drspec" / "contracts.db"
                conn = get_connection(db_path)
                insert_artifact(
                    conn,
                    function_id="src/test.py::queued_func",
                    file_path="src/test.py",
                    function_name="queued_func",
                    signature="def queued_func():",
                    body="pass",
                    code_hash="hash1",
                    language="python",
                    start_line=1,
                    end_line=2,
                )
                # Add function to queue (creates FK reference)
                conn.execute(
                    """INSERT INTO queue (function_id, priority, status, reason)
                       VALUES (?, 100, 'PROCESSING', 'NEW')""",
                    ["src/test.py::queued_func"]
                )
                conn.close()

                # This should succeed despite FK reference from queue
                result = runner.invoke(
                    app,
                    ["contract", "save", "src/test.py::queued_func", "--confidence", "88"],
                    input=make_valid_contract("def queued_func()"),
                )

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["success"] is True
                assert response["data"]["status"] == "VERIFIED"

                # Verify queue entry was preserved and marked COMPLETED
                conn = get_connection(db_path)
                queue_row = conn.execute(
                    "SELECT status FROM queue WHERE function_id = ?",
                    ["src/test.py::queued_func"]
                ).fetchone()
                assert queue_row is not None
                assert queue_row[0] == "COMPLETED"
                conn.close()

    def test_save_preserves_reasoning_traces(self):
        """Test contract save preserves existing reasoning traces when updating."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                db_path = Path.cwd() / "_drspec" / "contracts.db"
                conn = get_connection(db_path)
                insert_artifact(
                    conn,
                    function_id="src/test.py::traced_func",
                    file_path="src/test.py",
                    function_name="traced_func",
                    signature="def traced_func():",
                    body="pass",
                    code_hash="hash1",
                    language="python",
                    start_line=1,
                    end_line=2,
                )
                # Add a reasoning trace (creates FK reference)
                conn.execute(
                    """INSERT INTO reasoning_traces (function_id, agent, trace_json)
                       VALUES (?, 'proposer', '{"initial": "trace"}')""",
                    ["src/test.py::traced_func"]
                )
                conn.close()

                # Save contract - should preserve existing trace
                result = runner.invoke(
                    app,
                    ["contract", "save", "src/test.py::traced_func", "--confidence", "75"],
                    input=make_valid_contract("def traced_func()"),
                )

                assert result.exit_code == 0

                # Verify trace was preserved
                conn = get_connection(db_path)
                traces = get_reasoning_traces(conn, "src/test.py::traced_func")
                assert len(traces) >= 1
                # Original trace should still exist
                proposer_traces = [t for t in traces if t["agent"] == "proposer"]
                assert len(proposer_traces) >= 1
                conn.close()

    def test_save_preserves_dependencies(self):
        """Test contract save preserves function dependencies when updating."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                db_path = Path.cwd() / "_drspec" / "contracts.db"
                conn = get_connection(db_path)

                # Create two artifacts
                insert_artifact(
                    conn,
                    function_id="src/test.py::caller",
                    file_path="src/test.py",
                    function_name="caller",
                    signature="def caller():",
                    body="callee()",
                    code_hash="hash1",
                    language="python",
                    start_line=1,
                    end_line=2,
                )
                insert_artifact(
                    conn,
                    function_id="src/test.py::callee",
                    file_path="src/test.py",
                    function_name="callee",
                    signature="def callee():",
                    body="pass",
                    code_hash="hash2",
                    language="python",
                    start_line=5,
                    end_line=6,
                )

                # Add dependency (creates FK reference)
                conn.execute(
                    "INSERT INTO dependencies (caller_id, callee_id) VALUES (?, ?)",
                    ["src/test.py::caller", "src/test.py::callee"]
                )
                conn.close()

                # Save contract for caller - should preserve dependencies
                result = runner.invoke(
                    app,
                    ["contract", "save", "src/test.py::caller", "--confidence", "80"],
                    input=make_valid_contract("def caller()"),
                )

                assert result.exit_code == 0

                # Verify dependency was preserved
                conn = get_connection(db_path)
                deps = conn.execute(
                    "SELECT callee_id FROM dependencies WHERE caller_id = ?",
                    ["src/test.py::caller"]
                ).fetchall()
                assert len(deps) == 1
                assert deps[0][0] == "src/test.py::callee"
                conn.close()
