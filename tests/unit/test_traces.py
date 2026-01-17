"""Tests for the reasoning trace storage module."""

import tempfile
from pathlib import Path

import pytest

from drspec.contracts.traces import (
    AgentType,
    ReasoningTrace,
    count_traces,
    delete_traces,
    get_latest_trace,
    get_traces,
    store_trace,
)
from drspec.db import get_connection, init_schema, insert_artifact


class TestAgentType:
    """Tests for AgentType enum."""

    def test_agent_type_values(self):
        """Test AgentType enum values."""
        assert AgentType.PROPOSER.value == "proposer"
        assert AgentType.CRITIC.value == "critic"
        assert AgentType.JUDGE.value == "judge"
        assert AgentType.VISION_ANALYST.value == "vision_analyst"
        assert AgentType.LIBRARIAN.value == "librarian"
        assert AgentType.DEBUGGER.value == "debugger"


class TestReasoningTrace:
    """Tests for ReasoningTrace dataclass."""

    def test_trace_creation(self):
        """Test creating a ReasoningTrace."""
        trace = ReasoningTrace(
            function_id="test.py::foo",
            agent="proposer",
            trace={"analysis": "test analysis"},
        )
        assert trace.function_id == "test.py::foo"
        assert trace.agent == "proposer"
        assert trace.trace == {"analysis": "test analysis"}
        assert trace.id is None
        assert trace.created_at is None

    def test_trace_to_json(self):
        """Test converting trace to JSON."""
        trace = ReasoningTrace(
            function_id="test.py::foo",
            agent="proposer",
            trace={"key": "value", "nested": {"a": 1}},
        )
        json_str = trace.to_json()
        assert '"key": "value"' in json_str
        assert '"nested"' in json_str

    def test_trace_to_dict(self):
        """Test converting trace to dict."""
        trace = ReasoningTrace(
            id=1,
            function_id="test.py::foo",
            agent="proposer",
            trace={"analysis": "test"},
        )
        d = trace.to_dict()
        assert d["id"] == 1
        assert d["function_id"] == "test.py::foo"
        assert d["agent"] == "proposer"
        assert d["trace"] == {"analysis": "test"}


class TestStoreTrace:
    """Tests for store_trace function."""

    def test_store_trace_success(self):
        """Test storing a reasoning trace."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = get_connection(db_path)
            init_schema(conn)

            # Create an artifact first (foreign key requirement)
            insert_artifact(
                conn,
                function_id="test.py::foo",
                file_path="test.py",
                function_name="foo",
                signature="def foo():",
                body="pass",
                code_hash="abc123",
                language="python",
                start_line=1,
                end_line=2,
            )

            # Store a trace
            store_trace(
                conn,
                function_id="test.py::foo",
                agent="proposer",
                trace={"analysis": "Function foo does nothing"},
            )

            # Verify it was stored
            traces = get_traces(conn, "test.py::foo")
            assert len(traces) == 1
            assert traces[0].agent == "proposer"
            assert traces[0].trace["analysis"] == "Function foo does nothing"
            conn.close()

    def test_store_trace_invalid_agent(self):
        """Test storing a trace with invalid agent type."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = get_connection(db_path)
            init_schema(conn)

            with pytest.raises(ValueError) as exc_info:
                store_trace(
                    conn,
                    function_id="test.py::foo",
                    agent="invalid_agent",
                    trace={"analysis": "test"},
                )
            assert "Invalid agent type" in str(exc_info.value)
            conn.close()

    def test_store_multiple_traces(self):
        """Test storing multiple traces for same function."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = get_connection(db_path)
            init_schema(conn)

            insert_artifact(
                conn,
                function_id="test.py::foo",
                file_path="test.py",
                function_name="foo",
                signature="def foo():",
                body="pass",
                code_hash="abc123",
                language="python",
                start_line=1,
                end_line=2,
            )

            # Store multiple traces
            store_trace(conn, "test.py::foo", "proposer", {"step": 1})
            store_trace(conn, "test.py::foo", "critic", {"step": 2})
            store_trace(conn, "test.py::foo", "judge", {"step": 3})

            traces = get_traces(conn, "test.py::foo")
            assert len(traces) == 3
            conn.close()


class TestGetTraces:
    """Tests for get_traces function."""

    def test_get_traces_empty(self):
        """Test getting traces when none exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = get_connection(db_path)
            init_schema(conn)

            traces = get_traces(conn, "nonexistent.py::foo")
            assert traces == []
            conn.close()

    def test_get_traces_with_filter(self):
        """Test getting traces with agent filter."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = get_connection(db_path)
            init_schema(conn)

            insert_artifact(
                conn,
                function_id="test.py::foo",
                file_path="test.py",
                function_name="foo",
                signature="def foo():",
                body="pass",
                code_hash="abc123",
                language="python",
                start_line=1,
                end_line=2,
            )

            store_trace(conn, "test.py::foo", "proposer", {"data": "p1"})
            store_trace(conn, "test.py::foo", "proposer", {"data": "p2"})
            store_trace(conn, "test.py::foo", "critic", {"data": "c1"})

            # Filter by proposer
            proposer_traces = get_traces(conn, "test.py::foo", agent="proposer")
            assert len(proposer_traces) == 2

            # Filter by critic
            critic_traces = get_traces(conn, "test.py::foo", agent="critic")
            assert len(critic_traces) == 1
            conn.close()

    def test_get_traces_limit(self):
        """Test getting traces with limit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = get_connection(db_path)
            init_schema(conn)

            insert_artifact(
                conn,
                function_id="test.py::foo",
                file_path="test.py",
                function_name="foo",
                signature="def foo():",
                body="pass",
                code_hash="abc123",
                language="python",
                start_line=1,
                end_line=2,
            )

            # Store 5 traces
            for i in range(5):
                store_trace(conn, "test.py::foo", "proposer", {"num": i})

            # Get with limit 3
            traces = get_traces(conn, "test.py::foo", limit=3)
            assert len(traces) == 3
            conn.close()


class TestGetLatestTrace:
    """Tests for get_latest_trace function."""

    def test_get_latest_trace_none(self):
        """Test getting latest trace when none exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = get_connection(db_path)
            init_schema(conn)

            trace = get_latest_trace(conn, "nonexistent.py::foo")
            assert trace is None
            conn.close()

    def test_get_latest_trace_success(self):
        """Test getting the most recent trace."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = get_connection(db_path)
            init_schema(conn)

            insert_artifact(
                conn,
                function_id="test.py::foo",
                file_path="test.py",
                function_name="foo",
                signature="def foo():",
                body="pass",
                code_hash="abc123",
                language="python",
                start_line=1,
                end_line=2,
            )

            store_trace(conn, "test.py::foo", "proposer", {"version": 1})
            store_trace(conn, "test.py::foo", "critic", {"version": 2})
            store_trace(conn, "test.py::foo", "judge", {"version": 3})

            # Latest should be the judge trace (most recently inserted)
            latest = get_latest_trace(conn, "test.py::foo")
            assert latest is not None
            assert latest.agent == "judge"
            assert latest.trace["version"] == 3
            conn.close()

    def test_get_latest_trace_with_filter(self):
        """Test getting latest trace with agent filter."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = get_connection(db_path)
            init_schema(conn)

            insert_artifact(
                conn,
                function_id="test.py::foo",
                file_path="test.py",
                function_name="foo",
                signature="def foo():",
                body="pass",
                code_hash="abc123",
                language="python",
                start_line=1,
                end_line=2,
            )

            store_trace(conn, "test.py::foo", "proposer", {"v": 1})
            store_trace(conn, "test.py::foo", "proposer", {"v": 2})
            store_trace(conn, "test.py::foo", "critic", {"v": 3})

            # Latest proposer trace
            latest = get_latest_trace(conn, "test.py::foo", agent="proposer")
            assert latest is not None
            assert latest.agent == "proposer"
            assert latest.trace["v"] == 2
            conn.close()


class TestCountTraces:
    """Tests for count_traces function."""

    def test_count_traces_empty(self):
        """Test counting traces when none exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = get_connection(db_path)
            init_schema(conn)

            count = count_traces(conn)
            assert count == 0
            conn.close()

    def test_count_traces_all(self):
        """Test counting all traces."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = get_connection(db_path)
            init_schema(conn)

            insert_artifact(
                conn,
                function_id="test.py::foo",
                file_path="test.py",
                function_name="foo",
                signature="def foo():",
                body="pass",
                code_hash="abc123",
                language="python",
                start_line=1,
                end_line=2,
            )

            store_trace(conn, "test.py::foo", "proposer", {})
            store_trace(conn, "test.py::foo", "critic", {})
            store_trace(conn, "test.py::foo", "judge", {})

            count = count_traces(conn)
            assert count == 3
            conn.close()

    def test_count_traces_with_filters(self):
        """Test counting traces with filters."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = get_connection(db_path)
            init_schema(conn)

            insert_artifact(
                conn,
                function_id="test.py::foo",
                file_path="test.py",
                function_name="foo",
                signature="def foo():",
                body="pass",
                code_hash="abc123",
                language="python",
                start_line=1,
                end_line=2,
            )
            insert_artifact(
                conn,
                function_id="test.py::bar",
                file_path="test.py",
                function_name="bar",
                signature="def bar():",
                body="pass",
                code_hash="def456",
                language="python",
                start_line=3,
                end_line=4,
            )

            store_trace(conn, "test.py::foo", "proposer", {})
            store_trace(conn, "test.py::foo", "proposer", {})
            store_trace(conn, "test.py::foo", "critic", {})
            store_trace(conn, "test.py::bar", "proposer", {})

            # Count by function
            assert count_traces(conn, function_id="test.py::foo") == 3
            assert count_traces(conn, function_id="test.py::bar") == 1

            # Count by agent
            assert count_traces(conn, agent="proposer") == 3
            assert count_traces(conn, agent="critic") == 1

            # Count by both
            assert count_traces(conn, function_id="test.py::foo", agent="proposer") == 2
            conn.close()


class TestDeleteTraces:
    """Tests for delete_traces function."""

    def test_delete_traces_all(self):
        """Test deleting all traces for a function."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = get_connection(db_path)
            init_schema(conn)

            insert_artifact(
                conn,
                function_id="test.py::foo",
                file_path="test.py",
                function_name="foo",
                signature="def foo():",
                body="pass",
                code_hash="abc123",
                language="python",
                start_line=1,
                end_line=2,
            )

            store_trace(conn, "test.py::foo", "proposer", {})
            store_trace(conn, "test.py::foo", "critic", {})
            store_trace(conn, "test.py::foo", "judge", {})

            deleted = delete_traces(conn, "test.py::foo")
            assert deleted == 3
            assert count_traces(conn, function_id="test.py::foo") == 0
            conn.close()

    def test_delete_traces_with_filter(self):
        """Test deleting traces with agent filter."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = get_connection(db_path)
            init_schema(conn)

            insert_artifact(
                conn,
                function_id="test.py::foo",
                file_path="test.py",
                function_name="foo",
                signature="def foo():",
                body="pass",
                code_hash="abc123",
                language="python",
                start_line=1,
                end_line=2,
            )

            store_trace(conn, "test.py::foo", "proposer", {})
            store_trace(conn, "test.py::foo", "proposer", {})
            store_trace(conn, "test.py::foo", "critic", {})

            # Delete only proposer traces
            deleted = delete_traces(conn, "test.py::foo", agent="proposer")
            assert deleted == 2

            # Critic trace should remain
            assert count_traces(conn, function_id="test.py::foo") == 1
            assert count_traces(conn, function_id="test.py::foo", agent="critic") == 1
            conn.close()

    def test_delete_traces_nonexistent(self):
        """Test deleting traces for nonexistent function."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = get_connection(db_path)
            init_schema(conn)

            deleted = delete_traces(conn, "nonexistent.py::foo")
            assert deleted == 0
            conn.close()
