"""matplotlib plot generation for DrSpec visualization.

Generates deterministic plots from function data for analysis by
Judge and Vision Analyst agents.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

import matplotlib
import matplotlib.pyplot as plt

# Use non-interactive backend for headless operation
matplotlib.use("Agg")


# =============================================================================
# Constants
# =============================================================================

DEFAULT_OUTPUT_DIR = "_drspec/plots"
DEFAULT_DPI = 100
DEFAULT_FIGSIZE = (10, 6)

# Consistent style for all DrSpec plots
DRSPEC_STYLE = {
    "figure.figsize": DEFAULT_FIGSIZE,
    "font.size": 12,
    "axes.titlesize": 14,
    "axes.labelsize": 12,
    "legend.fontsize": 10,
    "figure.dpi": DEFAULT_DPI,
}

# Deterministic colors for consistency
PLOT_COLORS = [
    "#1f77b4",  # blue
    "#ff7f0e",  # orange
    "#2ca02c",  # green
    "#d62728",  # red
    "#9467bd",  # purple
    "#8c564b",  # brown
    "#e377c2",  # pink
    "#7f7f7f",  # gray
]


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class PlotResult:
    """Result of plot generation.

    Attributes:
        path: Absolute path to saved plot file.
        plot_type: Type of plot (line, scatter, bar).
        width: Pixel width of the plot.
        height: Pixel height of the plot.
        data_points: Number of data points plotted.
    """

    path: str
    plot_type: str
    width: int
    height: int
    data_points: int


# =============================================================================
# Filename Generation
# =============================================================================


def generate_plot_filename(data: dict[str, Any], plot_type: str) -> str:
    """Generate deterministic filename from data hash.

    Args:
        data: Plot data dictionary.
        plot_type: Type of plot being generated.

    Returns:
        Filename in format plot_{hash}.png
    """
    content = json.dumps(data, sort_keys=True) + plot_type
    hash_val = hashlib.sha256(content.encode()).hexdigest()[:12]
    return f"plot_{hash_val}.png"


# =============================================================================
# Plot Type Detection
# =============================================================================


def _detect_plot_type(data: dict[str, Any]) -> str:
    """Auto-detect the best plot type from data structure.

    Args:
        data: Plot data dictionary.

    Returns:
        Plot type: 'line', 'scatter', or 'bar'.
    """
    # Bar chart: has categories and values
    if "categories" in data and "values" in data:
        return "bar"

    # Check if we have x and y data
    x = data.get("x", [])
    y = data.get("y", [])

    if not x or not y:
        # Default to bar if we can't determine
        return "bar"

    # If x values are all integers and sequential, use line plot
    if all(isinstance(v, int) for v in x):
        sorted_x = sorted(x)
        if sorted_x == list(range(sorted_x[0], sorted_x[-1] + 1)):
            return "line"

    # If there are many data points, use scatter
    if len(x) > 20:
        return "scatter"

    # Default to line for small datasets
    return "line"


# =============================================================================
# Plot Generation Functions
# =============================================================================


def generate_line_plot(
    data: dict[str, Any],
    title: str = "",
    x_label: str = "",
    y_label: str = "",
    output_dir: str = DEFAULT_OUTPUT_DIR,
) -> PlotResult:
    """Generate a line plot from data.

    Args:
        data: Dictionary with 'x' and 'y' lists, optional 'series_name'.
        title: Plot title.
        x_label: X-axis label.
        y_label: Y-axis label.
        output_dir: Directory to save plot.

    Returns:
        PlotResult with path and metadata.

    Raises:
        ValueError: If required data fields are missing.
    """
    x = data.get("x", [])
    y = data.get("y", [])

    if not x or not y:
        raise ValueError("Line plot requires 'x' and 'y' data")

    if len(x) != len(y):
        raise ValueError(f"x and y must have same length: {len(x)} vs {len(y)}")

    # Apply style
    with plt.rc_context(DRSPEC_STYLE):
        fig, ax = plt.subplots()

        series_name = data.get("series_name", "Data")
        ax.plot(x, y, color=PLOT_COLORS[0], linewidth=2, marker="o", label=series_name)

        if title:
            ax.set_title(title)
        if x_label:
            ax.set_xlabel(x_label)
        if y_label:
            ax.set_ylabel(y_label)

        ax.legend()
        ax.grid(True, alpha=0.3)

        # Save plot
        output_path = _save_plot(fig, data, "line", output_dir)
        data_points = len(x)

        plt.close(fig)

    return PlotResult(
        path=output_path,
        plot_type="line",
        width=int(DEFAULT_FIGSIZE[0] * DEFAULT_DPI),
        height=int(DEFAULT_FIGSIZE[1] * DEFAULT_DPI),
        data_points=data_points,
    )


def generate_scatter_plot(
    data: dict[str, Any],
    title: str = "",
    x_label: str = "",
    y_label: str = "",
    output_dir: str = DEFAULT_OUTPUT_DIR,
) -> PlotResult:
    """Generate a scatter plot from data.

    Args:
        data: Dictionary with 'x' and 'y' lists, optional 'labels'.
        title: Plot title.
        x_label: X-axis label.
        y_label: Y-axis label.
        output_dir: Directory to save plot.

    Returns:
        PlotResult with path and metadata.

    Raises:
        ValueError: If required data fields are missing.
    """
    x = data.get("x", [])
    y = data.get("y", [])

    if not x or not y:
        raise ValueError("Scatter plot requires 'x' and 'y' data")

    if len(x) != len(y):
        raise ValueError(f"x and y must have same length: {len(x)} vs {len(y)}")

    # Apply style
    with plt.rc_context(DRSPEC_STYLE):
        fig, ax = plt.subplots()

        ax.scatter(x, y, color=PLOT_COLORS[0], s=50, alpha=0.7)

        # Add point labels if provided
        labels = data.get("labels", [])
        if labels and len(labels) == len(x):
            for i, label in enumerate(labels):
                ax.annotate(
                    label,
                    (x[i], y[i]),
                    textcoords="offset points",
                    xytext=(5, 5),
                    fontsize=9,
                )

        if title:
            ax.set_title(title)
        if x_label:
            ax.set_xlabel(x_label)
        if y_label:
            ax.set_ylabel(y_label)

        ax.grid(True, alpha=0.3)

        # Save plot
        output_path = _save_plot(fig, data, "scatter", output_dir)
        data_points = len(x)

        plt.close(fig)

    return PlotResult(
        path=output_path,
        plot_type="scatter",
        width=int(DEFAULT_FIGSIZE[0] * DEFAULT_DPI),
        height=int(DEFAULT_FIGSIZE[1] * DEFAULT_DPI),
        data_points=data_points,
    )


def generate_bar_chart(
    data: dict[str, Any],
    title: str = "",
    x_label: str = "",
    y_label: str = "",
    output_dir: str = DEFAULT_OUTPUT_DIR,
) -> PlotResult:
    """Generate a bar chart from data.

    Args:
        data: Dictionary with 'categories' and 'values' lists.
        title: Plot title.
        x_label: X-axis label.
        y_label: Y-axis label.
        output_dir: Directory to save plot.

    Returns:
        PlotResult with path and metadata.

    Raises:
        ValueError: If required data fields are missing.
    """
    categories = data.get("categories", [])
    values = data.get("values", [])

    if not categories or not values:
        raise ValueError("Bar chart requires 'categories' and 'values' data")

    if len(categories) != len(values):
        raise ValueError(
            f"categories and values must have same length: {len(categories)} vs {len(values)}"
        )

    # Apply style
    with plt.rc_context(DRSPEC_STYLE):
        fig, ax = plt.subplots()

        # Use colors cycling for bars
        colors = [PLOT_COLORS[i % len(PLOT_COLORS)] for i in range(len(categories))]
        ax.bar(categories, values, color=colors, alpha=0.8)

        if title:
            ax.set_title(title)
        if x_label:
            ax.set_xlabel(x_label)
        if y_label:
            ax.set_ylabel(y_label)

        ax.grid(True, alpha=0.3, axis="y")

        # Rotate x labels if many categories
        if len(categories) > 5:
            plt.xticks(rotation=45, ha="right")

        plt.tight_layout()

        # Save plot
        output_path = _save_plot(fig, data, "bar", output_dir)
        data_points = len(categories)

        plt.close(fig)

    return PlotResult(
        path=output_path,
        plot_type="bar",
        width=int(DEFAULT_FIGSIZE[0] * DEFAULT_DPI),
        height=int(DEFAULT_FIGSIZE[1] * DEFAULT_DPI),
        data_points=data_points,
    )


def generate_plot(
    data: dict[str, Any],
    plot_type: Literal["auto", "line", "scatter", "bar"] = "auto",
    title: str = "",
    x_label: str = "",
    y_label: str = "",
    output_dir: str = DEFAULT_OUTPUT_DIR,
) -> PlotResult:
    """Generate a plot from data and save to disk.

    This is the main entry point for plot generation. It can auto-detect
    the best plot type based on the data structure.

    Args:
        data: Plot data dictionary. Format depends on plot type:
            - line/scatter: {'x': [...], 'y': [...], 'series_name': '...'}
            - bar: {'categories': [...], 'values': [...]}
        plot_type: Type of plot or 'auto' to detect.
        title: Plot title.
        x_label: X-axis label.
        y_label: Y-axis label.
        output_dir: Directory to save plot.

    Returns:
        PlotResult with path and metadata.

    Raises:
        ValueError: If data is invalid for the requested plot type.
    """
    if plot_type == "auto":
        plot_type = _detect_plot_type(data)

    if plot_type == "line":
        return generate_line_plot(data, title, x_label, y_label, output_dir)
    elif plot_type == "scatter":
        return generate_scatter_plot(data, title, x_label, y_label, output_dir)
    elif plot_type == "bar":
        return generate_bar_chart(data, title, x_label, y_label, output_dir)
    else:
        raise ValueError(f"Unknown plot type: {plot_type}")


# =============================================================================
# Helper Functions
# =============================================================================


def _save_plot(
    fig: plt.Figure, data: dict[str, Any], plot_type: str, output_dir: str
) -> str:
    """Save plot to disk with deterministic filename.

    Args:
        fig: Matplotlib figure to save.
        data: Original plot data (for hash generation).
        plot_type: Type of plot.
        output_dir: Directory to save plot.

    Returns:
        Absolute path to saved plot file.
    """
    # Create output directory if needed
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # Generate deterministic filename
    filename = generate_plot_filename(data, plot_type)
    filepath = output_path / filename

    # Save the figure
    fig.savefig(filepath, dpi=DEFAULT_DPI, bbox_inches="tight", facecolor="white")

    return str(filepath.resolve())
