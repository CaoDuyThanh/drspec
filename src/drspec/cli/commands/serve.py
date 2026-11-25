"""Serve command - live backend-connected graph viewer.

Registered directly on the main app, not as a nested Typer sub-app —
same reasoning as `scan`/`search`: one positional `function_id` plus
several flags is the shape that breaks a nested Click Group.
"""

from __future__ import annotations

from pathlib import Path

import typer

from drspec.cli.output import ErrorCode, error_response, output
from drspec.db import get_artifact
from drspec.server import DEFAULT_PORT, run_server
from drspec.cli.validators import resolve_or_exit
from drspec.worker_ipc import WorkerIPCError, get_worker_connection


def serve_command(
    ctx: typer.Context,
    function_id: str = typer.Argument(
        ...,
        help="Function ID to center the initial view on (re-center from inside the viewer afterward).",
    ),
    depth: int = typer.Option(
        2,
        "--depth",
        "-d",
        help="Initial dependency depth (default: 2) — changeable from inside the viewer.",
        min=1,
        max=5,
    ),
    direction: str = typer.Option(
        "both",
        "--direction",
        help="Initial direction: callers, callees, or both — changeable from inside the viewer.",
    ),
    port: int = typer.Option(
        DEFAULT_PORT,
        "--port",
        help=f"Port to bind on 127.0.0.1 (default: {DEFAULT_PORT}). Use 0 to let the OS pick a free one.",
    ),
) -> None:
    """Start a local, read-only, live graph viewer.

    Unlike 'drspec deps plot --html' (a static, one-shot snapshot), this
    starts a small HTTP server bound to 127.0.0.1 only, and the viewer
    page talks back to it: a Refresh button re-queries the DB's current
    state, and clicking "Center on this node" in a node's detail panel
    re-centers the view by re-querying — both without needing a new CLI
    invocation. Read-only: nothing here ever writes to the database.
    Blocks this terminal until Ctrl-C.

    Running 'drspec scan' in another terminal while this is up is safe —
    verified directly, not assumed: this server never holds a database
    connection open between requests, only for the brief moment of
    actually handling one, so a scan's own writes aren't blocked by it.

    Examples:
        drspec serve "src/app.py::main"
        drspec serve "src/app.py::main" --depth 3 --direction callers
        drspec serve "src/app.py::main" --port 9000
    """
    cli_ctx = ctx.obj or {}
    json_output = cli_ctx.get("json_output", False)
    db_path_override = cli_ctx.get("db_path")

    if direction not in ("callers", "callees", "both"):
        response = error_response(
            ErrorCode.INVALID_INPUT,
            f"Invalid --direction: {direction}. Must be one of callers, callees, both.",
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)

    drspec_dir = Path.cwd() / "_drspec"
    db_path = db_path_override or (drspec_dir / "contracts.db")

    if not db_path.parent.exists() or not db_path.exists():
        response = error_response(
            ErrorCode.DB_NOT_INITIALIZED,
            "DrSpec not initialized. Run 'drspec init' first.",
            {"expected_path": str(drspec_dir)},
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)

    try:
        conn = get_worker_connection(db_path, read_only=True)
    except WorkerIPCError as e:
        response = error_response(
            ErrorCode.DATABASE_BUSY,
            f"Could not reach the drspec worker: {e}",
            {"db_path": str(db_path)},
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)

    function_id = resolve_or_exit(conn, function_id, json_output)

    try:
        artifact = get_artifact(conn, function_id)
    finally:
        conn.close()

    if artifact is None:
        response = error_response(
            ErrorCode.FUNCTION_NOT_FOUND,
            f"Function not found: {function_id}",
            {"function_id": function_id},
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)

    try:
        run_server(db_path, function_id, depth=depth, direction=direction, port=port)
    except RuntimeError as e:
        response = error_response(ErrorCode.EXECUTION_ERROR, str(e))
        output(response, json_output=json_output)
        raise typer.Exit(1)
