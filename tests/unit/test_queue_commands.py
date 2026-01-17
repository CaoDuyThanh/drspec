"""Tests for the drspec queue commands."""

import json
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.db import get_connection, insert_artifact, queue_push


runner = CliRunner()


class TestQueueNextCommand:
    """Tests for the queue next CLI command."""

    def test_next_requires_init(self):
        """Test queue next fails without initialization."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                result = runner.invoke(app, ["queue", "next"])

                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["success"] is False
                assert response["error"]["code"] == "DB_NOT_INITIALIZED"

    def test_next_empty_queue(self):
        """Test queue next with empty queue."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                result = runner.invoke(app, ["queue", "next"])

                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["success"] is False
                assert response["error"]["code"] == "QUEUE_EMPTY"
                assert "suggestion" in response["error"]["details"]

    def test_next_success(self):
        """Test successful queue next."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                db_path = Path.cwd() / "_drspec" / "contracts.db"
                conn = get_connection(db_path)

                # Insert artifact and add to queue
                insert_artifact(
                    conn,
                    function_id="src/utils.py::helper",
                    file_path="src/utils.py",
                    function_name="helper",
                    signature="def helper():",
                    body="pass",
                    code_hash="hash1",
                    language="python",
                    start_line=1,
                    end_line=2,
                )
                queue_push(conn, "src/utils.py::helper", priority=50, reason="NEW")
                conn.close()

                result = runner.invoke(app, ["queue", "next"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["success"] is True
                data = response["data"]
                assert data["function_id"] == "src/utils.py::helper"
                assert data["priority"] == 50
                assert data["status"] == "PROCESSING"
                assert data["reason"] == "NEW"
                assert data["attempts"] == 1

    def test_next_pops_highest_priority(self):
        """Test queue next returns highest priority item (lowest number)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                db_path = Path.cwd() / "_drspec" / "contracts.db"
                conn = get_connection(db_path)

                # Insert multiple items with different priorities
                for i, priority in enumerate([100, 10, 50]):
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
                    queue_push(conn, fid, priority=priority, reason="NEW")
                conn.close()

                result = runner.invoke(app, ["queue", "next"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                # Should return lowest priority number (10)
                assert response["data"]["priority"] == 10
                assert response["data"]["function_id"] == "src/mod1.py::func1"


class TestQueuePeekCommand:
    """Tests for the queue peek CLI command."""

    def test_peek_requires_init(self):
        """Test queue peek fails without initialization."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                result = runner.invoke(app, ["queue", "peek"])

                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["error"]["code"] == "DB_NOT_INITIALIZED"

    def test_peek_empty_queue(self):
        """Test queue peek with empty queue."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                result = runner.invoke(app, ["queue", "peek"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["success"] is True
                assert response["data"]["items"] == []
                assert response["data"]["total_pending"] == 0

    def test_peek_with_items(self):
        """Test queue peek with items in queue."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                db_path = Path.cwd() / "_drspec" / "contracts.db"
                conn = get_connection(db_path)

                # Insert 5 items
                for i in range(5):
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
                    queue_push(conn, fid, priority=i * 10, reason="NEW")
                conn.close()

                result = runner.invoke(app, ["queue", "peek"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["success"] is True
                assert len(response["data"]["items"]) == 5
                assert response["data"]["total_pending"] == 5
                # Verify first item has lowest priority
                assert response["data"]["items"][0]["priority"] == 0

    def test_peek_limit(self):
        """Test queue peek with limit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                db_path = Path.cwd() / "_drspec" / "contracts.db"
                conn = get_connection(db_path)

                # Insert 10 items
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
                    queue_push(conn, fid, priority=100, reason="NEW")
                conn.close()

                result = runner.invoke(app, ["queue", "peek", "--limit", "3"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert len(response["data"]["items"]) == 3
                assert response["data"]["total_pending"] == 10


class TestQueueGetCommand:
    """Tests for the queue get CLI command."""

    def test_get_requires_init(self):
        """Test queue get fails without initialization."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                result = runner.invoke(app, ["queue", "get", "src/test.py::func"])

                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["error"]["code"] == "DB_NOT_INITIALIZED"

    def test_get_invalid_function_id(self):
        """Test queue get with invalid function ID."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                result = runner.invoke(app, ["queue", "get", "invalid_id"])

                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["error"]["code"] == "INVALID_FUNCTION_ID"

    def test_get_item_not_found(self):
        """Test queue get for non-existent item."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                result = runner.invoke(app, ["queue", "get", "src/test.py::nonexistent"])

                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["error"]["code"] == "QUEUE_ITEM_NOT_FOUND"
                assert "suggestion" in response["error"]["details"]

    def test_get_success(self):
        """Test successful queue get."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                db_path = Path.cwd() / "_drspec" / "contracts.db"
                conn = get_connection(db_path)

                insert_artifact(
                    conn,
                    function_id="src/utils.py::helper",
                    file_path="src/utils.py",
                    function_name="helper",
                    signature="def helper():",
                    body="pass",
                    code_hash="hash1",
                    language="python",
                    start_line=1,
                    end_line=2,
                )
                queue_push(conn, "src/utils.py::helper", priority=25, reason="HASH_MISMATCH")
                conn.close()

                result = runner.invoke(app, ["queue", "get", "src/utils.py::helper"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["success"] is True
                data = response["data"]
                assert data["function_id"] == "src/utils.py::helper"
                assert data["priority"] == 25
                assert data["status"] == "PENDING"
                assert data["reason"] == "HASH_MISMATCH"
                assert data["attempts"] == 0
                assert "queued_at" in data


class TestQueuePrioritizeCommand:
    """Tests for the queue prioritize CLI command."""

    def test_prioritize_requires_init(self):
        """Test queue prioritize fails without initialization."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                result = runner.invoke(app, ["queue", "prioritize", "src/test.py::func", "1"])

                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["error"]["code"] == "DB_NOT_INITIALIZED"

    def test_prioritize_invalid_function_id(self):
        """Test queue prioritize with invalid function ID."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                result = runner.invoke(app, ["queue", "prioritize", "invalid", "10"])

                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["error"]["code"] == "INVALID_FUNCTION_ID"

    def test_prioritize_item_not_found(self):
        """Test queue prioritize for non-existent item."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                result = runner.invoke(app, ["queue", "prioritize", "src/test.py::nonexistent", "1"])

                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["error"]["code"] == "QUEUE_ITEM_NOT_FOUND"

    def test_prioritize_success(self):
        """Test successful queue prioritize."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                db_path = Path.cwd() / "_drspec" / "contracts.db"
                conn = get_connection(db_path)

                insert_artifact(
                    conn,
                    function_id="src/utils.py::helper",
                    file_path="src/utils.py",
                    function_name="helper",
                    signature="def helper():",
                    body="pass",
                    code_hash="hash1",
                    language="python",
                    start_line=1,
                    end_line=2,
                )
                queue_push(conn, "src/utils.py::helper", priority=100, reason="NEW")
                conn.close()

                result = runner.invoke(app, ["queue", "prioritize", "src/utils.py::helper", "5"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["success"] is True
                data = response["data"]
                assert data["function_id"] == "src/utils.py::helper"
                assert data["old_priority"] == 100
                assert data["new_priority"] == 5
                assert "Priority updated successfully" in data["message"]

    def test_prioritize_verify_change(self):
        """Test queue prioritize actually changes the priority."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])

                db_path = Path.cwd() / "_drspec" / "contracts.db"
                conn = get_connection(db_path)

                insert_artifact(
                    conn,
                    function_id="src/utils.py::helper",
                    file_path="src/utils.py",
                    function_name="helper",
                    signature="def helper():",
                    body="pass",
                    code_hash="hash1",
                    language="python",
                    start_line=1,
                    end_line=2,
                )
                queue_push(conn, "src/utils.py::helper", priority=100, reason="NEW")
                conn.close()

                # Prioritize
                runner.invoke(app, ["queue", "prioritize", "src/utils.py::helper", "1"])

                # Verify with get
                result = runner.invoke(app, ["queue", "get", "src/utils.py::helper"])
                response = json.loads(result.output)
                assert response["data"]["priority"] == 1


class TestQueueHelp:
    """Tests for queue command help."""

    def test_queue_next_help(self):
        """Test queue next help displays options."""
        result = runner.invoke(app, ["queue", "next", "--help"])
        assert result.exit_code == 0
        assert "next" in result.stdout.lower()

    def test_queue_peek_help(self):
        """Test queue peek help displays options."""
        result = runner.invoke(app, ["queue", "peek", "--help"])
        assert result.exit_code == 0
        assert "limit" in result.stdout.lower()

    def test_queue_get_help(self):
        """Test queue get help displays options."""
        result = runner.invoke(app, ["queue", "get", "--help"])
        assert result.exit_code == 0
        assert "function_id" in result.stdout.lower()

    def test_queue_prioritize_help(self):
        """Test queue prioritize help displays options."""
        result = runner.invoke(app, ["queue", "prioritize", "--help"])
        assert result.exit_code == 0
        assert "priority" in result.stdout.lower()
