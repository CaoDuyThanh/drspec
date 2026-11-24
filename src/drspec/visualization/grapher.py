"""networkx graph visualization for DrSpec dependency analysis.

Generates deterministic dependency graphs for function relationships,
colored by contract status.
"""

from __future__ import annotations

import hashlib
import html
import json
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

import duckdb
import matplotlib
import matplotlib.pyplot as plt
import networkx as nx

from drspec.core.identity import display_name
from drspec.db import (
    get_artifact,
    get_callees,
    get_dependencies_by_callee,
    get_dependencies_by_caller,
    get_references_by_callee,
    get_references_by_caller,
)
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

    root = get_artifact(conn, function_id)
    if root is None:
        return G

    G.add_node(
        function_id,
        status=root.status,
        name=root.function_name,
        signature=root.signature,
        file_path=root.file_path,
        is_center=True,
    )

    visited = {function_id}
    queue: deque[tuple[str, int]] = deque([(function_id, 0)])

    while queue:
        current, current_depth = queue.popleft()
        if current_depth >= depth:
            continue

        # Both `calls` and `references` count as dependencies here — a
        # property read is just as real a dependency as a call. Full edge
        # records (not just the bare id) so relation/confidence can be
        # rendered distinctly.
        if direction in ("callees", "both"):
            out_edges = [
                (e.callee_id, "calls", e.confidence, e.call_site) for e in get_dependencies_by_caller(conn, current)
            ] + [
                (e.callee_id, "references", e.confidence, e.reference_site)
                for e in get_references_by_caller(conn, current)
            ]
            for callee, relation, confidence, text in out_edges:
                if callee not in visited:
                    artifact = get_artifact(conn, callee)
                    if artifact:
                        G.add_node(
                            callee,
                            status=artifact.status,
                            name=artifact.function_name,
                            signature=artifact.signature,
                            file_path=artifact.file_path,
                            is_center=False,
                        )
                        visited.add(callee)
                        queue.append((callee, current_depth + 1))
                G.add_edge(current, callee, relation=relation, confidence=confidence, text=text)

        if direction in ("callers", "both"):
            in_edges = [
                (e.caller_id, "calls", e.confidence, e.call_site) for e in get_dependencies_by_callee(conn, current)
            ] + [
                (e.caller_id, "references", e.confidence, e.reference_site)
                for e in get_references_by_callee(conn, current)
            ]
            for caller, relation, confidence, text in in_edges:
                if caller not in visited:
                    artifact = get_artifact(conn, caller)
                    if artifact:
                        G.add_node(
                            caller,
                            status=artifact.status,
                            name=artifact.function_name,
                            signature=artifact.signature,
                            file_path=artifact.file_path,
                            is_center=False,
                        )
                        visited.add(caller)
                        queue.append((caller, current_depth + 1))
                G.add_edge(caller, current, relation=relation, confidence=confidence, text=text)

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

    artifacts = list_artifacts(conn, file_path=path_prefix, limit=max_nodes)

    for artifact in artifacts:
        G.add_node(
            artifact.function_id,
            status=artifact.status,
            name=artifact.function_name,
            is_center=False,
        )

    node_ids = set(G.nodes())

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

    node_colors = []
    for node in G.nodes():
        status = G.nodes[node].get("status", "PENDING")
        node_colors.append(STATUS_COLORS.get(status, DEFAULT_COLOR))

    node_sizes = []
    for node in G.nodes():
        is_center = G.nodes[node].get("is_center", False)
        node_sizes.append(800 if is_center else 400)

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

    nx.draw_networkx_nodes(
        G,
        pos,
        ax=ax,
        node_color=node_colors,
        node_size=node_sizes,
        alpha=0.9,
    )

    # Full function names, not truncated — small font plus a background
    # box keeps them legible over edges without cutting names down to an
    # unreadable prefix.
    labels = {node: G.nodes[node].get("name", display_name(node)) for node in G.nodes()}

    nx.draw_networkx_labels(
        G,
        pos,
        labels,
        ax=ax,
        font_size=7,
        font_weight="bold",
        bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.7, "pad": 0.5},
    )

    if title:
        ax.set_title(title, fontsize=14, fontweight="bold", pad=20)

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
# Interactive HTML Rendering
# =============================================================================
#
# A single self-contained HTML file, opened via VS Code's built-in local-file
# preview — no server, no install. Rendered as plain inline SVG + vanilla JS
# (no vendored third-party graph library) to keep it fully local and
# dependency-free, reusing the same deterministic `pos` layout from
# `layout_graph()` that the matplotlib PNG path uses.

HTML_SVG_WIDTH = 1400
HTML_SVG_HEIGHT = 900
_HTML_NODE_RADIUS_CENTER = 14
_HTML_NODE_RADIUS = 9


def _scale_positions(
    pos: dict[str, tuple[float, float]],
    width: int,
    height: int,
    padding_top: int = 70,
    padding_right: int = 70,
    padding_bottom: int = 70,
    padding_left: int = 70,
) -> dict[str, tuple[float, float]]:
    """Map `layout_graph()`'s arbitrary-range float positions into SVG
    pixel coordinates, preserving relative layout exactly.

    Padding is per-side, not uniform, so the fixed HTML overlays
    (`#toolbar` top-left, `#legend` bottom-left — see `render_graph_html`)
    can be given a reserved margin no node ever lands under. Found by
    actually rendering a generated graph headlessly and looking at the
    screenshot, not assumed: a node positioned near the bottom-left
    corner under uniform padding was fully hidden behind the legend box.
    """
    if not pos:
        return {}
    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = (max_x - min_x) or 1.0
    span_y = (max_y - min_y) or 1.0
    return {
        node: (
            padding_left + (x - min_x) / span_x * (width - padding_left - padding_right),
            padding_top + (y - min_y) / span_y * (height - padding_top - padding_bottom),
        )
        for node, (x, y) in pos.items()
    }


def graph_to_dict(G: nx.DiGraph, pos: dict[str, tuple[float, float]]) -> dict:
    """Convert a built+laid-out graph into the plain JSON-able shape both
    the static HTML and the live server use — one shared conversion, not two.

    Args:
        G: networkx DiGraph (nodes need `status`/`name`/`signature`/
            `file_path`/`is_center`; edges need
            `relation`/`confidence`/`text` — see `build_dependency_graph`).
        pos: Node positions from `layout_graph`.

    Returns:
        `{"nodes": [...], "edges": [...]}`, scaled into SVG pixel space
        with room reserved for the fixed toolbar/legend/title-bar overlays.
    """
    scaled = _scale_positions(
        pos, HTML_SVG_WIDTH, HTML_SVG_HEIGHT, padding_top=90, padding_right=90, padding_bottom=190, padding_left=190
    )

    nodes_data = []
    for node in G.nodes():
        x, y = scaled.get(node, (HTML_SVG_WIDTH / 2, HTML_SVG_HEIGHT / 2))
        attrs = G.nodes[node]
        nodes_data.append(
            {
                "id": node,
                "name": attrs.get("name", display_name(node)),
                "signature": attrs.get("signature", ""),
                "filePath": attrs.get("file_path", ""),
                "status": attrs.get("status", "PENDING"),
                "isCenter": bool(attrs.get("is_center", False)),
                "x": x,
                "y": y,
            }
        )

    edges_data = []
    for source, target, attrs in G.edges(data=True):
        edges_data.append(
            {
                "source": source,
                "target": target,
                "relation": attrs.get("relation", "calls"),
                "confidence": attrs.get("confidence") or "EXTRACTED",
                "text": attrs.get("text") or "",
            }
        )

    return {"nodes": nodes_data, "edges": edges_data}


def _embed_json_script(data: dict, element_id: str) -> str:
    """A `<script type="application/json">` block holding `data`, safe to
    embed inside an already-open `<script>`/HTML document: a
    `</script>`-looking substring inside embedded string data (a
    signature, a call site) can't terminate the block early."""
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return f'<script id="{element_id}" type="application/json">{payload}</script>'


# Shared CSS for both the static and live HTML viewers — one visual
# language, not two independently-maintained copies.
_GRAPH_HTML_CSS = """
  :root { color-scheme: light dark; }
  body {
    margin: 0; display: flex; height: 100vh; overflow: hidden;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #1e1e1e; color: #ddd;
  }
  #graph-pane { flex: 1; position: relative; overflow: hidden; }
  #toolbar {
    position: absolute; top: 12px; left: 12px; z-index: 2;
    display: flex; gap: 8px; align-items: center; flex-wrap: wrap; max-width: 60%;
  }
  #search {
    padding: 6px 10px; border-radius: 4px; border: 1px solid #555;
    background: #2d2d2d; color: #ddd; font-size: 13px; width: 220px;
  }
  #toolbar select, #toolbar input[type=number], #toolbar button {
    padding: 6px 10px; border-radius: 4px; border: 1px solid #555;
    background: #2d2d2d; color: #ddd; font-size: 13px;
  }
  #toolbar button { cursor: pointer; }
  #toolbar button:hover { background: #383838; }
  #toolbar input[type=number] { width: 50px; }
  #title-bar {
    position: absolute; top: 12px; right: 12px; z-index: 2;
    font-size: 13px; color: #aaa; text-align: right;
  }
  #legend {
    position: absolute; bottom: 12px; left: 12px; z-index: 2;
    background: rgba(45,45,45,0.9); border: 1px solid #444; border-radius: 6px;
    padding: 10px 12px; font-size: 12px; line-height: 1.6;
  }
  .legend-dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; }
  svg { width: 100%; height: 100%; cursor: grab; }
  svg.panning { cursor: grabbing; }
  .node circle { stroke: #1e1e1e; stroke-width: 1.5px; cursor: pointer; }
  .node text {
    fill: #eee; font-size: 11px; font-weight: 600; pointer-events: none;
    paint-order: stroke; stroke: #1e1e1e; stroke-width: 3px;
  }
  .node.dimmed { opacity: 0.15; }
  .node.selected circle { stroke: #fff; stroke-width: 3px; }
  .edge { stroke: #888; fill: none; }
  .edge.references { stroke-dasharray: 5,4; }
  .edge.INFERRED { opacity: 0.55; }
  .edge.AMBIGUOUS { opacity: 0.35; }
  .edge.selected { stroke: #fff; stroke-width: 2.5px; opacity: 1; }
  #sidebar {
    width: 320px; border-left: 1px solid #444; padding: 16px;
    overflow-y: auto; background: #252525; box-sizing: border-box;
  }
  #sidebar h2 { font-size: 15px; margin: 0 0 10px; word-break: break-word; }
  #sidebar .field { margin-bottom: 10px; }
  #sidebar .label { font-size: 11px; text-transform: uppercase; color: #999; margin-bottom: 2px; }
  #sidebar .value { font-size: 13px; word-break: break-word; white-space: pre-wrap; }
  #sidebar .value.code { font-family: ui-monospace, monospace; background: #1e1e1e; padding: 6px 8px; border-radius: 4px; }
  #sidebar .placeholder { color: #888; font-size: 13px; }
  #sidebar .status-badge {
    display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; color: #111;
  }
  #sidebar button {
    margin-top: 6px; padding: 6px 10px; border-radius: 4px; border: 1px solid #555;
    background: #2d2d2d; color: #ddd; font-size: 12px; cursor: pointer;
  }
  #sidebar button:hover { background: #383838; }
  #status-banner {
    position: absolute; top: 12px; left: 50%; transform: translateX(-50%); z-index: 3;
    padding: 6px 14px; border-radius: 4px; font-size: 12px; display: none;
  }
  #status-banner.error { background: #5a1f1f; color: #ffb4b4; display: block; }
  #status-banner.loading { background: #2d2d2d; color: #ccc; display: block; }
"""

# Shared JS for drawing/interacting with a graph once `DATA` (the
# `graph_to_dict` shape) is available — common to both the static and
# live viewers. The live viewer additionally defines `DATA` dynamically
# (via `fetch`) instead of reading it from an embedded `<script>` tag,
# and redraws by re-running this same block.
_GRAPH_DRAW_JS = """
function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}

function statusBadge(status) {
  const color = STATUS_COLORS[status] || DEFAULT_COLOR;
  return `<span class="status-badge" style="background:${color}">${escapeHtml(status)}</span>`;
}

function drawGraph(data, onSelectNode) {
  viewport.innerHTML = '';
  const byId = new Map(data.nodes.map(n => [n.id, n]));
  let selectedEl = null;

  function clearSelection() {
    if (selectedEl) selectedEl.classList.remove('selected');
    selectedEl = null;
  }

  function selectNode(n, el) {
    clearSelection();
    el.classList.add('selected');
    selectedEl = el;
    onSelectNode(n);
  }

  function selectEdge(e, el) {
    clearSelection();
    el.classList.add('selected');
    selectedEl = el;
    const a = byId.get(e.source), b = byId.get(e.target);
    sidebar.innerHTML = `
      <h2>${escapeHtml((a && a.name) || e.source)} &rarr; ${escapeHtml((b && b.name) || e.target)}</h2>
      <div class="field"><div class="label">Relation</div><div class="value">${escapeHtml(e.relation)}</div></div>
      <div class="field"><div class="label">Confidence</div><div class="value">${escapeHtml(e.confidence)}</div></div>
      <div class="field"><div class="label">Source text</div><div class="value code">${escapeHtml(e.text || '(not available)')}</div></div>
    `;
  }

  // Edges first so nodes draw on top.
  for (const e of data.edges) {
    const a = byId.get(e.source), b = byId.get(e.target);
    if (!a || !b) continue;
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    path.setAttribute('x1', a.x); path.setAttribute('y1', a.y);
    path.setAttribute('x2', b.x); path.setAttribute('y2', b.y);
    path.setAttribute('class', `edge ${e.relation} ${e.confidence}`);
    path.setAttribute('marker-end', 'url(#arrow)');
    path.dataset.source = e.source; path.dataset.target = e.target;
    path.addEventListener('click', (ev) => { ev.stopPropagation(); selectEdge(e, path); });
    viewport.appendChild(path);
  }

  const nodeEls = new Map();
  for (const n of data.nodes) {
    const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    g.setAttribute('class', 'node');
    g.setAttribute('transform', `translate(${n.x},${n.y})`);
    const r = n.isCenter ? NODE_RADIUS_CENTER : NODE_RADIUS;
    const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    circle.setAttribute('r', r);
    circle.setAttribute('fill', STATUS_COLORS[n.status] || DEFAULT_COLOR);
    const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    text.setAttribute('x', r + 4); text.setAttribute('y', 4);
    text.textContent = n.name;
    g.appendChild(circle); g.appendChild(text);
    g.addEventListener('click', (ev) => { ev.stopPropagation(); selectNode(n, g); });
    viewport.appendChild(g);
    nodeEls.set(n.id, g);
  }

  svg.onclick = clearSelection;

  search.oninput = () => {
    const q = search.value.trim().toLowerCase();
    const matches = new Set();
    for (const n of data.nodes) {
      const hit = !q || n.name.toLowerCase().includes(q) || n.filePath.toLowerCase().includes(q);
      nodeEls.get(n.id).classList.toggle('dimmed', !hit);
      if (hit) matches.add(n.id);
    }
    for (const path of viewport.querySelectorAll('.edge')) {
      const show = !q || (matches.has(path.dataset.source) && matches.has(path.dataset.target));
      path.style.opacity = show ? '' : '0.08';
    }
  };
}

legend.innerHTML = Object.entries(STATUS_COLORS).map(
  ([status, color]) => `<div><span class="legend-dot" style="background:${color}"></span>${status}</div>`
).join('') + '<div style="margin-top:6px;border-top:1px solid #444;padding-top:6px;">— calls &nbsp;&nbsp; - - references</div>';

// Minimal pan/zoom — no library, just drag-to-pan and wheel-to-zoom on the viewport transform.
let viewX = 0, viewY = 0, viewScale = 1, dragging = false, dragStartX = 0, dragStartY = 0;
function applyTransform() {
  viewport.setAttribute('transform', `translate(${viewX},${viewY}) scale(${viewScale})`);
}
svg.addEventListener('mousedown', (ev) => {
  dragging = true; svg.classList.add('panning');
  dragStartX = ev.clientX - viewX; dragStartY = ev.clientY - viewY;
});
window.addEventListener('mousemove', (ev) => {
  if (!dragging) return;
  viewX = ev.clientX - dragStartX; viewY = ev.clientY - dragStartY;
  applyTransform();
});
window.addEventListener('mouseup', () => { dragging = false; svg.classList.remove('panning'); });
svg.addEventListener('wheel', (ev) => {
  ev.preventDefault();
  const delta = ev.deltaY > 0 ? 0.9 : 1.1;
  viewScale = Math.max(0.2, Math.min(4, viewScale * delta));
  applyTransform();
}, { passive: false });
"""


def render_graph_html(
    G: nx.DiGraph,
    pos: dict[str, tuple[float, float]],
    title: str = "",
) -> str:
    """Render graph to a single self-contained, interactive HTML document.

    Nodes colored by status (same `STATUS_COLORS` mapping the matplotlib
    PNG path uses — one color scheme for the same concept, not two).
    Edges distinguished by relation (`calls` solid, `references` dashed)
    and confidence (opacity: `EXTRACTED` full, `INFERRED`/`AMBIGUOUS`
    faded) rather than by color, since color is already spoken for by
    node status. Clicking a node or edge shows its full detail in a side
    panel; a search box filters nodes by name/file path, client-side.

    A static, one-shot snapshot — see `render_live_graph_html` for the
    backend-connected version with Refresh/re-center.

    Args:
        G: networkx DiGraph to render (nodes need `status`/`name`/
            `signature`/`file_path`/`is_center`; edges need
            `relation`/`confidence`/`text` — see `build_dependency_graph`).
        pos: Node positions from `layout_graph`.
        title: Optional title for the graph.

    Returns:
        A complete HTML document as a string.
    """
    graph_data = graph_to_dict(G, pos)
    status_colors_json = json.dumps(STATUS_COLORS)
    safe_title = html.escape(title or "Dependency Graph")
    stats_text = f"{len(G.nodes())} nodes, {len(G.edges())} edges"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{safe_title}</title>
<style>{_GRAPH_HTML_CSS}</style>
</head>
<body>
<div id="graph-pane">
  <div id="toolbar"><input id="search" type="text" placeholder="Filter by name or file..." /></div>
  <div id="title-bar">{safe_title}<br>{stats_text}</div>
  <div id="legend"></div>
  <svg id="graph-svg" viewBox="0 0 {HTML_SVG_WIDTH} {HTML_SVG_HEIGHT}">
    <defs>
      <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
        <path d="M 0 0 L 10 5 L 0 10 z" fill="#888"></path>
      </marker>
    </defs>
    <g id="viewport"></g>
  </svg>
</div>
<div id="sidebar"><div class="placeholder">Click a node or edge for details.</div></div>
{_embed_json_script(graph_data, "graph-data")}
<script>
const STATUS_COLORS = {status_colors_json};
const DEFAULT_COLOR = "#95a5a6";
const NODE_RADIUS_CENTER = {_HTML_NODE_RADIUS_CENTER};
const NODE_RADIUS = {_HTML_NODE_RADIUS};
const svg = document.getElementById('graph-svg');
const viewport = document.getElementById('viewport');
const sidebar = document.getElementById('sidebar');
const legend = document.getElementById('legend');
const search = document.getElementById('search');

{_GRAPH_DRAW_JS}

function renderSidebarForNode(n) {{
  sidebar.innerHTML = `
    <h2>${{escapeHtml(n.name)}}</h2>
    <div class="field"><div class="label">Status</div><div class="value">${{statusBadge(n.status)}}</div></div>
    <div class="field"><div class="label">File</div><div class="value">${{escapeHtml(n.filePath)}}</div></div>
    <div class="field"><div class="label">Signature</div><div class="value code">${{escapeHtml(n.signature)}}</div></div>
    <div class="field"><div class="label">Function ID</div><div class="value">${{escapeHtml(n.id)}}</div></div>
  `;
}}

const DATA = JSON.parse(document.getElementById('graph-data').textContent);
drawGraph(DATA, renderSidebarForNode);
</script>
</body>
</html>
"""


def render_live_graph_html(
    function_id: str,
    depth: int = DEFAULT_DEPTH,
    direction: Literal["callers", "callees", "both"] = "both",
) -> str:
    """Render the backend-connected interactive HTML document for
    `drspec serve` — same visual language as `render_graph_html` (shared
    CSS/draw JS), but fetches `/graph` from the local server instead of
    reading data embedded at generation time, so Refresh and "center on
    this node" actually reflect the DB's current state rather than a
    frozen snapshot.

    Args:
        function_id: Initial center function ID.
        depth: Initial traversal depth.
        direction: Initial traversal direction.

    Returns:
        A complete HTML document as a string (not a snapshot — this one
        has no embedded graph data at all, only the initial parameters).
    """
    status_colors_json = json.dumps(STATUS_COLORS)
    safe_function_id = html.escape(function_id)
    # Same guard as `graph_json` in `render_graph_html`/`_embed_json_script`:
    # `function_id` is attacker-influenceable (it's whatever a scanned
    # source file's function name/path happened to be, and it's also a
    # raw CLI argument) — a `</script>`-containing value would otherwise
    # terminate this script block early and let arbitrary markup/script
    # that follows execute.
    initial_state = json.dumps(
        {"functionId": function_id, "depth": depth, "direction": direction}
    ).replace("</", "<\\/")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>drspec serve — {safe_function_id}</title>
<style>{_GRAPH_HTML_CSS}</style>
</head>
<body>
<div id="graph-pane">
  <div id="toolbar">
    <input id="search" type="text" placeholder="Filter by name or file..." />
    <label>Depth <input id="depth-input" type="number" min="1" max="5" /></label>
    <select id="direction-select">
      <option value="both">both</option>
      <option value="callers">callers</option>
      <option value="callees">callees</option>
    </select>
    <button id="refresh-btn">Refresh</button>
  </div>
  <div id="title-bar"><span id="title-text"></span></div>
  <div id="status-banner"></div>
  <div id="legend"></div>
  <svg id="graph-svg" viewBox="0 0 {HTML_SVG_WIDTH} {HTML_SVG_HEIGHT}">
    <defs>
      <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
        <path d="M 0 0 L 10 5 L 0 10 z" fill="#888"></path>
      </marker>
    </defs>
    <g id="viewport"></g>
  </svg>
</div>
<div id="sidebar"><div class="placeholder">Loading...</div></div>
<script>
const STATUS_COLORS = {status_colors_json};
const DEFAULT_COLOR = "#95a5a6";
const NODE_RADIUS_CENTER = {_HTML_NODE_RADIUS_CENTER};
const NODE_RADIUS = {_HTML_NODE_RADIUS};
const svg = document.getElementById('graph-svg');
const viewport = document.getElementById('viewport');
const sidebar = document.getElementById('sidebar');
const legend = document.getElementById('legend');
const search = document.getElementById('search');
const statusBanner = document.getElementById('status-banner');
const titleText = document.getElementById('title-text');
const depthInput = document.getElementById('depth-input');
const directionSelect = document.getElementById('direction-select');

{_GRAPH_DRAW_JS}

let current = {initial_state};
depthInput.value = current.depth;
directionSelect.value = current.direction;

function renderSidebarForNode(n) {{
  sidebar.innerHTML = `
    <h2>${{escapeHtml(n.name)}}</h2>
    <div class="field"><div class="label">Status</div><div class="value">${{statusBadge(n.status)}}</div></div>
    <div class="field"><div class="label">File</div><div class="value">${{escapeHtml(n.filePath)}}</div></div>
    <div class="field"><div class="label">Signature</div><div class="value code">${{escapeHtml(n.signature)}}</div></div>
    <div class="field"><div class="label">Function ID</div><div class="value">${{escapeHtml(n.id)}}</div></div>
    <button id="center-btn">Center on this node</button>
  `;
  document.getElementById('center-btn').onclick = () => {{
    current.functionId = n.id;
    loadGraph();
  }};
}}

async function loadGraph() {{
  current.depth = parseInt(depthInput.value, 10) || current.depth;
  current.direction = directionSelect.value;
  statusBanner.className = 'loading';
  statusBanner.textContent = 'Loading...';
  try {{
    const url = `/graph?function_id=${{encodeURIComponent(current.functionId)}}&depth=${{current.depth}}&direction=${{current.direction}}`;
    const res = await fetch(url);
    const body = await res.json();
    if (!res.ok) {{
      throw new Error(body.error || `HTTP ${{res.status}}`);
    }}
    statusBanner.className = '';
    statusBanner.textContent = '';
    titleText.textContent = `${{body.title}} — ${{body.nodes.length}} nodes, ${{body.edges.length}} edges`;
    sidebar.innerHTML = '<div class="placeholder">Click a node or edge for details.</div>';
    drawGraph(body, renderSidebarForNode);
  }} catch (err) {{
    statusBanner.className = 'error';
    statusBanner.textContent = `Error: ${{err.message}} — is the function ID correct, and did a concurrent 'drspec scan' just happen to collide? Try again.`;
  }}
}}

document.getElementById('refresh-btn').onclick = loadGraph;

loadGraph();
</script>
</body>
</html>
"""


# =============================================================================
# Main API
# =============================================================================


def generate_dependency_graph(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
    depth: int = DEFAULT_DEPTH,
    direction: Literal["callers", "callees", "both"] = "both",
    output_dir: str = DEFAULT_OUTPUT_DIR,
    output_file: Optional[str] = None,
) -> GraphResult:
    """Generate dependency graph centered on a function.

    Args:
        conn: DuckDB connection.
        function_id: Center function ID.
        depth: Maximum depth to traverse (default 2).
        direction: Which direction to follow relationships.
        output_dir: Directory to save graph image.
        output_file: Exact file to write; overrides `output_dir`'s hashed name.

    Returns:
        GraphResult with path and metadata.
    """
    G = build_dependency_graph(conn, function_id, depth, direction)
    pos = layout_graph(G)

    artifact = get_artifact(conn, function_id)
    title = f"Dependencies: {artifact.function_name if artifact else function_id}"

    fig = render_graph(G, pos, title)
    try:
        output_path = _save_graph(fig, function_id, depth, direction, output_dir, output_file)
    finally:
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


def generate_dependency_graph_html(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
    depth: int = DEFAULT_DEPTH,
    direction: Literal["callers", "callees", "both"] = "both",
    output_dir: str = DEFAULT_OUTPUT_DIR,
    output_file: Optional[str] = None,
) -> GraphResult:
    """Generate an interactive HTML dependency graph centered on a
    function — same centered/depth model as `generate_dependency_graph`'s
    static PNG, deliberately not a whole-project view (a full graph gets
    illegible fast at this project's real scale).

    Args:
        conn: DuckDB connection.
        function_id: Center function ID.
        depth: Maximum depth to traverse (default 2).
        direction: Which direction to follow relationships.
        output_dir: Directory to save the HTML file.
        output_file: Exact file to write; overrides `output_dir`'s hashed name.

    Returns:
        GraphResult with path and metadata (width/height are the SVG
        viewBox's nominal size, not a real image resolution — the HTML
        itself is responsive/pannable/zoomable, not fixed-size).
    """
    G = build_dependency_graph(conn, function_id, depth, direction)
    pos = layout_graph(G)

    artifact = get_artifact(conn, function_id)
    title = f"Dependencies: {artifact.function_name if artifact else function_id}"

    html_doc = render_graph_html(G, pos, title)
    output_path = _save_graph_html(html_doc, function_id, depth, direction, output_dir, output_file)

    return GraphResult(
        path=output_path,
        plot_type="graph_html",
        width=HTML_SVG_WIDTH,
        height=HTML_SVG_HEIGHT,
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
    G = build_full_graph(conn, path_prefix, max_nodes)
    pos = layout_graph(G)

    title = f"Function Graph: {path_prefix or 'All Functions'}"
    if len(G.nodes()) >= max_nodes:
        title += f" (limited to {max_nodes})"

    fig = render_graph(G, pos, title)
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


def _generate_graph_filename(identifier: str, extension: str = "png") -> str:
    """Generate deterministic filename from graph identifier.

    Args:
        identifier: Unique identifier for the graph.
        extension: File extension, without the dot.

    Returns:
        Filename in format graph_{hash}.{extension}
    """
    hash_val = hashlib.sha256(identifier.encode()).hexdigest()[:12]
    return f"graph_{hash_val}.{extension}"


def _resolve_graph_path(
    output_dir: str,
    output_file: Optional[str],
    identifier: str,
    extension: str,
) -> Path:
    """Final file path: `output_file` verbatim when given (default
    `extension` appended if it has none), else a hashed name in `output_dir`.
    Creates the parent directory."""
    if output_file:
        filepath = Path(output_file)
        if not filepath.suffix:
            filepath = filepath.with_suffix(f".{extension}")
    else:
        filepath = Path(output_dir) / _generate_graph_filename(identifier, extension=extension)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    return filepath


def _save_graph(
    fig: plt.Figure,
    function_id: str,
    depth: int,
    direction: str,
    output_dir: str,
    output_file: Optional[str] = None,
) -> str:
    """Save dependency graph to disk.

    Args:
        fig: Matplotlib figure to save.
        function_id: Center function ID.
        depth: Depth used.
        direction: Direction used.
        output_dir: Directory to save, when `output_file` is not given.
        output_file: Exact file to write instead of a hashed name.

    Returns:
        Absolute path to saved file.
    """
    identifier = f"{function_id}:{depth}:{direction}"
    filepath = _resolve_graph_path(output_dir, output_file, identifier, "png")

    fig.savefig(filepath, dpi=DEFAULT_DPI, bbox_inches="tight", facecolor="white")

    return str(filepath.resolve())


def _save_graph_html(
    html_doc: str,
    function_id: str,
    depth: int,
    direction: str,
    output_dir: str,
    output_file: Optional[str] = None,
) -> str:
    """Save an interactive HTML dependency graph to disk. Same
    deterministic-filename scheme as `_save_graph`, `.html` instead
    of `.png` — same `(function_id, depth, direction)` inputs give the
    same filename, so a PNG and an HTML render of the same view sit
    side by side rather than colliding.

    Args:
        html_doc: Complete HTML document string.
        function_id: Center function ID.
        depth: Depth used.
        direction: Direction used.
        output_dir: Directory to save, when `output_file` is not given.
        output_file: Exact file to write instead of a hashed name.

    Returns:
        Absolute path to saved file.
    """
    identifier = f"{function_id}:{depth}:{direction}"
    filepath = _resolve_graph_path(output_dir, output_file, identifier, "html")

    filepath.write_text(html_doc, encoding="utf-8")

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
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    identifier = f"full:{path_prefix}:{max_nodes}"
    filename = _generate_graph_filename(identifier)
    filepath = output_path / filename

    fig.savefig(filepath, dpi=DEFAULT_DPI, bbox_inches="tight", facecolor="white")

    return str(filepath.resolve())
