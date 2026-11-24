"""Ack command - mark a document section as reviewed: the document is still right, whatever the code did."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from drspec.cli.output import ErrorCode, error_response, output, success_response
from drspec.cli.validators import resolve_or_exit
from drspec.core import docs as docs_core
from drspec.worker_ipc import WorkerIPCError, get_worker_connection


def ack_command(
    ctx: typer.Context,
    identifier: str = typer.Argument(..., help="Document section ID (`file.md::/Heading/Path/`; the #hash may be left out)."),
    function_id: Optional[str] = typer.Argument(None, help="Only this function's link; default: all of the section's."),
) -> None:
    """Mark a section as reviewed: the document is still right, whatever the code did.

    The section's links take the code as it is now, so it stops showing in
    'drspec search --status STALE --docs-only' until the code changes again.

    Examples:
        drspec ack docs/guide.md::/Guide/Widget/
        drspec ack docs/guide.md::/Guide/Widget/ src/widget.py::render
    """
    cli_ctx = ctx.obj or {}
    json_output = cli_ctx.get("json_output", False)
    db_path = cli_ctx.get("db_path") or (Path.cwd() / "_drspec" / "contracts.db")
    if not Path(db_path).exists():
        output(error_response(ErrorCode.DB_NOT_INITIALIZED, "DrSpec not initialized. Run 'drspec init' first.", {"expected_path": str(Path(db_path).parent)}), json_output=json_output)
        raise typer.Exit(1)
    try:
        conn = get_worker_connection(db_path)
    except WorkerIPCError as e:
        output(error_response(ErrorCode.DATABASE_BUSY, f"Could not reach the drspec worker: {e}", {"db_path": str(db_path)}), json_output=json_output)
        raise typer.Exit(1)
    try:
        target = docs_core.document_target(conn, identifier)
        if target is None or target[0] != "section":
            output(
                error_response(ErrorCode.INVALID_INPUT, f"No document section matches '{identifier}'. ack takes a section ID, as 'drspec search --docs-only' prints it.", {"identifier": identifier}),
                json_output=json_output,
            )
            raise typer.Exit(1)
        function = resolve_or_exit(conn, function_id, json_output) if function_id else None
        count = docs_core.acknowledge(conn, target[1], function)
    finally:
        conn.close()
    output(success_response({"section_id": target[1], "acknowledged": count}), json_output=json_output)
