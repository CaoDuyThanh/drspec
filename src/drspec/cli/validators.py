"""Shared validation utilities for DrSpec CLI commands."""

from __future__ import annotations

from typing import Optional


def validate_function_id(function_id: str) -> tuple[bool, Optional[str]]:
    """Validate function ID format.

    Function IDs must follow the format: filepath::function_name
    where both parts are non-empty strings.

    Args:
        function_id: Function ID to validate.

    Returns:
        Tuple of (is_valid, error_message). If valid, error_message is None.

    Examples:
        >>> validate_function_id("src/foo.py::bar")
        (True, None)
        >>> validate_function_id("invalid")
        (False, "Function ID must contain '::' separator ...")
    """
    if "::" not in function_id:
        return False, "Function ID must contain '::' separator (format: filepath::function_name)"

    parts = function_id.split("::", 1)
    if len(parts) != 2:
        return False, "Function ID must have exactly two parts separated by '::'"

    filepath, func_name = parts
    if not filepath:
        return False, "Function ID filepath part cannot be empty"
    if not func_name:
        return False, "Function ID function_name part cannot be empty"

    return True, None


def resolve_or_exit(conn, function_id: str, json_output: bool) -> str:
    """The stored identifier for what the user typed. `file::name` without the `#hash`
    stands for the one function with that name in that file; if several share it, the
    candidates are listed and the command ends (the connection is closed first)."""
    import typer

    from drspec.cli.output import ErrorCode, error_response, output
    from drspec.db import AmbiguousFunctionId, resolve_function_id

    try:
        resolved = resolve_function_id(conn, function_id)
        from drspec.core.index_notice import queue_if_unindexed

        queue_if_unindexed(conn, resolved)
        return resolved
    except AmbiguousFunctionId as e:
        conn.close()
        output(
            error_response(
                ErrorCode.INVALID_FUNCTION_ID,
                str(e),
                {"function_id": function_id, "candidates": [{"function_id": fid, "signature": sig} for fid, sig in e.candidates]},
            ),
            json_output=json_output,
        )
        raise typer.Exit(1)
