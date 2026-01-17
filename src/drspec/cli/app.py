"""Typer CLI application for DrSpec."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import typer

from drspec import __version__
from drspec.cli.output import error_response, output, success_response
from drspec.cli.commands import (
    contract,
    deps,
    init,
    learn,
    queue,
    scan,
    source,
    status,
    verify,
    vision,
)

# Global state for CLI options
class CLIContext:
    """Global CLI context for storing options."""

    def __init__(self) -> None:
        self.json_output: bool = True  # Default to JSON for agent-first design
        self.pretty: bool = False
        self.db_path: Optional[Path] = None


# Global context instance
ctx_data = CLIContext()

app = typer.Typer(
    name="drspec",
    help="AI-powered design-by-contract specification tool for runtime verification",
    no_args_is_help=True,
)

# Register subcommand groups
app.add_typer(init.app, name="init")
app.add_typer(scan.app, name="scan")
app.add_typer(status.app, name="status")
app.add_typer(queue.app, name="queue")
app.add_typer(contract.app, name="contract")
app.add_typer(source.app, name="source")
app.add_typer(verify.app, name="verify")
app.add_typer(deps.app, name="deps")
app.add_typer(learn.app, name="learn")
app.add_typer(vision.app, name="vision")


def version_callback(value: bool) -> None:
    """Print version and exit."""
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        "-v",
        help="Show version and exit",
        callback=version_callback,
        is_eager=True,
    ),
    json_output: bool = typer.Option(
        True,
        "--json/--no-json",
        help="Output in JSON format (default: enabled for agent compatibility)",
    ),
    pretty: bool = typer.Option(
        False,
        "--pretty",
        "-p",
        help="Enable human-readable pretty output",
    ),
    db_path: Optional[Path] = typer.Option(
        None,
        "--db",
        help="Override database path (default: ./_drspec/contracts.db)",
    ),
) -> None:
    """DrSpec - AI-powered design-by-contract specification tool.

    All commands output JSON by default for AI agent compatibility.
    Use --pretty for human-readable output.
    """
    # Store global options in context
    ctx_data.json_output = json_output and not pretty
    ctx_data.pretty = pretty
    ctx_data.db_path = db_path

    # Make context data available to subcommands
    ctx.ensure_object(dict)
    ctx.obj["json_output"] = ctx_data.json_output
    ctx.obj["pretty"] = ctx_data.pretty
    ctx.obj["db_path"] = ctx_data.db_path


def output_response(response: dict[str, Any]) -> None:
    """Output a response using the current global settings.

    This is the main function commands should use to output responses.
    It respects the global --json and --pretty flags.

    Args:
        response: Response dict from success_response() or error_response().
    """
    output(response, json_output=ctx_data.json_output, pretty=ctx_data.pretty)


def output_success(data: dict[str, Any]) -> None:
    """Output a success response using global settings.

    Args:
        data: Response data payload.
    """
    output_response(success_response(data))


def output_error(code: str, message: str, details: Optional[dict[str, Any]] = None) -> None:
    """Output an error response using global settings.

    Args:
        code: Error code (SCREAMING_SNAKE_CASE).
        message: Human-readable error message.
        details: Optional additional error details.
    """
    output_response(error_response(code, message, details))
