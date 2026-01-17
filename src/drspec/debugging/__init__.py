"""Debugging module for DrSpec debugger agent.

This module provides APIs for the debugger agent to:
- Verify contracts at runtime
- Query contracts and dependencies
- Identify invariant violations
"""

from __future__ import annotations

from drspec.debugging.runtime import (
    DEFAULT_TIMEOUT,
    InvariantResult,
    RuntimeVerificationResult,
    serialize_for_verification,
    deserialize_from_verification,
    verify_at_runtime,
)
from drspec.debugging.violation import (
    CRITICALITY_ORDER,
    ViolationDetail,
    ViolationReport,
    identify_violations,
    get_violation_by_name,
    get_high_criticality_violations,
    format_violation_report,
)
from drspec.debugging.root_cause import (
    RootCauseCandidate,
    RootCauseReport,
    identify_root_cause,
    format_root_cause_report,
    get_high_confidence_candidates,
)
from drspec.debugging.missing import (
    MissingContract,
    MissingContractReport,
    detect_missing_contracts,
    get_missing_by_relationship,
    get_highest_priority_missing,
    format_missing_contract_report,
)

__all__ = [
    # Runtime verification
    "DEFAULT_TIMEOUT",
    "InvariantResult",
    "RuntimeVerificationResult",
    "serialize_for_verification",
    "deserialize_from_verification",
    "verify_at_runtime",
    # Violation identification
    "CRITICALITY_ORDER",
    "ViolationDetail",
    "ViolationReport",
    "identify_violations",
    "get_violation_by_name",
    "get_high_criticality_violations",
    "format_violation_report",
    # Root cause analysis
    "RootCauseCandidate",
    "RootCauseReport",
    "identify_root_cause",
    "format_root_cause_report",
    "get_high_confidence_candidates",
    # Missing contract detection
    "MissingContract",
    "MissingContractReport",
    "detect_missing_contracts",
    "get_missing_by_relationship",
    "get_highest_priority_missing",
    "format_missing_contract_report",
]
