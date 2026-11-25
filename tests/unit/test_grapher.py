"""Tests for networkx graph visualization: the graph itself and its HTML page. The databases are in memory. No test here draws a figure (the
matplotlib drawing is not tested)."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from drspec.db import (
    init_schema,
    insert_artifact,
)
from drspec.visualization import (
    GraphResult,
    build_dependency_graph,
    build_full_graph,
    generate_dependency_graph_html,
    render_graph_html,
    render_live_graph_html,
)
from drspec.visualization.grapher import STATUS_COLORS, _scale_positions, layout_graph
from tests.helpers import insert_dependency

# =============================================================================
# Fixtures
# =============================================================================


def _memory_database():
    conn = duckdb.connect(":memory:")  # only the graph tables are read: no file
    init_schema(conn)
    return conn


@pytest.fixture
def db_with_deps():
    conn = _memory_database()

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

    insert_dependency(conn, "src/app.py::main", "src/app.py::process")
    insert_dependency(conn, "src/app.py::main", "src/app.py::transform")
    insert_dependency(conn, "src/app.py::process", "src/app.py::validate")

    yield conn
    conn.close()


@pytest.fixture
def db_with_large_graph():
    conn = _memory_database()

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

    for i in range(19):
        insert_dependency(conn, f"src/module.py::func_{i}", f"src/module.py::func_{i+1}")

    yield conn
    conn.close()


@pytest.fixture
def empty_db():
    conn = _memory_database()
    yield conn
    conn.close()


@pytest.fixture
def output_dir(tmp_path):
    plots_dir = tmp_path / "plots"
    plots_dir.mkdir()
    return str(plots_dir)


# =============================================================================
# Build Graph Tests
# =============================================================================


@pytest.mark.unit
class TestBuildDependencyGraph:
    """Tests for build_dependency_graph function."""

    def test_builds_graph_from_center(self, db_with_deps):
        G = build_dependency_graph(db_with_deps, "src/app.py::main", depth=2)

        assert len(G.nodes()) == 4
        assert "src/app.py::main" in G.nodes()
        assert "src/app.py::process" in G.nodes()
        assert "src/app.py::validate" in G.nodes()
        assert "src/app.py::transform" in G.nodes()

    def test_center_node_marked(self, db_with_deps):
        G = build_dependency_graph(db_with_deps, "src/app.py::main", depth=2)

        assert G.nodes["src/app.py::main"]["is_center"] is True
        assert G.nodes["src/app.py::process"]["is_center"] is False

    def test_nodes_have_status(self, db_with_deps):
        G = build_dependency_graph(db_with_deps, "src/app.py::main", depth=2)

        assert G.nodes["src/app.py::main"]["status"] == "PENDING"
        assert G.nodes["src/app.py::process"]["status"] == "VERIFIED"
        assert G.nodes["src/app.py::transform"]["status"] == "NEEDS_REVIEW"

    def test_nodes_have_name(self, db_with_deps):
        G = build_dependency_graph(db_with_deps, "src/app.py::main", depth=2)

        assert G.nodes["src/app.py::main"]["name"] == "main"
        assert G.nodes["src/app.py::process"]["name"] == "process"

    def test_edges_correct(self, db_with_deps):
        G = build_dependency_graph(db_with_deps, "src/app.py::main", depth=2)

        assert G.has_edge("src/app.py::main", "src/app.py::process")
        assert G.has_edge("src/app.py::main", "src/app.py::transform")
        assert G.has_edge("src/app.py::process", "src/app.py::validate")

    def test_depth_limiting(self, db_with_deps):
        G = build_dependency_graph(db_with_deps, "src/app.py::main", depth=1)

        assert "src/app.py::main" in G.nodes()
        assert "src/app.py::process" in G.nodes()
        assert "src/app.py::transform" in G.nodes()
        # validate is two hops away (depth 2)
        assert "src/app.py::validate" not in G.nodes()

    def test_direction_callees_only(self, db_with_deps):
        G = build_dependency_graph(
            db_with_deps, "src/app.py::process", depth=2, direction="callees"
        )

        assert "src/app.py::process" in G.nodes()
        assert "src/app.py::validate" in G.nodes()
        # main is a caller, not a callee, of process
        assert "src/app.py::main" not in G.nodes()

    def test_direction_callers_only(self, db_with_deps):
        G = build_dependency_graph(
            db_with_deps, "src/app.py::validate", depth=2, direction="callers"
        )

        assert "src/app.py::validate" in G.nodes()
        assert "src/app.py::process" in G.nodes()
        # transform is a sibling, not a caller, of validate
        assert "src/app.py::transform" not in G.nodes()

    def test_nonexistent_function(self, empty_db):
        G = build_dependency_graph(empty_db, "nonexistent::func", depth=2)

        assert len(G.nodes()) == 0
        assert len(G.edges()) == 0


@pytest.mark.unit
class TestBuildFullGraph:
    """Tests for build_full_graph function."""

    def test_builds_all_functions(self, db_with_deps):
        G = build_full_graph(db_with_deps)

        assert len(G.nodes()) == 4

    def test_filters_by_path_prefix(self, db_with_deps, tmp_path):
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

        assert len(G.nodes()) == 4
        assert "lib/util.py::helper" not in G.nodes()

    def test_respects_max_nodes(self, db_with_large_graph):
        G = build_full_graph(db_with_large_graph, max_nodes=5)

        assert len(G.nodes()) == 5


# =============================================================================
# Generate Graph Tests
# =============================================================================


@pytest.mark.unit
class TestGenerateDependencyGraphHtml:
    """Tests for generate_dependency_graph_html."""

    def test_generates_html_file(self, db_with_deps, output_dir):
        result = generate_dependency_graph_html(
            db_with_deps,
            "src/app.py::main",
            output_dir=output_dir,
        )

        assert isinstance(result, GraphResult)
        assert result.plot_type == "graph_html"
        assert Path(result.path).exists()
        assert Path(result.path).suffix == ".html"

    def test_output_file_is_used_verbatim_not_replaced_by_a_hashed_name(self, db_with_deps, tmp_path):
        target = tmp_path / "custom" / "my-graph.html"

        result = generate_dependency_graph_html(db_with_deps, "src/app.py::main", output_file=str(target))

        assert result.path == str(target.resolve())
        assert target.read_text(encoding="utf-8").lstrip().startswith("<")
        assert not list(target.parent.glob("graph_*.html"))

    def test_output_file_without_extension_gets_html(self, db_with_deps, tmp_path):
        result = generate_dependency_graph_html(db_with_deps, "src/app.py::main", output_file=str(tmp_path / "plain"))

        assert result.path == str((tmp_path / "plain.html").resolve())
        assert Path(result.path).exists()

    def test_result_has_correct_counts(self, db_with_deps, output_dir):
        result = generate_dependency_graph_html(
            db_with_deps,
            "src/app.py::main",
            depth=2,
            output_dir=output_dir,
        )

        assert result.nodes == 4
        assert result.edges == 3
        assert result.center_function == "src/app.py::main"


    def test_handles_empty_graph(self, empty_db, output_dir):
        result = generate_dependency_graph_html(
            empty_db,
            "nonexistent::func",
            output_dir=output_dir,
        )

        assert result.nodes == 0
        assert result.edges == 0
        assert Path(result.path).exists()
        assert "<html" in Path(result.path).read_text()

    def test_html_is_fully_self_contained_no_network_dependency(self, db_with_deps, output_dir):
        """No CDN, no network call. The `http://www.w3.org/2000/svg`
        namespace URI is a required XML identifier, not a fetched
        resource, so it doesn't trip these checks."""
        result = generate_dependency_graph_html(db_with_deps, "src/app.py::main", output_dir=output_dir)
        content = Path(result.path).read_text()

        assert "<script src=" not in content
        assert "<link " not in content
        assert "fetch(" not in content
        assert "cdn." not in content.lower()

    def test_html_embeds_real_node_and_edge_data(self, db_with_deps, output_dir):
        result = generate_dependency_graph_html(db_with_deps, "src/app.py::main", output_dir=output_dir)
        content = Path(result.path).read_text()

        assert "src/app.py::main" in content
        assert "src/app.py::process" in content
        assert '"relation": "calls"' in content


@pytest.mark.unit
class TestRenderGraphHtml:
    """Tests for render_graph_html directly."""

    def test_renders_relation_and_confidence_on_edges(self):
        import networkx as nx

        G = nx.DiGraph()
        G.add_node("a", status="PENDING", name="a", signature="def a():", file_path="x.py", is_center=True)
        G.add_node("b", status="VERIFIED", name="b", signature="def b():", file_path="x.py", is_center=False)
        G.add_edge("a", "b", relation="references", confidence="AMBIGUOUS", text="a.b")
        pos = layout_graph(G)

        doc = render_graph_html(G, pos, title="Test")

        assert "<!DOCTYPE html>" in doc
        assert '"relation": "references"' in doc
        assert '"confidence": "AMBIGUOUS"' in doc
        assert "a.b" in doc

    def test_escapes_script_terminator_in_embedded_data(self):
        """A `</script>`-looking substring inside a signature/call-site
        must not terminate the embedded JSON script block early."""
        import networkx as nx

        G = nx.DiGraph()
        G.add_node(
            "a",
            status="PENDING",
            name="a",
            signature="def a():  # </script><script>alert(1)</script>",
            file_path="x.py",
            is_center=True,
        )
        pos = layout_graph(G)

        doc = render_graph_html(G, pos, title="Test")

        assert "</script><script>alert(1)" not in doc


@pytest.mark.unit
class TestRenderLiveGraphHtml:
    """Tests for render_live_graph_html directly."""

    def test_renders_a_complete_document_with_initial_state(self):
        doc = render_live_graph_html("a.py::foo", depth=3, direction="callers")

        assert "<!DOCTYPE html>" in doc
        assert "</html>" in doc
        assert "a.py::foo" in doc
        assert '"depth": 3' in doc
        assert '"direction": "callers"' in doc

    def test_escapes_script_terminator_in_function_id(self):
        """`function_id` is embedded into `initial_state` without going
        through `_embed_json_script`'s guard; a `</script>`-containing id
        must not terminate the page's own script block."""
        evil = "a.py::foo</script><script>alert(1)</script>"

        doc = render_live_graph_html(evil, depth=2, direction="both")

        assert "</script><script>alert(1)</script>" not in doc
        # `\/` is a no-op JS escape for `/`, so this still reconstructs to the original string
        assert "<\\/script><script>alert(1)<\\/script>" in doc

    def test_escapes_script_terminator_regardless_of_position_in_function_id(self):
        """Not just a prefix/suffix case — a `</script>` in the middle of
        an otherwise-normal-looking function_id must be caught too."""
        evil = "a.py::foo</script><img src=x onerror=alert(1)>bar"

        doc = render_live_graph_html(evil, depth=1, direction="both")

        assert "</script><img" not in doc

    def test_empty_graph_still_produces_valid_document(self):
        import networkx as nx

        G = nx.DiGraph()
        doc = render_graph_html(G, {}, title="Empty")

        assert "<!DOCTYPE html>" in doc
        assert "</html>" in doc


@pytest.mark.unit
class TestScalePositions:
    """Tests for _scale_positions — layout padding must reserve room for
    the fixed toolbar/legend overlays."""

    def test_scales_into_requested_bounds(self):
        pos = {"a": (-1.0, -1.0), "b": (1.0, 1.0)}
        scaled = _scale_positions(pos, width=1000, height=800, padding_top=50, padding_right=50, padding_bottom=50, padding_left=50)

        assert scaled == {"a": (50.0, 50.0), "b": (950.0, 750.0)}  # the extremes land on the padding, the rest of the room is the width and height

    def test_asymmetric_padding_keeps_bottom_left_clear(self):
        """A node at the extreme bottom-left was hidden behind the legend
        overlay under uniform padding; larger bottom/left padding must be
        respected, not reduced to match the smaller sides."""
        pos = {"corner": (-1.0, -1.0), "other": (1.0, 1.0)}
        scaled = _scale_positions(
            pos, width=1400, height=900, padding_top=90, padding_right=90, padding_bottom=190, padding_left=190
        )

        x, y = scaled["corner"]
        assert x >= 190
        assert y >= 90

    def test_single_point_does_not_divide_by_zero(self):
        pos = {"a": (0.5, 0.5)}
        scaled = _scale_positions(pos, width=1000, height=800)
        assert scaled == {"a": (70.0, 70.0)}  # the default padding; the span of one point counts as 1

    def test_empty_positions(self):
        assert _scale_positions({}, width=1000, height=800) == {}


# =============================================================================
# Status Colors Tests
# =============================================================================


@pytest.mark.unit
class TestStatusColors:
    """Tests for status color mapping."""

    def test_all_statuses_have_colors(self):
        expected_statuses = ["VERIFIED", "NEEDS_REVIEW", "PENDING", "STALE", "BROKEN"]
        for status in expected_statuses:
            assert status in STATUS_COLORS
            assert STATUS_COLORS[status].startswith("#")

    def test_colors_are_valid_hex(self):
        for status, color in STATUS_COLORS.items():
            assert color.startswith("#")
            assert len(color) == 7
            int(color[1:], 16)
