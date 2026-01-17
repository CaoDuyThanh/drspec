"""Tests for networkx graph visualization."""

from __future__ import annotations

from pathlib import Path

import pytest

from drspec.db import (
    get_connection,
    init_schema,
    insert_artifact,
    insert_dependency,
)
from drspec.visualization import (
    GraphResult,
    generate_dependency_graph,
    generate_full_graph,
    build_dependency_graph,
    build_full_graph,
)
from drspec.visualization.grapher import STATUS_COLORS


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def db_with_deps(tmp_path):
    """Create a database with artifacts and dependencies."""
    db_path = tmp_path / "_drspec" / "drspec.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = get_connection(str(db_path))
    init_schema(conn)

    # Create a simple dependency tree:
    # main -> process -> validate
    #      -> transform
    artifacts = [
        ("src/app.py::main", "src/app.py", "main", "PENDING"),
        ("src/app.py::process", "src/app.py", "process", "VERIFIED"),
        ("src/app.py::validate", "src/app.py", "validate", "VERIFIED"),
        ("src/app.py::transform", "src/app.py", "transform", "NEEDS_REVIEW"),
    ]

    for func_id, file_path, name, status in artifacts:
        insert_artifact(
            conn,
            function_id=func_id,
            file_path=file_path,
            function_name=name,
            signature=f"def {name}()",
            body=f"def {name}(): pass",
            code_hash=f"hash_{name}",
            language="python",
            start_line=1,
            end_line=2,
            status=status,
        )

    # Add dependencies
    insert_dependency(conn, "src/app.py::main", "src/app.py::process")
    insert_dependency(conn, "src/app.py::main", "src/app.py::transform")
    insert_dependency(conn, "src/app.py::process", "src/app.py::validate")

    return conn


@pytest.fixture
def db_with_large_graph(tmp_path):
    """Create a database with many functions for testing limits."""
    db_path = tmp_path / "_drspec" / "drspec.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = get_connection(str(db_path))
    init_schema(conn)

    # Create 20 functions
    for i in range(20):
        insert_artifact(
            conn,
            function_id=f"src/module.py::func_{i}",
            file_path="src/module.py",
            function_name=f"func_{i}",
            signature=f"def func_{i}()",
            body=f"def func_{i}(): pass",
            code_hash=f"hash_{i}",
            language="python",
            start_line=i * 2 + 1,
            end_line=i * 2 + 2,
            status="PENDING",
        )

    # Create chain dependencies: func_0 -> func_1 -> func_2 -> ...
    for i in range(19):
        insert_dependency(conn, f"src/module.py::func_{i}", f"src/module.py::func_{i+1}")

    return conn


@pytest.fixture
def empty_db(tmp_path):
    """Create an empty database."""
    db_path = tmp_path / "_drspec" / "drspec.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = get_connection(str(db_path))
    init_schema(conn)

    return conn


@pytest.fixture
def output_dir(tmp_path):
    """Create a temporary output directory."""
    plots_dir = tmp_path / "plots"
    plots_dir.mkdir()
    return str(plots_dir)


# =============================================================================
# Build Graph Tests
# =============================================================================


class TestBuildDependencyGraph:
    """Tests for build_dependency_graph function."""

    def test_builds_graph_from_center(self, db_with_deps):
        """Should build graph centered on a function."""
        G = build_dependency_graph(db_with_deps, "src/app.py::main", depth=2)

        assert len(G.nodes()) == 4
        assert "src/app.py::main" in G.nodes()
        assert "src/app.py::process" in G.nodes()
        assert "src/app.py::validate" in G.nodes()
        assert "src/app.py::transform" in G.nodes()

    def test_center_node_marked(self, db_with_deps):
        """Should mark center node."""
        G = build_dependency_graph(db_with_deps, "src/app.py::main", depth=2)

        assert G.nodes["src/app.py::main"]["is_center"] is True
        assert G.nodes["src/app.py::process"]["is_center"] is False

    def test_nodes_have_status(self, db_with_deps):
        """Should include status in node attributes."""
        G = build_dependency_graph(db_with_deps, "src/app.py::main", depth=2)

        assert G.nodes["src/app.py::main"]["status"] == "PENDING"
        assert G.nodes["src/app.py::process"]["status"] == "VERIFIED"
        assert G.nodes["src/app.py::transform"]["status"] == "NEEDS_REVIEW"

    def test_nodes_have_name(self, db_with_deps):
        """Should include function name in node attributes."""
        G = build_dependency_graph(db_with_deps, "src/app.py::main", depth=2)

        assert G.nodes["src/app.py::main"]["name"] == "main"
        assert G.nodes["src/app.py::process"]["name"] == "process"

    def test_edges_correct(self, db_with_deps):
        """Should have correct edges."""
        G = build_dependency_graph(db_with_deps, "src/app.py::main", depth=2)

        assert G.has_edge("src/app.py::main", "src/app.py::process")
        assert G.has_edge("src/app.py::main", "src/app.py::transform")
        assert G.has_edge("src/app.py::process", "src/app.py::validate")

    def test_depth_limiting(self, db_with_deps):
        """Should respect depth limit."""
        G = build_dependency_graph(db_with_deps, "src/app.py::main", depth=1)

        # At depth 1, should get main and direct children only
        assert "src/app.py::main" in G.nodes()
        assert "src/app.py::process" in G.nodes()
        assert "src/app.py::transform" in G.nodes()
        # validate is at depth 2
        assert "src/app.py::validate" not in G.nodes()

    def test_direction_callees_only(self, db_with_deps):
        """Should only follow callees when direction=callees."""
        G = build_dependency_graph(
            db_with_deps, "src/app.py::process", depth=2, direction="callees"
        )

        # Should have process and its callee validate
        assert "src/app.py::process" in G.nodes()
        assert "src/app.py::validate" in G.nodes()
        # Should not have main (caller)
        assert "src/app.py::main" not in G.nodes()

    def test_direction_callers_only(self, db_with_deps):
        """Should only follow callers when direction=callers."""
        G = build_dependency_graph(
            db_with_deps, "src/app.py::validate", depth=2, direction="callers"
        )

        # Should have validate and its callers
        assert "src/app.py::validate" in G.nodes()
        assert "src/app.py::process" in G.nodes()
        # Should not have transform (sibling)
        assert "src/app.py::transform" not in G.nodes()

    def test_nonexistent_function(self, empty_db):
        """Should return empty graph for nonexistent function."""
        G = build_dependency_graph(empty_db, "nonexistent::func", depth=2)

        assert len(G.nodes()) == 0
        assert len(G.edges()) == 0


class TestBuildFullGraph:
    """Tests for build_full_graph function."""

    def test_builds_all_functions(self, db_with_deps):
        """Should build graph with all functions."""
        G = build_full_graph(db_with_deps)

        assert len(G.nodes()) == 4

    def test_filters_by_path_prefix(self, db_with_deps, tmp_path):
        """Should filter by path prefix."""
        # Add function in different path
        insert_artifact(
            db_with_deps,
            function_id="lib/util.py::helper",
            file_path="lib/util.py",
            function_name="helper",
            signature="def helper()",
            body="def helper(): pass",
            code_hash="hash_helper",
            language="python",
            start_line=1,
            end_line=2,
        )

        G = build_full_graph(db_with_deps, path_prefix="src/")

        # Should only have src/ functions
        assert len(G.nodes()) == 4
        assert "lib/util.py::helper" not in G.nodes()

    def test_respects_max_nodes(self, db_with_large_graph):
        """Should respect max_nodes limit."""
        G = build_full_graph(db_with_large_graph, max_nodes=5)

        assert len(G.nodes()) == 5


# =============================================================================
# Generate Graph Tests
# =============================================================================


class TestGenerateDependencyGraph:
    """Tests for generate_dependency_graph function."""

    def test_generates_graph_image(self, db_with_deps, output_dir):
        """Should generate a graph image."""
        result = generate_dependency_graph(
            db_with_deps,
            "src/app.py::main",
            output_dir=output_dir,
        )

        assert isinstance(result, GraphResult)
        assert result.plot_type == "graph"
        assert Path(result.path).exists()

    def test_result_has_correct_counts(self, db_with_deps, output_dir):
        """Should return correct node and edge counts."""
        result = generate_dependency_graph(
            db_with_deps,
            "src/app.py::main",
            depth=2,
            output_dir=output_dir,
        )

        assert result.nodes == 4
        assert result.edges == 3
        assert result.data_points == 4

    def test_result_has_center_function(self, db_with_deps, output_dir):
        """Should include center function in result."""
        result = generate_dependency_graph(
            db_with_deps,
            "src/app.py::main",
            output_dir=output_dir,
        )

        assert result.center_function == "src/app.py::main"

    def test_result_has_dimensions(self, db_with_deps, output_dir):
        """Should include dimensions in result."""
        result = generate_dependency_graph(
            db_with_deps,
            "src/app.py::main",
            output_dir=output_dir,
        )

        assert result.width == 1200  # 12 * 100 DPI
        assert result.height == 800  # 8 * 100 DPI

    def test_handles_empty_graph(self, empty_db, output_dir):
        """Should handle nonexistent function gracefully."""
        result = generate_dependency_graph(
            empty_db,
            "nonexistent::func",
            output_dir=output_dir,
        )

        assert result.nodes == 0
        assert result.edges == 0
        assert Path(result.path).exists()

    def test_deterministic_filename(self, db_with_deps, output_dir):
        """Should generate deterministic filenames."""
        result1 = generate_dependency_graph(
            db_with_deps,
            "src/app.py::main",
            output_dir=output_dir,
        )
        result2 = generate_dependency_graph(
            db_with_deps,
            "src/app.py::main",
            output_dir=output_dir,
        )

        assert Path(result1.path).name == Path(result2.path).name


class TestGenerateFullGraph:
    """Tests for generate_full_graph function."""

    def test_generates_full_graph_image(self, db_with_deps, output_dir):
        """Should generate a full graph image."""
        result = generate_full_graph(
            db_with_deps,
            output_dir=output_dir,
        )

        assert isinstance(result, GraphResult)
        assert result.plot_type == "graph"
        assert Path(result.path).exists()

    def test_filters_by_path(self, db_with_deps, output_dir):
        """Should filter by path prefix."""
        result = generate_full_graph(
            db_with_deps,
            path_prefix="src/app",
            output_dir=output_dir,
        )

        assert result.nodes == 4

    def test_center_function_is_none(self, db_with_deps, output_dir):
        """Full graph should have no center function."""
        result = generate_full_graph(
            db_with_deps,
            output_dir=output_dir,
        )

        assert result.center_function is None


# =============================================================================
# Status Colors Tests
# =============================================================================


class TestStatusColors:
    """Tests for status color mapping."""

    def test_all_statuses_have_colors(self):
        """Should have colors for all statuses."""
        expected_statuses = ["VERIFIED", "NEEDS_REVIEW", "PENDING", "STALE", "BROKEN"]
        for status in expected_statuses:
            assert status in STATUS_COLORS
            assert STATUS_COLORS[status].startswith("#")

    def test_colors_are_valid_hex(self):
        """Colors should be valid hex codes."""
        for status, color in STATUS_COLORS.items():
            assert color.startswith("#")
            assert len(color) == 7
            # Should be valid hex
            int(color[1:], 16)


# =============================================================================
# Integration Tests
# =============================================================================


class TestGraphIntegration:
    """Integration tests for graph visualization."""

    def test_graph_with_all_statuses(self, tmp_path, output_dir):
        """Should handle all status types correctly."""
        db_path = tmp_path / "_drspec" / "drspec.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)

        conn = get_connection(str(db_path))
        init_schema(conn)

        # Create functions with all statuses
        statuses = ["PENDING", "VERIFIED", "NEEDS_REVIEW", "STALE", "BROKEN"]
        for i, status in enumerate(statuses):
            insert_artifact(
                conn,
                function_id=f"src/test.py::func_{i}",
                file_path="src/test.py",
                function_name=f"func_{i}",
                signature=f"def func_{i}()",
                body=f"def func_{i}(): pass",
                code_hash=f"hash_{i}",
                language="python",
                start_line=i * 2 + 1,
                end_line=i * 2 + 2,
                status=status,
            )

        result = generate_full_graph(conn, output_dir=output_dir)

        assert result.nodes == 5
        assert Path(result.path).exists()

    def test_deep_dependency_chain(self, db_with_large_graph, output_dir):
        """Should handle deep dependency chains."""
        result = generate_dependency_graph(
            db_with_large_graph,
            "src/module.py::func_0",
            depth=5,
            output_dir=output_dir,
        )

        # Should traverse 5 levels deep
        assert result.nodes >= 6
        assert Path(result.path).exists()

    def test_creates_output_directory(self, db_with_deps, tmp_path):
        """Should create output directory if it doesn't exist."""
        output_dir = str(tmp_path / "new" / "nested" / "dir")

        result = generate_dependency_graph(
            db_with_deps,
            "src/app.py::main",
            output_dir=output_dir,
        )

        assert Path(result.path).exists()
        assert Path(output_dir).is_dir()
