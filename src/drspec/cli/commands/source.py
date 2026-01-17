"""Source command - Get source code for functions."""

from __future__ import annotations

from pathlib import Path

import typer

from drspec.cli.output import ErrorCode, error_response, output, success_response
from drspec.cli.validators import validate_function_id
from drspec.core.hints import extract_hints_simple
from drspec.db import get_artifact, get_connection, list_artifacts

app = typer.Typer(
    name="source",
    help="Get source code for functions",
)


def find_similar_functions(
    conn,
    function_id: str,
    limit: int = 5,
) -> list[str]:
    """Find similar function IDs for suggestions.

    Args:
        conn: Database connection.
        function_id: The function ID that wasn't found.
        limit: Maximum number of suggestions.

    Returns:
        List of similar function IDs.
    """
    suggestions = []

    # Extract function name from the ID
    if "::" in function_id:
        _, func_name = function_id.split("::", 1)
    else:
        func_name = function_id

    # Search for functions with matching name
    artifacts = list_artifacts(conn, limit=500)
    for artifact in artifacts:
        if func_name.lower() in artifact.function_name.lower():
            suggestions.append(artifact.function_id)
            if len(suggestions) >= limit:
                break

    # If we didn't find enough, also look for partial path matches
    if len(suggestions) < limit and "::" in function_id:
        filepath, _ = function_id.split("::", 1)
        for artifact in artifacts:
            if artifact.function_id not in suggestions:
                if filepath.lower() in artifact.file_path.lower():
                    suggestions.append(artifact.function_id)
                    if len(suggestions) >= limit:
                        break

    return suggestions


@app.command()
def get(
    ctx: typer.Context,
    function_id: str = typer.Argument(
        ...,
        help="Function ID (format: filepath::function_name)",
    ),
) -> None:
    """Get the source code for a specific function.

    Returns the function source code as stored in artifacts, including
    signature, body, line numbers, and any @invariant hints found in comments.

    Examples:
        drspec source get src/payments/reconcile.py::reconcile_transactions
        drspec source get --pretty src/utils.py::helper
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
        # Get the artifact
        artifact = get_artifact(conn, function_id)

        if artifact is None:
            # Artifact not found - try fuzzy matching for suggestions
            suggestions = find_similar_functions(conn, function_id)
            response = error_response(
                ErrorCode.FUNCTION_NOT_FOUND,
                f"No function exists with ID '{function_id}'",
                {
                    "function_id": function_id,
                    "suggestions": suggestions,
                },
            )
            output(response, json_output=json_output, pretty=pretty)
            raise typer.Exit(1)

        # Extract @invariant hints from the body
        hints = extract_hints_simple(artifact.body, artifact.start_line)

        # Build response
        data = {
            "function_id": artifact.function_id,
            "file_path": artifact.file_path,
            "function_name": artifact.function_name,
            "language": artifact.language,
            "start_line": artifact.start_line,
            "end_line": artifact.end_line,
            "signature": artifact.signature,
            "body": artifact.body,
            "hints": hints,
        }

        response = success_response(data)
        output(response, json_output=json_output, pretty=pretty)

    except typer.Exit:
        # Re-raise typer.Exit without catching it
        raise
    except Exception as e:
        response = error_response(
            ErrorCode.INTERNAL_ERROR,
            f"Error getting source: {str(e)}",
            {"function_id": function_id},
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)
    finally:
        conn.close()
