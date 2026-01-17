"""Tests for matplotlib plot generation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from drspec.visualization import (
    PlotResult,
    generate_plot,
    generate_line_plot,
    generate_scatter_plot,
    generate_bar_chart,
    generate_plot_filename,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def output_dir(tmp_path):
    """Create a temporary output directory for plots."""
    plots_dir = tmp_path / "plots"
    plots_dir.mkdir()
    return str(plots_dir)


@pytest.fixture
def line_data():
    """Sample data for line plots."""
    return {
        "x": [1, 2, 3, 4, 5],
        "y": [10, 20, 15, 25, 30],
        "series_name": "Test Series",
    }


@pytest.fixture
def scatter_data():
    """Sample data for scatter plots."""
    return {
        "x": [1.2, 2.5, 3.1, 4.8, 5.5],
        "y": [5.5, 6.2, 4.9, 7.1, 8.0],
        "labels": ["A", "B", "C", "D", "E"],
    }


@pytest.fixture
def bar_data():
    """Sample data for bar charts."""
    return {
        "categories": ["Jan", "Feb", "Mar", "Apr"],
        "values": [100, 150, 120, 180],
    }


# =============================================================================
# Filename Generation Tests
# =============================================================================


class TestGeneratePlotFilename:
    """Tests for generate_plot_filename function."""

    def test_generates_deterministic_filename(self):
        """Same data should produce same filename."""
        data = {"x": [1, 2, 3], "y": [4, 5, 6]}

        filename1 = generate_plot_filename(data, "line")
        filename2 = generate_plot_filename(data, "line")

        assert filename1 == filename2

    def test_different_data_produces_different_filename(self):
        """Different data should produce different filenames."""
        data1 = {"x": [1, 2, 3], "y": [4, 5, 6]}
        data2 = {"x": [1, 2, 3], "y": [4, 5, 7]}

        filename1 = generate_plot_filename(data1, "line")
        filename2 = generate_plot_filename(data2, "line")

        assert filename1 != filename2

    def test_different_plot_type_produces_different_filename(self):
        """Same data with different plot type should produce different filename."""
        data = {"x": [1, 2, 3], "y": [4, 5, 6]}

        filename1 = generate_plot_filename(data, "line")
        filename2 = generate_plot_filename(data, "scatter")

        assert filename1 != filename2

    def test_filename_format(self):
        """Filename should have correct format."""
        data = {"x": [1, 2], "y": [3, 4]}

        filename = generate_plot_filename(data, "line")

        assert filename.startswith("plot_")
        assert filename.endswith(".png")
        # Hash should be 12 characters
        assert len(filename) == len("plot_") + 12 + len(".png")


# =============================================================================
# Line Plot Tests
# =============================================================================


class TestGenerateLinePlot:
    """Tests for generate_line_plot function."""

    def test_generates_line_plot(self, line_data, output_dir):
        """Should generate a line plot successfully."""
        result = generate_line_plot(line_data, output_dir=output_dir)

        assert isinstance(result, PlotResult)
        assert result.plot_type == "line"
        assert result.data_points == 5
        assert Path(result.path).exists()

    def test_includes_title(self, line_data, output_dir):
        """Should include title in plot."""
        result = generate_line_plot(
            line_data,
            title="Test Title",
            output_dir=output_dir,
        )

        assert Path(result.path).exists()

    def test_includes_labels(self, line_data, output_dir):
        """Should include axis labels in plot."""
        result = generate_line_plot(
            line_data,
            x_label="Time",
            y_label="Value",
            output_dir=output_dir,
        )

        assert Path(result.path).exists()

    def test_returns_correct_dimensions(self, line_data, output_dir):
        """Should return correct plot dimensions."""
        result = generate_line_plot(line_data, output_dir=output_dir)

        assert result.width == 1000  # 10 * 100 DPI
        assert result.height == 600  # 6 * 100 DPI

    def test_missing_x_raises_error(self, output_dir):
        """Should raise error when x data is missing."""
        data = {"y": [1, 2, 3]}

        with pytest.raises(ValueError, match="requires 'x' and 'y' data"):
            generate_line_plot(data, output_dir=output_dir)

    def test_missing_y_raises_error(self, output_dir):
        """Should raise error when y data is missing."""
        data = {"x": [1, 2, 3]}

        with pytest.raises(ValueError, match="requires 'x' and 'y' data"):
            generate_line_plot(data, output_dir=output_dir)

    def test_mismatched_lengths_raises_error(self, output_dir):
        """Should raise error when x and y have different lengths."""
        data = {"x": [1, 2, 3], "y": [4, 5]}

        with pytest.raises(ValueError, match="must have same length"):
            generate_line_plot(data, output_dir=output_dir)

    def test_creates_output_directory(self, tmp_path):
        """Should create output directory if it doesn't exist."""
        output_dir = str(tmp_path / "new" / "nested" / "dir")
        data = {"x": [1, 2], "y": [3, 4]}

        result = generate_line_plot(data, output_dir=output_dir)

        assert Path(result.path).exists()
        assert Path(output_dir).is_dir()


# =============================================================================
# Scatter Plot Tests
# =============================================================================


class TestGenerateScatterPlot:
    """Tests for generate_scatter_plot function."""

    def test_generates_scatter_plot(self, scatter_data, output_dir):
        """Should generate a scatter plot successfully."""
        result = generate_scatter_plot(scatter_data, output_dir=output_dir)

        assert isinstance(result, PlotResult)
        assert result.plot_type == "scatter"
        assert result.data_points == 5
        assert Path(result.path).exists()

    def test_includes_point_labels(self, scatter_data, output_dir):
        """Should include point labels in scatter plot."""
        result = generate_scatter_plot(scatter_data, output_dir=output_dir)

        assert Path(result.path).exists()

    def test_works_without_labels(self, output_dir):
        """Should work without point labels."""
        data = {"x": [1, 2, 3], "y": [4, 5, 6]}

        result = generate_scatter_plot(data, output_dir=output_dir)

        assert result.plot_type == "scatter"
        assert Path(result.path).exists()

    def test_missing_x_raises_error(self, output_dir):
        """Should raise error when x data is missing."""
        data = {"y": [1, 2, 3]}

        with pytest.raises(ValueError, match="requires 'x' and 'y' data"):
            generate_scatter_plot(data, output_dir=output_dir)

    def test_missing_y_raises_error(self, output_dir):
        """Should raise error when y data is missing."""
        data = {"x": [1, 2, 3]}

        with pytest.raises(ValueError, match="requires 'x' and 'y' data"):
            generate_scatter_plot(data, output_dir=output_dir)


# =============================================================================
# Bar Chart Tests
# =============================================================================


class TestGenerateBarChart:
    """Tests for generate_bar_chart function."""

    def test_generates_bar_chart(self, bar_data, output_dir):
        """Should generate a bar chart successfully."""
        result = generate_bar_chart(bar_data, output_dir=output_dir)

        assert isinstance(result, PlotResult)
        assert result.plot_type == "bar"
        assert result.data_points == 4
        assert Path(result.path).exists()

    def test_includes_title_and_labels(self, bar_data, output_dir):
        """Should include title and labels in bar chart."""
        result = generate_bar_chart(
            bar_data,
            title="Monthly Sales",
            x_label="Month",
            y_label="Revenue",
            output_dir=output_dir,
        )

        assert Path(result.path).exists()

    def test_missing_categories_raises_error(self, output_dir):
        """Should raise error when categories are missing."""
        data = {"values": [100, 200]}

        with pytest.raises(ValueError, match="requires 'categories' and 'values'"):
            generate_bar_chart(data, output_dir=output_dir)

    def test_missing_values_raises_error(self, output_dir):
        """Should raise error when values are missing."""
        data = {"categories": ["A", "B"]}

        with pytest.raises(ValueError, match="requires 'categories' and 'values'"):
            generate_bar_chart(data, output_dir=output_dir)

    def test_mismatched_lengths_raises_error(self, output_dir):
        """Should raise error when categories and values have different lengths."""
        data = {"categories": ["A", "B", "C"], "values": [100, 200]}

        with pytest.raises(ValueError, match="must have same length"):
            generate_bar_chart(data, output_dir=output_dir)

    def test_many_categories_rotates_labels(self, output_dir):
        """Should handle many categories (rotation should occur)."""
        data = {
            "categories": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul"],
            "values": [100, 150, 120, 180, 160, 200, 140],
        }

        result = generate_bar_chart(data, output_dir=output_dir)

        assert result.data_points == 7
        assert Path(result.path).exists()


# =============================================================================
# Auto Plot Type Detection Tests
# =============================================================================


class TestGeneratePlot:
    """Tests for generate_plot function with auto-detection."""

    def test_auto_detects_line_for_sequential_integers(self, output_dir):
        """Should auto-detect line plot for sequential integer x values."""
        data = {"x": [1, 2, 3, 4, 5], "y": [10, 20, 30, 40, 50]}

        result = generate_plot(data, plot_type="auto", output_dir=output_dir)

        assert result.plot_type == "line"

    def test_auto_detects_bar_for_categories(self, bar_data, output_dir):
        """Should auto-detect bar chart when categories and values present."""
        result = generate_plot(bar_data, plot_type="auto", output_dir=output_dir)

        assert result.plot_type == "bar"

    def test_auto_detects_scatter_for_many_points(self, output_dir):
        """Should auto-detect scatter for large datasets."""
        import random

        random.seed(42)
        data = {
            "x": [random.random() * 100 for _ in range(25)],
            "y": [random.random() * 100 for _ in range(25)],
        }

        result = generate_plot(data, plot_type="auto", output_dir=output_dir)

        assert result.plot_type == "scatter"

    def test_explicit_line_type(self, line_data, output_dir):
        """Should use explicit line type."""
        result = generate_plot(line_data, plot_type="line", output_dir=output_dir)

        assert result.plot_type == "line"

    def test_explicit_scatter_type(self, scatter_data, output_dir):
        """Should use explicit scatter type."""
        # Remove labels to use x/y data
        data = {"x": scatter_data["x"], "y": scatter_data["y"]}

        result = generate_plot(data, plot_type="scatter", output_dir=output_dir)

        assert result.plot_type == "scatter"

    def test_explicit_bar_type(self, bar_data, output_dir):
        """Should use explicit bar type."""
        result = generate_plot(bar_data, plot_type="bar", output_dir=output_dir)

        assert result.plot_type == "bar"

    def test_unknown_plot_type_raises_error(self, line_data, output_dir):
        """Should raise error for unknown plot type."""
        with pytest.raises(ValueError, match="Unknown plot type"):
            generate_plot(line_data, plot_type="invalid", output_dir=output_dir)


# =============================================================================
# Determinism Tests
# =============================================================================


class TestDeterminism:
    """Tests for deterministic plot generation."""

    def test_same_data_produces_same_file(self, line_data, output_dir):
        """Same data should produce the same filename."""
        result1 = generate_line_plot(line_data, output_dir=output_dir)
        result2 = generate_line_plot(line_data, output_dir=output_dir)

        # Filenames should be identical
        assert Path(result1.path).name == Path(result2.path).name

    def test_same_data_produces_identical_content(self, line_data, output_dir):
        """Same data should produce identical file content."""
        # Generate first plot
        result1 = generate_line_plot(line_data, output_dir=output_dir)

        # Read content
        with open(result1.path, "rb") as f:
            content1 = f.read()

        # Delete and regenerate
        os.remove(result1.path)
        result2 = generate_line_plot(line_data, output_dir=output_dir)

        with open(result2.path, "rb") as f:
            content2 = f.read()

        # Content should be identical
        assert content1 == content2


# =============================================================================
# Integration Tests
# =============================================================================


class TestPlotIntegration:
    """Integration tests for plot generation."""

    def test_default_output_directory(self, tmp_path, monkeypatch):
        """Should use default output directory when not specified."""
        # Change to temp directory
        monkeypatch.chdir(tmp_path)

        data = {"x": [1, 2, 3], "y": [4, 5, 6]}
        result = generate_line_plot(data)

        assert "_drspec/plots" in result.path
        assert Path(result.path).exists()

    def test_complex_data(self, output_dir):
        """Should handle complex data correctly."""
        data = {
            "x": list(range(100)),
            "y": [x**2 for x in range(100)],
            "series_name": "Quadratic",
        }

        result = generate_line_plot(
            data,
            title="Quadratic Function",
            x_label="x",
            y_label="x²",
            output_dir=output_dir,
        )

        assert result.data_points == 100
        assert Path(result.path).exists()

    def test_float_data(self, output_dir):
        """Should handle float data correctly."""
        data = {
            "x": [0.1, 0.2, 0.3, 0.4, 0.5],
            "y": [1.1, 2.2, 3.3, 4.4, 5.5],
        }

        result = generate_scatter_plot(data, output_dir=output_dir)

        assert result.data_points == 5
        assert Path(result.path).exists()

    def test_negative_values(self, output_dir):
        """Should handle negative values correctly."""
        data = {
            "categories": ["Loss", "Break-even", "Profit"],
            "values": [-100, 0, 150],
        }

        result = generate_bar_chart(data, output_dir=output_dir)

        assert result.data_points == 3
        assert Path(result.path).exists()
