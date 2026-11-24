"""Trace command - read the optional trace of what drspec did.

The trace itself is switched on with `drspec config set trace.level slow|all`
(for the background worker) or `--trace` / `DRSPEC_TRACE` (for one command).
The log is kept outside the project, one file per project (`trace path` prints
where). These subcommands only read it, so they never touch the database or
start the worker.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from drspec.cli.output import ErrorCode, error_response, output, success_response
from drspec.core import tracing
from drspec.core.trace_report import TraceError, clear_trace, trace_summary, trace_tail

app = typer.Typer(
    name="trace",
    help="Read the optional trace of what drspec did (turn it on with: drspec config set trace.level all)",
    no_args_is_help=True,
)


def _context(ctx: typer.Context) -> tuple[Path, bool]:
    cli_ctx = ctx.obj or {}
    db_path = cli_ctx.get("db_path") or (Path.cwd() / "_drspec" / "contracts.db")
    return Path(db_path), cli_ctx.get("json_output", False)


@app.command("summary")
def summary_command(
    ctx: typer.Context,
    since: Optional[str] = typer.Option(None, "--since", help="Only events from the last period, such as 30m, 1h or 2d."),
    slowest: int = typer.Option(20, "--slowest", min=0, help="How many of the slowest events to list."),
    cid: Optional[str] = typer.Option(None, "--cid", help="Print one command's whole path instead (its correlation id)."),
) -> None:
    """Count, total, median, 95th percentile and maximum per event, then the
    slowest events with their correlation ids.

    Examples:
        drspec trace summary --since 1h
        drspec trace summary --cid 3fa9c1d2
    """
    db_path, json_output = _context(ctx)
    try:
        data = trace_summary(db_path, since=since, slowest=slowest, cid=cid)
    except TraceError as e:
        output(error_response(ErrorCode.INVALID_INPUT, str(e)), json_output=json_output)
        raise typer.Exit(1)
    output(success_response(data), json_output=json_output)


@app.command("tail")
def tail_command(
    ctx: typer.Context,
    count: int = typer.Option(20, "-n", "--lines", min=0, help="How many of the latest events to show."),
) -> None:
    """Show the latest events."""
    db_path, json_output = _context(ctx)
    output(success_response(trace_tail(db_path, count)), json_output=json_output)


@app.command("clear")
def clear_command(
    ctx: typer.Context,
    everything: bool = typer.Option(False, "--all", help="Empty the logs of every project, not only this one."),
) -> None:
    """Empty this project's trace log (and remove one an older version left in the project)."""
    db_path, json_output = _context(ctx)
    data = clear_trace(db_path, everything=everything)
    # The retention settings live in the database, which this command does not open (the worker may hold
    # it); the worker applies the configured numbers at its start, this applies the defaults.
    data["logs_deleted"] = tracing.prune_logs(tracing.DEFAULT_KEEP_DAYS, tracing.DEFAULT_MAX_TOTAL_MB, in_use=tracing.log_file_for(db_path))
    output(success_response(data), json_output=json_output)


@app.command("path")
def path_command(ctx: typer.Context) -> None:
    """Print where this project's trace log is (it does not exist until tracing writes to it)."""
    db_path, json_output = _context(ctx)
    log = tracing.log_file_for(db_path)
    output(success_response({"path": str(log), "exists": log.exists(), "folder": str(log.parent)}), json_output=json_output)


@app.command("list")
def list_command(ctx: typer.Context) -> None:
    """List every project's trace log in the folder: its name, size and time of the last event.
    A log holds no project path, so a log is matched to its project with `trace path`."""
    _db_path, json_output = _context(ctx)
    output(success_response({"folder": str(tracing.default_trace_dir()), "logs": tracing.list_logs()}), json_output=json_output)
