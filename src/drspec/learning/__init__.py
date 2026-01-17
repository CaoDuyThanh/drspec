"""Learning module for DrSpec bug-driven learning.

This module provides functionality for:
- Analyzing git diffs for bug fixes
- Extracting failure patterns from code changes
- Strengthening contracts based on learned patterns
- Storing learning history for auditing
"""

from __future__ import annotations

from drspec.learning.diff import (
    DiffHunk,
    FileDiff,
    CommitDiff,
    DiffAnalysis,
    parse_unified_diff,
    analyze_commit,
    analyze_commit_range,
    get_modified_functions,
)
from drspec.learning.patterns import (
    PatternType,
    ExtractedPattern,
    extract_patterns_from_diff,
    categorize_pattern,
    generate_pattern_description,
)
from drspec.learning.strengthening import (
    InvariantSuggestion,
    ContractStrengthening,
    strengthen_contract,
    match_pattern_to_contract,
    suggest_invariants,
)
from drspec.learning.history import (
    LearningEvent,
    insert_learning_event,
    get_learning_history,
    get_learning_stats,
)

__all__ = [
    # Diff analysis
    "DiffHunk",
    "FileDiff",
    "CommitDiff",
    "DiffAnalysis",
    "parse_unified_diff",
    "analyze_commit",
    "analyze_commit_range",
    "get_modified_functions",
    # Pattern extraction
    "PatternType",
    "ExtractedPattern",
    "extract_patterns_from_diff",
    "categorize_pattern",
    "generate_pattern_description",
    # Contract strengthening
    "InvariantSuggestion",
    "ContractStrengthening",
    "strengthen_contract",
    "match_pattern_to_contract",
    "suggest_invariants",
    # History
    "LearningEvent",
    "insert_learning_event",
    "get_learning_history",
    "get_learning_stats",
]
