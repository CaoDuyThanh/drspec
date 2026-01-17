"""Queue command - Manage the processing queue."""

from __future__ import annotations

from pathlib import Path

import typer

from drspec.cli.output import ErrorCode, error_response, output, success_response
from drspec.cli.validators import validate_function_id
from drspec.db import (
    get_connection,
    queue_count,
    queue_get,
    queue_peek,
    queue_pop,
    queue_prioritize,
)

app = typer.Typer(
    name="queue",
    help="Manage the function processing queue",
)


@app.command(name="next")
def next_item(
    ctx: typer.Context,
) -> None:
    """Get the next function from the queue for processing.

    Returns the highest priority unprocessed function and marks it as PROCESSING.

    Examples:
        drspec queue next
    """
    # Get CLI context
    cli_ctx = ctx.obj or {}
    json_output = cli_ctx.get("json_output", True)
    pretty = cli_ctx.get("pretty", False)
    db_path_override = cli_ctx.get("db_path")

    # Check if database exists
    drspec_dir = Path.cwd() / "_drspec"
    if db_path_override:
        db_path = db_path_override
    else:
        db_path = drspec_dir / "contracts.db"

    if not db_path.parent.exists():
        response = error_response(
            ErrorCode.DB_NOT_INITIALIZED,
            "DrSpec not initialized. Run 'drspec init' first.",
            {"expected_path": str(drspec_dir)},
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)

    try:
        conn = get_connection(db_path)
    except FileNotFoundError:
        response = error_response(
            ErrorCode.DB_NOT_INITIALIZED,
            "DrSpec not initialized. Run 'drspec init' first.",
            {"db_path": str(db_path)},
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)

    try:
        # Pop the next item from the queue
        item = queue_pop(conn)

        if item is None:
            response = error_response(
                ErrorCode.QUEUE_EMPTY,
                "No pending items in the processing queue",
                {"suggestion": "Run 'drspec scan' to add functions to the queue"},
            )
            output(response, json_output=json_output, pretty=pretty)
            raise typer.Exit(1)

        # Build response
        data = {
            "function_id": item.function_id,
            "priority": item.priority,
            "status": item.status,
            "reason": item.reason,
            "attempts": item.attempts,
        }

        response = success_response(data)
        output(response, json_output=json_output, pretty=pretty)

    except typer.Exit:
        raise
    except Exception as e:
        response = error_response(
            ErrorCode.INTERNAL_ERROR,
            f"Error getting next queue item: {str(e)}",
            {},
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)
    finally:
        conn.close()


@app.command()
def peek(
    ctx: typer.Context,
    limit: int = typer.Option(
        10,
        "--limit",
        "-n",
        help="Number of items to peek",
        min=1,
        max=100,
    ),
) -> None:
    """Peek at upcoming items in the queue without consuming them.

    Shows the next N items that would be processed.

    Examples:
        drspec queue peek
        drspec queue peek --limit 20
    """
    # Get CLI context
    cli_ctx = ctx.obj or {}
    json_output = cli_ctx.get("json_output", True)
    pretty = cli_ctx.get("pretty", False)
    db_path_override = cli_ctx.get("db_path")

    # Check if database exists
    drspec_dir = Path.cwd() / "_drspec"
    if db_path_override:
        db_path = db_path_override
    else:
        db_path = drspec_dir / "contracts.db"

    if not db_path.parent.exists():
        response = error_response(
            ErrorCode.DB_NOT_INITIALIZED,
            "DrSpec not initialized. Run 'drspec init' first.",
            {"expected_path": str(drspec_dir)},
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)

    try:
        conn = get_connection(db_path)
    except FileNotFoundError:
        response = error_response(
            ErrorCode.DB_NOT_INITIALIZED,
            "DrSpec not initialized. Run 'drspec init' first.",
            {"db_path": str(db_path)},
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)

    try:
        # Peek at queue items
        items = queue_peek(conn, count=limit)

        # Get total pending count
        total_pending = queue_count(conn, status="PENDING")

        # Build response
        item_list = []
        for item in items:
            item_list.append({
                "function_id": item.function_id,
                "priority": item.priority,
                "status": item.status,
                "queued_at": item.created_at.isoformat() if item.created_at else None,
            })

        data = {
            "items": item_list,
            "total_pending": total_pending,
        }

        response = success_response(data)
        output(response, json_output=json_output, pretty=pretty)

    except typer.Exit:
        raise
    except Exception as e:
        response = error_response(
            ErrorCode.INTERNAL_ERROR,
            f"Error peeking queue: {str(e)}",
            {},
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)
    finally:
        conn.close()


@app.command()
def get(
    ctx: typer.Context,
    function_id: str = typer.Argument(
        ...,
        help="Function ID (format: filepath::function_name)",
    ),
) -> None:
    """Get queue status for a specific function.

    Returns the queue item details including status, priority, and attempts.

    Examples:
        drspec queue get src/payments/reconcile.py::reconcile_transactions
    """
    # Get CLI context
    cli_ctx = ctx.obj or {}
    json_output = cli_ctx.get("json_output", True)
    pretty = cli_ctx.get("pretty", False)
    db_path_override = cli_ctx.get("db_path")

    # Check if database exists
    drspec_dir = Path.cwd() / "_drspec"
    if db_path_override:
        db_path = db_path_override
    else:
        db_path = drspec_dir / "contracts.db"

    if not db_path.parent.exists():
        response = error_response(
            ErrorCode.DB_NOT_INITIALIZED,
            "DrSpec not initialized. Run 'drspec init' first.",
            {"expected_path": str(drspec_dir)},
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)

    # Validate function ID format
    is_valid, error_msg = validate_function_id(function_id)
    if not is_valid:
        response = error_response(
            ErrorCode.INVALID_FUNCTION_ID,
            error_msg,
            {"function_id": function_id, "expected_format": "filepath::function_name"},
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)

    try:
        conn = get_connection(db_path)
    except FileNotFoundError:
        response = error_response(
            ErrorCode.DB_NOT_INITIALIZED,
            "DrSpec not initialized. Run 'drspec init' first.",
            {"db_path": str(db_path)},
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)

    try:
        # Get queue item
        item = queue_get(conn, function_id)

        if item is None:
            response = error_response(
                ErrorCode.QUEUE_ITEM_NOT_FOUND,
                "Function not found in processing queue",
                {
                    "function_id": function_id,
                    "suggestion": "Run 'drspec scan' to discover functions",
                },
            )
            output(response, json_output=json_output, pretty=pretty)
            raise typer.Exit(1)

        # Build response
        data = {
            "function_id": item.function_id,
            "priority": item.priority,
            "status": item.status,
            "reason": item.reason,
            "attempts": item.attempts,
            "queued_at": item.created_at.isoformat() if item.created_at else None,
        }

        response = success_response(data)
        output(response, json_output=json_output, pretty=pretty)

    except typer.Exit:
        raise
    except Exception as e:
        response = error_response(
            ErrorCode.INTERNAL_ERROR,
            f"Error getting queue item: {str(e)}",
            {"function_id": function_id},
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)
    finally:
        conn.close()


@app.command()
def prioritize(
    ctx: typer.Context,
    function_id: str = typer.Argument(
        ...,
        help="Function ID to prioritize (format: filepath::function_name)",
    ),
    priority: int = typer.Argument(
        ...,
        help="New priority level (lower = higher priority)",
    ),
) -> None:
    """Update priority for a function in the queue.

    Lower priority values are processed first.

    Examples:
        drspec queue prioritize src/payments/reconcile.py::reconcile_transactions 1
        drspec queue prioritize src/utils.py::helper 100
    """
    # Get CLI context
    cli_ctx = ctx.obj or {}
    json_output = cli_ctx.get("json_output", True)
    pretty = cli_ctx.get("pretty", False)
    db_path_override = cli_ctx.get("db_path")

    # Check if database exists
    drspec_dir = Path.cwd() / "_drspec"
    if db_path_override:
        db_path = db_path_override
    else:
        db_path = drspec_dir / "contracts.db"

    if not db_path.parent.exists():
        response = error_response(
            ErrorCode.DB_NOT_INITIALIZED,
            "DrSpec not initialized. Run 'drspec init' first.",
            {"expected_path": str(drspec_dir)},
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)

    # Validate function ID format
    is_valid, error_msg = validate_function_id(function_id)
    if not is_valid:
        response = error_response(
            ErrorCode.INVALID_FUNCTION_ID,
            error_msg,
            {"function_id": function_id, "expected_format": "filepath::function_name"},
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)

    try:
        conn = get_connection(db_path)
    except FileNotFoundError:
        response = error_response(
            ErrorCode.DB_NOT_INITIALIZED,
            "DrSpec not initialized. Run 'drspec init' first.",
            {"db_path": str(db_path)},
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)

    try:
        # Get current item to retrieve old priority
        item = queue_get(conn, function_id)

        if item is None:
            response = error_response(
                ErrorCode.QUEUE_ITEM_NOT_FOUND,
                "Function not found in processing queue",
                {
                    "function_id": function_id,
                    "suggestion": "Run 'drspec scan' to discover functions",
                },
            )
            output(response, json_output=json_output, pretty=pretty)
            raise typer.Exit(1)

        old_priority = item.priority

        # Update priority
        queue_prioritize(conn, function_id, priority)

        # Build response
        data = {
            "function_id": function_id,
            "old_priority": old_priority,
            "new_priority": priority,
            "message": "Priority updated successfully",
        }

        response = success_response(data)
        output(response, json_output=json_output, pretty=pretty)

    except typer.Exit:
        raise
    except Exception as e:
        response = error_response(
            ErrorCode.INTERNAL_ERROR,
            f"Error updating priority: {str(e)}",
            {"function_id": function_id},
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)
    finally:
        conn.close()
