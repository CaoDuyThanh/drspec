"""Pattern extraction from bug-fix diffs.

This module provides functionality to:
- Extract failure patterns from code changes
- Categorize patterns by type
- Generate natural language descriptions
- Map patterns to invariant types
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from drspec.learning.diff import FileDiff


class PatternType(str, Enum):
    """Categories of bug fix patterns.

    These correspond to common failure modes that contracts can prevent.
    """

    NULL_CHECK = "null_check"
    BOUNDS_CHECK = "bounds_check"
    TYPE_CHECK = "type_check"
    EMPTY_CHECK = "empty_check"
    DUPLICATE_CHECK = "duplicate_check"
    RANGE_CHECK = "range_check"
    FORMAT_CHECK = "format_check"
    EXCEPTION_HANDLING = "exception_handling"
    OFF_BY_ONE = "off_by_one"
    INITIALIZATION = "initialization"
    RESOURCE_MANAGEMENT = "resource_management"
    CONCURRENCY = "concurrency"
    UNKNOWN = "unknown"


# Pattern detection rules
PATTERN_RULES: Dict[PatternType, List[str]] = {
    PatternType.NULL_CHECK: [
        r"if\s+\w+\s+is\s+None",
        r"if\s+\w+\s+is\s+not\s+None",
        r"if\s+not\s+\w+:",
        r"if\s+\w+:",
        r"\w+\s+!=\s+None",
        r"\w+\s+==\s+None",
        r"\.get\(",
        r"or\s+\[\]",
        r"or\s+\{\}",
        r"or\s+\"\"",
    ],
    PatternType.BOUNDS_CHECK: [
        r"if\s+len\(",
        r"if\s+\w+\s*<\s*len\(",
        r"if\s+\w+\s*>=\s*0",
        r"if\s+\w+\s*>\s*0",
        r"\[\d+:\d+\]",
        r"min\(",
        r"max\(",
    ],
    PatternType.TYPE_CHECK: [
        r"isinstance\(",
        r"type\(\w+\)\s*==",
        r"type\(\w+\)\s*is",
        r"hasattr\(",
    ],
    PatternType.EMPTY_CHECK: [
        r"if\s+not\s+\w+:",
        r"if\s+len\(\w+\)\s*==\s*0",
        r"if\s+len\(\w+\)\s*>\s*0",
        r"if\s+\w+\s*==\s*\[\]",
        r"if\s+\w+\s*==\s*\{\}",
        r"if\s+\w+\s*==\s*\"\"",
    ],
    PatternType.DUPLICATE_CHECK: [
        r"set\(",
        r"if\s+\w+\s+in\s+",
        r"if\s+\w+\s+not\s+in\s+",
        r"\.add\(",
        r"dedupe",
        r"unique",
    ],
    PatternType.RANGE_CHECK: [
        r"\d+\s*<=\s*\w+\s*<=\s*\d+",
        r"\w+\s*>=\s*\d+",
        r"\w+\s*<=\s*\d+",
        r"clamp\(",
        r"between",
    ],
    PatternType.FORMAT_CHECK: [
        r"re\.(match|search|findall)",
        r"\.strip\(",
        r"\.lower\(",
        r"\.upper\(",
        r"validate",
        r"parse",
    ],
    PatternType.EXCEPTION_HANDLING: [
        r"try:",
        r"except\s+\w+:",
        r"finally:",
        r"raise\s+\w+",
        r"\.catch\(",
    ],
    PatternType.OFF_BY_ONE: [
        r"range\(\s*\d+\s*,\s*len\(",
        r"range\(\s*len\(",
        r"\[\s*:\s*-\s*1\s*\]",
        r"\+\s*1\s*\]",
        r"-\s*1\s*\]",
    ],
    PatternType.INITIALIZATION: [
        r"=\s*None",
        r"=\s*\[\]",
        r"=\s*\{\}",
        r"=\s*0",
        r"=\s*\"\"",
        r"default",
        r"init",
    ],
    PatternType.RESOURCE_MANAGEMENT: [
        r"with\s+open\(",
        r"\.close\(",
        r"finally:",
        r"__enter__",
        r"__exit__",
    ],
    PatternType.CONCURRENCY: [
        r"lock",
        r"mutex",
        r"async\s+def",
        r"await\s+",
        r"\.acquire\(",
        r"\.release\(",
        r"thread",
    ],
}

# Invariant templates for each pattern type
INVARIANT_TEMPLATES: Dict[PatternType, List[str]] = {
    PatternType.NULL_CHECK: [
        "{param} must not be None",
        "Output is never None when input is valid",
        "Returns None only when {condition}",
    ],
    PatternType.BOUNDS_CHECK: [
        "Index must be within bounds of {collection}",
        "Length of output matches expected length",
        "Access is always within array bounds",
    ],
    PatternType.TYPE_CHECK: [
        "{param} must be of type {type}",
        "Output is always of type {type}",
        "Returns consistent type",
    ],
    PatternType.EMPTY_CHECK: [
        "{param} must not be empty",
        "Output is not empty when input is valid",
        "Returns empty only when {condition}",
    ],
    PatternType.DUPLICATE_CHECK: [
        "Output contains no duplicates",
        "All items in output are unique",
        "No duplicate {item} in result",
    ],
    PatternType.RANGE_CHECK: [
        "{param} must be between {min} and {max}",
        "Output value is within expected range",
        "Result is always positive/non-negative",
    ],
    PatternType.FORMAT_CHECK: [
        "{param} must match expected format",
        "Output follows {format} format",
        "String is properly formatted",
    ],
    PatternType.EXCEPTION_HANDLING: [
        "Does not raise {exception} under normal conditions",
        "Handles {error} gracefully",
        "Returns error result instead of throwing",
    ],
    PatternType.OFF_BY_ONE: [
        "Iteration covers all elements",
        "Slice includes/excludes boundary correctly",
        "Count is exactly correct",
    ],
    PatternType.INITIALIZATION: [
        "{param} has default value when not provided",
        "State is properly initialized before use",
        "All fields are set before return",
    ],
    PatternType.RESOURCE_MANAGEMENT: [
        "Resources are properly cleaned up",
        "File/connection is closed after use",
        "No resource leaks",
    ],
    PatternType.CONCURRENCY: [
        "Thread-safe access to shared state",
        "No race conditions on {resource}",
        "Properly awaits async operations",
    ],
}


@dataclass
class ExtractedPattern:
    """A pattern extracted from a bug-fix diff.

    Attributes:
        pattern_type: Category of the pattern.
        description: Natural language description.
        code_before: Code before the fix.
        code_after: Code after the fix.
        file_path: Path to the file.
        function_name: Name of the affected function.
        line_range: Affected line range (start, end).
        confidence: Confidence in the pattern detection (0-1).
        invariant_suggestions: Suggested invariant texts.
    """

    pattern_type: PatternType
    description: str
    code_before: str
    code_after: str
    file_path: str
    function_name: Optional[str] = None
    line_range: Optional[Tuple[int, int]] = None
    confidence: float = 0.5
    invariant_suggestions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "pattern_type": self.pattern_type.value,
            "description": self.description,
            "code_before": self.code_before,
            "code_after": self.code_after,
            "file_path": self.file_path,
            "function_name": self.function_name,
            "line_range": self.line_range,
            "confidence": self.confidence,
            "invariant_suggestions": self.invariant_suggestions,
        }


def categorize_pattern(
    removed_lines: List[str],
    added_lines: List[str],
) -> Tuple[PatternType, float]:
    """Categorize a code change into a pattern type.

    Args:
        removed_lines: Lines that were removed.
        added_lines: Lines that were added.

    Returns:
        Tuple of (pattern_type, confidence).
    """
    # Combine added lines for pattern matching
    added_text = "\n".join(added_lines)
    removed_text = "\n".join(removed_lines)

    scores: Dict[PatternType, float] = {pt: 0.0 for pt in PatternType}

    # Score each pattern type based on matches in added code
    for pattern_type, rules in PATTERN_RULES.items():
        for rule in rules:
            if re.search(rule, added_text, re.IGNORECASE):
                scores[pattern_type] += 1.0

    # Bonus for patterns that appear in added but not removed
    for pattern_type, rules in PATTERN_RULES.items():
        for rule in rules:
            added_match = re.search(rule, added_text, re.IGNORECASE)
            removed_match = re.search(rule, removed_text, re.IGNORECASE)
            if added_match and not removed_match:
                scores[pattern_type] += 0.5

    # Find best match
    best_type = PatternType.UNKNOWN
    best_score = 0.0

    for pattern_type, score in scores.items():
        if score > best_score:
            best_type = pattern_type
            best_score = score

    # Normalize confidence
    confidence = min(1.0, best_score / 3.0) if best_score > 0 else 0.0

    return best_type, confidence


def generate_pattern_description(
    pattern_type: PatternType,
    removed_lines: List[str],
    added_lines: List[str],
) -> str:
    """Generate a natural language description of a pattern.

    Args:
        pattern_type: The detected pattern type.
        removed_lines: Lines that were removed.
        added_lines: Lines that were added.

    Returns:
        Human-readable description.
    """
    descriptions: Dict[PatternType, str] = {
        PatternType.NULL_CHECK: "Added null/None check to prevent NoneType errors",
        PatternType.BOUNDS_CHECK: "Added bounds checking to prevent index out of range",
        PatternType.TYPE_CHECK: "Added type validation to ensure correct input types",
        PatternType.EMPTY_CHECK: "Added empty check to handle empty inputs gracefully",
        PatternType.DUPLICATE_CHECK: "Added duplicate detection to prevent duplicate entries",
        PatternType.RANGE_CHECK: "Added range validation to ensure values are in expected range",
        PatternType.FORMAT_CHECK: "Added format validation for string inputs",
        PatternType.EXCEPTION_HANDLING: "Added exception handling for error conditions",
        PatternType.OFF_BY_ONE: "Fixed off-by-one error in loop or slice",
        PatternType.INITIALIZATION: "Fixed initialization to ensure proper default values",
        PatternType.RESOURCE_MANAGEMENT: "Fixed resource management to prevent leaks",
        PatternType.CONCURRENCY: "Fixed concurrency issue for thread safety",
        PatternType.UNKNOWN: "Code change with unidentified pattern",
    }

    base_desc = descriptions.get(pattern_type, "Unknown pattern")

    # Try to extract more specific info from the code
    added_text = "\n".join(added_lines)

    # Look for specific variable names
    var_match = re.search(r"if\s+(\w+)\s+is\s+", added_text)
    if var_match and pattern_type == PatternType.NULL_CHECK:
        return f"Added null check for '{var_match.group(1)}'"

    len_match = re.search(r"if\s+len\((\w+)\)", added_text)
    if len_match and pattern_type in (PatternType.BOUNDS_CHECK, PatternType.EMPTY_CHECK):
        return f"Added length check for '{len_match.group(1)}'"

    return base_desc


def _get_invariant_suggestions(pattern_type: PatternType) -> List[str]:
    """Get invariant suggestions for a pattern type.

    Args:
        pattern_type: The pattern type.

    Returns:
        List of invariant suggestion templates.
    """
    templates = INVARIANT_TEMPLATES.get(pattern_type, [])
    if not templates:
        return ["Code behavior should be verified"]
    return templates[:2]  # Return first 2 suggestions


def extract_patterns_from_diff(
    file_diff: FileDiff,
    function_name: Optional[str] = None,
) -> List[ExtractedPattern]:
    """Extract patterns from a file diff.

    Args:
        file_diff: The file diff to analyze.
        function_name: Optional function name for context.

    Returns:
        List of extracted patterns.
    """
    patterns: List[ExtractedPattern] = []

    for hunk in file_diff.hunks:
        removed = hunk.removed_lines
        added = hunk.added_lines

        # Skip if no real changes
        if not removed and not added:
            continue

        # Skip if only whitespace changes
        if (
            "".join(removed).strip() == "".join(added).strip()
        ):
            continue

        # Categorize the pattern
        pattern_type, confidence = categorize_pattern(removed, added)

        # Generate description
        description = generate_pattern_description(pattern_type, removed, added)

        # Get invariant suggestions
        suggestions = _get_invariant_suggestions(pattern_type)

        pattern = ExtractedPattern(
            pattern_type=pattern_type,
            description=description,
            code_before="\n".join(removed),
            code_after="\n".join(added),
            file_path=file_diff.path,
            function_name=function_name or hunk.header.strip(),
            line_range=(hunk.new_start, hunk.new_start + hunk.new_count),
            confidence=confidence,
            invariant_suggestions=suggestions,
        )

        patterns.append(pattern)

    return patterns


def extract_all_patterns(
    file_diffs: List[FileDiff],
    modified_functions: Optional[List[Tuple[str, str, str]]] = None,
) -> List[ExtractedPattern]:
    """Extract patterns from multiple file diffs.

    Args:
        file_diffs: List of file diffs.
        modified_functions: Optional list of (function_id, file_path, function_name).

    Returns:
        List of all extracted patterns.
    """
    all_patterns: List[ExtractedPattern] = []

    # Build function map
    func_map: Dict[str, str] = {}
    if modified_functions:
        for fid, fpath, fname in modified_functions:
            func_map[fpath] = fname

    for file_diff in file_diffs:
        function_name = func_map.get(file_diff.path)
        patterns = extract_patterns_from_diff(file_diff, function_name)
        all_patterns.extend(patterns)

    return all_patterns
