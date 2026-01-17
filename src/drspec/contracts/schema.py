"""Pydantic models for DrSpec semantic contracts.

This module defines the contract schema used throughout DrSpec:
- Invariant: Individual rule that should hold for a function
- Contract: Complete semantic contract for a function
- IOExample: Input/output example for a contract

All field names use snake_case per architecture naming conventions.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class Criticality(str, Enum):
    """Criticality level for invariants.

    Determines how severe a violation is:
    - HIGH: Data corruption, security issue, or crash
    - MEDIUM: Incorrect behavior but recoverable
    - LOW: Code smell but might not cause issues
    """

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class OnFail(str, Enum):
    """Action to take when invariant fails.

    - error: Raise an error (fail the verification)
    - warn: Log a warning but continue
    """

    ERROR = "error"
    WARN = "warn"


class Invariant(BaseModel):
    """A single invariant rule for a function contract.

    An invariant describes a rule that should always hold for a function,
    such as input validation, output guarantees, or state relationships.

    Attributes:
        name: Short identifier for the invariant (e.g., 'non_negative_output')
        logic: Natural language description of the rule
        criticality: How severe a violation is (HIGH, MEDIUM, LOW)
        on_fail: Action to take on violation (error, warn)
    """

    name: str = Field(
        ...,
        min_length=1,
        description="Short identifier for the invariant",
        examples=["non_negative_output", "valid_email_format"],
    )
    logic: str = Field(
        ...,
        min_length=1,
        description="Natural language description of the rule",
        examples=["Output value is always >= 0", "Email contains @ symbol"],
    )
    criticality: Criticality = Field(
        ...,
        description="How severe a violation is",
    )
    on_fail: OnFail = Field(
        ...,
        description="Action to take on violation",
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        """Validate invariant name is a valid identifier-like string."""
        v = v.strip()
        if not v:
            raise ValueError("Invariant name cannot be empty or whitespace")
        return v

    @field_validator("logic")
    @classmethod
    def validate_logic(cls, v: str) -> str:
        """Validate logic description is meaningful."""
        v = v.strip()
        if not v:
            raise ValueError("Invariant logic cannot be empty or whitespace")
        if len(v) < 5:
            raise ValueError("Invariant logic must be at least 5 characters")
        return v


class IOExample(BaseModel):
    """An input/output example for a contract.

    Examples help clarify the expected behavior of a function
    with concrete input and output values.

    Attributes:
        input: Dictionary of input parameter values
        output: Expected output value or structure
        description: Optional description of what this example demonstrates
    """

    input: Dict[str, Any] = Field(
        ...,
        description="Input parameter values",
    )
    output: Any = Field(
        ...,
        description="Expected output value",
    )
    description: Optional[str] = Field(
        default=None,
        description="What this example demonstrates",
    )


class Contract(BaseModel):
    """A semantic contract for a function.

    A contract captures the expected behavior of a function through:
    - A summary of its intent
    - A list of invariants (rules that should always hold)
    - Optional input/output examples

    Attributes:
        function_signature: The function's signature string
        intent_summary: 1-2 sentence summary of what the function does
        invariants: List of invariant rules (at least one required)
        io_examples: Optional list of input/output examples
    """

    function_signature: str = Field(
        ...,
        min_length=1,
        description="The function's signature",
        examples=["def add(a: int, b: int) -> int"],
    )
    intent_summary: str = Field(
        ...,
        min_length=1,
        description="Brief summary of the function's purpose",
        examples=["Adds two integers and returns the sum"],
    )
    invariants: List[Invariant] = Field(
        ...,
        min_length=1,
        description="List of invariant rules",
    )
    io_examples: List[IOExample] = Field(
        default_factory=list,
        description="Optional input/output examples",
    )

    @field_validator("function_signature")
    @classmethod
    def validate_function_signature(cls, v: str) -> str:
        """Validate function signature is non-empty."""
        v = v.strip()
        if not v:
            raise ValueError("Function signature cannot be empty or whitespace")
        return v

    @field_validator("intent_summary")
    @classmethod
    def validate_intent_summary(cls, v: str) -> str:
        """Validate intent summary is meaningful."""
        v = v.strip()
        if not v:
            raise ValueError("Intent summary cannot be empty or whitespace")
        if len(v) < 10:
            raise ValueError("Intent summary must be at least 10 characters")
        return v

    @model_validator(mode="after")
    def validate_invariants_not_empty(self) -> "Contract":
        """Ensure contract has at least one invariant."""
        if not self.invariants:
            raise ValueError("Contract must have at least one invariant")
        return self

    def to_json(self, indent: Optional[int] = None) -> str:
        """Serialize contract to JSON string.

        Args:
            indent: Indentation level for pretty printing (None for compact)

        Returns:
            JSON string representation of the contract
        """
        return self.model_dump_json(indent=indent)

    def to_dict(self) -> Dict[str, Any]:
        """Convert contract to dictionary.

        Returns:
            Dictionary representation with snake_case keys
        """
        return self.model_dump()

    @classmethod
    def from_json(cls, json_str: str) -> "Contract":
        """Deserialize contract from JSON string.

        Args:
            json_str: JSON string to parse

        Returns:
            Contract instance

        Raises:
            ValueError: If JSON is invalid or doesn't match schema
        """
        try:
            data = json.loads(json_str)
            return cls.model_validate(data)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {e}")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Contract":
        """Create contract from dictionary.

        Args:
            data: Dictionary with contract data

        Returns:
            Contract instance

        Raises:
            ValueError: If data doesn't match schema
        """
        return cls.model_validate(data)
