"""Tests for CLI application."""

from typer.testing import CliRunner

from drspec import __version__
from drspec.cli.app import app


runner = CliRunner()


def test_app_exists():
    """Test that the Typer app is defined."""
    assert app is not None


def test_version_flag():
    """Test --version flag returns version string."""
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_version_short_flag():
    """Test -v flag returns version string."""
    result = runner.invoke(app, ["-v"])

    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_no_args_shows_help():
    """Test that running with no args shows help."""
    result = runner.invoke(app, [])

    # Typer's no_args_is_help=True returns exit code 0 (help shown)
    # but standalone_mode=True may return 2 for no command
    assert result.exit_code in (0, 2), f"Unexpected exit code: {result.exit_code}"
    assert "Usage" in result.stdout or "drspec" in result.stdout.lower()
