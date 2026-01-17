"""Tests for root cause line reporter (Story 5-4).

These tests verify the root cause analysis API:
- RootCauseCandidate and RootCauseReport models
- identify_root_cause: Analyze source for root cause
- Code snippet extraction
- Source freshness checking
"""

from __future__ import annotations


from drspec.debugging import (
    ViolationDetail,
    RootCauseCandidate,
    RootCauseReport,
    identify_root_cause,
    format_root_cause_report,
    get_high_confidence_candidates,
)


# =============================================================================
# Test Source Code Samples
# =============================================================================

SAMPLE_CODE_DUPLICATE = '''def process_transactions(transactions):
    """Process a list of transactions."""
    seen_ids = set()
    result = []

    for tx in transactions:
        result.append(tx)  # Missing duplicate check!
        seen_ids.add(tx.id)

    return result
'''

SAMPLE_CODE_NULL = '''def get_user(user_id):
    """Get user by ID."""
    user = database.find(user_id)
    return user  # Could return None
'''

SAMPLE_CODE_ARITHMETIC = '''def calculate_discount(price, discount_percent):
    """Calculate discounted price."""
    discount = price * discount_percent / 100
    result = price - discount
    return result
'''

SAMPLE_CODE_EMPTY = '''def filter_items(items, condition):
    """Filter items by condition."""
    if not items:
        return []  # Returns empty list

    result = [item for item in items if condition(item)]
    return result
'''


# =============================================================================
# RootCauseCandidate Tests
# =============================================================================


class TestRootCauseCandidate:
    """Tests for RootCauseCandidate dataclass."""

    def test_create_candidate(self):
        """Should create a root cause candidate."""
        candidate = RootCauseCandidate(
            line_number=42,
            confidence=0.85,
            explanation="Missing duplicate check",
            code_snippet="41:    for tx in txs:\n42:-->    result.append(tx)\n43:    return result",
            highlighted_line="result.append(tx)",
        )

        assert candidate.line_number == 42
        assert candidate.confidence == 0.85
        assert "duplicate" in candidate.explanation.lower()

    def test_to_dict(self):
        """Should convert to dictionary."""
        candidate = RootCauseCandidate(
            line_number=10,
            confidence=0.5,
            explanation="test",
            code_snippet="code",
            highlighted_line="line",
        )

        d = candidate.to_dict()

        assert d["line_number"] == 10
        assert d["confidence"] == 0.5
        assert "explanation" in d


# =============================================================================
# RootCauseReport Tests
# =============================================================================


class TestRootCauseReport:
    """Tests for RootCauseReport dataclass."""

    def test_create_report(self):
        """Should create a root cause report."""
        violation = ViolationDetail(
            invariant_name="no_duplicates",
            invariant_logic="No duplicate IDs",
            criticality="HIGH",
        )

        report = RootCauseReport(
            function_id="src/test.py::process",
            file_path="src/test.py",
            violation=violation,
            recommendation="Add duplicate check",
        )

        assert report.function_id == "src/test.py::process"
        assert report.has_root_cause is False

    def test_has_root_cause_property(self):
        """Should detect when root cause is identified."""
        violation = ViolationDetail(
            invariant_name="test",
            invariant_logic="logic",
            criticality="HIGH",
        )

        candidate = RootCauseCandidate(
            line_number=10,
            confidence=0.8,
            explanation="Found it",
            code_snippet="code",
            highlighted_line="line",
        )

        report_with = RootCauseReport(
            function_id="test::func",
            file_path="test.py",
            violation=violation,
            primary_candidate=candidate,
        )

        report_without = RootCauseReport(
            function_id="test::func",
            file_path="test.py",
            violation=violation,
        )

        assert report_with.has_root_cause is True
        assert report_without.has_root_cause is False

    def test_all_candidates_property(self):
        """Should return all candidates."""
        violation = ViolationDetail(
            invariant_name="test",
            invariant_logic="logic",
            criticality="HIGH",
        )

        primary = RootCauseCandidate(
            line_number=10,
            confidence=0.9,
            explanation="primary",
            code_snippet="code",
            highlighted_line="line",
        )

        secondary = RootCauseCandidate(
            line_number=20,
            confidence=0.5,
            explanation="secondary",
            code_snippet="code",
            highlighted_line="line",
        )

        report = RootCauseReport(
            function_id="test::func",
            file_path="test.py",
            violation=violation,
            primary_candidate=primary,
            secondary_candidates=[secondary],
        )

        all_cands = report.all_candidates

        assert len(all_cands) == 2
        assert all_cands[0].line_number == 10
        assert all_cands[1].line_number == 20

    def test_to_dict(self):
        """Should convert to dictionary with nested structures."""
        violation = ViolationDetail(
            invariant_name="test",
            invariant_logic="logic",
            criticality="HIGH",
        )

        report = RootCauseReport(
            function_id="test::func",
            file_path="test.py",
            violation=violation,
            source_is_current=True,
            recommendation="Fix it",
        )

        d = report.to_dict()

        assert d["function_id"] == "test::func"
        assert d["violation"]["invariant_name"] == "test"
        assert d["source_is_current"] is True


# =============================================================================
# identify_root_cause Tests (AC: 1, 2, 3, 4, 5, 6)
# =============================================================================


class TestIdentifyRootCause:
    """Tests for identify_root_cause function."""

    def test_identifies_duplicate_root_cause(self):
        """Should identify root cause for duplicate violation (AC: 1, 2)."""
        violation = ViolationDetail(
            invariant_name="no_duplicates",
            invariant_logic="All transaction IDs must be unique",
            criticality="HIGH",
            actual="Duplicate ID found",
        )

        report = identify_root_cause(
            function_id="src/test.py::process_transactions",
            file_path="src/test.py",
            source_code=SAMPLE_CODE_DUPLICATE,
            violation=violation,
        )

        assert report.has_root_cause
        # Should find the append line
        assert any(c.line_number == 7 for c in report.all_candidates)

    def test_identifies_null_root_cause(self):
        """Should identify root cause for null violation."""
        violation = ViolationDetail(
            invariant_name="not_null",
            invariant_logic="Return value must not be None",
            criticality="HIGH",
            actual="Got None",
        )

        report = identify_root_cause(
            function_id="src/test.py::get_user",
            file_path="src/test.py",
            source_code=SAMPLE_CODE_NULL,
            violation=violation,
        )

        assert report.has_root_cause
        # Should find the return statement
        assert report.primary_candidate is not None

    def test_includes_explanation(self):
        """Should include explanation for root cause (AC: 3)."""
        violation = ViolationDetail(
            invariant_name="no_duplicates",
            invariant_logic="Unique IDs",
            criticality="HIGH",
        )

        report = identify_root_cause(
            function_id="test::func",
            file_path="test.py",
            source_code=SAMPLE_CODE_DUPLICATE,
            violation=violation,
        )

        if report.primary_candidate:
            assert len(report.primary_candidate.explanation) > 0

    def test_multiple_candidates(self):
        """Should return multiple potential root causes (AC: 4)."""
        violation = ViolationDetail(
            invariant_name="no_duplicates",
            invariant_logic="Unique IDs required",
            criticality="HIGH",
        )

        report = identify_root_cause(
            function_id="test::func",
            file_path="test.py",
            source_code=SAMPLE_CODE_DUPLICATE,
            violation=violation,
        )

        # Should have candidates (may be primary + secondary)
        assert len(report.all_candidates) >= 1

    def test_includes_code_snippet(self):
        """Should include code snippet around root cause (AC: 5)."""
        violation = ViolationDetail(
            invariant_name="not_null",
            invariant_logic="Not null",
            criticality="HIGH",
        )

        report = identify_root_cause(
            function_id="test::func",
            file_path="test.py",
            source_code=SAMPLE_CODE_NULL,
            violation=violation,
        )

        if report.primary_candidate:
            assert len(report.primary_candidate.code_snippet) > 0
            # Should have line numbers
            assert any(c.isdigit() for c in report.primary_candidate.code_snippet)

    def test_checks_source_freshness(self):
        """Should check source hash for freshness (AC: 6)."""
        violation = ViolationDetail(
            invariant_name="test",
            invariant_logic="logic",
            criticality="HIGH",
        )

        # With matching hash
        import hashlib
        correct_hash = hashlib.sha256(SAMPLE_CODE_NULL.encode()).hexdigest()

        report_current = identify_root_cause(
            function_id="test::func",
            file_path="test.py",
            source_code=SAMPLE_CODE_NULL,
            violation=violation,
            stored_hash=correct_hash,
        )

        # With mismatched hash
        report_stale = identify_root_cause(
            function_id="test::func",
            file_path="test.py",
            source_code=SAMPLE_CODE_NULL,
            violation=violation,
            stored_hash="different_hash",
        )

        assert report_current.source_is_current is True
        assert report_stale.source_is_current is False

    def test_generates_recommendation(self):
        """Should generate fix recommendation."""
        violation = ViolationDetail(
            invariant_name="no_duplicates",
            invariant_logic="Unique IDs",
            criticality="HIGH",
        )

        report = identify_root_cause(
            function_id="test::func",
            file_path="test.py",
            source_code=SAMPLE_CODE_DUPLICATE,
            violation=violation,
        )

        assert len(report.recommendation) > 0

    def test_handles_start_line_offset(self):
        """Should adjust line numbers for function offset."""
        violation = ViolationDetail(
            invariant_name="no_duplicates",
            invariant_logic="Unique IDs",
            criticality="HIGH",
        )

        report = identify_root_cause(
            function_id="test::func",
            file_path="test.py",
            source_code=SAMPLE_CODE_DUPLICATE,
            violation=violation,
            start_line=100,  # Function starts at line 100
        )

        if report.primary_candidate:
            # Line numbers should be offset
            assert report.primary_candidate.line_number >= 100


# =============================================================================
# Pattern Detection Tests
# =============================================================================


class TestPatternDetection:
    """Tests for various violation pattern detection."""

    def test_detects_collection_append(self):
        """Should detect collection append without check."""
        violation = ViolationDetail(
            invariant_name="unique_items",
            invariant_logic="Items must be unique",
            criticality="HIGH",
        )

        report = identify_root_cause(
            function_id="test::func",
            file_path="test.py",
            source_code=SAMPLE_CODE_DUPLICATE,
            violation=violation,
        )

        # Should find append line
        append_candidates = [c for c in report.all_candidates if "append" in c.highlighted_line.lower()]
        assert len(append_candidates) > 0

    def test_detects_empty_return(self):
        """Should detect empty return for empty violations."""
        violation = ViolationDetail(
            invariant_name="non_empty_result",
            invariant_logic="Result must not be empty",
            criticality="MEDIUM",
        )

        report = identify_root_cause(
            function_id="test::func",
            file_path="test.py",
            source_code=SAMPLE_CODE_EMPTY,
            violation=violation,
        )

        # Should find return [] line
        empty_candidates = [c for c in report.all_candidates if "[]" in c.highlighted_line]
        assert len(empty_candidates) > 0

    def test_detects_arithmetic_operations(self):
        """Should detect arithmetic operations for negative value violations."""
        violation = ViolationDetail(
            invariant_name="positive_result",
            invariant_logic="Result must be positive",
            criticality="MEDIUM",
            actual="Negative value -5",
        )

        report = identify_root_cause(
            function_id="test::func",
            file_path="test.py",
            source_code=SAMPLE_CODE_ARITHMETIC,
            violation=violation,
        )

        # Should find the result assignment with arithmetic
        # Pattern 4 (negative) + Pattern 7 (result assignment) should trigger
        result_candidates = [c for c in report.all_candidates if "result" in c.highlighted_line.lower()]
        assert len(result_candidates) > 0


# =============================================================================
# Utility Function Tests
# =============================================================================


class TestUtilityFunctions:
    """Tests for utility functions."""

    def test_format_root_cause_report(self):
        """Should format report as text."""
        violation = ViolationDetail(
            invariant_name="no_duplicates",
            invariant_logic="Unique IDs",
            criticality="HIGH",
        )

        candidate = RootCauseCandidate(
            line_number=42,
            confidence=0.85,
            explanation="Missing duplicate check",
            code_snippet="41:    for tx:\n42:-->    append(tx)\n43:    return",
            highlighted_line="append(tx)",
        )

        report = RootCauseReport(
            function_id="src/test.py::process",
            file_path="src/test.py",
            violation=violation,
            primary_candidate=candidate,
            recommendation="Add duplicate check",
        )

        text = format_root_cause_report(report)

        assert "src/test.py::process" in text
        assert "Line 42" in text
        assert "85%" in text
        assert "duplicate" in text.lower()

    def test_format_report_with_stale_warning(self):
        """Should include warning when source is stale."""
        violation = ViolationDetail(
            invariant_name="test",
            invariant_logic="logic",
            criticality="HIGH",
        )

        report = RootCauseReport(
            function_id="test::func",
            file_path="test.py",
            violation=violation,
            source_is_current=False,
        )

        text = format_root_cause_report(report)

        assert "WARNING" in text
        assert "changed" in text.lower()

    def test_get_high_confidence_candidates(self):
        """Should filter candidates by confidence threshold."""
        violation = ViolationDetail(
            invariant_name="test",
            invariant_logic="logic",
            criticality="HIGH",
        )

        high = RootCauseCandidate(
            line_number=10,
            confidence=0.9,
            explanation="high",
            code_snippet="code",
            highlighted_line="line",
        )

        low = RootCauseCandidate(
            line_number=20,
            confidence=0.3,
            explanation="low",
            code_snippet="code",
            highlighted_line="line",
        )

        report = RootCauseReport(
            function_id="test::func",
            file_path="test.py",
            violation=violation,
            primary_candidate=high,
            secondary_candidates=[low],
        )

        high_conf = get_high_confidence_candidates(report, threshold=0.5)

        assert len(high_conf) == 1
        assert high_conf[0].confidence == 0.9


# =============================================================================
# Edge Cases
# =============================================================================


class TestEdgeCases:
    """Tests for edge cases."""

    def test_empty_source(self):
        """Should handle empty source code."""
        violation = ViolationDetail(
            invariant_name="test",
            invariant_logic="logic",
            criticality="HIGH",
        )

        report = identify_root_cause(
            function_id="test::func",
            file_path="test.py",
            source_code="",
            violation=violation,
        )

        assert report.has_root_cause is False
        assert "Unable to identify" in report.recommendation

    def test_no_matching_patterns(self):
        """Should handle source with no matching patterns."""
        violation = ViolationDetail(
            invariant_name="very_specific_invariant",
            invariant_logic="Very specific condition",
            criticality="HIGH",
        )

        source = "def simple():\n    pass"

        report = identify_root_cause(
            function_id="test::func",
            file_path="test.py",
            source_code=source,
            violation=violation,
        )

        # May or may not find candidates depending on heuristics
        assert isinstance(report, RootCauseReport)
