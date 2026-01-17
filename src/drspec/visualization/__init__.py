"""Visualization module for DrSpec.

Provides matplotlib plot generation and networkx graph visualization
for analyzing function behavior and relationships.
"""

from __future__ import annotations

from drspec.visualization.plotter import (
    PlotResult,
    generate_plot,
    generate_line_plot,
    generate_scatter_plot,
    generate_bar_chart,
    generate_plot_filename,
)
from drspec.visualization.grapher import (
    GraphResult,
    generate_dependency_graph,
    generate_full_graph,
    build_dependency_graph,
    build_full_graph,
)

__all__ = [
    # Plotter
    "PlotResult",
    "generate_plot",
    "generate_line_plot",
    "generate_scatter_plot",
    "generate_bar_chart",
    "generate_plot_filename",
    # Grapher
    "GraphResult",
    "generate_dependency_graph",
    "generate_full_graph",
    "build_dependency_graph",
    "build_full_graph",
]
