"""Status command - Show DrSpec project status."""

from __future__ import annotations

from pathlib import Path

import typer

from drspec.cli.output import ErrorCode, error_response, output, success_response
from drspec.db import (
    count_artifacts,
    count_contracts,
    get_connection,
    get_contract_confidence_stats,
    queue_count,
    VALID_ARTIFACT_STATUSES,
    VALID_QUEUE_STATUSES,
)

app = typer.Typer(
    name="status",
    help="Show DrSpec project and queue status",
    no_args_is_help=False,
)


@app.callback(invoke_without_command=True)
def status_command(
    ctx: typer.Context,
) -> None:
    """Display DrSpec project status.

    Shows artifact statistics, queue size, contract counts, and
    confidence distribution.

    Examples:
        drspec status              # Show full status
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
        # Gather artifact statistics
        artifact_total = count_artifacts(conn)
        artifact_by_status = {}
        for status in VALID_ARTIFACT_STATUSES:
            artifact_by_status[status] = count_artifacts(conn, status=status)

        # Gather queue statistics
        queue_total = queue_count(conn)
        queue_by_status = {}
        for status in VALID_QUEUE_STATUSES:
            queue_by_status[status] = queue_count(conn, status=status)

        # Gather contract statistics
        contract_total = count_contracts(conn)
        confidence_stats = get_contract_confidence_stats(conn)

        # Calculate items needing attention
        items_pending = artifact_by_status.get("PENDING", 0)
        items_broken = artifact_by_status.get("BROKEN", 0)
        items_needs_review = artifact_by_status.get("NEEDS_REVIEW", 0)
        items_stale = artifact_by_status.get("STALE", 0)
        items_needing_attention = items_pending + items_broken + items_needs_review + items_stale

        # Build response data
        data = {
            "artifacts": {
                "total": artifact_total,
                "by_status": artifact_by_status,
            },
            "queue": {
                "total": queue_total,
                "by_status": queue_by_status,
            },
            "contracts": {
                "total": contract_total,
                "confidence": confidence_stats,
            },
            "summary": {
                "items_needing_attention": items_needing_attention,
                "completion_rate": round(
                    artifact_by_status.get("VERIFIED", 0) / artifact_total * 100, 1
                ) if artifact_total > 0 else 0.0,
            },
        }

        response = success_response(data)
        output(response, json_output=json_output, pretty=pretty)

    except Exception as e:
        response = error_response(
            ErrorCode.STATUS_ERROR,
            f"Error getting status: {str(e)}",
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)
    finally:
        conn.close()
