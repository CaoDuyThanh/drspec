"""Contract strengthening from bug patterns.

This module provides functionality to:
- Match patterns to existing contracts
- Suggest new invariants from patterns
- Boost confidence for validated contracts
- Track validation history
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from drspec.contracts.schema import Contract, Invariant, Criticality, OnFail
from drspec.learning.patterns import ExtractedPattern, PatternType


# Map pattern types to criticality levels
PATTERN_CRITICALITY: Dict[PatternType, Criticality] = {
    PatternType.NULL_CHECK: Criticality.HIGH,
    PatternType.BOUNDS_CHECK: Criticality.HIGH,
    PatternType.TYPE_CHECK: Criticality.MEDIUM,
    PatternType.EMPTY_CHECK: Criticality.MEDIUM,
    PatternType.DUPLICATE_CHECK: Criticality.MEDIUM,
    PatternType.RANGE_CHECK: Criticality.MEDIUM,
    PatternType.FORMAT_CHECK: Criticality.LOW,
    PatternType.EXCEPTION_HANDLING: Criticality.HIGH,
    PatternType.OFF_BY_ONE: Criticality.HIGH,
    PatternType.INITIALIZATION: Criticality.MEDIUM,
    PatternType.RESOURCE_MANAGEMENT: Criticality.HIGH,
    PatternType.CONCURRENCY: Criticality.HIGH,
    PatternType.UNKNOWN: Criticality.LOW,
}

# Confidence boost amounts
CONFIDENCE_BOOST_BUG_FIX = 0.05  # Boost when pattern validates existing invariant
CONFIDENCE_BOOST_VALIDATED = 0.10  # Boost when invariant is fully validated


@dataclass
class InvariantSuggestion:
    """A suggested invariant from a bug pattern.

    Attributes:
        name: Suggested invariant name.
        logic: Natural language description of the rule.
        criticality: Suggested criticality level.
        on_fail: Suggested failure action.
        source_pattern: The pattern that generated this suggestion.
        confidence: Confidence in the suggestion.
        reasoning: Why this invariant is suggested.
    """

    name: str
    logic: str
    criticality: Criticality
    on_fail: OnFail
    source_pattern: PatternType
    confidence: float = 0.5
    reasoning: str = ""

    def to_invariant(self) -> Invariant:
        """Convert to an Invariant object."""
        return Invariant(
            name=self.name,
            logic=self.logic,
            criticality=self.criticality,
            on_fail=self.on_fail,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "name": self.name,
            "logic": self.logic,
            "criticality": self.criticality.value,
            "on_fail": self.on_fail.value,
            "source_pattern": self.source_pattern.value,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
        }


@dataclass
class ContractStrengthening:
    """Result of contract strengthening analysis.

    Attributes:
        function_id: Function ID being strengthened.
        existing_contract: The existing contract (if any).
        new_invariants: Suggested new invariants.
        validated_invariants: Existing invariants validated by patterns.
        confidence_boost: Suggested confidence boost.
        patterns_used: Patterns used for strengthening.
        recommendations: Human-readable recommendations.
    """

    function_id: str
    existing_contract: Optional[Contract] = None
    new_invariants: List[InvariantSuggestion] = field(default_factory=list)
    validated_invariants: List[str] = field(default_factory=list)
    confidence_boost: float = 0.0
    patterns_used: List[ExtractedPattern] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)

    @property
    def has_suggestions(self) -> bool:
        """Check if there are any suggestions."""
        return bool(self.new_invariants or self.validated_invariants)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "function_id": self.function_id,
            "has_existing_contract": self.existing_contract is not None,
            "new_invariants": [inv.to_dict() for inv in self.new_invariants],
            "validated_invariants": self.validated_invariants,
            "confidence_boost": self.confidence_boost,
            "patterns_used": [p.to_dict() for p in self.patterns_used],
            "recommendations": self.recommendations,
        }


def match_pattern_to_contract(
    pattern: ExtractedPattern,
    contract: Contract,
) -> List[str]:
    """Find invariants in a contract that match a pattern.

    Args:
        pattern: The extracted pattern.
        contract: The existing contract.

    Returns:
        List of matching invariant names.
    """
    matches: List[str] = []

    # Keywords to look for based on pattern type
    keywords: Dict[PatternType, List[str]] = {
        PatternType.NULL_CHECK: ["none", "null", "not none", "is not none"],
        PatternType.BOUNDS_CHECK: ["bounds", "length", "size", "index", "within"],
        PatternType.TYPE_CHECK: ["type", "isinstance", "is a", "must be"],
        PatternType.EMPTY_CHECK: ["empty", "not empty", "non-empty", "length"],
        PatternType.DUPLICATE_CHECK: ["duplicate", "unique", "distinct", "no duplicates"],
        PatternType.RANGE_CHECK: ["range", "between", "minimum", "maximum", "positive", "negative"],
        PatternType.FORMAT_CHECK: ["format", "pattern", "valid", "match"],
        PatternType.EXCEPTION_HANDLING: ["error", "exception", "raise", "throw"],
        PatternType.OFF_BY_ONE: ["all", "every", "each", "count"],
        PatternType.INITIALIZATION: ["default", "initial", "set", "defined"],
        PatternType.RESOURCE_MANAGEMENT: ["close", "cleanup", "release", "dispose"],
        PatternType.CONCURRENCY: ["thread", "safe", "lock", "concurrent"],
    }

    pattern_keywords = keywords.get(pattern.pattern_type, [])

    for invariant in contract.invariants:
        logic_lower = invariant.logic.lower()
        name_lower = invariant.name.lower()

        for keyword in pattern_keywords:
            if keyword in logic_lower or keyword in name_lower:
                matches.append(invariant.name)
                break

    return matches


def suggest_invariants(
    pattern: ExtractedPattern,
) -> List[InvariantSuggestion]:
    """Generate invariant suggestions from a pattern.

    Args:
        pattern: The extracted pattern.

    Returns:
        List of suggested invariants.
    """
    suggestions: List[InvariantSuggestion] = []

    criticality = PATTERN_CRITICALITY.get(pattern.pattern_type, Criticality.LOW)
    on_fail = OnFail.ERROR if criticality == Criticality.HIGH else OnFail.WARN

    for i, logic in enumerate(pattern.invariant_suggestions):
        name = f"{pattern.pattern_type.value}_{i + 1}"

        suggestion = InvariantSuggestion(
            name=name,
            logic=logic,
            criticality=criticality,
            on_fail=on_fail,
            source_pattern=pattern.pattern_type,
            confidence=pattern.confidence * 0.8,  # Slightly lower confidence
            reasoning=f"Suggested based on {pattern.pattern_type.value} pattern: {pattern.description}",
        )

        suggestions.append(suggestion)

    return suggestions


def strengthen_contract(
    function_id: str,
    patterns: List[ExtractedPattern],
    existing_contract: Optional[Contract] = None,
) -> ContractStrengthening:
    """Suggest contract improvements from bug patterns.

    Args:
        function_id: Function ID to strengthen.
        patterns: Extracted patterns from bug fixes.
        existing_contract: Existing contract (if any).

    Returns:
        ContractStrengthening with suggestions.
    """
    result = ContractStrengthening(
        function_id=function_id,
        existing_contract=existing_contract,
        patterns_used=patterns,
    )

    seen_invariant_names: set = set()

    for pattern in patterns:
        # Check for matches with existing contract
        if existing_contract:
            matches = match_pattern_to_contract(pattern, existing_contract)
            for match in matches:
                if match not in result.validated_invariants:
                    result.validated_invariants.append(match)
                    result.confidence_boost += CONFIDENCE_BOOST_BUG_FIX

        # Generate new invariant suggestions
        suggestions = suggest_invariants(pattern)
        for suggestion in suggestions:
            # Skip if we already have a similar invariant
            if existing_contract:
                existing_names = {inv.name for inv in existing_contract.invariants}
                if suggestion.name in existing_names:
                    continue

            # Skip duplicates
            if suggestion.name in seen_invariant_names:
                continue

            seen_invariant_names.add(suggestion.name)
            result.new_invariants.append(suggestion)

    # Cap confidence boost
    result.confidence_boost = min(0.25, result.confidence_boost)

    # Generate recommendations
    if result.validated_invariants:
        result.recommendations.append(
            f"Bug fix validated {len(result.validated_invariants)} existing invariant(s). "
            f"Consider boosting contract confidence by {result.confidence_boost:.0%}."
        )

    if result.new_invariants:
        result.recommendations.append(
            f"Found {len(result.new_invariants)} potential new invariant(s) from bug patterns. "
            "Review and add relevant ones to the contract."
        )

    if not result.has_suggestions:
        result.recommendations.append(
            "No contract strengthening suggestions from this bug fix. "
            "The patterns may not map to contract invariants."
        )

    return result


def apply_strengthening(
    contract: Contract,
    strengthening: ContractStrengthening,
    add_new_invariants: bool = False,
) -> Tuple[Contract, float]:
    """Apply strengthening to a contract.

    Args:
        contract: The contract to strengthen.
        strengthening: The strengthening suggestions.
        add_new_invariants: Whether to add new invariants.

    Returns:
        Tuple of (updated_contract, new_confidence_score).
    """
    # Create new invariants list
    new_invariants = list(contract.invariants)

    if add_new_invariants:
        for suggestion in strengthening.new_invariants:
            new_invariants.append(suggestion.to_invariant())

    # Create updated contract
    updated = Contract(
        function_signature=contract.function_signature,
        intent_summary=contract.intent_summary,
        invariants=new_invariants,
        io_examples=contract.io_examples,
    )

    # Return with confidence boost
    return updated, strengthening.confidence_boost
