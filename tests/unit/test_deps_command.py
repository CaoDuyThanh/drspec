"""`drspec deps get`: the lookup behind it (`drspec.core.dependency_info.get_dependency_info` / `find_similar_functions`, which the MCP server's
`deps_get` shares) and the command's range check for `--depth`. The databases are in memory; the lookup tests call the product's functions."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.core.dependency_info import find_similar_functions, get_dependency_info
from drspec.db import (
    get_connection,
    init_schema,
    insert_artifact,
    insert_contract,
)
from tests.helpers import insert_dependency

runner = CliRunner()

ENTRY_FIELDS = {"function_id", "function_name", "file_path", "depth", "relation", "confidence", "has_contract", "status", "possibly_stale"}


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def db_conn():
    conn = get_connection(":memory:")
    init_schema(conn)
    yield conn
    conn.close()


def _insert_test_artifact(conn, function_id, file_path, function_name):
    insert_artifact(
        conn,
        function_id=function_id,
        file_path=file_path,
        function_name=function_name,
        signature=f"def {function_name}()",
        body="pass",
        code_hash=f"hash_{function_id}",
        language="python",
        start_line=1,
        end_line=5,
        parent=None,
        status="PENDING",
    )


@pytest.fixture
def sample_dependency_graph(db_conn):
    """Graph structure:
        main -> process -> validate -> transform
              |         -> helper
              -> log
    """
    _insert_test_artifact(db_conn, "main.py::main", "main.py", "main")
    _insert_test_artifact(db_conn, "process.py::process", "process.py", "process")
    _insert_test_artifact(db_conn, "validate.py::validate", "validate.py", "validate")
    _insert_test_artifact(db_conn, "transform.py::transform", "transform.py", "transform")
    _insert_test_artifact(db_conn, "helper.py::helper", "helper.py", "helper")
    _insert_test_artifact(db_conn, "log.py::log", "log.py", "log")

    insert_dependency(db_conn, "main.py::main", "process.py::process")
    insert_dependency(db_conn, "main.py::main", "log.py::log")
    insert_dependency(db_conn, "process.py::process", "validate.py::validate")
    insert_dependency(db_conn, "process.py::process", "helper.py::helper")
    insert_dependency(db_conn, "validate.py::validate", "transform.py::transform")

    insert_contract(db_conn, "process.py::process", '{"invariants": []}', 0.8)
    insert_contract(db_conn, "validate.py::validate", '{"invariants": []}', 0.7)

    return db_conn


def _ids(entries):
    return [(entry["function_id"], entry["depth"]) for entry in entries]


# =============================================================================
# get_dependency_info Tests
# =============================================================================


@pytest.mark.unit
class TestGetDependencyInfo:
    """Tests for `get_dependency_info` (the product's function)."""

    def test_returns_direct_callees(self, sample_dependency_graph):
        callees, _ = get_dependency_info(sample_dependency_graph, "main.py::main", depth=1)

        assert _ids(callees) == [("log.py::log", 1), ("process.py::process", 1)]

    def test_returns_direct_callers(self, sample_dependency_graph):
        _, callers = get_dependency_info(sample_dependency_graph, "process.py::process", depth=1)

        assert _ids(callers) == [("main.py::main", 1)]


    def test_depth_one_only_direct(self, sample_dependency_graph):
        callees, _ = get_dependency_info(sample_dependency_graph, "main.py::main", depth=1)

        assert len(callees) == 2
        assert {callee["depth"] for callee in callees} == {1}

    def test_depth_two_includes_transitive(self, sample_dependency_graph):
        callees, _ = get_dependency_info(sample_dependency_graph, "main.py::main", depth=2)

        assert _ids(callees) == [("log.py::log", 1), ("process.py::process", 1), ("helper.py::helper", 2), ("validate.py::validate", 2)]


    def test_includes_contract_status(self, sample_dependency_graph):
        callees, _ = get_dependency_info(sample_dependency_graph, "main.py::main", depth=3)

        assert {c["function_id"]: c["has_contract"] for c in callees} == {
            "log.py::log": False,
            "process.py::process": True,  # a contract in the fixture
            "helper.py::helper": False,
            "validate.py::validate": True,  # a contract in the fixture
            "transform.py::transform": False,
        }

    def test_includes_function_details(self, sample_dependency_graph):
        callees, _ = get_dependency_info(sample_dependency_graph, "main.py::main", depth=1)

        process = next(c for c in callees if c["function_id"] == "process.py::process")
        assert process == {
            "function_id": "process.py::process",
            "function_name": "process",
            "file_path": "process.py",
            "depth": 1,
            "relation": "calls",
            "confidence": None,
            "has_contract": True,
            "status": "PENDING",
            "possibly_stale": False,
        }

    def test_empty_when_no_dependencies(self, db_conn):
        _insert_test_artifact(db_conn, "isolated::func", "isolated.py", "func")

        callees, callers = get_dependency_info(db_conn, "isolated::func", depth=1)

        assert callees == []
        assert callers == []

    def test_handles_circular_dependencies(self, db_conn):
        _insert_test_artifact(db_conn, "a::func_a", "a.py", "func_a")
        _insert_test_artifact(db_conn, "b::func_b", "b.py", "func_b")

        # a -> b -> a
        insert_dependency(db_conn, "a::func_a", "b::func_b")
        insert_dependency(db_conn, "b::func_b", "a::func_a")

        callees, callers = get_dependency_info(db_conn, "a::func_a", depth=3)

        # the function asked about never comes back as its own callee or caller
        assert _ids(callees) == [("b::func_b", 1)]
        assert _ids(callers) == [("b::func_b", 1)]


# =============================================================================
# find_similar_functions Tests
# =============================================================================


@pytest.mark.unit
class TestFindSimilarFunctions:
    """Tests for `find_similar_functions` (the product's function)."""

    def test_finds_similar_names(self, db_conn):
        _insert_test_artifact(db_conn, "a.py::process_data", "a.py", "process_data")
        _insert_test_artifact(db_conn, "b.py::process_items", "b.py", "process_items")
        _insert_test_artifact(db_conn, "c.py::validate", "c.py", "validate")

        similar = find_similar_functions(db_conn, "x.py::process")

        assert sorted(similar) == ["a.py::process_data", "b.py::process_items"]


    def test_returns_empty_when_no_match(self, db_conn):
        _insert_test_artifact(db_conn, "a.py::foo", "a.py", "foo")

        similar = find_similar_functions(db_conn, "x.py::completely_different_name_xyz")

        assert similar == []

    def test_limits_results(self, db_conn):
        for i in range(10):
            _insert_test_artifact(db_conn, f"f{i}.py::process_{i}", f"f{i}.py", f"process_{i}")

        similar = find_similar_functions(db_conn, "x.py::process", limit=3)

        assert len(similar) == 3
        assert all("process_" in function_id for function_id in similar)


# =============================================================================
# Response Format Tests
# =============================================================================


@pytest.mark.unit
class TestDepsResponseFormat:
    """Tests for response format validation."""

    def test_callees_sorted_by_depth_then_id(self, sample_dependency_graph):
        callees, _ = get_dependency_info(sample_dependency_graph, "main.py::main", depth=3)

        assert _ids(callees) == sorted(_ids(callees), key=lambda pair: (pair[1], pair[0]))
        assert [depth for _, depth in _ids(callees)] == [1, 1, 2, 2, 3]

    def test_response_has_all_required_fields(self, sample_dependency_graph):
        callees, callers = get_dependency_info(sample_dependency_graph, "process.py::process", depth=1)

        assert len(callees) == 2 and len(callers) == 1
        for entry in callees + callers:
            assert set(entry) == ENTRY_FIELDS


# =============================================================================
# Edge Cases
# =============================================================================


@pytest.mark.unit
class TestEdgeCases:
    """Tests for edge cases."""

    def test_function_with_many_callees(self, db_conn):
        # ten: past any single-digit cap (`find_similar_functions` stops at 5); the lookup itself has none
        _insert_test_artifact(db_conn, "hub::func", "hub.py", "func")

        for i in range(10):
            _insert_test_artifact(db_conn, f"callee{i}.py::func{i}", f"callee{i}.py", f"func{i}")
            insert_dependency(db_conn, "hub::func", f"callee{i}.py::func{i}")

        callees, _ = get_dependency_info(db_conn, "hub::func", depth=1)

        assert sorted(c["function_id"] for c in callees) == sorted(f"callee{i}.py::func{i}" for i in range(10))

    def test_function_with_many_callers(self, db_conn):
        # ten, as above
        _insert_test_artifact(db_conn, "hub::func", "hub.py", "func")

        for i in range(10):
            _insert_test_artifact(db_conn, f"caller{i}.py::func{i}", f"caller{i}.py", f"func{i}")
            insert_dependency(db_conn, f"caller{i}.py::func{i}", "hub::func")

        _, callers = get_dependency_info(db_conn, "hub::func", depth=1)

        assert sorted(c["function_id"] for c in callers) == sorted(f"caller{i}.py::func{i}" for i in range(10))


@pytest.mark.component  # the command runner with the project database (in memory, kept between the calls)
@pytest.mark.usefixtures("kept_duckdb", "cached_cli_command")
class TestTheCommandChecksTheDepth:
    def test_a_depth_outside_1_to_5_is_refused_before_any_lookup(self, tmp_path):
        db_path = tmp_path / "_drspec" / "drspec.db"
        db_path.parent.mkdir()
        conn = get_connection(str(db_path))
        init_schema(conn)
        conn.close()

        result = runner.invoke(app, ["--db", str(db_path), "deps", "get", "a.py::f", "--depth", "0"])

        assert result.exit_code == 2  # a usage error, not "function not found"
        assert "--depth" in result.output and "1<=x<=5" in result.output.replace(" ", "")
        assert "not found" not in result.output.lower()
