"""Worker command - control the background staleness-resolution worker.

`run` is launched internally by `drspec scan` on completion; it's not
meant to be invoked by hand, but stays a documented command so someone
debugging a missing worker can run it directly. `status`/`stop` exist
so this background process isn't invisible and uncontrollable.
"""

from __future__ import annotations

from pathlib import Path

import typer

from drspec.db.migration import MigrationError
from drspec.cli.output import ErrorCode, error_response, output, success_response
from drspec.staleness_worker import get_worker_status, run_worker, stop_worker

app = typer.Typer(
    name="worker",
    help="Control the background staleness-resolution worker",
)


def _resolve_db_path(ctx: typer.Context) -> Path:
    cli_ctx = ctx.obj or {}
    db_path_override = cli_ctx.get("db_path")
    return db_path_override or (Path.cwd() / "_drspec" / "contracts.db")


@app.command()
def run(ctx: typer.Context) -> None:
    """Run the staleness worker in the foreground, blocking until it
    idles out or is stopped. Normally launched automatically by
    `drspec scan`; run directly only to debug it.
    """
    db_path = _resolve_db_path(ctx)
    try:
        run_worker(db_path)
    except MigrationError as e:
        typer.echo(f"The worker did not start: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def embed(ctx: typer.Context) -> None:
    """Embed what the worker says is owed, at low priority, then exit.
    Started by the worker itself when there is a backlog; not meant to be
    run by hand.
    """
    from drspec.core.embedding_runner import run_embedding_process
    from drspec.worker_ipc import connect_loopback

    from drspec.core import tracing

    db_path = _resolve_db_path(ctx)
    tracing.init_process("embed", honor_env=False)
    tracing.bind(db_path)
    connection = connect_loopback(db_path)
    try:
        code = run_embedding_process(connection)
    finally:
        connection.close()
    raise typer.Exit(code)


@app.command()
def status(ctx: typer.Context) -> None:
    """Report whether the worker is running and how many files are
    currently flagged as possibly stale.
    """
    cli_ctx = ctx.obj or {}
    json_output = cli_ctx.get("json_output", False)
    db_path = _resolve_db_path(ctx)

    output(success_response(get_worker_status(db_path)), json_output=json_output)


@app.command()
def stop(ctx: typer.Context) -> None:
    """Stop the running worker, if there is one. A clean, explicit
    alternative to killing the PID by hand.
    """
    cli_ctx = ctx.obj or {}
    json_output = cli_ctx.get("json_output", False)
    db_path = _resolve_db_path(ctx)

    try:
        pid, exited = stop_worker(db_path)
    except OSError as e:
        response = error_response(ErrorCode.EXECUTION_ERROR, f"Could not stop worker: {e}")
        output(response, json_output=json_output)
        raise typer.Exit(1)

    if pid is None:
        output(success_response({"stopped": False, "message": "No worker is running."}), json_output=json_output)
        return
    if not exited:
        response = error_response(ErrorCode.EXECUTION_ERROR, f"Worker (pid {pid}) was signaled but is still running.")
        output(response, json_output=json_output)
        raise typer.Exit(1)

    output(success_response({"stopped": True, "pid": pid}), json_output=json_output)
