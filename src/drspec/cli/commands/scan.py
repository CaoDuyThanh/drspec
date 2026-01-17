"""Scan command - Scan source files for functions."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from drspec.cli.output import ErrorCode, error_response, output, success_response
from drspec.core import (
    Scanner,
    compute_hash,
)
from drspec.db import (
    get_artifact,
    get_connection,
    insert_artifact,
    queue_push,
)

app = typer.Typer(
    name="scan",
    help="Scan source files and extract function signatures",
    no_args_is_help=False,
)


@app.callback(invoke_without_command=True)
def scan_command(
    ctx: typer.Context,
    path: Optional[Path] = typer.Argument(
        None,
        help="Path to scan (file or directory). Defaults to current directory.",
    ),
    recursive: bool = typer.Option(
        True,
        "--recursive/--no-recursive",
        help="Recursively scan directories",
    ),
    queue_new: bool = typer.Option(
        True,
        "--queue/--no-queue",
        help="Add changed/new functions to the processing queue",
    ),
) -> None:
    """Scan source files and add functions to the artifacts table.

    Extracts function signatures using tree-sitter and stores them
    in the artifacts table with normalized hashes. Changed or new
    functions are added to the processing queue by default.

    Examples:
        drspec scan                    # Scan current directory
        drspec scan ./src              # Scan specific directory
        drspec scan ./src/module.py    # Scan single file
        drspec scan --no-queue         # Scan without queueing
    """
    # Get CLI context
    cli_ctx = ctx.obj or {}
    json_output = cli_ctx.get("json_output", True)
    pretty = cli_ctx.get("pretty", False)
    db_path_override = cli_ctx.get("db_path")

    # Default to current directory
    scan_path = path or Path.cwd()

    # Validate path exists
    if not scan_path.exists():
        response = error_response(
            ErrorCode.PATH_NOT_FOUND,
            f"Path does not exist: {scan_path}",
            {"path": str(scan_path)},
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)

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
        # Initialize scanner
        scanner = Scanner()

        # Track statistics
        files_scanned = 0
        functions_found = 0
        functions_new = 0
        functions_changed = 0
        functions_unchanged = 0
        errors: list[dict] = []

        # Scan path
        if scan_path.is_file():
            # Scan single file
            result = scanner.scan_file(str(scan_path))
            if result:
                files_scanned = 1
                for func in result.functions:
                    functions_found += 1
                    code_hash = compute_hash(func.body, func.language)
                    # Check if artifact exists before inserting
                    existing = get_artifact(conn, func.function_id)
                    is_new = existing is None
                    changed = insert_artifact(
                        conn,
                        function_id=func.function_id,
                        file_path=func.file_path,
                        function_name=func.name,
                        signature=func.signature,
                        body=func.body,
                        code_hash=code_hash,
                        language=func.language,
                        start_line=func.start_line,
                        end_line=func.end_line,
                        parent=func.parent,
                    )
                    if changed:
                        if is_new:
                            functions_new += 1
                            if queue_new:
                                queue_push(conn, func.function_id, reason="NEW")
                        else:
                            functions_changed += 1
                            if queue_new:
                                queue_push(conn, func.function_id, reason="HASH_MISMATCH")
                    else:
                        functions_unchanged += 1
                for err in result.errors:
                    errors.append({
                        "file": str(scan_path),
                        "line": err.line,
                        "message": err.message,
                    })
        else:
            # Scan directory
            result = scanner.scan_directory(
                str(scan_path),
                recursive=recursive,
            )
            files_scanned = result.files_scanned

            for func in result.functions:
                functions_found += 1
                code_hash = compute_hash(func.body, func.language)
                # Check if artifact exists before inserting
                existing = get_artifact(conn, func.function_id)
                is_new = existing is None
                changed = insert_artifact(
                    conn,
                    function_id=func.function_id,
                    file_path=func.file_path,
                    function_name=func.name,
                    signature=func.signature,
                    body=func.body,
                    code_hash=code_hash,
                    language=func.language,
                    start_line=func.start_line,
                    end_line=func.end_line,
                    parent=func.parent,
                )
                if changed:
                    if is_new:
                        functions_new += 1
                        if queue_new:
                            queue_push(conn, func.function_id, reason="NEW")
                    else:
                        functions_changed += 1
                        if queue_new:
                            queue_push(conn, func.function_id, reason="HASH_MISMATCH")
                else:
                    functions_unchanged += 1

            for err in result.errors:
                # result.errors is list[tuple[str, str]] - (file_path, error_message)
                if isinstance(err, tuple) and len(err) >= 2:
                    errors.append({
                        "file": err[0],
                        "message": err[1],
                    })
                else:
                    errors.append({
                        "file": "unknown",
                        "message": str(err),
                    })

        # Build response
        data = {
            "message": f"Scanned {files_scanned} file(s), found {functions_found} function(s)",
            "path": str(scan_path),
            "recursive": recursive,
            "files_scanned": files_scanned,
            "functions_found": functions_found,
            "functions_new": functions_new,
            "functions_changed": functions_changed,
            "functions_unchanged": functions_unchanged,
            "queue_enabled": queue_new,
        }

        if errors:
            data["errors"] = errors
            data["error_count"] = len(errors)

        response = success_response(data)
        output(response, json_output=json_output, pretty=pretty)

    except Exception as e:
        response = error_response(
            ErrorCode.SCAN_ERROR,
            f"Error during scan: {str(e)}",
            {"path": str(scan_path)},
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)
    finally:
        conn.close()
