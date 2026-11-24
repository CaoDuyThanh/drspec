"""Source command - Get source code for functions."""

from __future__ import annotations

from pathlib import Path

import typer

from drspec.cli.output import ErrorCode, error_response, output, success_response
from drspec.cli.validators import resolve_or_exit, validate_function_id
from drspec.core.declarations import declared_in, is_declaration_only
from drspec.core import docs as documents
from drspec.core.docs import documented_in
from drspec.core.hints import extract_hints_simple
from drspec.core.index_notice import add_index_notice
from drspec.core.partial import not_found_with_reason, partial_function_ids
from drspec.core import usage
from drspec.core.staleness import stale_paths_checked
from drspec.db import get_artifact
from drspec.worker_ipc import WorkerIPCError, get_worker_connection

def documents_extensions() -> tuple:
    """The suffixes a document path can end in (the default list: reading the setting needs the database)."""
    return documents.extensions({})


app = typer.Typer(
    name="source",
    help="Get source code for functions",
)


def find_similar_functions(
    conn,
    function_id: str,
    limit: int = 5,
) -> list[str]:
    """Similar function IDs for suggestions (one implementation for every command: `core.dependency_info.find_similar_functions`)."""
    from drspec.core.dependency_info import find_similar_functions as shared

    return shared(conn, function_id, limit)


@app.command()
def get(
    ctx: typer.Context,
    function_id: str = typer.Argument(
        ...,
        help="Function ID (format: filepath::name#hash as printed by search; the #hash may be left out when only one function in the file has that name)",
    ),
) -> None:
    """Get the source code for a specific function.

    Returns the function source code as stored in artifacts, including
    signature, body, line numbers, and any @invariant hints found in comments.

    Examples:
        drspec source get src/payments/reconcile.py::reconcile_transactions
    """
    cli_ctx = ctx.obj or {}
    json_output = cli_ctx.get("json_output", False)
    db_path_override = cli_ctx.get("db_path")

    drspec_dir = Path.cwd() / "_drspec"
    if db_path_override:
        db_path = db_path_override
    else:
        db_path = drspec_dir / "contracts.db"

    if not db_path.parent.exists() or not db_path.exists():
        response = error_response(
            ErrorCode.DB_NOT_INITIALIZED,
            "DrSpec not initialized. Run 'drspec init' first.",
            {"expected_path": str(drspec_dir)},
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)

    is_valid, error_msg = validate_function_id(function_id)
    if not is_valid and "::" not in function_id and not function_id.lower().endswith(tuple(documents_extensions())):
        message, details = error_msg, {"function_id": function_id, "expected_format": "filepath::function_name"}
        try:  # a bare name that a parse error kept out of the index is said to be that, not only malformed
            probe = get_worker_connection(db_path, read_only=True)
            try:
                message, details = not_found_with_reason(probe, message, function_id, details)
            finally:
                probe.close()
        except WorkerIPCError:
            pass
        output(error_response(ErrorCode.INVALID_FUNCTION_ID, message, details), json_output=json_output)
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

    target = documents.document_target(conn, function_id)
    if target is not None:  # a document section or a document: its text
        try:
            data = documents.section_source(conn, *target)
        finally:
            conn.close()
        output(success_response(data), json_output=json_output)
        return
    if not is_valid:
        message, details = not_found_with_reason(conn, error_msg, function_id, {"function_id": function_id, "expected_format": "filepath::function_name"})
        conn.close()
        output(error_response(ErrorCode.INVALID_FUNCTION_ID, message, details), json_output=json_output)
        raise typer.Exit(1)

    function_id = resolve_or_exit(conn, function_id, json_output)

    try:
        artifact = get_artifact(conn, function_id)

        if artifact is None:
            suggestions = find_similar_functions(conn, function_id)
            message, details = not_found_with_reason(
                conn, f"No function exists with ID '{function_id}'", function_id, {"function_id": function_id, "suggestions": suggestions}
            )
            response = error_response(ErrorCode.FUNCTION_NOT_FOUND, message, details)
            output(response, json_output=json_output)
            raise typer.Exit(1)

        hints = extract_hints_simple(artifact.body, artifact.start_line)

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
            "possibly_stale": artifact.file_path in stale_paths_checked(conn, [artifact.file_path]),
        }
        usage.record_usage(conn, usage.nodes([artifact.function_id]), "usage.lookup_weight")
        if artifact.function_id in partial_function_ids(conn, [artifact.file_path]):
            data["partial"] = True  # kept although its body was read in part
        headers = declared_in(conn, artifact.function_id)
        if headers:
            data["declared_in"] = headers
        if is_declaration_only(conn, artifact.function_id):
            data["declaration_only"] = True
        documented = documented_in(conn, artifact.function_id)
        if documented:
            data["documented_in"] = documented
        add_index_notice(conn, data)

        response = success_response(data)
        output(response, json_output=json_output)

    except typer.Exit:
        raise
    except Exception as e:
        response = error_response(
            ErrorCode.INTERNAL_ERROR,
            f"Error getting source: {str(e)}",
            {"function_id": function_id},
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)
    finally:
        conn.close()
