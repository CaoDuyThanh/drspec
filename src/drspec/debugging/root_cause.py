"""Root cause line reporter for DrSpec debugger agent.

This module provides APIs for identifying the likely root cause lines
in source code based on invariant violations.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from drspec.debugging.violation import ViolationDetail


# =============================================================================
# Root Cause Models
# =============================================================================


@dataclass
class RootCauseCandidate:
    """A potential root cause line in source code.

    Attributes:
        line_number: 1-based line number in source.
        confidence: Confidence score 0.0 to 1.0.
        explanation: Why this line is a likely root cause.
        code_snippet: Context lines around the root cause.
        highlighted_line: The specific line of code.
    """

    line_number: int
    confidence: float
    explanation: str
    code_snippet: str
    highlighted_line: str

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "line_number": self.line_number,
            "confidence": self.confidence,
            "explanation": self.explanation,
            "code_snippet": self.code_snippet,
            "highlighted_line": self.highlighted_line,
        }


@dataclass
class RootCauseReport:
    """Complete root cause analysis report.

    Attributes:
        function_id: Function that was analyzed.
        file_path: File path of the function.
        violation: The violation being analyzed.
        primary_candidate: Most likely root cause.
        secondary_candidates: Other potential root causes.
        source_is_current: Whether source hash matches artifact.
        recommendation: Action to fix the issue.
    """

    function_id: str
    file_path: str
    violation: ViolationDetail
    primary_candidate: Optional[RootCauseCandidate] = None
    secondary_candidates: list[RootCauseCandidate] = field(default_factory=list)
    source_is_current: bool = True
    recommendation: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "function_id": self.function_id,
            "file_path": self.file_path,
            "violation": self.violation.to_dict(),
            "primary_candidate": self.primary_candidate.to_dict() if self.primary_candidate else None,
            "secondary_candidates": [c.to_dict() for c in self.secondary_candidates],
            "source_is_current": self.source_is_current,
            "recommendation": self.recommendation,
        }

    @property
    def has_root_cause(self) -> bool:
        """Check if a root cause was identified."""
        return self.primary_candidate is not None

    @property
    def all_candidates(self) -> list[RootCauseCandidate]:
        """Get all candidates including primary."""
        if self.primary_candidate:
            return [self.primary_candidate] + self.secondary_candidates
        return self.secondary_candidates


# =============================================================================
# Root Cause Analysis
# =============================================================================


def identify_root_cause(
    function_id: str,
    file_path: str,
    source_code: str,
    violation: ViolationDetail,
    start_line: int = 1,
    stored_hash: Optional[str] = None,
) -> RootCauseReport:
    """Identify likely root cause lines for a violation.

    Analyzes source code to find lines that likely caused the
    invariant violation based on pattern matching and heuristics.

    Args:
        function_id: Function ID being analyzed.
        file_path: Path to the source file.
        source_code: Source code of the function.
        violation: The violation to analyze.
        start_line: Starting line number of the function in file.
        stored_hash: Hash of stored source (for freshness check).

    Returns:
        RootCauseReport with candidate root cause lines.
    """
    # Check source freshness
    current_hash = hashlib.sha256(source_code.encode()).hexdigest()
    source_is_current = stored_hash is None or current_hash == stored_hash

    # Analyze for root cause candidates
    candidates = _analyze_source_for_root_cause(
        source_code=source_code,
        violation=violation,
        start_line=start_line,
    )

    # Sort by confidence
    candidates = sorted(candidates, key=lambda c: c.confidence, reverse=True)

    # Separate primary and secondary
    primary = candidates[0] if candidates else None
    secondary = candidates[1:5] if len(candidates) > 1 else []  # Top 4 secondary

    # Generate recommendation
    recommendation = _generate_recommendation(violation, primary)

    return RootCauseReport(
        function_id=function_id,
        file_path=file_path,
        violation=violation,
        primary_candidate=primary,
        secondary_candidates=secondary,
        source_is_current=source_is_current,
        recommendation=recommendation,
    )


def _analyze_source_for_root_cause(
    source_code: str,
    violation: ViolationDetail,
    start_line: int,
) -> list[RootCauseCandidate]:
    """Analyze source code to find potential root cause lines.

    Args:
        source_code: Function source code.
        violation: Violation to analyze.
        start_line: Starting line number in file.

    Returns:
        List of RootCauseCandidate objects.
    """
    candidates = []
    lines = source_code.split("\n")

    violation_lower = (violation.invariant_logic or "").lower()
    actual_lower = (violation.actual or "").lower()
    name_lower = violation.invariant_name.lower()

    # Extract keywords from violation
    keywords = _extract_keywords(violation)

    # Pattern analysis
    for i, line in enumerate(lines):
        line_lower = line.lower()
        line_number = start_line + i
        confidence = 0.0
        explanations = []

        # Pattern 1: Keyword matching
        matched_keywords = [kw for kw in keywords if kw in line_lower]
        if matched_keywords:
            confidence += 0.3
            explanations.append(f"Contains relevant keywords: {', '.join(matched_keywords)}")

        # Pattern 2: Duplicate-related violations
        if "duplicate" in name_lower or "duplicate" in violation_lower or "unique" in name_lower:
            if _is_collection_add(line):
                if not _has_check_before(lines, i, ["not in", "if", "check"]):
                    confidence += 0.4
                    explanations.append("Adds to collection without duplicate check")

        # Pattern 3: Null/None violations
        if "null" in name_lower or "none" in actual_lower or "null" in actual_lower:
            if _might_return_none(line):
                confidence += 0.35
                explanations.append("Could return or assign None without check")

        # Pattern 4: Negative/positive violations
        if "negative" in actual_lower or "positive" in name_lower:
            if _is_arithmetic_operation(line):
                confidence += 0.3
                explanations.append("Arithmetic operation that could produce invalid values")

        # Pattern 5: Empty check violations
        if "empty" in name_lower or "empty" in violation_lower:
            if "return" in line_lower and ("[]" in line or "{}" in line or '""' in line):
                confidence += 0.4
                explanations.append("Returns empty collection/string")

        # Pattern 6: Return statements (often root cause for output violations)
        if "return" in line_lower:
            confidence += 0.15
            explanations.append("Return statement affecting output")

        # Pattern 7: Assignment to result variables
        if _is_result_assignment(line):
            confidence += 0.2
            explanations.append("Assignment to potential output variable")

        # Pattern 8: Conditional without else (potential missing case)
        if line.strip().startswith("if ") and not _has_else_following(lines, i):
            confidence += 0.1
            explanations.append("Conditional without else clause")

        # Only add as candidate if confidence is above threshold
        if confidence >= 0.25 and explanations:
            snippet = _extract_snippet(lines, i, context=2)
            candidates.append(RootCauseCandidate(
                line_number=line_number,
                confidence=min(confidence, 1.0),
                explanation="; ".join(explanations),
                code_snippet=snippet,
                highlighted_line=line.strip(),
            ))

    return candidates


def _extract_keywords(violation: ViolationDetail) -> list[str]:
    """Extract relevant keywords from violation.

    Args:
        violation: ViolationDetail to analyze.

    Returns:
        List of lowercase keywords.
    """
    keywords = set()

    # From invariant name (split on underscore)
    for part in violation.invariant_name.lower().split("_"):
        if len(part) >= 3:
            keywords.add(part)

    # From actual value
    if violation.actual:
        # Extract identifiers from actual
        words = re.findall(r"\b[a-z_][a-z0-9_]*\b", violation.actual.lower())
        for word in words:
            if len(word) >= 3:
                keywords.add(word)

    # Common violation-related keywords
    violation_keywords = {
        "duplicate": ["duplicate", "add", "append", "extend", "insert"],
        "null": ["none", "null", "return", "="],
        "empty": ["empty", "len", "[]", "{}", '""'],
        "negative": ["negative", "-", "subtract", "minus"],
        "positive": ["positive", "abs", "max", "min"],
        "unique": ["unique", "set", "distinct"],
    }

    name_lower = violation.invariant_name.lower()
    for key, extra in violation_keywords.items():
        if key in name_lower:
            keywords.update(extra)

    return list(keywords)


def _is_collection_add(line: str) -> bool:
    """Check if line adds to a collection."""
    patterns = [".append(", ".extend(", ".add(", ".insert(", ".update(", "+="]
    return any(p in line for p in patterns)


def _has_check_before(lines: list[str], index: int, check_keywords: list[str]) -> bool:
    """Check if there's a validation check before the given line."""
    # Look at previous 3 lines
    for i in range(max(0, index - 3), index):
        line_lower = lines[i].lower()
        if any(kw in line_lower for kw in check_keywords):
            return True
    return False


def _might_return_none(line: str) -> bool:
    """Check if line might return or assign None."""
    line_lower = line.lower()
    return ("return" in line_lower and "none" in line_lower) or ("= none" in line_lower)


def _is_arithmetic_operation(line: str) -> bool:
    """Check if line contains arithmetic that could produce negative values."""
    return bool(re.search(r"[-+*/]", line) and "=" in line)


def _is_result_assignment(line: str) -> bool:
    """Check if line assigns to a result-like variable."""
    result_vars = ["result", "output", "ret", "response", "data", "value"]
    line_lower = line.lower()
    return any(f"{var} =" in line_lower or f"{var}=" in line_lower for var in result_vars)


def _has_else_following(lines: list[str], index: int) -> bool:
    """Check if there's an else clause following an if statement."""
    # Look at next 5 lines for else
    for i in range(index + 1, min(len(lines), index + 6)):
        stripped = lines[i].strip()
        if stripped.startswith("else:") or stripped.startswith("elif "):
            return True
        # If we hit another if at same indentation, stop
        if stripped.startswith("if ") and len(lines[index]) - len(lines[index].lstrip()) == len(lines[i]) - len(lines[i].lstrip()):
            return False
    return False


def _extract_snippet(lines: list[str], index: int, context: int = 2) -> str:
    """Extract code snippet with line numbers.

    Args:
        lines: Source code lines.
        index: 0-based index of target line.
        context: Number of context lines before/after.

    Returns:
        Formatted code snippet string.
    """
    start = max(0, index - context)
    end = min(len(lines), index + context + 1)

    snippet_lines = []
    for i in range(start, end):
        marker = "-->" if i == index else "   "
        line_num = i + 1
        snippet_lines.append(f"{line_num:4}{marker} {lines[i]}")

    return "\n".join(snippet_lines)


def _generate_recommendation(
    violation: ViolationDetail,
    primary: Optional[RootCauseCandidate],
) -> str:
    """Generate a recommendation for fixing the violation.

    Args:
        violation: The violation being analyzed.
        primary: Primary root cause candidate (if found).

    Returns:
        Recommendation string.
    """
    if not primary:
        return "Unable to identify specific root cause. Review the invariant logic manually."

    name_lower = violation.invariant_name.lower()
    logic_lower = (violation.invariant_logic or "").lower()

    # Generate specific recommendations based on violation type
    if "duplicate" in name_lower or "unique" in name_lower:
        return f"Add a uniqueness check before line {primary.line_number}. Consider using a set or checking membership before adding."

    if "null" in name_lower or "none" in logic_lower:
        return f"Add a null/None check at line {primary.line_number}. Consider early return or raising an exception for invalid input."

    if "positive" in name_lower or "negative" in logic_lower:
        return f"Add bounds validation at line {primary.line_number}. Ensure values are within expected range."

    if "empty" in name_lower:
        return f"Handle the empty case at line {primary.line_number}. Consider returning a default value or raising an error."

    if "range" in name_lower or "bound" in name_lower:
        return f"Add boundary checks at line {primary.line_number}. Validate input is within acceptable range."

    # Generic recommendation
    return f"Review line {primary.line_number}: {primary.explanation}. Consider adding validation to ensure the invariant holds."


# =============================================================================
# Convenience Functions
# =============================================================================


def format_root_cause_report(report: RootCauseReport) -> str:
    """Format root cause report as human-readable text.

    Args:
        report: RootCauseReport to format.

    Returns:
        Formatted text report.
    """
    lines = []
    lines.append(f"Root Cause Analysis: {report.function_id}")
    lines.append("=" * 60)
    lines.append(f"File: {report.file_path}")
    lines.append(f"Violation: {report.violation.invariant_name} ({report.violation.criticality})")
    lines.append("")

    if not report.source_is_current:
        lines.append("WARNING: Source code has changed since last scan!")
        lines.append("         Line numbers may be inaccurate. Run 'drspec scan' to update.")
        lines.append("")

    if report.primary_candidate:
        lines.append("PRIMARY ROOT CAUSE")
        lines.append("-" * 40)
        lines.append(f"Line {report.primary_candidate.line_number} (confidence: {report.primary_candidate.confidence:.0%})")
        lines.append(f"Explanation: {report.primary_candidate.explanation}")
        lines.append("")
        lines.append("Code:")
        lines.append(report.primary_candidate.code_snippet)
        lines.append("")
    else:
        lines.append("No specific root cause identified.")
        lines.append("")

    if report.secondary_candidates:
        lines.append("SECONDARY CANDIDATES")
        lines.append("-" * 40)
        for i, candidate in enumerate(report.secondary_candidates, 1):
            lines.append(f"{i}. Line {candidate.line_number} (confidence: {candidate.confidence:.0%})")
            lines.append(f"   {candidate.explanation}")
        lines.append("")

    lines.append("RECOMMENDATION")
    lines.append("-" * 40)
    lines.append(report.recommendation)

    return "\n".join(lines)


def get_high_confidence_candidates(
    report: RootCauseReport,
    threshold: float = 0.5,
) -> list[RootCauseCandidate]:
    """Get candidates above a confidence threshold.

    Args:
        report: RootCauseReport to filter.
        threshold: Minimum confidence score.

    Returns:
        List of candidates meeting threshold.
    """
    return [c for c in report.all_candidates if c.confidence >= threshold]
