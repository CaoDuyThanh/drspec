"""Invariant violation identification for DrSpec debugger agent.

This module provides APIs for identifying and analyzing invariant violations
from runtime verification results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from drspec.debugging.runtime import InvariantResult, RuntimeVerificationResult


# Criticality ordering for sorting (lower = more critical)
CRITICALITY_ORDER = {
    "HIGH": 0,
    "MEDIUM": 1,
    "LOW": 2,
}


# =============================================================================
# Violation Models
# =============================================================================


@dataclass
class ViolationDetail:
    """Detailed information about a single invariant violation.

    Attributes:
        invariant_name: Name of the violated invariant.
        invariant_logic: Natural language description of the invariant.
        criticality: Criticality level (HIGH, MEDIUM, LOW).
        on_fail: Action on failure (error, warn).
        expected: Description of expected behavior.
        actual: Description of actual observed behavior.
        suggestion: Debugging suggestion.
        line_reference: Line number in source if known.
    """

    invariant_name: str
    invariant_logic: str
    criticality: str
    on_fail: str = "error"
    expected: Optional[str] = None
    actual: Optional[str] = None
    suggestion: Optional[str] = None
    line_reference: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "invariant_name": self.invariant_name,
            "invariant_logic": self.invariant_logic,
            "criticality": self.criticality,
            "on_fail": self.on_fail,
            "expected": self.expected,
            "actual": self.actual,
            "suggestion": self.suggestion,
            "line_reference": self.line_reference,
        }


@dataclass
class ViolationReport:
    """Complete report of invariant violations.

    Attributes:
        function_id: Function that was verified.
        total_invariants: Total number of invariants checked.
        passed_count: Number of invariants that passed.
        failed_count: Number of invariants that failed.
        violations: List of violation details, sorted by criticality.
        most_critical: The most critical violation (if any).
        summary: Human-readable summary of violations.
    """

    function_id: str
    total_invariants: int
    passed_count: int
    failed_count: int
    violations: list[ViolationDetail] = field(default_factory=list)
    most_critical: Optional[ViolationDetail] = None
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "function_id": self.function_id,
            "total_invariants": self.total_invariants,
            "passed_count": self.passed_count,
            "failed_count": self.failed_count,
            "violations": [v.to_dict() for v in self.violations],
            "most_critical": self.most_critical.to_dict() if self.most_critical else None,
            "summary": self.summary,
        }

    @property
    def has_violations(self) -> bool:
        """Check if there are any violations."""
        return len(self.violations) > 0

    @property
    def has_critical_violations(self) -> bool:
        """Check if there are any HIGH criticality violations."""
        return any(v.criticality == "HIGH" for v in self.violations)


# =============================================================================
# Violation Identification
# =============================================================================


def identify_violations(
    result: RuntimeVerificationResult,
    invariant_info: Optional[list[dict[str, Any]]] = None,
) -> ViolationReport:
    """Identify and analyze violations from a verification result.

    Analyzes the verification result to create detailed violation reports
    for each failed invariant.

    Args:
        result: RuntimeVerificationResult from verify_at_runtime.
        invariant_info: Optional list of invariant metadata dicts with
            name, logic, criticality, on_fail keys.

    Returns:
        ViolationReport with detailed violation analysis.
    """
    total_invariants = len(result.invariants)
    passed_count = sum(1 for inv in result.invariants if inv.passed)
    failed_count = total_invariants - passed_count

    # Build invariant info lookup
    info_lookup = {}
    if invariant_info:
        for info in invariant_info:
            info_lookup[info.get("name", "")] = info

    # Identify violations
    violations = []
    for i, inv_result in enumerate(result.invariants):
        if not inv_result.passed:
            # Get additional info if available
            info = info_lookup.get(inv_result.name, {})
            logic = info.get("logic", inv_result.message or "")
            on_fail = info.get("on_fail", "error")

            # Parse expected/actual from message
            expected, actual = _parse_expected_actual(inv_result)

            # Generate suggestion
            suggestion = _generate_suggestion(inv_result, logic)

            violation = ViolationDetail(
                invariant_name=inv_result.name,
                invariant_logic=logic,
                criticality=inv_result.criticality,
                on_fail=on_fail,
                expected=expected,
                actual=actual,
                suggestion=suggestion,
                line_reference=None,  # May be populated by line reporter
            )
            violations.append(violation)

    # Sort by criticality (HIGH first)
    violations = _sort_by_criticality(violations)

    # Determine most critical
    most_critical = violations[0] if violations else None

    # Generate summary
    summary = _generate_summary(result.function_id, total_invariants, violations)

    return ViolationReport(
        function_id=result.function_id,
        total_invariants=total_invariants,
        passed_count=passed_count,
        failed_count=failed_count,
        violations=violations,
        most_critical=most_critical,
        summary=summary,
    )


def _sort_by_criticality(violations: list[ViolationDetail]) -> list[ViolationDetail]:
    """Sort violations by criticality, HIGH first.

    Args:
        violations: List of violations to sort.

    Returns:
        Sorted list with HIGH criticality first.
    """
    return sorted(violations, key=lambda v: CRITICALITY_ORDER.get(v.criticality, 99))


def _parse_expected_actual(inv_result: InvariantResult) -> tuple[Optional[str], Optional[str]]:
    """Parse expected and actual values from invariant result.

    Args:
        inv_result: InvariantResult with message and optional expected/actual.

    Returns:
        Tuple of (expected, actual) strings.
    """
    # If explicitly provided, use those
    if inv_result.expected is not None or inv_result.actual is not None:
        expected = str(inv_result.expected) if inv_result.expected is not None else None
        actual = str(inv_result.actual) if inv_result.actual is not None else None
        return expected, actual

    # Try to parse from message
    message = inv_result.message or ""
    expected = None
    actual = None

    # Common message patterns
    if "violated:" in message.lower():
        # Format: "Invariant violated: <logic>"
        parts = message.split(":", 1)
        if len(parts) > 1:
            expected = f"Should satisfy: {parts[1].strip()}"
            actual = "Condition was false"
    elif "failed" in message.lower():
        expected = f"Invariant '{inv_result.name}' should pass"
        actual = "Invariant check returned false"
    elif message:
        expected = f"Invariant '{inv_result.name}' to hold"
        actual = message

    return expected, actual


def _generate_suggestion(inv_result: InvariantResult, logic: str) -> str:
    """Generate debugging suggestion based on violation.

    Args:
        inv_result: InvariantResult with failure details.
        logic: Invariant logic description.

    Returns:
        Debugging suggestion string.
    """
    message = (inv_result.message or "").lower()
    logic_lower = logic.lower()
    name_lower = inv_result.name.lower()

    # Pattern matching for common violation types
    if "duplicate" in message or "duplicate" in logic_lower or "unique" in name_lower:
        return "Check for missing uniqueness validation or deduplication logic"

    if "null" in message or "none" in message or "null" in logic_lower:
        return "Add null/None checks before this operation"

    if "negative" in message or "negative" in logic_lower:
        return "Check input validation for numeric bounds"

    if "positive" in logic_lower:
        return "Ensure values are validated to be positive"

    if "empty" in message or "empty" in logic_lower:
        return "Add empty check or ensure input is not empty"

    if "range" in logic_lower or "between" in logic_lower or "bound" in logic_lower:
        return "Check boundary conditions and range validation"

    if "type" in message or "isinstance" in logic_lower:
        return "Verify input types match expected types"

    if "length" in logic_lower or "size" in logic_lower or "count" in logic_lower:
        return "Check length/size constraints on input or output"

    if "sorted" in logic_lower or "order" in logic_lower:
        return "Verify sorting/ordering logic is correct"

    if "sum" in logic_lower or "total" in logic_lower:
        return "Check arithmetic operations preserve totals"

    if inv_result.criticality == "HIGH":
        return "This is a critical invariant - review the logic carefully and add defensive checks"

    return "Review the invariant logic against the code implementation"


def _generate_summary(
    function_id: str,
    total_invariants: int,
    violations: list[ViolationDetail],
) -> str:
    """Generate human-readable summary of violations.

    Args:
        function_id: Function that was verified.
        total_invariants: Total number of invariants.
        violations: List of violations.

    Returns:
        Summary string.
    """
    if not violations:
        return f"All {total_invariants} invariants passed for {function_id}"

    failed_count = len(violations)

    if failed_count == 1:
        v = violations[0]
        return f"1 of {total_invariants} invariants violated: {v.invariant_name} ({v.criticality})"

    # Multiple violations
    most_critical = violations[0]  # Already sorted by criticality
    return (
        f"{failed_count} of {total_invariants} invariants violated. "
        f"Most critical: {most_critical.invariant_name} ({most_critical.criticality})"
    )


# =============================================================================
# Convenience Functions
# =============================================================================


def get_violation_by_name(
    report: ViolationReport,
    name: str,
) -> Optional[ViolationDetail]:
    """Get a specific violation by invariant name.

    Args:
        report: ViolationReport to search.
        name: Invariant name to find.

    Returns:
        ViolationDetail if found, None otherwise.
    """
    for violation in report.violations:
        if violation.invariant_name == name:
            return violation
    return None


def get_high_criticality_violations(report: ViolationReport) -> list[ViolationDetail]:
    """Get all HIGH criticality violations.

    Args:
        report: ViolationReport to filter.

    Returns:
        List of HIGH criticality violations.
    """
    return [v for v in report.violations if v.criticality == "HIGH"]


def format_violation_report(report: ViolationReport) -> str:
    """Format violation report as human-readable text.

    Args:
        report: ViolationReport to format.

    Returns:
        Formatted text report.
    """
    lines = []
    lines.append(f"Violation Report for: {report.function_id}")
    lines.append("=" * 60)
    lines.append(f"Total Invariants: {report.total_invariants}")
    lines.append(f"Passed: {report.passed_count}")
    lines.append(f"Failed: {report.failed_count}")
    lines.append("")

    if not report.violations:
        lines.append("No violations detected.")
    else:
        lines.append("Violations (sorted by criticality):")
        lines.append("-" * 40)

        for i, v in enumerate(report.violations, 1):
            lines.append(f"\n{i}. {v.invariant_name} [{v.criticality}]")
            lines.append(f"   Logic: {v.invariant_logic}")
            if v.expected:
                lines.append(f"   Expected: {v.expected}")
            if v.actual:
                lines.append(f"   Actual: {v.actual}")
            if v.suggestion:
                lines.append(f"   Suggestion: {v.suggestion}")
            if v.line_reference:
                lines.append(f"   Line: {v.line_reference}")

    lines.append("")
    lines.append(f"Summary: {report.summary}")

    return "\n".join(lines)
