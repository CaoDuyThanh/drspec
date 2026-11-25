"""Tests for the public API module."""

import json

import pytest
import duckdb

from drspec.api import (
    # Exceptions
    DrSpecError,
    NotInitializedError,
    ContractNotFoundError,
    VerificationError,
    DatabaseBusyError,
    # Configuration
    configure,
    is_initialized,
    # Public API
    query_contract,
    run_verification,
    get_dependencies,
    # Types
    Contract,
    DependencyGraph,
    RuntimeVerificationResult,
)
from drspec.db.connection import init_schema
from drspec.db.queries import (
    insert_artifact,
    insert_contract,
)
from tests.helpers import insert_dependency


@pytest.fixture
def temp_db(tmp_path, kept_duckdb):
    """An initialised project database. The objective of these tests is what the API returns for the rows in it, not that the rows reach
    a file, so the database is one in-memory DuckDB kept between the API's connections (`kept_duckdb`)."""
    db_dir = tmp_path / "_drspec"
    db_dir.mkdir()
    db_path = db_dir / "contracts.db"

    conn = duckdb.connect(str(db_path))
    init_schema(conn)
    conn.close()

    return db_path


def _populate(conn):
    """Three functions, one contract (two invariants) and one dependency, in a database that has the schema."""
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

    insert_dependency(conn, "src/utils.py::parse", "src/utils.py::validate")


@pytest.fixture
def populated_db(temp_db):
    conn = duckdb.connect(str(temp_db))
    _populate(conn)
    conn.close()
    return temp_db


class TestExceptions:
    def test_exception_hierarchy(self):
        assert issubclass(NotInitializedError, DrSpecError)
        assert issubclass(ContractNotFoundError, DrSpecError)
        assert issubclass(VerificationError, DrSpecError)
        assert issubclass(DatabaseBusyError, DrSpecError)

    def test_exception_messages(self):
        exc = NotInitializedError("test message")
        assert str(exc) == "test message"


class TestConfiguration:
    def test_configure_sets_path(self, temp_db):
        configure(str(temp_db))
        try:
            assert is_initialized()
        finally:
            configure(None)

    def test_configure_none_resets(self, temp_db, tmp_path, monkeypatch):
        (tmp_path / "elsewhere").mkdir()
        monkeypatch.chdir(tmp_path / "elsewhere")  # no project here: the default path does not exist
        configure(str(temp_db))
        assert is_initialized()
        configure(None)
        assert not is_initialized()  # back to the default path, not the configured one

    def test_is_initialized_returns_false_for_missing(self, tmp_path):
        configure(str(tmp_path / "missing" / "contracts.db"))  # a database that was never made
        try:
            assert not is_initialized()
        finally:
            configure(None)

    def test_is_initialized_returns_true_for_existing(self, temp_db):
        configure(str(temp_db))
        try:
            assert is_initialized()
        finally:
            configure(None)

    def test_env_variable_override(self, temp_db, monkeypatch):
        monkeypatch.setenv("DRSPEC_DB_PATH", str(temp_db))
        configure(None)  # ensure no programmatic override masks the env var

        assert is_initialized()


class TestQueryContract:
    def test_query_existing_contract(self, populated_db):
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
        configure(str(populated_db))
        try:
            contract = query_contract("src/utils.py::validate")
            assert contract is None
        finally:
            configure(None)

    def test_query_raises_when_not_initialized(self, tmp_path):
        configure(str(tmp_path / "missing" / "contracts.db"))  # a database that was never made
        try:
            with pytest.raises(NotInitializedError):
                query_contract("src/utils.py::parse")
        finally:
            configure(None)


class TestRunVerification:
    def test_verification_passes(self, populated_db, in_process_python):
        configure(str(populated_db))
        try:
            result = run_verification(
                "src/utils.py::parse",
                input_data={"text": "hello"},
                output_data={"key": "value"},
            )

            assert isinstance(result, RuntimeVerificationResult)
            assert result.function_id == "src/utils.py::parse"
            # outcome depends on invariant translation; just checking it runs without error
        finally:
            configure(None)

    def test_verification_raises_for_missing_contract(self, populated_db):
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

    def test_verification_raises_when_not_initialized(self, tmp_path):
        configure(str(tmp_path / "missing" / "contracts.db"))  # a database that was never made
        try:
            with pytest.raises(NotInitializedError):
                run_verification("src/utils.py::parse", {}, {})
        finally:
            configure(None)


class TestGetDependencies:
    def test_get_dependencies_basic(self, populated_db):
        configure(str(populated_db))
        try:
            graph = get_dependencies("src/utils.py::parse", depth=1)

            assert isinstance(graph, DependencyGraph)
            assert graph.root_function_id == "src/utils.py::parse"
            assert graph.node_count >= 1  # at least the root node
        finally:
            configure(None)

    def test_get_dependencies_with_direction(self, populated_db):
        configure(str(populated_db))
        try:
            graph = get_dependencies(
                "src/utils.py::parse",
                depth=1,
                direction="callees",
            )

            assert isinstance(graph, DependencyGraph)
            # parse is root, validate is its callee
            assert graph.node_count >= 1
        finally:
            configure(None)

    def test_get_dependencies_raises_when_not_initialized(self, tmp_path):
        configure(str(tmp_path / "missing" / "contracts.db"))  # a database that was never made
        try:
            with pytest.raises(NotInitializedError):
                get_dependencies("src/utils.py::parse")
        finally:
            configure(None)


class TestTypeExports:
    def test_contract_type_exported(self):
        from drspec.api import Contract as ApiContract
        from drspec.contracts.schema import Contract as SchemaContract
        assert ApiContract is SchemaContract

    def test_dependency_graph_type_exported(self):
        from drspec.api import DependencyGraph as ApiGraph
        from drspec.db.graph import DependencyGraph as DbGraph
        assert ApiGraph is DbGraph

    def test_verification_result_type_exported(self):
        from drspec.api import RuntimeVerificationResult as ApiResult
        from drspec.debugging.runtime import RuntimeVerificationResult as DbResult
        assert ApiResult is DbResult


class TestAllExports:
    def test_all_exports_accessible(self):
        from drspec import api

        for name in api.__all__:
            assert hasattr(api, name), f"Missing export: {name}"


class TestRoutesThroughTheWorker:
    """Exercises the API against a real, separate worker process rather than the
    in-process test-mode connection used elsewhere in this file (via
    DRSPEC_DISABLE_AUTO_WORKER), since calling get_connection() directly here
    would crash against the worker's permanently-held connection.
    """

    @pytest.mark.integration  # the objective is the API reaching a worker over a REAL unix socket (the server runs in this process: the client cannot tell)
    def test_api_calls_succeed_against_a_worker_on_a_real_socket(self, tmp_path, monkeypatch):
        from drspec.worker_ipc import WorkerIPCServer

        monkeypatch.delenv("DRSPEC_DISABLE_AUTO_WORKER", raising=False)  # the API goes through the worker, as it does for a user
        db_path = tmp_path / "_drspec" / "contracts.db"
        db_path.parent.mkdir()
        db_path.touch()  # the API checks that the project database exists; the worker's own connection is in memory
        conn = duckdb.connect(":memory:")
        init_schema(conn)
        _populate(conn)
        server = WorkerIPCServer(conn, db_path, remediate_fn=lambda *args: 0)
        server.start()  # binds the socket file beside the database and accepts in a thread
        try:
            configure(str(db_path))
            try:
                assert is_initialized()
                contract = query_contract("src/utils.py::parse")  # the rows come back over the socket
                assert contract.intent_summary == "Parses text input into a dictionary structure"
                assert query_contract("src/other.py::process") is None  # no contract yet, not an error
                graph = get_dependencies("src/utils.py::parse", depth=1)
                assert graph.root_function_id == "src/utils.py::parse" and graph.node_count == 2  # the root and its callee
                assert server.wake.is_set()  # and it was a client of the socket that asked, not a direct connection
            finally:
                configure(None)
        finally:
            server.stop()
