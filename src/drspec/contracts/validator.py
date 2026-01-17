"""Contract validation engine for DrSpec.

This module provides validation functions for semantic contracts,
ensuring contracts are valid before storage in the database.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic import ValidationError as PydanticValidationError

from drspec.contracts.schema import Contract


@dataclass
class ValidationErrorDetail:
    """A single validation error detail.

    Attributes:
        loc: Location of the error (field path)
        msg: Error message
        type: Error type from Pydantic
    """

    loc: tuple[str | int, ...]
    msg: str
    type: str

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "loc": list(self.loc),
            "msg": self.msg,
            "type": self.type,
        }


@dataclass
class ValidationError:
    """Validation error with code and details.

    Attributes:
        code: Error code (INVALID_JSON or INVALID_SCHEMA)
        message: Human-readable error message
        details: Additional error details
    """

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


@dataclass
class ValidationResult:
    """Result of contract validation.

    Either contains a validated contract (success=True) or
    an error with details (success=False).

    Attributes:
        success: Whether validation succeeded
        contract: Validated Contract object (if success=True)
        error: ValidationError object (if success=False)
    """

    success: bool
    contract: Optional[Contract] = None
    error: Optional[ValidationError] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        if self.success:
            return {
                "success": True,
                "contract": self.contract.to_dict() if self.contract else None,
                "error": None,
            }
        else:
            return {
                "success": False,
                "contract": None,
                "error": self.error.to_dict() if self.error else None,
            }


def validate_contract(json_str: str) -> ValidationResult:
    """Validate contract JSON string against schema.

    Parses the JSON string and validates it against the Contract schema.
    Returns a ValidationResult with either the validated contract or
    detailed error information.

    Args:
        json_str: JSON string to validate

    Returns:
        ValidationResult with success status and either contract or error

    Examples:
        >>> result = validate_contract('{"function_signature": "def foo()", ...}')
        >>> if result.success:
        ...     print(result.contract.function_signature)
        ... else:
        ...     print(result.error.code)
    """
    # First, try to parse JSON
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        return ValidationResult(
            success=False,
            error=ValidationError(
                code="INVALID_JSON",
                message=f"Invalid JSON at position {e.pos}: {e.msg}",
                details={
                    "position": e.pos,
                    "line": e.lineno,
                    "column": e.colno,
                },
            ),
        )

    # Then validate against Pydantic schema
    return validate_contract_dict(data)


def validate_contract_dict(data: dict[str, Any]) -> ValidationResult:
    """Validate contract data dictionary against schema.

    Args:
        data: Dictionary with contract data

    Returns:
        ValidationResult with success status and either contract or error
    """
    try:
        contract = Contract.model_validate(data)
        return ValidationResult(success=True, contract=contract)

    except PydanticValidationError as e:
        # Extract detailed error information
        errors = []
        for err in e.errors():
            errors.append(
                ValidationErrorDetail(
                    loc=tuple(err.get("loc", ())),
                    msg=err.get("msg", "Unknown error"),
                    type=err.get("type", "unknown"),
                ).to_dict()
            )

        return ValidationResult(
            success=False,
            error=ValidationError(
                code="INVALID_SCHEMA",
                message="Contract validation failed",
                details={"errors": errors},
            ),
        )


def format_validation_errors(result: ValidationResult) -> str:
    """Format validation errors as human-readable string.

    Args:
        result: ValidationResult with errors

    Returns:
        Formatted error string
    """
    if result.success:
        return "Validation successful"

    if not result.error:
        return "Unknown validation error"

    lines = [f"Error: {result.error.message} ({result.error.code})"]

    if "errors" in result.error.details:
        lines.append("Details:")
        for err in result.error.details["errors"]:
            loc = " -> ".join(str(x) for x in err.get("loc", []))
            msg = err.get("msg", "Unknown")
            lines.append(f"  - {loc}: {msg}")
    elif "position" in result.error.details:
        pos = result.error.details.get("position", "?")
        line = result.error.details.get("line", "?")
        col = result.error.details.get("column", "?")
        lines.append(f"  At line {line}, column {col} (position {pos})")

    return "\n".join(lines)
