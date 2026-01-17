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
