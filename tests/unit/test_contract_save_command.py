"""Tests for the drspec contract save command."""

import json
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
    def test_save_requires_init(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)  # an empty folder: no project
        result = runner.invoke(
            app,
            ["--json", "contract", "save", "test.py::foo", "--confidence", "80"],
            input=make_valid_contract(),
        )

        assert result.exit_code == 1
        response = json.loads(result.output)
        assert response["success"] is False
        assert response["error"]["code"] == "DB_NOT_INITIALIZED"

    def test_save_invalid_function_id(self, kept_project):

        result = runner.invoke(
            app,
            ["--json", "contract", "save", "invalid_no_separator", "--confidence", "80"],
            input=make_valid_contract(),
        )

        assert result.exit_code == 1
        response = json.loads(result.output)
        assert response["error"]["code"] == "INVALID_FUNCTION_ID"

    def test_save_artifact_not_found(self, kept_project):

        result = runner.invoke(
            app,
            ["--json", "contract", "save", "src/test.py::nonexistent", "--confidence", "80"],
            input=make_valid_contract(),
        )

        assert result.exit_code == 1
        response = json.loads(result.output)
        assert response["error"]["code"] == "FUNCTION_NOT_FOUND"

    def test_save_invalid_contract_schema(self, kept_project):

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

        # missing required fields on purpose, to trigger INVALID_SCHEMA
        invalid_contract = json.dumps({"function_signature": "def func()"})

        result = runner.invoke(
            app,
            ["--json", "contract", "save", "src/test.py::func", "--confidence", "80"],
            input=invalid_contract,
        )

        assert result.exit_code == 1
        response = json.loads(result.output)
        assert response["error"]["code"] == "INVALID_SCHEMA"

    def test_save_success_verified(self, kept_project):

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
            ["--json", "contract", "save", "src/utils.py::helper", "--confidence", "85"],
            input=make_valid_contract(),
        )

        assert result.exit_code == 0
        response = json.loads(result.output)
        assert response["success"] is True
        assert response["data"]["function_id"] == "src/utils.py::helper"
        assert response["data"]["confidence_score"] == 85
        assert response["data"]["status"] == "NEEDS_REVIEW"
        assert "Contract saved successfully" in response["data"]["message"]

        conn = get_connection(db_path)
        contract = get_contract(conn, "src/utils.py::helper")
        assert contract is not None
        assert abs(contract["confidence_score"] - 0.85) < 0.001  # float precision

        artifact = get_artifact(conn, "src/utils.py::helper")
        assert artifact.status == "NEEDS_REVIEW"
        conn.close()

    def test_save_success_needs_review(self, kept_project):

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
            ["--json", "contract", "save", "src/test.py::func", "--confidence", "50"],
            input=make_valid_contract("def func()"),
        )

        assert result.exit_code == 0
        response = json.loads(result.output)
        assert response["data"]["status"] == "NEEDS_REVIEW"

        conn = get_connection(db_path)
        artifact = get_artifact(conn, "src/test.py::func")
        assert artifact.status == "NEEDS_REVIEW"
        conn.close()

    def test_save_boundary_confidence_70(self, kept_project):

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
            ["--json", "contract", "save", "src/test.py::func", "--confidence", "70"],
            input=make_valid_contract("def func()"),
        )

        assert result.exit_code == 0
        response = json.loads(result.output)
        assert response["data"]["status"] == "NEEDS_REVIEW"

    def test_save_boundary_confidence_69(self, kept_project):

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
            ["--json", "contract", "save", "src/test.py::func", "--confidence", "69"],
            input=make_valid_contract("def func()"),
        )

        assert result.exit_code == 0
        response = json.loads(result.output)
        assert response["data"]["status"] == "NEEDS_REVIEW"

    def test_save_with_reasoning_trace(self, kept_project):

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

        conn = get_connection(db_path)
        traces = get_reasoning_traces(conn, "src/test.py::func")
        assert len(traces) == 1
        assert traces[0]["agent"] == "judge"
        conn.close()

    def test_save_with_custom_agent(self, kept_project):

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

    def test_save_updates_existing_contract(self, kept_project):

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

        result1 = runner.invoke(
            app,
            ["contract", "save", "src/test.py::func", "--confidence", "50"],
            input=make_valid_contract("def func()", "First intent"),
        )
        assert result1.exit_code == 0

        result2 = runner.invoke(
            app,
            ["contract", "save", "src/test.py::func", "--confidence", "90"],
            input=make_valid_contract("def func()", "Updated intent"),
        )
        assert result2.exit_code == 0

        conn = get_connection(db_path)
        contract = get_contract(conn, "src/test.py::func")
        contract_data = json.loads(contract["contract_json"])
        assert contract_data["intent_summary"] == "Updated intent"
        assert abs(contract["confidence_score"] - 0.90) < 0.001  # float precision

        artifact = get_artifact(conn, "src/test.py::func")
        assert artifact.status == "NEEDS_REVIEW"
        conn.close()

    def test_save_invalid_json_syntax(self, kept_project):

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
            ["--json", "contract", "save", "src/test.py::func", "--confidence", "80"],
            input="not valid json {",
        )

        assert result.exit_code == 1
        response = json.loads(result.output)
        assert response["error"]["code"] == "INVALID_SCHEMA"

    def test_save_help(self):
        result = runner.invoke(app, ["contract", "save", "--help"])
        assert result.exit_code == 0
        assert "confidence" in result.stdout.lower()
        assert "function_id" in result.stdout.lower()

    def test_save_confidence_min_boundary(self, kept_project):

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
            ["--json", "contract", "save", "src/test.py::func", "--confidence", "0"],
            input=make_valid_contract("def func()"),
        )

        assert result.exit_code == 0
        response = json.loads(result.output)
        assert response["data"]["confidence_score"] == 0
        assert response["data"]["status"] == "NEEDS_REVIEW"

    def test_save_confidence_max_boundary(self, kept_project):

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
            ["--json", "contract", "save", "src/test.py::func", "--confidence", "100"],
            input=make_valid_contract("def func()"),
        )

        assert result.exit_code == 0
        response = json.loads(result.output)
        assert response["data"]["confidence_score"] == 100
        assert response["data"]["status"] == "NEEDS_REVIEW"

    def test_save_with_a_row_referencing_the_function_fk_workaround(self, kept_project):

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
        # a row referencing this function must not block the save via its FK
        conn.execute(
            "INSERT INTO vision_findings (function_id, finding_type, significance, description) "
                    "VALUES (?, 'outlier', 'HIGH', 'spike')",
            ["src/test.py::queued_func"]
        )
        conn.close()

        result = runner.invoke(
            app,
            ["--json", "contract", "save", "src/test.py::queued_func", "--confidence", "88"],
            input=make_valid_contract("def queued_func()"),
        )

        assert result.exit_code == 0
        response = json.loads(result.output)
        assert response["success"] is True
        assert response["data"]["status"] == "NEEDS_REVIEW"

        conn = get_connection(db_path)
        finding = conn.execute(
            "SELECT description FROM vision_findings WHERE function_id = ?",
            ["src/test.py::queued_func"]
        ).fetchone()
        assert finding == ("spike",)
        conn.close()

    def test_save_preserves_reasoning_traces(self, kept_project):

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
        # a pre-existing trace referencing this function must survive the save
        conn.execute(
            """INSERT INTO reasoning_traces (function_id, agent, trace_json)
                       VALUES (?, 'proposer', '{"initial": "trace"}')""",
            ["src/test.py::traced_func"]
        )
        conn.close()

        result = runner.invoke(
            app,
            ["contract", "save", "src/test.py::traced_func", "--confidence", "75"],
            input=make_valid_contract("def traced_func()"),
        )

        assert result.exit_code == 0

        conn = get_connection(db_path)
        traces = get_reasoning_traces(conn, "src/test.py::traced_func")
        assert len(traces) >= 1
        proposer_traces = [t for t in traces if t["agent"] == "proposer"]
        assert len(proposer_traces) >= 1
        conn.close()

    def test_save_preserves_dependencies(self, kept_project):

        db_path = Path.cwd() / "_drspec" / "contracts.db"
        conn = get_connection(db_path)

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

        # a dependency referencing this function must survive the save
        conn.execute(
            "INSERT INTO dependencies (caller_id, callee_id) VALUES (?, ?)",
            ["src/test.py::caller", "src/test.py::callee"]
        )
        conn.close()

        result = runner.invoke(
            app,
            ["contract", "save", "src/test.py::caller", "--confidence", "80"],
            input=make_valid_contract("def caller()"),
        )

        assert result.exit_code == 0

        conn = get_connection(db_path)
        deps = conn.execute(
            "SELECT callee_id FROM dependencies WHERE caller_id = ?",
            ["src/test.py::caller"]
        ).fetchall()
        assert len(deps) == 1
        assert deps[0][0] == "src/test.py::callee"
        conn.close()
