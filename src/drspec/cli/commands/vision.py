"""Vision command - Manage vision analysis findings."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from drspec.cli.output import ErrorCode, error_response, output, success_response
from drspec.db import get_connection

app = typer.Typer(
    name="vision",
    help="Manage vision analysis findings",
)


@app.command()
def save(
    ctx: typer.Context,
    function_id: str = typer.Argument(
        ...,
        help="Function ID the finding relates to",
    ),
    finding_type: str = typer.Option(
        ...,
        "--type",
        "-t",
        help="Finding type: outlier, discontinuity, boundary, correlation, missing_pattern",
    ),
    significance: str = typer.Option(
        ...,
        "--significance",
        "-s",
        help="Significance: HIGH, MEDIUM, LOW",
    ),
    description: str = typer.Option(
        ...,
        "--description",
        "-d",
        help="Description of the finding",
    ),
    location: Optional[str] = typer.Option(
        None,
        "--location",
        "-l",
        help="Location in plot (e.g., 'x: 5-10')",
    ),
    invariant: Optional[str] = typer.Option(
        None,
        "--invariant",
        "-i",
        help="Suggested invariant implication",
    ),
    plot_path: Optional[str] = typer.Option(
        None,
        "--plot-path",
        "-p",
        help="Path to the plot image",
    ),
) -> None:
    """Save a vision analysis finding to the database.

    Examples:
        drspec vision save "src/foo.py::process" -t outlier -s HIGH -d "Unexpected spike at x=5"
        drspec vision save "src/foo.py::process" -t discontinuity -s MEDIUM -d "Gap in data" -l "x: 100-150"
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
        from drspec.db.queries import insert_vision_finding

        finding_id = insert_vision_finding(
            conn=conn,
            function_id=function_id,
            finding_type=finding_type,
            significance=significance.upper(),
            description=description,
            location=location,
            invariant_implication=invariant,
            plot_path=plot_path,
        )

        data = {
            "finding_id": finding_id,
            "function_id": function_id,
            "finding_type": finding_type,
            "significance": significance.upper(),
            "status": "NEW",
        }

        response = success_response(data)
        output(response, json_output=json_output, pretty=pretty)

    except ValueError as e:
        response = error_response(
            ErrorCode.INVALID_INPUT,
            str(e),
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as e:
        response = error_response(
            ErrorCode.INTERNAL_ERROR,
            f"Error saving finding: {str(e)}",
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)
    finally:
        conn.close()


@app.command("list")
def list_findings(
    ctx: typer.Context,
    function_id: Optional[str] = typer.Option(
        None,
        "--function",
        "-f",
        help="Filter by function ID",
    ),
    status: Optional[str] = typer.Option(
        None,
        "--status",
        "-s",
        help="Filter by status: NEW, ADDRESSED, IGNORED",
    ),
    significance: Optional[str] = typer.Option(
        None,
        "--significance",
        help="Filter by significance: HIGH, MEDIUM, LOW",
    ),
    limit: int = typer.Option(
        20,
        "--limit",
        help="Maximum number of results",
    ),
) -> None:
    """List vision analysis findings.

    Examples:
        drspec vision list
        drspec vision list --function "src/foo.py::process"
        drspec vision list --status NEW --significance HIGH
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
        from drspec.db.queries import get_all_vision_findings, get_vision_findings

        if function_id:
            findings = get_vision_findings(
                conn, function_id, status=status, significance=significance
            )
        else:
            findings = get_all_vision_findings(
                conn, status=status, significance=significance, limit=limit
            )

        # Apply limit if using function filter
        findings = findings[:limit]

        data = {
            "findings": [
                {
                    "id": f.id,
                    "function_id": f.function_id,
                    "finding_type": f.finding_type,
                    "significance": f.significance,
                    "description": f.description,
                    "location": f.location,
                    "invariant_implication": f.invariant_implication,
                    "status": f.status,
                    "resolution_note": f.resolution_note,
                    "plot_path": f.plot_path,
                    "created_at": f.created_at.isoformat() if f.created_at else None,
                }
                for f in findings
            ],
            "total": len(findings),
        }

        response = success_response(data)
        output(response, json_output=json_output, pretty=pretty)

    except ValueError as e:
        response = error_response(
            ErrorCode.INVALID_INPUT,
            str(e),
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as e:
        response = error_response(
            ErrorCode.INTERNAL_ERROR,
            f"Error listing findings: {str(e)}",
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)
    finally:
        conn.close()


@app.command()
def update(
    ctx: typer.Context,
    finding_id: int = typer.Argument(
        ...,
        help="Finding ID to update",
    ),
    status: str = typer.Option(
        ...,
        "--status",
        "-s",
        help="New status: NEW, ADDRESSED, IGNORED",
    ),
    note: Optional[str] = typer.Option(
        None,
        "--note",
        "-n",
        help="Resolution note",
    ),
) -> None:
    """Update a vision finding's status.

    Examples:
        drspec vision update 1 --status ADDRESSED --note "Added boundary check invariant"
        drspec vision update 2 -s IGNORED -n "False positive - expected behavior"
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
        from drspec.db.queries import update_vision_finding_status

        updated = update_vision_finding_status(
            conn=conn,
            finding_id=finding_id,
            status=status.upper(),
            resolution_note=note,
        )

        if not updated:
            response = error_response(
                ErrorCode.NOT_FOUND,
                f"Finding {finding_id} not found",
            )
            output(response, json_output=json_output, pretty=pretty)
            raise typer.Exit(1)

        data = {
            "finding_id": finding_id,
            "status": status.upper(),
            "resolution_note": note,
        }

        response = success_response(data)
        output(response, json_output=json_output, pretty=pretty)

    except ValueError as e:
        response = error_response(
            ErrorCode.INVALID_INPUT,
            str(e),
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as e:
        response = error_response(
            ErrorCode.INTERNAL_ERROR,
            f"Error updating finding: {str(e)}",
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)
    finally:
        conn.close()
