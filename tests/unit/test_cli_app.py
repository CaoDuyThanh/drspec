"""Tests for CLI application."""

from typer.testing import CliRunner

from drspec import __version__
from drspec.cli.app import app


runner = CliRunner()


def test_app_exists():
    assert app is not None


def test_version_flag():
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_version_short_flag():
    result = runner.invoke(app, ["-v"])

    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_no_args_shows_help():
    result = runner.invoke(app, [])

    # no_args_is_help can exit 0 or 2 depending on standalone_mode
    assert result.exit_code in (0, 2), f"Unexpected exit code: {result.exit_code}"
    assert "Usage" in result.stdout or "drspec" in result.stdout.lower()
