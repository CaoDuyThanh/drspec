"""Dependency graph queries for DrSpec.

This module provides efficient graph traversal queries for function dependencies,
supporting BFS traversal, cycle detection, and configurable depth limits.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, List, Optional, Set, Tuple

import duckdb


# =============================================================================
# Graph Models
# =============================================================================


@dataclass
class DependencyNode:
    """A node in the dependency graph.

    Attributes:
        function_id: Unique function ID (filepath::function_name).
        function_name: Function name.
        file_path: Relative file path.
        status: Artifact status (PENDING, VERIFIED, etc.).
        has_contract: Whether a contract exists for this function.
        depth: Distance from root node (0 for root).
        relationship: Relationship to root ("root", "callee", "caller", "both").
    """

    function_id: str
    function_name: str
    file_path: str
    status: str
    has_contract: bool
    depth: int
    relationship: str

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "function_id": self.function_id,
            "function_name": self.function_name,
            "file_path": self.file_path,
            "status": self.status,
            "has_contract": self.has_contract,
            "depth": self.depth,
            "relationship": self.relationship,
        }


@dataclass
class DependencyEdge:
    """An edge in the dependency graph.

    Attributes:
        caller_id: Function that makes the call.
        callee_id: Function being called.
        is_cyclic: True if this edge creates a cycle.
    """

    caller_id: str
    callee_id: str
    is_cyclic: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "caller_id": self.caller_id,
            "callee_id": self.callee_id,
            "is_cyclic": self.is_cyclic,
        }


@dataclass
class DependencyGraph:
    """Complete dependency graph centered on a function.

    Attributes:
        root_function_id: Center node of the graph.
        nodes: List of nodes in the graph.
        edges: List of edges in the graph.
        has_cycles: True if the graph contains cycles.
        max_depth_reached: Maximum depth actually reached.
    """

    root_function_id: str
    nodes: List[DependencyNode] = field(default_factory=list)
    edges: List[DependencyEdge] = field(default_factory=list)
    has_cycles: bool = False
    max_depth_reached: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "root_function_id": self.root_function_id,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "has_cycles": self.has_cycles,
            "max_depth_reached": self.max_depth_reached,
        }

    @property
    def node_count(self) -> int:
        """Number of nodes in the graph."""
        return len(self.nodes)

    @property
    def edge_count(self) -> int:
        """Number of edges in the graph."""
        return len(self.edges)


# =============================================================================
# Graph Query Functions
# =============================================================================


def get_dependency_graph(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
    depth: int = 2,
    direction: str = "both",
) -> DependencyGraph:
    """Get dependency graph centered on a function.

    Uses BFS traversal to find all connected nodes up to the specified depth.
    Handles cyclic dependencies gracefully by tracking visited nodes.

    Args:
        conn: DuckDB connection.
        function_id: Center node of the graph.
        depth: Maximum traversal depth (1-5, default 2).
        direction: Direction(s) to traverse ("callees", "callers", "both").

    Returns:
        DependencyGraph with nodes and edges.

    Example:
        >>> graph = get_dependency_graph(conn, "src/api.py::handle", depth=2)
        >>> len(graph.nodes)
        5
        >>> graph.has_cycles
        False
    """
    # Validate depth
    depth = max(1, min(5, depth))

    # Initialize graph
    graph = DependencyGraph(root_function_id=function_id)

    # Track visited nodes and their relationships
    visited: dict[str, str] = {}  # function_id -> relationship
    all_edges: List[Tuple[str, str]] = []

    # Add root node
    root_info = _get_node_info(conn, function_id)
    if root_info:
        graph.nodes.append(DependencyNode(
            function_id=function_id,
            function_name=root_info["function_name"],
            file_path=root_info["file_path"],
            status=root_info["status"],
            has_contract=root_info["has_contract"],
            depth=0,
            relationship="root",
        ))
        visited[function_id] = "root"
    else:
        # Root doesn't exist in artifacts
        graph.nodes.append(DependencyNode(
            function_id=function_id,
            function_name=function_id.split("::")[-1] if "::" in function_id else function_id,
            file_path="",
            status="UNKNOWN",
            has_contract=False,
            depth=0,
            relationship="root",
        ))
        visited[function_id] = "root"

    # BFS for callees
    if direction in ("callees", "both"):
        _bfs_traverse(
            conn=conn,
            start_id=function_id,
            depth=depth,
            get_neighbors=_get_callees_raw,
            relationship="callee",
            visited=visited,
            edges=all_edges,
            nodes=graph.nodes,
        )

    # BFS for callers
    if direction in ("callers", "both"):
        _bfs_traverse(
            conn=conn,
            start_id=function_id,
            depth=depth,
            get_neighbors=_get_callers_raw,
            relationship="caller",
            visited=visited,
            edges=all_edges,
            nodes=graph.nodes,
        )

    # Detect cycles and build edges
    cyclic_edges = _detect_cycles(all_edges)
    for caller_id, callee_id in all_edges:
        graph.edges.append(DependencyEdge(
            caller_id=caller_id,
            callee_id=callee_id,
            is_cyclic=(caller_id, callee_id) in cyclic_edges,
        ))

    graph.has_cycles = len(cyclic_edges) > 0
    graph.max_depth_reached = max((n.depth for n in graph.nodes), default=0)

    return graph


def get_callee_graph(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
    depth: int = 2,
) -> DependencyGraph:
    """Get graph of functions called by the given function.

    Convenience wrapper for get_dependency_graph with direction="callees".

    Args:
        conn: DuckDB connection.
        function_id: Starting function.
        depth: Maximum traversal depth.

    Returns:
        DependencyGraph with callee nodes only.
    """
    return get_dependency_graph(conn, function_id, depth=depth, direction="callees")


def get_caller_graph(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
    depth: int = 2,
) -> DependencyGraph:
    """Get graph of functions that call the given function.

    Convenience wrapper for get_dependency_graph with direction="callers".

    Args:
        conn: DuckDB connection.
        function_id: Target function.
        depth: Maximum traversal depth.

    Returns:
        DependencyGraph with caller nodes only.
    """
    return get_dependency_graph(conn, function_id, depth=depth, direction="callers")


# =============================================================================
# Helper Functions
# =============================================================================


def _get_node_info(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
) -> Optional[dict[str, Any]]:
    """Get node information for a function.

    Args:
        conn: DuckDB connection.
        function_id: Function to look up.

    Returns:
        Dictionary with node info or None if not found.
    """
    result = conn.execute(
        """
        SELECT
            a.function_name,
            a.file_path,
            a.status,
            c.function_id IS NOT NULL as has_contract
        FROM artifacts a
        LEFT JOIN contracts c ON a.function_id = c.function_id
        WHERE a.function_id = ?
        """,
        [function_id],
    ).fetchone()

    if result is None:
        return None

    return {
        "function_name": result[0],
        "file_path": result[1],
        "status": result[2],
        "has_contract": bool(result[3]),
    }


def _get_callees_raw(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
) -> List[str]:
    """Get raw list of callee function IDs.

    Args:
        conn: DuckDB connection.
        function_id: Caller function.

    Returns:
        List of callee function IDs.
    """
    result = conn.execute(
        "SELECT callee_id FROM dependencies WHERE caller_id = ?",
        [function_id],
    ).fetchall()
    return [row[0] for row in result]


def _get_callers_raw(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
) -> List[str]:
    """Get raw list of caller function IDs.

    Args:
        conn: DuckDB connection.
        function_id: Callee function.

    Returns:
        List of caller function IDs.
    """
    result = conn.execute(
        "SELECT caller_id FROM dependencies WHERE callee_id = ?",
        [function_id],
    ).fetchall()
    return [row[0] for row in result]


def _bfs_traverse(
    conn: duckdb.DuckDBPyConnection,
    start_id: str,
    depth: int,
    get_neighbors: Any,
    relationship: str,
    visited: dict[str, str],
    edges: List[Tuple[str, str]],
    nodes: List[DependencyNode],
) -> None:
    """BFS traversal to collect nodes and edges.

    Args:
        conn: DuckDB connection.
        start_id: Starting function ID.
        depth: Maximum depth to traverse.
        get_neighbors: Function to get neighbors (callees or callers).
        relationship: Relationship type ("callee" or "caller").
        visited: Dictionary tracking visited nodes and their relationships.
        edges: List to append edges to.
        nodes: List to append nodes to.
    """
    queue: deque[Tuple[str, int]] = deque()

    # Get initial neighbors
    for neighbor_id in get_neighbors(conn, start_id):
        if neighbor_id not in visited:
            queue.append((neighbor_id, 1))
            visited[neighbor_id] = relationship

        # Add edge
        if relationship == "callee":
            edges.append((start_id, neighbor_id))
        else:  # caller
            edges.append((neighbor_id, start_id))

    while queue:
        current_id, current_depth = queue.popleft()

        # Get node info and add to nodes list
        node_info = _get_node_info(conn, current_id)
        if node_info:
            nodes.append(DependencyNode(
                function_id=current_id,
                function_name=node_info["function_name"],
                file_path=node_info["file_path"],
                status=node_info["status"],
                has_contract=node_info["has_contract"],
                depth=current_depth,
                relationship=visited[current_id],
            ))
        else:
            # Function not in artifacts - add with limited info
            nodes.append(DependencyNode(
                function_id=current_id,
                function_name=current_id.split("::")[-1] if "::" in current_id else current_id,
                file_path="",
                status="UNKNOWN",
                has_contract=False,
                depth=current_depth,
                relationship=visited[current_id],
            ))

        # Continue BFS if not at max depth
        if current_depth < depth:
            for neighbor_id in get_neighbors(conn, current_id):
                # Add edge
                if relationship == "callee":
                    edges.append((current_id, neighbor_id))
                else:  # caller
                    edges.append((neighbor_id, current_id))

                # Queue if not visited
                if neighbor_id not in visited:
                    visited[neighbor_id] = relationship
                    queue.append((neighbor_id, current_depth + 1))
                elif visited[neighbor_id] != relationship:
                    # Node was visited from other direction - mark as "both"
                    # Update existing node
                    for node in nodes:
                        if node.function_id == neighbor_id:
                            node.relationship = "both"
                            break


def _detect_cycles(edges: List[Tuple[str, str]]) -> Set[Tuple[str, str]]:
    """Detect cyclic edges in the graph.

    Uses DFS-based cycle detection.

    Args:
        edges: List of (caller_id, callee_id) tuples.

    Returns:
        Set of cyclic edges.
    """
    # Build adjacency list
    graph: dict[str, Set[str]] = defaultdict(set)
    for caller_id, callee_id in edges:
        graph[caller_id].add(callee_id)

    cyclic_edges: Set[Tuple[str, str]] = set()
    visited: Set[str] = set()
    rec_stack: Set[str] = set()

    def dfs(node: str) -> None:
        visited.add(node)
        rec_stack.add(node)

        for neighbor in graph[node]:
            if neighbor not in visited:
                dfs(neighbor)
            elif neighbor in rec_stack:
                # Found back edge - marks a cycle
                cyclic_edges.add((node, neighbor))

        rec_stack.remove(node)

    # Run DFS from all unvisited nodes
    for node in list(graph.keys()):
        if node not in visited:
            dfs(node)

    return cyclic_edges


# =============================================================================
# Statistics Functions
# =============================================================================


def get_graph_statistics(
    conn: duckdb.DuckDBPyConnection,
) -> dict[str, Any]:
    """Get overall dependency graph statistics.

    Args:
        conn: DuckDB connection.

    Returns:
        Dictionary with graph statistics.
    """
    # Count nodes and edges
    node_count = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
    edge_count = conn.execute("SELECT COUNT(*) FROM dependencies").fetchone()[0]

    # Count functions with contracts
    with_contracts = conn.execute(
        """
        SELECT COUNT(*)
        FROM artifacts a
        JOIN contracts c ON a.function_id = c.function_id
        """
    ).fetchone()[0]

    # Find most connected functions (by callee count)
    most_callees = conn.execute(
        """
        SELECT caller_id, COUNT(*) as cnt
        FROM dependencies
        GROUP BY caller_id
        ORDER BY cnt DESC
        LIMIT 5
        """
    ).fetchall()

    # Find most called functions
    most_callers = conn.execute(
        """
        SELECT callee_id, COUNT(*) as cnt
        FROM dependencies
        GROUP BY callee_id
        ORDER BY cnt DESC
        LIMIT 5
        """
    ).fetchall()

    return {
        "total_functions": node_count,
        "total_dependencies": edge_count,
        "functions_with_contracts": with_contracts,
        "most_outgoing": [
            {"function_id": row[0], "callee_count": row[1]}
            for row in most_callees
        ],
        "most_incoming": [
            {"function_id": row[0], "caller_count": row[1]}
            for row in most_callers
        ],
    }
