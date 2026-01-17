"""networkx graph visualization for DrSpec dependency analysis.

Generates deterministic dependency graphs for function relationships,
colored by contract status.
"""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

import duckdb
import matplotlib
import matplotlib.pyplot as plt
import networkx as nx

from drspec.db import get_artifact, get_callers, get_callees
from drspec.visualization.plotter import PlotResult

# Use non-interactive backend for headless operation
matplotlib.use("Agg")


# =============================================================================
# Constants
# =============================================================================

DEFAULT_OUTPUT_DIR = "_drspec/plots"
DEFAULT_DPI = 100
DEFAULT_FIGSIZE = (12, 8)
DEFAULT_MAX_NODES = 100
DEFAULT_DEPTH = 2

# Color scheme by contract status
STATUS_COLORS = {
    "VERIFIED": "#2ecc71",      # Green
    "NEEDS_REVIEW": "#f1c40f",  # Yellow
    "PENDING": "#95a5a6",       # Gray
    "STALE": "#e67e22",         # Orange
    "BROKEN": "#e74c3c",        # Red
}

# Default color for unknown status
DEFAULT_COLOR = "#95a5a6"  # Gray


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class GraphResult(PlotResult):
    """Result of graph generation with additional metadata.

    Attributes:
        path: Absolute path to saved graph image.
        plot_type: Always 'graph' for graph visualizations.
        width: Pixel width of the graph.
        height: Pixel height of the graph.
        data_points: Number of nodes in the graph.
        nodes: Number of nodes in the graph.
        edges: Number of edges in the graph.
        center_function: The function ID the graph is centered on.
    """

    nodes: int = 0
    edges: int = 0
    center_function: Optional[str] = None


# =============================================================================
# Graph Building
# =============================================================================


def build_dependency_graph(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
    depth: int = DEFAULT_DEPTH,
    direction: Literal["callers", "callees", "both"] = "both",
) -> nx.DiGraph:
    """Build networkx graph from database dependencies.

    Args:
        conn: DuckDB connection.
        function_id: Center function ID.
        depth: Maximum depth to traverse.
        direction: Which direction to follow relationships.

    Returns:
        networkx DiGraph with nodes containing status and name attributes.
    """
    G = nx.DiGraph()

    # Get root function
    root = get_artifact(conn, function_id)
    if root is None:
        return G

    # Add root node
    G.add_node(
        function_id,
        status=root.status,
        name=root.function_name,
        is_center=True,
    )

    # BFS to add connected nodes
    visited = {function_id}
    queue: deque[tuple[str, int]] = deque([(function_id, 0)])

    while queue:
        current, current_depth = queue.popleft()
        if current_depth >= depth:
            continue

        # Add callees (functions this function calls)
        if direction in ("callees", "both"):
            for callee in get_callees(conn, current):
                if callee not in visited:
                    artifact = get_artifact(conn, callee)
                    if artifact:
                        G.add_node(
                            callee,
                            status=artifact.status,
                            name=artifact.function_name,
                            is_center=False,
                        )
                        visited.add(callee)
                        queue.append((callee, current_depth + 1))
                G.add_edge(current, callee)

        # Add callers (functions that call this function)
        if direction in ("callers", "both"):
            for caller in get_callers(conn, current):
                if caller not in visited:
                    artifact = get_artifact(conn, caller)
                    if artifact:
                        G.add_node(
                            caller,
                            status=artifact.status,
                            name=artifact.function_name,
                            is_center=False,
                        )
                        visited.add(caller)
                        queue.append((caller, current_depth + 1))
                G.add_edge(caller, current)

    return G


def build_full_graph(
    conn: duckdb.DuckDBPyConnection,
    path_prefix: str = "",
    max_nodes: int = DEFAULT_MAX_NODES,
) -> nx.DiGraph:
    """Build graph of all functions matching path prefix.

    Args:
        conn: DuckDB connection.
        path_prefix: Optional file path prefix filter.
        max_nodes: Maximum number of nodes to include.

    Returns:
        networkx DiGraph with all matching functions.
    """
    from drspec.db import list_artifacts

    G = nx.DiGraph()

    # Get all artifacts matching prefix
    artifacts = list_artifacts(conn, file_path=path_prefix, limit=max_nodes)

    # Add nodes
    for artifact in artifacts:
        G.add_node(
            artifact.function_id,
            status=artifact.status,
            name=artifact.function_name,
            is_center=False,
        )

    # Get function IDs in graph
    node_ids = set(G.nodes())

    # Add edges only between nodes in the graph
    for artifact in artifacts:
        for callee in get_callees(conn, artifact.function_id):
            if callee in node_ids:
                G.add_edge(artifact.function_id, callee)

    return G


# =============================================================================
# Graph Layout
# =============================================================================


def layout_graph(G: nx.DiGraph) -> dict[str, tuple[float, float]]:
    """Generate deterministic layout for graph.

    Args:
        G: networkx DiGraph to layout.

    Returns:
        Dictionary mapping node IDs to (x, y) positions.
    """
    if len(G.nodes()) == 0:
        return {}

    if len(G.nodes()) == 1:
        # Single node - center it
        node = list(G.nodes())[0]
        return {node: (0.5, 0.5)}

    # Use spring layout with fixed seed for reproducibility
    pos = nx.spring_layout(G, seed=42, k=2.0, iterations=50)
    return pos


# =============================================================================
# Graph Rendering
# =============================================================================


def render_graph(
    G: nx.DiGraph,
    pos: dict[str, tuple[float, float]],
    title: str = "",
) -> plt.Figure:
    """Render graph to matplotlib figure.

    Args:
        G: networkx DiGraph to render.
        pos: Node positions from layout_graph.
        title: Optional title for the graph.

    Returns:
        matplotlib Figure.
    """
    fig, ax = plt.subplots(figsize=DEFAULT_FIGSIZE)

    if len(G.nodes()) == 0:
        ax.text(0.5, 0.5, "No dependencies found", ha="center", va="center", fontsize=14)
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.axis("off")
        if title:
            ax.set_title(title, fontsize=14, fontweight="bold")
        return fig

    # Get node colors based on status
    node_colors = []
    for node in G.nodes():
        status = G.nodes[node].get("status", "PENDING")
        node_colors.append(STATUS_COLORS.get(status, DEFAULT_COLOR))

    # Get node sizes (larger for center node)
    node_sizes = []
    for node in G.nodes():
        is_center = G.nodes[node].get("is_center", False)
        node_sizes.append(800 if is_center else 400)

    # Draw edges
    nx.draw_networkx_edges(
        G,
        pos,
        ax=ax,
        edge_color="#cccccc",
        arrows=True,
        arrowsize=15,
        arrowstyle="-|>",
        connectionstyle="arc3,rad=0.1",
    )

    # Draw nodes
    nx.draw_networkx_nodes(
        G,
        pos,
        ax=ax,
        node_color=node_colors,
        node_size=node_sizes,
        alpha=0.9,
    )

    # Draw labels (truncated function names)
    labels = {}
    for node in G.nodes():
        name = G.nodes[node].get("name", node.split("::")[-1])
        # Truncate long names
        if len(name) > 15:
            name = name[:12] + "..."
        labels[node] = name

    nx.draw_networkx_labels(
        G,
        pos,
        labels,
        ax=ax,
        font_size=8,
        font_weight="bold",
    )

    # Add title
    if title:
        ax.set_title(title, fontsize=14, fontweight="bold", pad=20)

    # Add legend
    legend_elements = [
        plt.scatter([], [], c=STATUS_COLORS["VERIFIED"], s=100, label="VERIFIED"),
        plt.scatter([], [], c=STATUS_COLORS["NEEDS_REVIEW"], s=100, label="NEEDS_REVIEW"),
        plt.scatter([], [], c=STATUS_COLORS["PENDING"], s=100, label="PENDING"),
        plt.scatter([], [], c=STATUS_COLORS["STALE"], s=100, label="STALE"),
        plt.scatter([], [], c=STATUS_COLORS["BROKEN"], s=100, label="BROKEN"),
    ]
    ax.legend(
        handles=legend_elements,
        loc="upper left",
        fontsize=8,
        framealpha=0.9,
    )

    # Add stats
    stats_text = f"Nodes: {len(G.nodes())} | Edges: {len(G.edges())}"
    ax.text(
        0.99,
        0.01,
        stats_text,
        transform=ax.transAxes,
        ha="right",
        va="bottom",
        fontsize=9,
        bbox=dict(boxstyle="round", facecolor="white", alpha=0.8),
    )

    ax.axis("off")
    plt.tight_layout()

    return fig


# =============================================================================
# Main API
# =============================================================================


def generate_dependency_graph(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
    depth: int = DEFAULT_DEPTH,
    direction: Literal["callers", "callees", "both"] = "both",
    output_dir: str = DEFAULT_OUTPUT_DIR,
) -> GraphResult:
    """Generate dependency graph centered on a function.

    Args:
        conn: DuckDB connection.
        function_id: Center function ID.
        depth: Maximum depth to traverse (default 2).
        direction: Which direction to follow relationships.
        output_dir: Directory to save graph image.

    Returns:
        GraphResult with path and metadata.
    """
    # Build graph
    G = build_dependency_graph(conn, function_id, depth, direction)

    # Generate layout
    pos = layout_graph(G)

    # Get function name for title
    artifact = get_artifact(conn, function_id)
    title = f"Dependencies: {artifact.function_name if artifact else function_id}"

    # Render graph
    fig = render_graph(G, pos, title)

    # Save graph
    output_path = _save_graph(fig, function_id, depth, direction, output_dir)

    plt.close(fig)

    return GraphResult(
        path=output_path,
        plot_type="graph",
        width=int(DEFAULT_FIGSIZE[0] * DEFAULT_DPI),
        height=int(DEFAULT_FIGSIZE[1] * DEFAULT_DPI),
        data_points=len(G.nodes()),
        nodes=len(G.nodes()),
        edges=len(G.edges()),
        center_function=function_id,
    )


def generate_full_graph(
    conn: duckdb.DuckDBPyConnection,
    path_prefix: str = "",
    max_nodes: int = DEFAULT_MAX_NODES,
    output_dir: str = DEFAULT_OUTPUT_DIR,
) -> GraphResult:
    """Generate graph of all functions matching path prefix.

    Args:
        conn: DuckDB connection.
        path_prefix: Optional file path prefix filter.
        max_nodes: Maximum number of nodes.
        output_dir: Directory to save graph image.

    Returns:
        GraphResult with path and metadata.
    """
    # Build graph
    G = build_full_graph(conn, path_prefix, max_nodes)

    # Generate layout
    pos = layout_graph(G)

    # Generate title
    title = f"Function Graph: {path_prefix or 'All Functions'}"
    if len(G.nodes()) >= max_nodes:
        title += f" (limited to {max_nodes})"

    # Render graph
    fig = render_graph(G, pos, title)

    # Save graph
    output_path = _save_full_graph(fig, path_prefix, max_nodes, output_dir)

    plt.close(fig)

    return GraphResult(
        path=output_path,
        plot_type="graph",
        width=int(DEFAULT_FIGSIZE[0] * DEFAULT_DPI),
        height=int(DEFAULT_FIGSIZE[1] * DEFAULT_DPI),
        data_points=len(G.nodes()),
        nodes=len(G.nodes()),
        edges=len(G.edges()),
        center_function=None,
    )


# =============================================================================
# Helper Functions
# =============================================================================


def _generate_graph_filename(identifier: str) -> str:
    """Generate deterministic filename from graph identifier.

    Args:
        identifier: Unique identifier for the graph.

    Returns:
        Filename in format graph_{hash}.png
    """
    hash_val = hashlib.sha256(identifier.encode()).hexdigest()[:12]
    return f"graph_{hash_val}.png"


def _save_graph(
    fig: plt.Figure,
    function_id: str,
    depth: int,
    direction: str,
    output_dir: str,
) -> str:
    """Save dependency graph to disk.

    Args:
        fig: Matplotlib figure to save.
        function_id: Center function ID.
        depth: Depth used.
        direction: Direction used.
        output_dir: Directory to save.

    Returns:
        Absolute path to saved file.
    """
    # Create output directory if needed
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # Generate deterministic filename
    identifier = f"{function_id}:{depth}:{direction}"
    filename = _generate_graph_filename(identifier)
    filepath = output_path / filename

    # Save the figure
    fig.savefig(filepath, dpi=DEFAULT_DPI, bbox_inches="tight", facecolor="white")

    return str(filepath.resolve())


def _save_full_graph(
    fig: plt.Figure,
    path_prefix: str,
    max_nodes: int,
    output_dir: str,
) -> str:
    """Save full graph to disk.

    Args:
        fig: Matplotlib figure to save.
        path_prefix: Path prefix used.
        max_nodes: Max nodes used.
        output_dir: Directory to save.

    Returns:
        Absolute path to saved file.
    """
    # Create output directory if needed
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # Generate deterministic filename
    identifier = f"full:{path_prefix}:{max_nodes}"
    filename = _generate_graph_filename(identifier)
    filepath = output_path / filename

    # Save the figure
    fig.savefig(filepath, dpi=DEFAULT_DPI, bbox_inches="tight", facecolor="white")

    return str(filepath.resolve())
