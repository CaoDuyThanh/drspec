"""JSON output formatting for DrSpec CLI.

Provides consistent response formatting for all CLI commands with support
for both compact JSON (agent-optimized) and human-readable pretty output.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Any, Optional

import typer


# =============================================================================
# Error Code Catalog
# =============================================================================


class ErrorCode(str, Enum):
    """Standard error codes for DrSpec CLI responses.

    All error codes use SCREAMING_SNAKE_CASE convention.
    """

    # Initialization errors
    DB_NOT_INITIALIZED = "DB_NOT_INITIALIZED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    INIT_FAILED = "INIT_FAILED"
    PATH_NOT_FOUND = "PATH_NOT_FOUND"

    # Command errors
    SCAN_ERROR = "SCAN_ERROR"
    STATUS_ERROR = "STATUS_ERROR"

    # Contract errors
    CONTRACT_NOT_FOUND = "CONTRACT_NOT_FOUND"
    INVALID_SCHEMA = "INVALID_SCHEMA"
    INVALID_FUNCTION_ID = "INVALID_FUNCTION_ID"

    # Queue errors
    QUEUE_EMPTY = "QUEUE_EMPTY"
    QUEUE_ITEM_NOT_FOUND = "QUEUE_ITEM_NOT_FOUND"

    # Function/artifact errors
    FUNCTION_NOT_FOUND = "FUNCTION_NOT_FOUND"

    # Parsing errors
    PARSE_ERROR = "PARSE_ERROR"
    UNSUPPORTED_LANGUAGE = "UNSUPPORTED_LANGUAGE"
    INVALID_JSON = "INVALID_JSON"

    # Verification errors
    VERIFICATION_FAILED = "VERIFICATION_FAILED"

    # General errors
    INTERNAL_ERROR = "INTERNAL_ERROR"
    INVALID_INPUT = "INVALID_INPUT"
    EXECUTION_ERROR = "EXECUTION_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"


# =============================================================================
# Response Builders
# =============================================================================


def success_response(data: dict[str, Any]) -> dict[str, Any]:
    """Create a success response wrapper.

    Args:
        data: Response data payload (must use snake_case keys).

    Returns:
        Standard success response wrapper.

    Example:
        >>> success_response({"function_id": "foo.py::bar", "status": "VERIFIED"})
        {
            "success": True,
            "data": {"function_id": "foo.py::bar", "status": "VERIFIED"},
            "error": None
        }
    """
    return {
        "success": True,
        "data": data,
        "error": None,
    }


def error_response(
    code: str | ErrorCode,
    message: str,
    details: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Create an error response wrapper.

    Args:
        code: Error code (SCREAMING_SNAKE_CASE).
        message: Human-readable error message.
        details: Optional additional error details.

    Returns:
        Standard error response wrapper.

    Example:
        >>> error_response("FUNCTION_NOT_FOUND", "No function with ID foo.py::bar")
        {
            "success": False,
            "data": None,
            "error": {
                "code": "FUNCTION_NOT_FOUND",
                "message": "No function with ID foo.py::bar",
                "details": {}
            }
        }
    """
    # Convert ErrorCode enum to string if needed
    code_str = code.value if isinstance(code, ErrorCode) else code

    return {
        "success": False,
        "data": None,
        "error": {
            "code": code_str,
            "message": message,
            "details": details or {},
        },
    }


# =============================================================================
# JSON Serialization
# =============================================================================


class DrSpecEncoder(json.JSONEncoder):
    """Custom JSON encoder for DrSpec types.

    Handles:
    - datetime objects (ISO format)
    - Pydantic models (.model_dump())
    - Enum values
    - Path objects
    """

    def default(self, obj: Any) -> Any:
        """Encode non-standard types."""
        # Handle datetime
        if isinstance(obj, datetime):
            return obj.isoformat()

        # Handle Pydantic models
        if hasattr(obj, "model_dump"):
            return obj.model_dump()

        # Handle Enum
        if isinstance(obj, Enum):
            return obj.value

        # Handle pathlib.Path
        if hasattr(obj, "__fspath__"):
            return str(obj)

        return super().default(obj)


def output_json(response: dict[str, Any], pretty: bool = False) -> None:
    """Output JSON response to stdout.

    Args:
        response: Response dictionary to output.
        pretty: If True, output human-readable indented JSON.
    """
    if pretty:
        output = json.dumps(response, cls=DrSpecEncoder, indent=2, sort_keys=False)
    else:
        output = json.dumps(response, cls=DrSpecEncoder, separators=(",", ":"))

    typer.echo(output)


# =============================================================================
# Human-Readable Output
# =============================================================================


def output_pretty(response: dict[str, Any]) -> None:
    """Output human-readable formatted response.

    Formats success and error responses with clear structure
    and optional terminal colors.

    Args:
        response: Response dictionary to output.
    """
    if response.get("success"):
        _output_success_pretty(response.get("data", {}))
    else:
        _output_error_pretty(response.get("error", {}))


def _output_success_pretty(data: dict[str, Any]) -> None:
    """Format success data for human reading."""
    # Check for message field (common in many responses)
    if "message" in data:
        typer.echo(typer.style("✓ ", fg=typer.colors.GREEN, bold=True) + data["message"])
        typer.echo()

    # Output other fields
    for key, value in data.items():
        if key == "message":
            continue

        # Format key nicely
        display_key = key.replace("_", " ").title()

        # Format value based on type
        if isinstance(value, list):
            typer.echo(f"{display_key}:")
            for item in value:
                typer.echo(f"  - {item}")
        elif isinstance(value, dict):
            typer.echo(f"{display_key}:")
            for k, v in value.items():
                typer.echo(f"  {k}: {v}")
        elif isinstance(value, bool):
            status = "Yes" if value else "No"
            typer.echo(f"{display_key}: {status}")
        else:
            typer.echo(f"{display_key}: {value}")


def _output_error_pretty(error: dict[str, Any]) -> None:
    """Format error for human reading."""
    code = error.get("code", "UNKNOWN_ERROR")
    message = error.get("message", "An unknown error occurred")
    details = error.get("details", {})

    typer.echo(typer.style("✗ Error: ", fg=typer.colors.RED, bold=True) + message)
    typer.echo(typer.style(f"  Code: {code}", fg=typer.colors.YELLOW))

    if details:
        typer.echo("  Details:")
        for key, value in details.items():
            typer.echo(f"    {key}: {value}")


# =============================================================================
# Output Helper
# =============================================================================


def output(response: dict[str, Any], json_output: bool = True, pretty: bool = False) -> None:
    """Output response in appropriate format.

    This is the main output function that should be used by commands.
    It respects the global --json and --pretty flags.

    Args:
        response: Response dictionary to output.
        json_output: If True, output as JSON (default for agent compatibility).
        pretty: If True and json_output, output indented JSON.
                If True and not json_output, output human-readable format.
    """
    if json_output:
        output_json(response, pretty=pretty)
    else:
        output_pretty(response)
