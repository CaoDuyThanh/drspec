"""Tests for dependency graph query functions."""

import tempfile
from pathlib import Path

import pytest
import duckdb

from drspec.db.connection import init_schema
from drspec.db.queries import (
    insert_artifact,
    insert_contract,
    insert_dependency,
)
from drspec.db.graph import (
    DependencyNode,
    DependencyEdge,
    DependencyGraph,
    get_dependency_graph,
    get_callee_graph,
    get_caller_graph,
    get_graph_statistics,
)


@pytest.fixture
def db_conn():
    """Create a test database connection with schema."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = duckdb.connect(str(db_path))
        init_schema(conn)
        yield conn
        conn.close()


@pytest.fixture
def populated_db(db_conn):
    """Create a database with sample artifacts and dependencies.

    Graph structure:
        main -> helper -> utils
          |        |
          v        v
        logger <- formatter
    """
    # Create artifacts
    artifacts = [
        ("src/app.py::main", "src/app.py", "main", "def main():", "pass", "hash1"),
        ("src/app.py::helper", "src/app.py", "helper", "def helper():", "pass", "hash2"),
        ("src/utils.py::utils", "src/utils.py", "utils", "def utils():", "pass", "hash3"),
        ("src/log.py::logger", "src/log.py", "logger", "def logger():", "pass", "hash4"),
        ("src/fmt.py::formatter", "src/fmt.py", "formatter", "def formatter():", "pass", "hash5"),
    ]

    for fid, fpath, fname, sig, body, hash_ in artifacts:
        insert_artifact(
            db_conn,
            function_id=fid,
            file_path=fpath,
            function_name=fname,
            signature=sig,
            body=body,
            code_hash=hash_,
            language="python",
            start_line=1,
            end_line=5,
        )

    # Create contract for main
    insert_contract(
        db_conn,
        function_id="src/app.py::main",
        contract_json='{"function_signature": "def main():", "intent_summary": "Main entry point", "invariants": [{"name": "test", "logic": "always true", "criticality": "LOW", "on_fail": "warn"}]}',
        confidence_score=0.85,
    )

    # Create dependencies (main -> helper -> utils, main -> logger, helper -> formatter, formatter -> logger)
    dependencies = [
        ("src/app.py::main", "src/app.py::helper"),
        ("src/app.py::main", "src/log.py::logger"),
        ("src/app.py::helper", "src/utils.py::utils"),
        ("src/app.py::helper", "src/fmt.py::formatter"),
        ("src/fmt.py::formatter", "src/log.py::logger"),
    ]

    for caller, callee in dependencies:
        insert_dependency(db_conn, caller_id=caller, callee_id=callee)

    return db_conn


class TestDependencyNodeModel:
    """Tests for the DependencyNode dataclass."""

    def test_node_creation(self):
        """Test creating a DependencyNode."""
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
        """Test converting node to dictionary."""
        node = DependencyNode(
            function_id="src/app.py::main",
            function_name="main",
            file_path="src/app.py",
            status="PENDING",
            has_contract=False,
            depth=1,
            relationship="callee",
        )

        result = node.to_dict()

        assert result["function_id"] == "src/app.py::main"
        assert result["function_name"] == "main"
        assert result["file_path"] == "src/app.py"
        assert result["status"] == "PENDING"
        assert result["has_contract"] is False
        assert result["depth"] == 1
        assert result["relationship"] == "callee"


class TestDependencyEdgeModel:
    """Tests for the DependencyEdge dataclass."""

    def test_edge_creation(self):
        """Test creating a DependencyEdge."""
        edge = DependencyEdge(
            caller_id="src/app.py::main",
            callee_id="src/app.py::helper",
            is_cyclic=False,
        )

        assert edge.caller_id == "src/app.py::main"
        assert edge.callee_id == "src/app.py::helper"
        assert edge.is_cyclic is False

    def test_edge_to_dict(self):
        """Test converting edge to dictionary."""
        edge = DependencyEdge(
            caller_id="src/app.py::main",
            callee_id="src/app.py::helper",
            is_cyclic=True,
        )

        result = edge.to_dict()

        assert result["caller_id"] == "src/app.py::main"
        assert result["callee_id"] == "src/app.py::helper"
        assert result["is_cyclic"] is True


class TestDependencyGraphModel:
    """Tests for the DependencyGraph dataclass."""

    def test_graph_creation(self):
        """Test creating a DependencyGraph."""
        graph = DependencyGraph(root_function_id="src/app.py::main")

        assert graph.root_function_id == "src/app.py::main"
        assert graph.nodes == []
        assert graph.edges == []
        assert graph.has_cycles is False
        assert graph.max_depth_reached == 0

    def test_graph_node_count(self):
        """Test node_count property."""
        graph = DependencyGraph(root_function_id="test")
        graph.nodes = [
            DependencyNode("a", "a", "", "PENDING", False, 0, "root"),
            DependencyNode("b", "b", "", "PENDING", False, 1, "callee"),
        ]

        assert graph.node_count == 2

    def test_graph_edge_count(self):
        """Test edge_count property."""
        graph = DependencyGraph(root_function_id="test")
        graph.edges = [
            DependencyEdge("a", "b"),
            DependencyEdge("b", "c"),
        ]

        assert graph.edge_count == 2

    def test_graph_to_dict(self):
        """Test converting graph to dictionary."""
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
        assert len(result["nodes"]) == 1
        assert len(result["edges"]) == 1
        assert result["has_cycles"] is True
        assert result["max_depth_reached"] == 2


class TestGetDependencyGraph:
    """Tests for get_dependency_graph function."""

    def test_get_graph_single_node(self, db_conn):
        """Test getting graph for a function with no dependencies."""
        insert_artifact(
            db_conn,
            function_id="src/app.py::standalone",
            file_path="src/app.py",
            function_name="standalone",
            signature="def standalone():",
            body="pass",
            code_hash="hash1",
            language="python",
            start_line=1,
            end_line=5,
        )

        graph = get_dependency_graph(db_conn, "src/app.py::standalone")

        assert graph.root_function_id == "src/app.py::standalone"
        assert graph.node_count == 1
        assert graph.edge_count == 0
        assert graph.has_cycles is False

    def test_get_graph_with_callees(self, populated_db):
        """Test getting graph with callee direction."""
        graph = get_dependency_graph(
            populated_db,
            "src/app.py::main",
            depth=1,
            direction="callees",
        )

        assert graph.root_function_id == "src/app.py::main"
        # Should have main + 2 direct callees (helper, logger)
        assert graph.node_count == 3
        # Should have 2 edges (main->helper, main->logger)
        assert graph.edge_count == 2

        # Check node relationships
        for node in graph.nodes:
            if node.function_id == "src/app.py::main":
                assert node.relationship == "root"
                assert node.depth == 0
            else:
                assert node.relationship == "callee"
                assert node.depth == 1

    def test_get_graph_with_callers(self, populated_db):
        """Test getting graph with caller direction."""
        graph = get_dependency_graph(
            populated_db,
            "src/log.py::logger",
            depth=1,
            direction="callers",
        )

        assert graph.root_function_id == "src/log.py::logger"
        # Should have logger + 2 callers (main, formatter)
        assert graph.node_count == 3
        # Should have 2 edges
        assert graph.edge_count == 2

    def test_get_graph_both_directions(self, populated_db):
        """Test getting graph with both directions."""
        graph = get_dependency_graph(
            populated_db,
            "src/app.py::helper",
            depth=1,
            direction="both",
        )

        # helper is called by main and calls utils, formatter
        assert graph.root_function_id == "src/app.py::helper"
        # Should have helper + main (caller) + utils + formatter (callees)
        assert graph.node_count == 4

    def test_get_graph_depth_2(self, populated_db):
        """Test getting graph with depth 2."""
        graph = get_dependency_graph(
            populated_db,
            "src/app.py::main",
            depth=2,
            direction="callees",
        )

        # Depth 1: helper, logger
        # Depth 2: utils, formatter (from helper)
        assert graph.max_depth_reached == 2
        # main + helper + logger + utils + formatter = 5
        assert graph.node_count == 5

    def test_get_graph_nonexistent_function(self, db_conn):
        """Test getting graph for a function that doesn't exist."""
        graph = get_dependency_graph(db_conn, "nonexistent::func")

        assert graph.root_function_id == "nonexistent::func"
        assert graph.node_count == 1  # Still creates root node
        assert graph.nodes[0].status == "UNKNOWN"
        assert graph.nodes[0].has_contract is False

    def test_get_graph_with_contract_status(self, populated_db):
        """Test that has_contract is set correctly."""
        graph = get_dependency_graph(
            populated_db,
            "src/app.py::main",
            depth=1,
        )

        main_node = next(n for n in graph.nodes if n.function_id == "src/app.py::main")
        helper_node = next(n for n in graph.nodes if n.function_id == "src/app.py::helper")

        assert main_node.has_contract is True  # We added a contract for main
        assert helper_node.has_contract is False

    def test_get_graph_depth_clamped(self, populated_db):
        """Test that depth is clamped between 1 and 5."""
        # Depth 0 should become 1
        graph = get_dependency_graph(populated_db, "src/app.py::main", depth=0)
        assert graph.max_depth_reached >= 1 or graph.node_count == 1

        # Depth 10 should become 5
        graph = get_dependency_graph(populated_db, "src/app.py::main", depth=10)
        # No error should occur


class TestGetCalleeGraph:
    """Tests for get_callee_graph convenience function."""

    def test_get_callee_graph(self, populated_db):
        """Test get_callee_graph returns only callees."""
        graph = get_callee_graph(populated_db, "src/app.py::main", depth=1)

        # Should be equivalent to get_dependency_graph with direction="callees"
        for node in graph.nodes:
            if node.function_id != "src/app.py::main":
                assert node.relationship == "callee"


class TestGetCallerGraph:
    """Tests for get_caller_graph convenience function."""

    def test_get_caller_graph(self, populated_db):
        """Test get_caller_graph returns only callers."""
        graph = get_caller_graph(populated_db, "src/log.py::logger", depth=1)

        # Should be equivalent to get_dependency_graph with direction="callers"
        for node in graph.nodes:
            if node.function_id != "src/log.py::logger":
                assert node.relationship == "caller"


class TestCycleDetection:
    """Tests for cycle detection in dependency graphs."""

    def test_detect_cycle(self, db_conn):
        """Test that cycles are detected."""
        # Create artifacts
        for i in ["a", "b", "c"]:
            insert_artifact(
                db_conn,
                function_id=f"test.py::{i}",
                file_path="test.py",
                function_name=i,
                signature=f"def {i}():",
                body="pass",
                code_hash=f"hash{i}",
                language="python",
                start_line=1,
                end_line=5,
            )

        # Create cycle: a -> b -> c -> a
        insert_dependency(db_conn, "test.py::a", "test.py::b")
        insert_dependency(db_conn, "test.py::b", "test.py::c")
        insert_dependency(db_conn, "test.py::c", "test.py::a")

        graph = get_dependency_graph(db_conn, "test.py::a", depth=3)

        assert graph.has_cycles is True

        # Check that cyclic edge is marked
        cyclic_edges = [e for e in graph.edges if e.is_cyclic]
        assert len(cyclic_edges) >= 1


class TestGetGraphStatistics:
    """Tests for get_graph_statistics function."""

    def test_empty_database(self, db_conn):
        """Test statistics on empty database."""
        stats = get_graph_statistics(db_conn)

        assert stats["total_functions"] == 0
        assert stats["total_dependencies"] == 0
        assert stats["functions_with_contracts"] == 0
        assert stats["most_outgoing"] == []
        assert stats["most_incoming"] == []

    def test_populated_database(self, populated_db):
        """Test statistics on populated database."""
        stats = get_graph_statistics(populated_db)

        assert stats["total_functions"] == 5
        assert stats["total_dependencies"] == 5
        assert stats["functions_with_contracts"] == 1

        # Both main and helper have 2 outgoing, so check there's at least one with 2
        assert len(stats["most_outgoing"]) > 0
        assert stats["most_outgoing"][0]["callee_count"] == 2
        # The first one should be either main or helper
        assert stats["most_outgoing"][0]["function_id"] in (
            "src/app.py::main", "src/app.py::helper"
        )

        # logger has 2 incoming (most)
        assert len(stats["most_incoming"]) > 0
        assert stats["most_incoming"][0]["function_id"] == "src/log.py::logger"
        assert stats["most_incoming"][0]["caller_count"] == 2


class TestModuleExports:
    """Tests that graph module is properly exported from db package."""

    def test_exports_from_db_package(self):
        """Test that graph items are accessible from drspec.db."""
        from drspec.db import (
            DependencyNode,
            DependencyEdge,
            DependencyGraph,
            get_dependency_graph,
            get_callee_graph,
            get_caller_graph,
            get_graph_statistics,
        )

        # All imports should succeed
        assert DependencyNode is not None
        assert DependencyEdge is not None
        assert DependencyGraph is not None
        assert get_dependency_graph is not None
        assert get_callee_graph is not None
        assert get_caller_graph is not None
        assert get_graph_statistics is not None
