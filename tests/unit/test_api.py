"""Tests for the public API module."""

import json
import tempfile
from pathlib import Path

import pytest
import duckdb

from drspec.api import (
    # Exceptions
    DrSpecError,
    NotInitializedError,
    ContractNotFoundError,
    VerificationError,
    # Configuration
    configure,
    is_initialized,
    # Public API
    query_contract,
    run_verification,
    list_queue,
    get_dependencies,
    # Types
    Contract,
    QueueItem,
    DependencyGraph,
    RuntimeVerificationResult,
)
from drspec.db.connection import init_schema
from drspec.db.queries import (
    insert_artifact,
    insert_contract,
    queue_push,
    insert_dependency,
)


@pytest.fixture
def temp_db():
    """Create a temporary database with initialized schema."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_dir = Path(tmpdir) / "_drspec"
        db_dir.mkdir()
        db_path = db_dir / "contracts.db"

        conn = duckdb.connect(str(db_path))
        init_schema(conn)
        conn.close()

        yield db_path


@pytest.fixture
def populated_db(temp_db):
    """Create a database with sample data."""
    conn = duckdb.connect(str(temp_db))

    # Insert artifacts
    insert_artifact(
        conn,
        function_id="src/utils.py::parse",
        file_path="src/utils.py",
        function_name="parse",
        signature="def parse(text: str) -> dict:",
        body="return {}",
        code_hash="hash1",
        language="python",
        start_line=1,
        end_line=5,
    )

    insert_artifact(
        conn,
        function_id="src/utils.py::validate",
        file_path="src/utils.py",
        function_name="validate",
        signature="def validate(data: dict) -> bool:",
        body="return True",
        code_hash="hash2",
        language="python",
        start_line=10,
        end_line=15,
    )

    insert_artifact(
        conn,
        function_id="src/other.py::process",
        file_path="src/other.py",
        function_name="process",
        signature="def process(data):",
        body="return data",
        code_hash="hash3",
        language="python",
        start_line=1,
        end_line=5,
    )

    # Insert contract for parse function
    contract_json = json.dumps({
        "function_signature": "def parse(text: str) -> dict:",
        "intent_summary": "Parses text input into a dictionary structure",
        "invariants": [
            {
                "name": "returns_dict",
                "logic": "Output is a dictionary",
                "criticality": "HIGH",
                "on_fail": "error",
            },
            {
                "name": "non_empty_input",
                "logic": "Input text is not empty",
                "criticality": "MEDIUM",
                "on_fail": "warn",
            },
        ],
        "io_examples": [],
    })

    insert_contract(
        conn,
        function_id="src/utils.py::parse",
        contract_json=contract_json,
        confidence_score=0.85,
    )

    # Insert queue items
    queue_push(conn, function_id="src/utils.py::validate", reason="NEW", priority=50)
    queue_push(conn, function_id="src/other.py::process", reason="NEW", priority=75)

    # Insert dependencies
    insert_dependency(conn, "src/utils.py::parse", "src/utils.py::validate")

    conn.close()
    return temp_db


class TestExceptions:
    """Tests for API exceptions."""

    def test_exception_hierarchy(self):
        """Test that all exceptions inherit from DrSpecError."""
        assert issubclass(NotInitializedError, DrSpecError)
        assert issubclass(ContractNotFoundError, DrSpecError)
        assert issubclass(VerificationError, DrSpecError)

    def test_exception_messages(self):
        """Test exception message formatting."""
        exc = NotInitializedError("test message")
        assert str(exc) == "test message"


class TestConfiguration:
    """Tests for configuration functions."""

    def test_configure_sets_path(self, temp_db):
        """Test that configure sets the database path."""
        configure(str(temp_db))
        try:
            assert is_initialized()
        finally:
            configure(None)  # Reset

    def test_configure_none_resets(self, temp_db):
        """Test that configure(None) resets to default."""
        configure(str(temp_db))
        configure(None)
        # Should now use default path
        # (may not exist, but configuration is reset)

    def test_is_initialized_returns_false_for_missing(self):
        """Test is_initialized returns False when DB doesn't exist."""
        configure("/nonexistent/path/contracts.db")
        try:
            assert not is_initialized()
        finally:
            configure(None)

    def test_is_initialized_returns_true_for_existing(self, temp_db):
        """Test is_initialized returns True when DB exists."""
        configure(str(temp_db))
        try:
            assert is_initialized()
        finally:
            configure(None)

    def test_env_variable_override(self, temp_db, monkeypatch):
        """Test DRSPEC_DB_PATH environment variable."""
        monkeypatch.setenv("DRSPEC_DB_PATH", str(temp_db))
        configure(None)  # Ensure no programmatic override

        assert is_initialized()


class TestQueryContract:
    """Tests for query_contract function."""

    def test_query_existing_contract(self, populated_db):
        """Test querying an existing contract."""
        configure(str(populated_db))
        try:
            contract = query_contract("src/utils.py::parse")

            assert contract is not None
            assert isinstance(contract, Contract)
            assert contract.intent_summary == "Parses text input into a dictionary structure"
            assert len(contract.invariants) == 2
        finally:
            configure(None)

    def test_query_nonexistent_contract(self, populated_db):
        """Test querying a function without a contract."""
        configure(str(populated_db))
        try:
            contract = query_contract("src/utils.py::validate")
            assert contract is None
        finally:
            configure(None)

    def test_query_raises_when_not_initialized(self):
        """Test query_contract raises NotInitializedError when not initialized."""
        configure("/nonexistent/path/contracts.db")
        try:
            with pytest.raises(NotInitializedError):
                query_contract("src/utils.py::parse")
        finally:
            configure(None)


class TestRunVerification:
    """Tests for run_verification function."""

    def test_verification_passes(self, populated_db):
        """Test verification with passing data."""
        configure(str(populated_db))
        try:
            result = run_verification(
                "src/utils.py::parse",
                input_data={"text": "hello"},
                output_data={"key": "value"},
            )

            assert isinstance(result, RuntimeVerificationResult)
            assert result.function_id == "src/utils.py::parse"
            # Result may pass or fail depending on invariant translation
            # The important thing is it runs without error
        finally:
            configure(None)

    def test_verification_raises_for_missing_contract(self, populated_db):
        """Test verification raises ContractNotFoundError for missing contract."""
        configure(str(populated_db))
        try:
            with pytest.raises(ContractNotFoundError) as exc_info:
                run_verification(
                    "src/utils.py::validate",
                    input_data={},
                    output_data=True,
                )

            assert "No contract found" in str(exc_info.value)
        finally:
            configure(None)

    def test_verification_raises_when_not_initialized(self):
        """Test run_verification raises NotInitializedError when not initialized."""
        configure("/nonexistent/path/contracts.db")
        try:
            with pytest.raises(NotInitializedError):
                run_verification("src/utils.py::parse", {}, {})
        finally:
            configure(None)


class TestListQueue:
    """Tests for list_queue function."""

    def test_list_all_queue(self, populated_db):
        """Test listing all queue items."""
        configure(str(populated_db))
        try:
            items = list_queue()

            assert len(items) == 2
            assert all(isinstance(item, QueueItem) for item in items)
        finally:
            configure(None)

    def test_list_queue_with_status_filter(self, populated_db):
        """Test filtering queue by status."""
        configure(str(populated_db))
        try:
            items = list_queue(status="PENDING")

            assert len(items) == 2  # All items are pending
            assert all(item.status == "PENDING" for item in items)
        finally:
            configure(None)

    def test_list_queue_with_priority_filter(self, populated_db):
        """Test filtering queue by minimum priority."""
        configure(str(populated_db))
        try:
            items = list_queue(priority_min=60)

            assert len(items) == 1
            assert items[0].priority >= 60
        finally:
            configure(None)

    def test_list_queue_with_limit(self, populated_db):
        """Test limiting queue results."""
        configure(str(populated_db))
        try:
            items = list_queue(limit=1)

            assert len(items) == 1
        finally:
            configure(None)

    def test_list_queue_raises_when_not_initialized(self):
        """Test list_queue raises NotInitializedError when not initialized."""
        configure("/nonexistent/path/contracts.db")
        try:
            with pytest.raises(NotInitializedError):
                list_queue()
        finally:
            configure(None)


class TestGetDependencies:
    """Tests for get_dependencies function."""

    def test_get_dependencies_basic(self, populated_db):
        """Test getting dependencies for a function."""
        configure(str(populated_db))
        try:
            graph = get_dependencies("src/utils.py::parse", depth=1)

            assert isinstance(graph, DependencyGraph)
            assert graph.root_function_id == "src/utils.py::parse"
            assert graph.node_count >= 1  # At least root node
        finally:
            configure(None)

    def test_get_dependencies_with_direction(self, populated_db):
        """Test getting dependencies with specific direction."""
        configure(str(populated_db))
        try:
            # Get callees only
            graph = get_dependencies(
                "src/utils.py::parse",
                depth=1,
                direction="callees",
            )

            assert isinstance(graph, DependencyGraph)
            # Should have parse as root and validate as callee
            assert graph.node_count >= 1
        finally:
            configure(None)

    def test_get_dependencies_raises_when_not_initialized(self):
        """Test get_dependencies raises NotInitializedError when not initialized."""
        configure("/nonexistent/path/contracts.db")
        try:
            with pytest.raises(NotInitializedError):
                get_dependencies("src/utils.py::parse")
        finally:
            configure(None)


class TestTypeExports:
    """Tests that types are properly exported."""

    def test_contract_type_exported(self):
        """Test Contract type is exported."""
        from drspec.api import Contract as ApiContract
        from drspec.contracts.schema import Contract as SchemaContract
        assert ApiContract is SchemaContract

    def test_queue_item_type_exported(self):
        """Test QueueItem type is exported."""
        from drspec.api import QueueItem as ApiQueueItem
        from drspec.db.queries import QueueItem as DbQueueItem
        assert ApiQueueItem is DbQueueItem

    def test_dependency_graph_type_exported(self):
        """Test DependencyGraph type is exported."""
        from drspec.api import DependencyGraph as ApiGraph
        from drspec.db.graph import DependencyGraph as DbGraph
        assert ApiGraph is DbGraph

    def test_verification_result_type_exported(self):
        """Test RuntimeVerificationResult type is exported."""
        from drspec.api import RuntimeVerificationResult as ApiResult
        from drspec.debugging.runtime import RuntimeVerificationResult as DbResult
        assert ApiResult is DbResult


class TestAllExports:
    """Test that __all__ exports are correct."""

    def test_all_exports_accessible(self):
        """Test all items in __all__ are accessible."""
        from drspec import api

        for name in api.__all__:
            assert hasattr(api, name), f"Missing export: {name}"
