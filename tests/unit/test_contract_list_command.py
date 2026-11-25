"""Tests for the drspec contract list command."""

import json
from pathlib import Path

from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.db import get_connection, insert_artifact, insert_contract

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


class TestContractListCommand:
    def test_list_requires_init(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)  # an empty folder: no project
        result = runner.invoke(app, ["--json", "contract", "list"])

        assert result.exit_code == 1
        response = json.loads(result.output)
        assert response["success"] is False
        assert response["error"]["code"] == "DB_NOT_INITIALIZED"

    def test_list_empty_database(self, kept_project):
        result = runner.invoke(app, ["--json", "contract", "list"])

        assert result.exit_code == 0
        response = json.loads(result.output)
        assert response["success"] is True
        assert response["data"]["contracts"] == []
        assert response["data"]["pagination"]["total"] == 0

    def test_list_with_contracts(self, kept_project):
        db_path = Path.cwd() / "_drspec" / "contracts.db"
        conn = get_connection(db_path)

        for i in range(3):
            insert_artifact(
                conn,
                function_id=f"src/module{i}.py::func{i}",
                file_path=f"src/module{i}.py",
                function_name=f"func{i}",
                signature=f"def func{i}():",
                body="pass",
                code_hash=f"hash{i}",
                language="python",
                start_line=1,
                end_line=2,
                status="VERIFIED" if i % 2 == 0 else "NEEDS_REVIEW",
            )
            insert_contract(
                conn,
                f"src/module{i}.py::func{i}",
                make_valid_contract(f"def func{i}()", f"Function {i} description"),
                0.70 + i * 0.1,
            )
        conn.close()

        result = runner.invoke(app, ["--json", "contract", "list"])

        assert result.exit_code == 0
        response = json.loads(result.output)
        assert response["success"] is True
        assert len(response["data"]["contracts"]) == 3
        assert response["data"]["pagination"]["total"] == 3

    def test_list_filter_by_status(self, kept_project):
        db_path = Path.cwd() / "_drspec" / "contracts.db"
        conn = get_connection(db_path)

        insert_artifact(
            conn,
            function_id="src/a.py::verified",
            file_path="src/a.py",
            function_name="verified",
            signature="def verified():",
            body="pass",
            code_hash="hash1",
            language="python",
            start_line=1,
            end_line=2,
            status="VERIFIED",
        )
        insert_contract(conn, "src/a.py::verified", make_valid_contract(), 0.85)

        insert_artifact(
            conn,
            function_id="src/b.py::review",
            file_path="src/b.py",
            function_name="review",
            signature="def review():",
            body="pass",
            code_hash="hash2",
            language="python",
            start_line=1,
            end_line=2,
            status="NEEDS_REVIEW",
        )
        insert_contract(conn, "src/b.py::review", make_valid_contract(), 0.50)
        conn.close()

        result = runner.invoke(app, ["--json", "contract", "list", "--status", "VERIFIED"])

        assert result.exit_code == 0
        response = json.loads(result.output)
        assert len(response["data"]["contracts"]) == 1
        assert response["data"]["contracts"][0]["status"] == "VERIFIED"

    def test_list_filter_by_path(self, kept_project):
        db_path = Path.cwd() / "_drspec" / "contracts.db"
        conn = get_connection(db_path)

        for path_prefix, func_name in [
            ("src/payments/", "process"),
            ("src/payments/", "validate"),
            ("src/utils/", "helper"),
            ("lib/src/payments/", "old"),  # the prefix is a prefix: this one only has it in the middle
        ]:
            fid = f"{path_prefix}{func_name}.py::{func_name}"
            insert_artifact(
                conn,
                function_id=fid,
                file_path=f"{path_prefix}{func_name}.py",
                function_name=func_name,
                signature=f"def {func_name}():",
                body="pass",
                code_hash=f"hash_{func_name}",
                language="python",
                start_line=1,
                end_line=2,
            )
            insert_contract(conn, fid, make_valid_contract(), 0.80)
        conn.close()

        result = runner.invoke(app, ["--json", "contract", "list", "--path", "src/payments/"])

        assert result.exit_code == 0
        response = json.loads(result.output)
        assert len(response["data"]["contracts"]) == 2
        for c in response["data"]["contracts"]:
            assert c["function_id"].startswith("src/payments/")

    def test_list_filter_by_min_confidence(self, kept_project):
        db_path = Path.cwd() / "_drspec" / "contracts.db"
        conn = get_connection(db_path)

        # 0.71 instead of 0.70 to avoid float precision issues
        for i, conf in enumerate([0.50, 0.71, 0.90]):
            fid = f"src/mod{i}.py::func{i}"
            insert_artifact(
                conn,
                function_id=fid,
                file_path=f"src/mod{i}.py",
                function_name=f"func{i}",
                signature=f"def func{i}():",
                body="pass",
                code_hash=f"hash{i}",
                language="python",
                start_line=1,
                end_line=2,
            )
            insert_contract(conn, fid, make_valid_contract(), conf)
        conn.close()

        result = runner.invoke(app, ["--json", "contract", "list", "--min-confidence", "70"])

        assert result.exit_code == 0
        response = json.loads(result.output)
        assert len(response["data"]["contracts"]) == 2
        for c in response["data"]["contracts"]:
            assert c["confidence"]["base"] >= 70


    def test_list_pagination(self, kept_project):
        db_path = Path.cwd() / "_drspec" / "contracts.db"
        conn = get_connection(db_path)

        for i in range(10):
            fid = f"src/mod{i}.py::func{i}"
            insert_artifact(
                conn,
                function_id=fid,
                file_path=f"src/mod{i}.py",
                function_name=f"func{i}",
                signature=f"def func{i}():",
                body="pass",
                code_hash=f"hash{i}",
                language="python",
                start_line=1,
                end_line=2,
            )
            insert_contract(conn, fid, make_valid_contract(), 0.80)
        conn.close()

        result1 = runner.invoke(app, ["--json", "contract", "list", "--limit", "3", "--offset", "0"])
        assert result1.exit_code == 0
        response1 = json.loads(result1.output)
        assert len(response1["data"]["contracts"]) == 3
        assert response1["data"]["pagination"]["total"] == 10
        assert response1["data"]["pagination"]["has_more"] is True

        result2 = runner.invoke(app, ["--json", "contract", "list", "--limit", "3", "--offset", "3"])
        assert result2.exit_code == 0
        response2 = json.loads(result2.output)
        assert len(response2["data"]["contracts"]) == 3
        assert response2["data"]["pagination"]["has_more"] is True

        result3 = runner.invoke(app, ["--json", "contract", "list", "--limit", "3", "--offset", "9"])
        assert result3.exit_code == 0
        response3 = json.loads(result3.output)
        assert len(response3["data"]["contracts"]) == 1
        assert response3["data"]["pagination"]["has_more"] is False

    def test_list_combined_filters(self, kept_project):
        db_path = Path.cwd() / "_drspec" / "contracts.db"
        conn = get_connection(db_path)

        test_data = [
            ("src/payments/a.py::func_a", "VERIFIED", 0.90),
            ("src/payments/b.py::func_b", "NEEDS_REVIEW", 0.60),
            ("src/payments/c.py::func_c", "VERIFIED", 0.75),
            ("src/utils/d.py::func_d", "VERIFIED", 0.85),
        ]

        for fid, status, conf in test_data:
            file_path = fid.split("::")[0]
            func_name = fid.split("::")[1]
            insert_artifact(
                conn,
                function_id=fid,
                file_path=file_path,
                function_name=func_name,
                signature=f"def {func_name}():",
                body="pass",
                code_hash=f"hash_{func_name}",
                language="python",
                start_line=1,
                end_line=2,
                status=status,
            )
            insert_contract(conn, fid, make_valid_contract(), conf)
        conn.close()

        result = runner.invoke(app, ["--json",
            "contract", "list",
            "--status", "VERIFIED",
            "--path", "src/payments/",
            "--min-confidence", "70",
        ])

        assert result.exit_code == 0
        response = json.loads(result.output)
        # only func_a (90%) and func_c (75%) satisfy all three filters
        assert len(response["data"]["contracts"]) == 2
        for c in response["data"]["contracts"]:
            assert c["status"] == "VERIFIED"
            assert c["function_id"].startswith("src/payments/")
            assert c["confidence"]["base"] >= 70

    def test_list_includes_intent_summary(self, kept_project):
        db_path = Path.cwd() / "_drspec" / "contracts.db"
        conn = get_connection(db_path)

        long_intent = "A" * 150  # longer than the 100-char truncation limit

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
        insert_contract(
            conn,
            "src/test.py::func",
            make_valid_contract("def func()", long_intent),
            0.80,
        )
        conn.close()

        result = runner.invoke(app, ["--json", "contract", "list"])

        assert result.exit_code == 0
        response = json.loads(result.output)
        assert len(response["data"]["contracts"]) == 1
        intent = response["data"]["contracts"][0]["intent_summary"]
        assert len(intent) == 103  # 100 + "..."
        assert intent.endswith("...")

    def test_list_includes_invariant_count(self, kept_project):
        db_path = Path.cwd() / "_drspec" / "contracts.db"
        conn = get_connection(db_path)

        contract_json = json.dumps({
            "function_signature": "def func()",
            "intent_summary": "Test function",
            "invariants": [
                {"name": "inv1", "logic": "Logic 1", "criticality": "HIGH", "on_fail": "error"},
                {"name": "inv2", "logic": "Logic 2", "criticality": "MEDIUM", "on_fail": "warn"},
                {"name": "inv3", "logic": "Logic 3", "criticality": "LOW", "on_fail": "warn"},
            ],
        })

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
        insert_contract(conn, "src/test.py::func", contract_json, 0.80)
        conn.close()

        result = runner.invoke(app, ["--json", "contract", "list"])

        assert result.exit_code == 0
        response = json.loads(result.output)
        assert response["data"]["contracts"][0]["invariant_count"] == 3

    def test_list_help(self):
        result = runner.invoke(app, ["contract", "list", "--help"])
        assert result.exit_code == 0
