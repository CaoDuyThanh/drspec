"""Typer CLI application for DrSpec."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Optional

import click
import typer

from drspec import __version__
from drspec.core import tracing
from drspec.cli.output import error_response, output, success_response

# Global state for CLI options
class CLIContext:
    """Global CLI context for storing options."""

    def __init__(self) -> None:
        self.json_output: bool = False  # Default: labeled plain text, not JSON
        self.db_path: Optional[Path] = None


# Global context instance
ctx_data = CLIContext()

# The commands, in the order `--help` lists them: (module, what it holds, help text of a command). A command module is imported
# when its command runs (or when `--help` lists them all), not at start-up: a version print or a `config get` does not
# import the graph viewer (matplotlib), the contract models, the parsers or the embedding code. `holds` is the Typer app of a
# group of commands, or the function of a command that is registered directly so `--flag` can follow its positional
# argument (a nested Typer sub-app is a Click Group, which stops accepting its own options after the first positional).
_COMMANDS: dict[str, tuple[str, str, Optional[str]]] = {
    "scan": ("scan", "scan_command", "Scan source files and extract function signatures"),
    "search": ("search", "search_command", "Search functions by name/signature/body, grep-flag-compatible"),
    "serve": ("serve", "serve_command", "Start a local, read-only, live-refreshing graph viewer"),
    "ack": ("ack", "ack_command", "Mark a document section as reviewed: the document is still right"),
    "mcp-serve": ("mcp_serve", "mcp_serve_command", "Start the MCP stdio server for AI agent clients (e.g. .mcp.json) — design.md §18"),
    "init": ("init", "app", None),
    "status": ("status", "app", None),
    "config": ("config", "app", None),
    "contract": ("contract", "app", None),
    "source": ("source", "app", None),
    "verify": ("verify", "app", None),
    "deps": ("deps", "app", None),
    "refs": ("refs", "app", None),
    "strings": ("strings", "app", None),
    "learn": ("learn", "app", None),
    "vision": ("vision", "app", None),
    "worker": ("worker", "app", None),
    "trace": ("trace", "app", None),
}


def _load_command(name: str) -> click.Command:
    """The Click command of `name`, made from its module (imported now)."""
    import importlib

    from typer.main import get_command

    module_name, attribute, help_text = _COMMANDS[name]
    target = getattr(importlib.import_module(f"drspec.cli.commands.{module_name}"), attribute)
    holder = typer.Typer()

    @holder.callback()
    def _holder() -> None:  # (keeps the holder a group, whatever it holds)
        pass

    if help_text is None:
        holder.add_typer(target, name=name)
    else:
        holder.command(name=name, help=help_text)(target)
    return get_command(holder).commands[name]  # type: ignore[attr-defined]


class _LazyCommands(typer.core.TyperGroup):
    """The root group: its commands are loaded from their modules the first time one is asked for."""

    def list_commands(self, ctx: click.Context) -> list[str]:
        return list(_COMMANDS)

    def get_command(self, ctx: click.Context, cmd_name: str) -> Optional[click.Command]:
        if cmd_name in _COMMANDS:
            if cmd_name not in self.commands:
                self.commands[cmd_name] = _load_command(cmd_name)
            return self.commands[cmd_name]
        return super().get_command(ctx, cmd_name)


app = typer.Typer(
    name="drspec",
    help="AI-powered design-by-contract specification tool for runtime verification",
    no_args_is_help=True,
    cls=_LazyCommands,
)


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
        False,
        "--json/--no-json",
        help="Output compact JSON instead of the default labeled plain text — for scripts/automation.",
    ),
    db_path: Optional[Path] = typer.Option(
        None,
        "--db",
        help="Override database path (default: ./_drspec/contracts.db)",
    ),
    trace_all: bool = typer.Option(
        False,
        "--trace",
        help="Trace this command to _drspec/trace.log (same as --trace-level all).",
    ),
    trace_level: Optional[str] = typer.Option(
        None,
        "--trace-level",
        help="Trace this command at off, slow or all. DRSPEC_TRACE does the same through the environment.",
    ),
) -> None:
    """DrSpec - AI-powered design-by-contract specification tool.

    Commands output labeled plain text by default; use --json for
    machine-parseable output.
    """
    ctx_data.json_output = json_output
    ctx_data.db_path = db_path

    ctx.ensure_object(dict)
    ctx.obj["json_output"] = ctx_data.json_output
    ctx.obj["db_path"] = ctx_data.db_path

    _start_command_trace(ctx, db_path, trace_all, trace_level)


# Commands that run for a long time or are not clients of the worker's data:
# a single `command` event for them would only measure how long they stayed up.
_NO_COMMAND_EVENT = {"mcp-serve", "serve", "worker run", "worker embed", "trace summary", "trace tail", "trace clear"}


def _command_name(root: click.Command, argv: list[str]) -> tuple[str, list[str]]:
    """The command path typed (`deps get`) and the option names used, never their values."""
    words: list[str] = []
    flags: list[str] = []
    node: click.Command = root
    skip = False
    for token in argv:
        if skip:
            skip = False
            continue
        if token.startswith("-"):
            name = token.split("=", 1)[0]
            if words:  # global options (--json, --trace, --db) are not the command's own
                flags.append(name)
            if name in ("--db", "--trace-level") and "=" not in token:
                skip = True
            continue
        child = node.get_command(click.Context(node), token) if isinstance(node, click.Group) else None  # (a lazy command is loaded here)
        if child is not None:
            node = child
            words.append(token)
    return " ".join(words), sorted(set(flags))


def _first_positional(argv: list[str], after: str) -> Optional[str]:
    seen = False
    for token in argv:
        if seen and not token.startswith("-"):
            return token
        if token == after:
            seen = True
    return None


def _start_command_trace(ctx: typer.Context, db_path: Optional[Path], trace_all: bool, trace_level: Optional[str]) -> None:
    tracing.init_process("cli", honor_env=True)
    if trace_all:
        tracing.set_level("all")
    if trace_level is not None:
        chosen = tracing.parse_level(trace_level)
        if chosen is None:
            raise typer.BadParameter(f"must be off, slow or all, got {trace_level!r}", param_hint="--trace-level")
        tracing.set_level(chosen)
    tracing.bind(db_path or Path.cwd() / "_drspec" / "contracts.db")
    started = time.perf_counter()
    # Click empties the context's own argument list before a group callback runs.
    argv = sys.argv[1:]

    def finish() -> None:
        if not tracing.enabled():
            return
        try:
            command, flags = _command_name(ctx.command, argv)
            if command in _NO_COMMAND_EVENT:
                return
            totals = tracing.take_totals()
            result = tracing.take_result()
            fields: dict[str, Any] = {
                "command": command,
                "args": flags,
                "status": result.get("status", "unknown"),
                "out_bytes": result.get("out_bytes"),
                "connect_ms": round(totals.get("connect_ms", 0.0), 3),
                "worker_started": bool(totals.get("worker_starts")),
                "ipc_ms": round(totals.get("ipc_ms", 0.0), 3),
                "ipc_calls": int(totals.get("ipc_calls", 0)),
            }
            if command == "search" and tracing.level() == "all":
                pattern = _first_positional(argv, "search")
                if pattern is not None:
                    # Only the size and the kind of search: what was typed may name project data.
                    fields["pattern_length"] = len(pattern)
                    fields["pattern_mode"] = "semantic" if "--semantic" in flags else "regex" if ("--regex" in flags or "-E" in flags) else "literal"
            tracing.emit("command", (time.perf_counter() - started) * 1000.0, cid=tracing.current_cid(), **fields, **tracing.process_usage())
        except Exception:  # noqa: BLE001 - tracing must never affect a command
            pass

    ctx.call_on_close(finish)


def output_response(response: dict[str, Any]) -> None:
    """Output a response using the current global settings.

    This is the main function commands should use to output responses.
    It respects the global --json flag.

    Args:
        response: Response dict from success_response() or error_response().
    """
    output(response, json_output=ctx_data.json_output)


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
