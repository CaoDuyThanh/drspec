"""Strings command - who sets a key of a YAML config file, and which code reads it by name."""

from __future__ import annotations

from pathlib import Path

import typer

from drspec.cli.output import ErrorCode, error_response, output, success_response
from drspec.worker_ipc import WorkerIPCError, get_worker_connection

app = typer.Typer(name="strings", help="Who sets a key of a YAML config file, and which code reads it", no_args_is_help=True)


@app.command("get")
def strings_get_command(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="The key path, joined with `/` (avoidance/timeout); the dot form (avoidance.timeout) and a leading `/` or `~/` are the same key. A key ending with `/` lists its sub-keys."),
    limit: int = typer.Option(50, "--limit", help="Maximum sites of each kind to return.", min=1),
    problems: bool = typer.Option(False, "--problems", help="List the yaml files that could not be read (otherwise only their count)."),
    format: str = typer.Option("text", "--format", help="`text` (labelled plain text) or `json` (strict JSON, as the global --json)."),
) -> None:
    """List the YAML files that set a key (with the line and the value) and the functions that read it (the line of code).

    Exact string matches only: a literal built at run time (`"avoidance/" + name`) and a key set through a launch file or a
    namespace are not found, and the answer says what it could not see.

    Examples:
        drspec strings get avoidance/timeout
        drspec strings get avoidance/
        drspec strings get avoidance.retry.count --limit 10
        drspec strings get robot/ --limit 100
        drspec strings get --problems
    """
    cli_ctx = ctx.obj or {}
    json_output = cli_ctx.get("json_output", False) or format == "json"
    if format not in ("text", "json"):
        output(error_response(ErrorCode.INVALID_INPUT, f"Invalid format: {format!r}. Must be text or json."), json_output=json_output)
        raise typer.Exit(1)
    db_path = cli_ctx.get("db_path") or Path.cwd() / "_drspec" / "contracts.db"
    if not db_path.parent.exists() or not db_path.exists():
        output(
            error_response(ErrorCode.DB_NOT_INITIALIZED, "DrSpec not initialized. Run 'drspec init' first.", {"expected_path": str(db_path.parent)}),
            json_output=json_output,
        )
        raise typer.Exit(1)
    try:
        conn = get_worker_connection(db_path, read_only=True)
    except WorkerIPCError as e:
        output(error_response(ErrorCode.DATABASE_BUSY, f"Could not reach the drspec worker: {e}", {"db_path": str(db_path)}), json_output=json_output)
        raise typer.Exit(1)
    from drspec.core.changed_notice import take_before
    from drspec.core.strings import add_strings_notice, get_strings

    try:
        before = take_before(conn)  # taken before the answer's reads (see changed_notice)
        data = get_strings(conn, key, limit=limit, root=db_path.parent.parent, problems=problems)
        add_strings_notice(conn, data, db_path.parent.parent, before=before)
    except ValueError as e:
        output(error_response(ErrorCode.INVALID_INPUT, str(e)), json_output=json_output)
        raise typer.Exit(1)
    finally:
        conn.close()
    output(success_response(data), json_output=json_output)
