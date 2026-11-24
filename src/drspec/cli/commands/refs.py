"""Refs command - who reads and writes a member (a field of a class)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from drspec.cli.output import ErrorCode, error_response, output, success_response
from drspec.worker_ipc import WorkerIPCError, get_worker_connection

app = typer.Typer(name="refs", help="Who reads and writes a member (a field of a class)", no_args_is_help=True)


@app.command("get")
def refs_get_command(
    ctx: typer.Context,
    symbol: str = typer.Argument(..., help="The member: `Owner::member` (for example AvoidanceResult::data), or a member name alone (every class that has it is listed)"),
    kind: Optional[str] = typer.Option(None, "--kind", help="Only the accesses that read (`read`) or that write (`write`); a `+=` or `++` is both."),
    context: int = typer.Option(0, "--context", help="Lines of code around each access, inside its function.", min=0),
    limit: int = typer.Option(50, "--limit", help="Maximum accesses to return (and of unresolved functions or files listed).", min=1),
    unresolved: str = typer.Option("files", "--unresolved", help="The unresolved accesses: `files` (counted by file, the default), `full` (each function with its lines) or `count` (only the numbers)."),
    format: str = typer.Option("text", "--format", help="`text` (labelled plain text) or `json` (strict JSON, as the global --json)."),
) -> None:
    """List the functions that read or write a member, with the kind, the line and the line of code of each access.

    C++ first. The accesses whose receiver's type is not known are counted and named in `unresolved`: an empty or short
    list is not "nobody uses it".

    Examples:
        drspec refs get AvoidanceResult::data
        drspec refs get data --kind write
        drspec refs get AvoidanceResult::data --kind read --context 3
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
    from drspec.core.refs import get_refs

    try:
        data = get_refs(conn, symbol, kind=kind, context=context, limit=limit, unresolved=unresolved, root=db_path.parent.parent)
    except ValueError as e:
        output(error_response(ErrorCode.INVALID_INPUT, str(e)), json_output=json_output)
        raise typer.Exit(1)
    finally:
        conn.close()
    output(success_response(data), json_output=json_output)
