"""DrSpec contract schema and validation.

This module provides Pydantic models for semantic contracts,
ensuring consistent structure and validation before storage.
"""

from __future__ import annotations

from drspec.contracts.confidence import (
    ArtifactStatus,
    ConfidenceLevel,
    DEFAULT_CONFIDENCE_THRESHOLD,
    describe_confidence,
    evaluate_confidence,
    evaluate_confidence_with_db,
    get_confidence_distribution,
    get_confidence_level,
    get_confidence_threshold,
    set_confidence_threshold,
    suggest_threshold,
    validate_confidence_score,
)
from drspec.contracts.schema import (
    Contract,
    Criticality,
    Invariant,
    IOExample,
    OnFail,
)
from drspec.contracts.validator import (
    ValidationError,
    ValidationErrorDetail,
    ValidationResult,
    format_validation_errors,
    validate_contract,
    validate_contract_dict,
)
from drspec.contracts.traces import (
    AgentType,
    ReasoningTrace,
    store_trace,
    get_traces,
    get_latest_trace,
    count_traces,
    delete_traces,
)
from drspec.contracts.generator import (
    generate_verification_script,
    compute_script_hash,
)
from drspec.contracts.executor import (
    DEFAULT_TIMEOUT,
    VerificationResult,
    execute_verification,
    validate_script,
)

# Note: Runtime verification APIs are now in drspec.debugging module
# to avoid circular import issues with Pydantic on Python 3.8

__all__ = [
    # Schema
    "Contract",
    "Criticality",
    "Invariant",
    "IOExample",
    "OnFail",
    # Validation
    "ValidationError",
    "ValidationErrorDetail",
    "ValidationResult",
    "format_validation_errors",
    "validate_contract",
    "validate_contract_dict",
    # Confidence
    "ArtifactStatus",
    "ConfidenceLevel",
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "describe_confidence",
    "evaluate_confidence",
    "evaluate_confidence_with_db",
    "get_confidence_distribution",
    "get_confidence_level",
    "get_confidence_threshold",
    "set_confidence_threshold",
    "suggest_threshold",
    "validate_confidence_score",
    # Reasoning Traces
    "AgentType",
    "ReasoningTrace",
    "store_trace",
    "get_traces",
    "get_latest_trace",
    "count_traces",
    "delete_traces",
    # Script Generator
    "generate_verification_script",
    "compute_script_hash",
    # Script Executor
    "DEFAULT_TIMEOUT",
    "VerificationResult",
    "execute_verification",
    "validate_script",
]
