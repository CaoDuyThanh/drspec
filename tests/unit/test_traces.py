
from unittest.mock import MagicMock

import duckdb
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
from drspec.db import init_schema, insert_artifact


class TestAgentType:
    def test_agent_type_values(self):
        assert AgentType.PROPOSER.value == "proposer"
        assert AgentType.CRITIC.value == "critic"
        assert AgentType.JUDGE.value == "judge"
        assert AgentType.VISION_ANALYST.value == "vision_analyst"
        assert AgentType.LIBRARIAN.value == "librarian"
        assert AgentType.DEBUGGER.value == "debugger"


class TestReasoningTrace:
    def test_trace_creation(self):
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
        trace = ReasoningTrace(
            function_id="test.py::foo",
            agent="proposer",
            trace={"key": "value", "nested": {"a": 1}},
        )
        json_str = trace.to_json()
        assert '"key": "value"' in json_str
        assert '"nested"' in json_str

    def test_trace_to_dict(self):
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
    def test_store_trace_success(self):
        conn = duckdb.connect(":memory:")
        init_schema(conn)

        # store_trace has a foreign key to artifacts
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

        store_trace(
            conn,
            function_id="test.py::foo",
            agent="proposer",
            trace={"analysis": "Function foo does nothing"},
        )

        traces = get_traces(conn, "test.py::foo")
        assert len(traces) == 1
        assert traces[0].agent == "proposer"
        assert traces[0].trace["analysis"] == "Function foo does nothing"
        conn.close()

    def test_store_trace_invalid_agent(self):
        """The agent is checked before anything is written: no connection is needed, and none is used."""
        conn = MagicMock()

        with pytest.raises(ValueError) as exc_info:
            store_trace(
                conn,
                function_id="test.py::foo",
                agent="invalid_agent",
                trace={"analysis": "test"},
            )
        assert "Invalid agent type" in str(exc_info.value)
        conn.execute.assert_not_called()

    def test_store_multiple_traces(self):
        conn = duckdb.connect(":memory:")
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

        store_trace(conn, "test.py::foo", "proposer", {"step": 1})
        store_trace(conn, "test.py::foo", "critic", {"step": 2})
        store_trace(conn, "test.py::foo", "judge", {"step": 3})

        traces = get_traces(conn, "test.py::foo")
        assert len(traces) == 3
        conn.close()


class TestGetTraces:
    def test_get_traces_empty(self):
        conn = duckdb.connect(":memory:")
        init_schema(conn)

        traces = get_traces(conn, "nonexistent.py::foo")
        assert traces == []
        conn.close()

    def test_get_traces_with_filter(self):
        conn = duckdb.connect(":memory:")
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

        proposer_traces = get_traces(conn, "test.py::foo", agent="proposer")
        assert len(proposer_traces) == 2

        critic_traces = get_traces(conn, "test.py::foo", agent="critic")
        assert len(critic_traces) == 1
        conn.close()

    def test_get_traces_limit(self):
        conn = duckdb.connect(":memory:")
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

        for i in range(5):
            store_trace(conn, "test.py::foo", "proposer", {"num": i})

        traces = get_traces(conn, "test.py::foo", limit=3)
        assert len(traces) == 3
        conn.close()


class TestGetLatestTrace:
    def test_get_latest_trace_none(self):
        conn = duckdb.connect(":memory:")
        init_schema(conn)

        trace = get_latest_trace(conn, "nonexistent.py::foo")
        assert trace is None
        conn.close()

    def test_get_latest_trace_success(self):
        conn = duckdb.connect(":memory:")
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

        latest = get_latest_trace(conn, "test.py::foo")
        assert latest is not None
        assert latest.agent == "judge"
        assert latest.trace["version"] == 3
        conn.close()

    def test_get_latest_trace_with_filter(self):
        conn = duckdb.connect(":memory:")
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

        latest = get_latest_trace(conn, "test.py::foo", agent="proposer")
        assert latest is not None
        assert latest.agent == "proposer"
        assert latest.trace["v"] == 2
        conn.close()


class TestCountTraces:
    def test_count_traces_empty(self):
        conn = duckdb.connect(":memory:")
        init_schema(conn)

        count = count_traces(conn)
        assert count == 0
        conn.close()

    def test_count_traces_all(self):
        conn = duckdb.connect(":memory:")
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
        conn = duckdb.connect(":memory:")
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

        assert count_traces(conn, function_id="test.py::foo") == 3
        assert count_traces(conn, function_id="test.py::bar") == 1

        assert count_traces(conn, agent="proposer") == 3
        assert count_traces(conn, agent="critic") == 1

        assert count_traces(conn, function_id="test.py::foo", agent="proposer") == 2
        conn.close()


class TestDeleteTraces:
    def test_delete_traces_all(self):
        conn = duckdb.connect(":memory:")
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
        conn = duckdb.connect(":memory:")
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

        deleted = delete_traces(conn, "test.py::foo", agent="proposer")
        assert deleted == 2

        assert count_traces(conn, function_id="test.py::foo") == 1
        assert count_traces(conn, function_id="test.py::foo", agent="critic") == 1
        conn.close()

    def test_delete_traces_nonexistent(self):
        conn = duckdb.connect(":memory:")
        init_schema(conn)

        deleted = delete_traces(conn, "nonexistent.py::foo")
        assert deleted == 0
        conn.close()
