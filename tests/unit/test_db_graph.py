"""Tests for dependency graph query functions. The databases are in memory; the populated graph is built once for the module (every test that uses
it only reads, and a check at the end of the module says nothing changed it)."""

import duckdb
import pytest

import drspec.db
from drspec.db import graph as graph_module
from drspec.db.connection import init_schema
from drspec.db.graph import (
    DependencyEdge,
    DependencyGraph,
    DependencyNode,
    get_callee_graph,
    get_caller_graph,
    get_dependency_graph,
    get_graph_statistics,
)
from drspec.db.queries import (
    insert_artifact,
    insert_contract,
)
from tests.helpers import insert_dependency

pytestmark = pytest.mark.unit  # pure functions and an in-memory database


def _artifact(conn, function_id, file_path, name, code_hash):
    insert_artifact(
        conn,
        function_id=function_id,
        file_path=file_path,
        function_name=name,
        signature=f"def {name}():",
        body="pass",
        code_hash=code_hash,
        language="python",
        start_line=1,
        end_line=5,
    )


def _short(function_id):
    return function_id.split("::")[1]


def _nodes(graph):
    """What a graph holds, in order: (name, depth, relationship)."""
    return [(_short(n.function_id), n.depth, n.relationship) for n in graph.nodes]


def _edges(graph):
    return [(_short(e.caller_id), _short(e.callee_id)) for e in graph.edges]


def _fingerprint(conn):
    return [conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("artifacts", "dependencies", "contracts")]


@pytest.fixture
def db_conn():
    conn = duckdb.connect(":memory:")
    init_schema(conn)
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def populated_db():
    """A database with sample artifacts and dependencies, for tests that only read it.

    Graph structure:
        main -> helper -> utils
          |        |
          v        v
        logger <- formatter
    """
    conn = duckdb.connect(":memory:")
    init_schema(conn)
    for fid, fpath, fname in [
        ("src/app.py::main", "src/app.py", "main"),
        ("src/app.py::helper", "src/app.py", "helper"),
        ("src/utils.py::utils", "src/utils.py", "utils"),
        ("src/log.py::logger", "src/log.py", "logger"),
        ("src/fmt.py::formatter", "src/fmt.py", "formatter"),
    ]:
        _artifact(conn, fid, fpath, fname, f"hash_{fname}")

    insert_contract(
        conn,
        function_id="src/app.py::main",
        contract_json='{"function_signature": "def main():", "intent_summary": "Main entry point", "invariants": [{"name": "test", "logic": "always true", "criticality": "LOW", "on_fail": "warn"}]}',
        confidence_score=0.85,
    )

    for caller, callee in [
        ("src/app.py::main", "src/app.py::helper"),
        ("src/app.py::main", "src/log.py::logger"),
        ("src/app.py::helper", "src/utils.py::utils"),
        ("src/app.py::helper", "src/fmt.py::formatter"),
        ("src/fmt.py::formatter", "src/log.py::logger"),
    ]:
        insert_dependency(conn, caller_id=caller, callee_id=callee)

    before = _fingerprint(conn)
    assert before == [5, 5, 1]
    yield conn
    assert _fingerprint(conn) == before, "a test changed the database that every test of the module shares"
    conn.close()


@pytest.fixture
def chain_db(db_conn):
    """f0 -> f1 -> ... -> f7."""
    for i in range(8):
        _artifact(db_conn, f"chain.py::f{i}", "chain.py", f"f{i}", f"chain{i}")
    for i in range(7):
        insert_dependency(db_conn, f"chain.py::f{i}", f"chain.py::f{i + 1}")
    return db_conn


class TestDependencyNodeModel:
    def test_node_creation(self):
        node = DependencyNode(
            function_id="src/app.py::main",
            function_name="main",
            file_path="src/app.py",
            status="VERIFIED",
            has_contract=True,
            depth=0,
            relationship="root",
        )

        assert node.function_id == "src/app.py::main"
        assert node.function_name == "main"
        assert node.file_path == "src/app.py"
        assert node.status == "VERIFIED"
        assert node.has_contract is True
        assert node.depth == 0
        assert node.relationship == "root"

    def test_node_to_dict(self):
        node = DependencyNode(
            function_id="src/app.py::main",
            function_name="main",
            file_path="src/app.py",
            status="PENDING",
            has_contract=False,
            depth=1,
            relationship="callee",
        )

        assert node.to_dict() == {
            "function_id": "src/app.py::main",
            "function_name": "main",
            "file_path": "src/app.py",
            "status": "PENDING",
            "has_contract": False,
            "depth": 1,
            "relationship": "callee",
        }


class TestDependencyEdgeModel:
    def test_edge_creation(self):
        edge = DependencyEdge(
            caller_id="src/app.py::main",
            callee_id="src/app.py::helper",
            is_cyclic=False,
        )

        assert edge.caller_id == "src/app.py::main"
        assert edge.callee_id == "src/app.py::helper"
        assert edge.is_cyclic is False

    def test_edge_to_dict(self):
        edge = DependencyEdge(
            caller_id="src/app.py::main",
            callee_id="src/app.py::helper",
            is_cyclic=True,
        )

        assert edge.to_dict() == {"caller_id": "src/app.py::main", "callee_id": "src/app.py::helper", "is_cyclic": True}


class TestDependencyGraphModel:
    def test_graph_creation(self):
        graph = DependencyGraph(root_function_id="src/app.py::main")

        assert graph.root_function_id == "src/app.py::main"
        assert graph.nodes == []
        assert graph.edges == []
        assert graph.has_cycles is False
        assert graph.max_depth_reached == 0

    def test_graph_node_count(self):
        graph = DependencyGraph(root_function_id="test")
        graph.nodes = [
            DependencyNode("a", "a", "", "PENDING", False, 0, "root"),
            DependencyNode("b", "b", "", "PENDING", False, 1, "callee"),
        ]

        assert graph.node_count == 2

    def test_graph_edge_count(self):
        graph = DependencyGraph(root_function_id="test")
        graph.edges = [
            DependencyEdge("a", "b"),
            DependencyEdge("b", "c"),
        ]

        assert graph.edge_count == 2

    def test_graph_to_dict(self):
        graph = DependencyGraph(root_function_id="src/app.py::main")
        graph.nodes = [
            DependencyNode("a", "a", "", "PENDING", False, 0, "root"),
        ]
        graph.edges = [
            DependencyEdge("a", "b"),
        ]
        graph.has_cycles = True
        graph.max_depth_reached = 2

        result = graph.to_dict()

        assert result["root_function_id"] == "src/app.py::main"
        assert result["nodes"] == [graph.nodes[0].to_dict()]
        assert result["edges"] == [{"caller_id": "a", "callee_id": "b", "is_cyclic": False}]
        assert result["has_cycles"] is True
        assert result["max_depth_reached"] == 2


class TestGetDependencyGraph:
    def test_get_graph_single_node(self, db_conn):
        _artifact(db_conn, "src/app.py::standalone", "src/app.py", "standalone", "hash1")

        graph = get_dependency_graph(db_conn, "src/app.py::standalone")

        assert graph.root_function_id == "src/app.py::standalone"
        assert _nodes(graph) == [("standalone", 0, "root")]
        assert graph.edge_count == 0
        assert graph.has_cycles is False

    def test_get_graph_with_callees(self, populated_db):
        graph = get_dependency_graph(populated_db, "src/app.py::main", depth=1, direction="callees")

        assert graph.root_function_id == "src/app.py::main"
        # main + 2 direct callees (helper, logger)
        assert _nodes(graph) == [("main", 0, "root"), ("helper", 1, "callee"), ("logger", 1, "callee")]
        assert _edges(graph) == [("main", "helper"), ("main", "logger")]

    def test_get_graph_with_callers(self, populated_db):
        graph = get_dependency_graph(populated_db, "src/log.py::logger", depth=1, direction="callers")

        assert graph.root_function_id == "src/log.py::logger"
        # logger + 2 callers (main, formatter)
        assert _nodes(graph) == [("logger", 0, "root"), ("main", 1, "caller"), ("formatter", 1, "caller")]
        assert _edges(graph) == [("main", "logger"), ("formatter", "logger")]

    def test_get_graph_both_directions(self, populated_db):
        graph = get_dependency_graph(populated_db, "src/app.py::helper", depth=1, direction="both")

        # helper is called by main and calls utils, formatter
        assert graph.root_function_id == "src/app.py::helper"
        assert _nodes(graph) == [("helper", 0, "root"), ("utils", 1, "callee"), ("formatter", 1, "callee"), ("main", 1, "caller")]
        assert _edges(graph) == [("helper", "utils"), ("helper", "formatter"), ("main", "helper")]

    def test_get_graph_depth_2(self, populated_db):
        graph = get_dependency_graph(populated_db, "src/app.py::main", depth=2, direction="callees")

        # depth 1: helper, logger; depth 2: utils, formatter (from helper)
        assert graph.max_depth_reached == 2
        assert _nodes(graph) == [
            ("main", 0, "root"), ("helper", 1, "callee"), ("logger", 1, "callee"), ("utils", 2, "callee"), ("formatter", 2, "callee"),
        ]
        assert _edges(graph) == [("main", "helper"), ("main", "logger"), ("helper", "utils"), ("helper", "formatter")]


    def test_get_graph_nonexistent_function(self, db_conn):
        graph = get_dependency_graph(db_conn, "nonexistent::func")

        assert graph.root_function_id == "nonexistent::func"
        assert graph.node_count == 1  # still creates a root node
        assert graph.nodes[0].status == "UNKNOWN"
        assert graph.nodes[0].has_contract is False
        assert graph.nodes[0].function_name == "func"  # the short name of the id, since there is no artifact

    def test_get_graph_with_contract_status(self, populated_db):
        graph = get_dependency_graph(populated_db, "src/app.py::main", depth=2)

        # the contract was added only for main
        assert {_short(n.function_id): n.has_contract for n in graph.nodes} == {
            "main": True, "helper": False, "logger": False, "utils": False, "formatter": False,
        }


    def test_a_depth_above_five_is_taken_as_five(self, chain_db):
        graph = get_dependency_graph(chain_db, "chain.py::f0", depth=10, direction="callees")

        assert graph.max_depth_reached == 5
        assert [name for name, _, _ in _nodes(graph)] == ["f0", "f1", "f2", "f3", "f4", "f5"]  # f6 and f7 are beyond five hops


class TestGetCalleeGraph:
    def test_get_callee_graph(self, populated_db):
        graph = get_callee_graph(populated_db, "src/app.py::main", depth=1)

        assert graph.to_dict() == get_dependency_graph(populated_db, "src/app.py::main", depth=1, direction="callees").to_dict()
        assert _nodes(graph) == [("main", 0, "root"), ("helper", 1, "callee"), ("logger", 1, "callee")]


class TestGetCallerGraph:
    def test_get_caller_graph(self, populated_db):
        graph = get_caller_graph(populated_db, "src/log.py::logger", depth=1)

        assert graph.to_dict() == get_dependency_graph(populated_db, "src/log.py::logger", depth=1, direction="callers").to_dict()
        assert _nodes(graph) == [("logger", 0, "root"), ("main", 1, "caller"), ("formatter", 1, "caller")]


class TestCycleDetection:
    def test_detect_cycle(self, db_conn):
        for i in ["a", "b", "c"]:
            _artifact(db_conn, f"test.py::{i}", "test.py", i, f"hash{i}")

        # cycle: a -> b -> c -> a
        insert_dependency(db_conn, "test.py::a", "test.py::b")
        insert_dependency(db_conn, "test.py::b", "test.py::c")
        insert_dependency(db_conn, "test.py::c", "test.py::a")

        graph = get_dependency_graph(db_conn, "test.py::a", depth=3)

        assert graph.has_cycles is True
        # the edge that closes the loop is the cyclic one; the two that lead to it are not
        assert {(_short(e.caller_id), _short(e.callee_id)) for e in graph.edges if e.is_cyclic} == {("c", "a")}
        assert {(_short(e.caller_id), _short(e.callee_id)) for e in graph.edges if not e.is_cyclic} == {("a", "b"), ("b", "c")}


class TestGetGraphStatistics:
    def test_empty_database(self, db_conn):
        stats = get_graph_statistics(db_conn)

        assert stats["total_functions"] == 0
        assert stats["total_dependencies"] == 0
        assert stats["functions_with_contracts"] == 0
        assert stats["most_outgoing"] == []
        assert stats["most_incoming"] == []

    def test_populated_database(self, populated_db):
        stats = get_graph_statistics(populated_db)

        assert stats["total_functions"] == 5
        assert stats["total_dependencies"] == 5
        assert stats["functions_with_contracts"] == 1

        # main and helper are tied at 2 outgoing each (either may sort first), formatter has 1; utils and logger call nothing
        outgoing = stats["most_outgoing"]
        assert [row["callee_count"] for row in outgoing] == [2, 2, 1]
        assert {row["function_id"] for row in outgoing[:2]} == {"src/app.py::main", "src/app.py::helper"}
        assert outgoing[2]["function_id"] == "src/fmt.py::formatter"

        # logger has the most incoming edges (2); utils, helper and formatter have 1 each, main has none
        incoming = stats["most_incoming"]
        assert [row["caller_count"] for row in incoming] == [2, 1, 1, 1]
        assert incoming[0]["function_id"] == "src/log.py::logger"
        assert {row["function_id"] for row in incoming[1:]} == {"src/utils.py::utils", "src/app.py::helper", "src/fmt.py::formatter"}


class TestModuleExports:
    def test_exports_from_db_package(self):
        names = ["DependencyEdge", "DependencyGraph", "DependencyNode", "get_callee_graph", "get_caller_graph", "get_dependency_graph", "get_graph_statistics"]

        for name in names:
            assert name in drspec.db.__all__, name
            assert getattr(drspec.db, name) is getattr(graph_module, name), name  # the package re-exports the module's own object
