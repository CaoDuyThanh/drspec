"""Missing contract detection for DrSpec debugger agent.

This module provides APIs for detecting functions that lack contracts
within a debugging workflow, enabling handoff to the Architect Council.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import duckdb

from drspec.db import get_artifact, get_callees


# =============================================================================
# Missing Contract Models
# =============================================================================


@dataclass
class MissingContract:
    """Information about a function lacking a contract.

    Attributes:
        function_id: Full function ID (filepath::name).
        file_path: Path to the source file.
        function_name: Name of the function.
        relationship: How related to debug target (direct, callee, transitive).
        depth: Distance from debug target (0=direct, 1=callee, etc.).
        priority: Priority for contract generation (1=highest).
        reason: Why this contract would help debugging.
    """

    function_id: str
    file_path: str
    function_name: str
    relationship: str
    depth: int
    priority: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "function_id": self.function_id,
            "file_path": self.file_path,
            "function_name": self.function_name,
            "relationship": self.relationship,
            "depth": self.depth,
            "priority": self.priority,
            "reason": self.reason,
        }


@dataclass
class MissingContractReport:
    """Complete report of missing contracts in a debug flow.

    Attributes:
        target_function_id: Function being debugged.
        target_has_contract: Whether the target has a contract.
        missing_contracts: List of functions lacking contracts.
        total_missing: Total count of missing contracts.
        suggestion: What to do next (activate Architect Council).
    """

    target_function_id: str
    target_has_contract: bool
    missing_contracts: list[MissingContract] = field(default_factory=list)
    total_missing: int = 0
    suggestion: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "target_function_id": self.target_function_id,
            "target_has_contract": self.target_has_contract,
            "missing_contracts": [m.to_dict() for m in self.missing_contracts],
            "total_missing": self.total_missing,
            "suggestion": self.suggestion,
        }

    @property
    def has_missing(self) -> bool:
        """Check if there are any missing contracts."""
        return len(self.missing_contracts) > 0

    @property
    def target_is_missing(self) -> bool:
        """Check if the target function itself is missing a contract."""
        return not self.target_has_contract


# =============================================================================
# Contract Detection Functions
# =============================================================================


def _has_contract(conn: duckdb.DuckDBPyConnection, function_id: str) -> bool:
    """Check if a function has a contract.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to check.

    Returns:
        True if function has a contract, False otherwise.
    """
    result = conn.execute(
        "SELECT 1 FROM contracts WHERE function_id = ?",
        [function_id],
    ).fetchone()
    return result is not None


def detect_missing_contracts(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
    max_depth: int = 2,
) -> MissingContractReport:
    """Detect missing contracts for a function and its call chain.

    Analyzes the target function and its callees (up to max_depth) to
    identify which functions lack contracts. Results are prioritized
    by proximity to the debug target.

    Args:
        conn: DuckDB connection.
        function_id: Function ID being debugged.
        max_depth: Maximum call chain depth to analyze (default 2).

    Returns:
        MissingContractReport with prioritized missing contracts.
    """
    missing: list[MissingContract] = []
    priority = 1

    # Check direct function first
    target_has_contract = _has_contract(conn, function_id)

    if not target_has_contract:
        artifact = get_artifact(conn, function_id)
        if artifact:
            missing.append(MissingContract(
                function_id=function_id,
                file_path=artifact.file_path,
                function_name=artifact.function_name,
                relationship="direct",
                depth=0,
                priority=priority,
                reason="This is the function being debugged. A contract is essential for debugging.",
            ))
            priority += 1

    # BFS through call chain
    visited = {function_id}
    queue: list[tuple[str, int]] = [(function_id, 0)]

    while queue:
        current_id, depth = queue.pop(0)

        if depth >= max_depth:
            continue

        # Get callees of current function
        callees = get_callees(conn, current_id)

        for callee_id in callees:
            if callee_id in visited:
                continue
            visited.add(callee_id)

            # Check if callee has a contract
            if not _has_contract(conn, callee_id):
                artifact = get_artifact(conn, callee_id)
                if artifact:
                    relationship = "callee" if depth == 0 else "transitive"
                    reason = _generate_reason(relationship, depth + 1, current_id)

                    missing.append(MissingContract(
                        function_id=callee_id,
                        file_path=artifact.file_path,
                        function_name=artifact.function_name,
                        relationship=relationship,
                        depth=depth + 1,
                        priority=priority,
                        reason=reason,
                    ))
                    priority += 1

            # Add to queue for further exploration
            queue.append((callee_id, depth + 1))

    # Generate suggestion
    suggestion = _generate_suggestion(missing, function_id)

    return MissingContractReport(
        target_function_id=function_id,
        target_has_contract=target_has_contract,
        missing_contracts=missing,
        total_missing=len(missing),
        suggestion=suggestion,
    )


def _generate_reason(relationship: str, depth: int, caller_id: str) -> str:
    """Generate reason for why a contract would help.

    Args:
        relationship: Type of relationship (callee, transitive).
        depth: Depth in call chain.
        caller_id: ID of the calling function.

    Returns:
        Human-readable reason string.
    """
    caller_name = caller_id.split("::")[-1] if "::" in caller_id else caller_id

    if relationship == "callee":
        return (
            "Directly called by the debug target. "
            "A contract would help verify inputs/outputs at this call site."
        )

    return (
        f"Called at depth {depth} in the call chain (via {caller_name}). "
        f"A contract could help isolate issues in the call hierarchy."
    )


def _generate_suggestion(
    missing: list[MissingContract],
    target_function_id: str,
) -> str:
    """Generate suggestion for addressing missing contracts.

    Args:
        missing: List of missing contracts.
        target_function_id: The debug target function ID.

    Returns:
        Suggestion string with actionable guidance.
    """
    if not missing:
        return "All functions in the debug chain have contracts. Ready for debugging."

    lines = []

    if len(missing) == 1:
        m = missing[0]
        if m.relationship == "direct":
            lines.append(
                "The function being debugged lacks a contract. "
                "Generate one first for effective debugging."
            )
        else:
            lines.append(
                f"1 function in the call chain lacks a contract: {m.function_name}"
            )
    else:
        direct_missing = [m for m in missing if m.relationship == "direct"]
        if direct_missing:
            lines.append(
                f"The debug target lacks a contract, plus {len(missing) - 1} "
                f"function(s) in the call chain."
            )
        else:
            lines.append(
                f"{len(missing)} functions in the call chain lack contracts."
            )

    lines.append("")
    lines.append("To build missing contracts:")
    lines.append("1. Activate the Architect Council")

    # Add prioritized function list
    if len(missing) <= 3:
        for m in missing:
            lines.append(f"   - {m.function_id} (priority {m.priority})")
    else:
        lines.append("   - Top 3 priorities:")
        for m in missing[:3]:
            lines.append(f"     {m.function_id}")

    # Add queue command example
    if missing:
        top_function = missing[0].function_id
        lines.append("")
        lines.append(f'Example: drspec queue prioritize "{top_function}" 1')

    return "\n".join(lines)


# =============================================================================
# Convenience Functions
# =============================================================================


def get_missing_by_relationship(
    report: MissingContractReport,
    relationship: str,
) -> list[MissingContract]:
    """Filter missing contracts by relationship type.

    Args:
        report: MissingContractReport to filter.
        relationship: Relationship type (direct, callee, transitive).

    Returns:
        List of MissingContract with matching relationship.
    """
    return [m for m in report.missing_contracts if m.relationship == relationship]


def get_highest_priority_missing(
    report: MissingContractReport,
    n: int = 1,
) -> list[MissingContract]:
    """Get the N highest priority missing contracts.

    Args:
        report: MissingContractReport to query.
        n: Number of contracts to return.

    Returns:
        List of up to N highest priority missing contracts.
    """
    return report.missing_contracts[:n]


def format_missing_contract_report(report: MissingContractReport) -> str:
    """Format missing contract report as human-readable text.

    Args:
        report: MissingContractReport to format.

    Returns:
        Formatted text report.
    """
    lines = []
    lines.append(f"Missing Contract Analysis: {report.target_function_id}")
    lines.append("=" * 60)
    lines.append(f"Target has contract: {'Yes' if report.target_has_contract else 'NO'}")
    lines.append(f"Total missing: {report.total_missing}")
    lines.append("")

    if not report.missing_contracts:
        lines.append("All functions have contracts. Ready for debugging.")
    else:
        lines.append("Missing Contracts (by priority):")
        lines.append("-" * 40)

        for m in report.missing_contracts:
            lines.append(f"\n{m.priority}. {m.function_name} [{m.relationship}]")
            lines.append(f"   ID: {m.function_id}")
            lines.append(f"   File: {m.file_path}")
            lines.append(f"   Depth: {m.depth}")
            lines.append(f"   Reason: {m.reason}")

    lines.append("")
    lines.append("SUGGESTION")
    lines.append("-" * 40)
    lines.append(report.suggestion)

    return "\n".join(lines)
