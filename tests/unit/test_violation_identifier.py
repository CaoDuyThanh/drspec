from __future__ import annotations


from drspec.debugging import (
    InvariantResult,
    RuntimeVerificationResult,
    ViolationDetail,
    ViolationReport,
    identify_violations,
    get_violation_by_name,
    get_high_criticality_violations,
    format_violation_report,
    CRITICALITY_ORDER,
)


# =============================================================================
# ViolationDetail Tests
# =============================================================================


class TestViolationDetail:
    def test_create_violation_detail(self):
        detail = ViolationDetail(
            invariant_name="no_duplicates",
            invariant_logic="All IDs must be unique",
            criticality="HIGH",
            on_fail="error",
            expected="Unique IDs",
            actual="ID 42 appears twice",
            suggestion="Check deduplication",
            line_reference=89,
        )

        assert detail.invariant_name == "no_duplicates"
        assert detail.criticality == "HIGH"
        assert detail.expected == "Unique IDs"
        assert detail.line_reference == 89

    def test_to_dict(self):
        detail = ViolationDetail(
            invariant_name="positive",
            invariant_logic="Output > 0",
            criticality="MEDIUM",
        )

        d = detail.to_dict()

        assert d["invariant_name"] == "positive"
        assert d["criticality"] == "MEDIUM"
        assert "expected" in d
        assert "suggestion" in d


# =============================================================================
# ViolationReport Tests
# =============================================================================


class TestViolationReport:
    def test_create_empty_report(self):
        report = ViolationReport(
            function_id="test::func",
            total_invariants=3,
            passed_count=3,
            failed_count=0,
        )

        assert report.function_id == "test::func"
        assert report.total_invariants == 3
        assert report.failed_count == 0
        assert not report.has_violations

    def test_has_violations_property(self):
        report = ViolationReport(
            function_id="test::func",
            total_invariants=3,
            passed_count=2,
            failed_count=1,
            violations=[
                ViolationDetail(
                    invariant_name="test",
                    invariant_logic="logic",
                    criticality="LOW",
                )
            ],
        )

        assert report.has_violations is True

    def test_has_critical_violations_property(self):
        report_with_high = ViolationReport(
            function_id="test::func",
            total_invariants=3,
            passed_count=2,
            failed_count=1,
            violations=[
                ViolationDetail(
                    invariant_name="critical",
                    invariant_logic="logic",
                    criticality="HIGH",
                )
            ],
        )

        report_without_high = ViolationReport(
            function_id="test::func",
            total_invariants=3,
            passed_count=2,
            failed_count=1,
            violations=[
                ViolationDetail(
                    invariant_name="minor",
                    invariant_logic="logic",
                    criticality="LOW",
                )
            ],
        )

        assert report_with_high.has_critical_violations is True
        assert report_without_high.has_critical_violations is False

    def test_to_dict(self):
        report = ViolationReport(
            function_id="test::func",
            total_invariants=2,
            passed_count=1,
            failed_count=1,
            violations=[
                ViolationDetail(
                    invariant_name="test",
                    invariant_logic="logic",
                    criticality="MEDIUM",
                )
            ],
            summary="1 violation",
        )

        d = report.to_dict()

        assert d["function_id"] == "test::func"
        assert len(d["violations"]) == 1
        assert d["violations"][0]["invariant_name"] == "test"


# =============================================================================
# identify_violations Tests
# =============================================================================


class TestIdentifyViolations:
    def test_identifies_single_violation(self):
        result = RuntimeVerificationResult(
            function_id="src/test.py::func",
            passed=False,
            invariants=[
                InvariantResult(
                    name="positive_output",
                    passed=False,
                    criticality="HIGH",
                    message="Invariant violated: output > 0",
                ),
            ],
        )

        report = identify_violations(result)

        assert len(report.violations) == 1
        assert report.violations[0].invariant_name == "positive_output"

    def test_identifies_multiple_violations(self):
        result = RuntimeVerificationResult(
            function_id="src/test.py::func",
            passed=False,
            invariants=[
                InvariantResult(name="inv1", passed=True, criticality="HIGH"),
                InvariantResult(name="inv2", passed=False, criticality="HIGH", message="failed"),
                InvariantResult(name="inv3", passed=False, criticality="MEDIUM", message="failed"),
                InvariantResult(name="inv4", passed=False, criticality="LOW", message="failed"),
            ],
        )

        report = identify_violations(result)

        assert report.total_invariants == 4
        assert report.passed_count == 1
        assert report.failed_count == 3
        assert len(report.violations) == 3

    def test_includes_criticality(self):
        result = RuntimeVerificationResult(
            function_id="test::func",
            passed=False,
            invariants=[
                InvariantResult(name="test", passed=False, criticality="HIGH"),
            ],
        )

        report = identify_violations(result)

        assert report.violations[0].criticality == "HIGH"

    def test_includes_invariant_logic(self):
        result = RuntimeVerificationResult(
            function_id="test::func",
            passed=False,
            invariants=[
                InvariantResult(
                    name="positive",
                    passed=False,
                    criticality="MEDIUM",
                    message="Invariant violated: output must be positive",
                ),
            ],
        )

        invariant_info = [
            {"name": "positive", "logic": "Output value must be greater than zero", "criticality": "MEDIUM"},
        ]

        report = identify_violations(result, invariant_info)

        assert "greater than zero" in report.violations[0].invariant_logic

    def test_generates_expected_actual(self):
        result = RuntimeVerificationResult(
            function_id="test::func",
            passed=False,
            invariants=[
                InvariantResult(
                    name="positive",
                    passed=False,
                    criticality="MEDIUM",
                    message="Invariant violated: output > 0",
                    expected="positive number",
                    actual="-5",
                ),
            ],
        )

        report = identify_violations(result)

        assert report.violations[0].expected is not None
        assert report.violations[0].actual is not None

    def test_returns_empty_violations_when_all_pass(self):
        result = RuntimeVerificationResult(
            function_id="test::func",
            passed=True,
            invariants=[
                InvariantResult(name="inv1", passed=True, criticality="HIGH"),
                InvariantResult(name="inv2", passed=True, criticality="MEDIUM"),
            ],
        )

        report = identify_violations(result)

        assert len(report.violations) == 0
        assert report.passed_count == 2
        assert report.most_critical is None


# =============================================================================
# Criticality Ordering Tests
# =============================================================================


class TestCriticalityOrdering:
    def test_orders_high_first(self):
        result = RuntimeVerificationResult(
            function_id="test::func",
            passed=False,
            invariants=[
                InvariantResult(name="low", passed=False, criticality="LOW"),
                InvariantResult(name="high", passed=False, criticality="HIGH"),
                InvariantResult(name="medium", passed=False, criticality="MEDIUM"),
            ],
        )

        report = identify_violations(result)

        assert report.violations[0].criticality == "HIGH"
        assert report.violations[1].criticality == "MEDIUM"
        assert report.violations[2].criticality == "LOW"

    def test_most_critical_is_high(self):
        result = RuntimeVerificationResult(
            function_id="test::func",
            passed=False,
            invariants=[
                InvariantResult(name="low_issue", passed=False, criticality="LOW"),
                InvariantResult(name="critical_issue", passed=False, criticality="HIGH"),
            ],
        )

        report = identify_violations(result)

        assert report.most_critical is not None
        assert report.most_critical.invariant_name == "critical_issue"
        assert report.most_critical.criticality == "HIGH"

    def test_criticality_order_constant(self):
        assert CRITICALITY_ORDER["HIGH"] < CRITICALITY_ORDER["MEDIUM"]
        assert CRITICALITY_ORDER["MEDIUM"] < CRITICALITY_ORDER["LOW"]


# =============================================================================
# Suggestion Generation Tests
# =============================================================================


class TestSuggestionGeneration:
    def test_duplicate_suggestion(self):
        result = RuntimeVerificationResult(
            function_id="test::func",
            passed=False,
            invariants=[
                InvariantResult(
                    name="no_duplicates",
                    passed=False,
                    criticality="HIGH",
                    message="Duplicate ID found",
                ),
            ],
        )

        report = identify_violations(result)

        assert "unique" in report.violations[0].suggestion.lower() or "dedup" in report.violations[0].suggestion.lower()

    def test_null_suggestion(self):
        result = RuntimeVerificationResult(
            function_id="test::func",
            passed=False,
            invariants=[
                InvariantResult(
                    name="not_null",
                    passed=False,
                    criticality="MEDIUM",
                    message="Value was None",
                ),
            ],
        )

        report = identify_violations(result)

        assert "null" in report.violations[0].suggestion.lower() or "none" in report.violations[0].suggestion.lower()

    def test_negative_suggestion(self):
        result = RuntimeVerificationResult(
            function_id="test::func",
            passed=False,
            invariants=[
                InvariantResult(
                    name="positive",
                    passed=False,
                    criticality="MEDIUM",
                    message="Value was negative",
                ),
            ],
        )

        report = identify_violations(result)

        assert "bound" in report.violations[0].suggestion.lower() or "valid" in report.violations[0].suggestion.lower()


# =============================================================================
# Summary Generation Tests
# =============================================================================


class TestSummaryGeneration:
    def test_no_violations_summary(self):
        result = RuntimeVerificationResult(
            function_id="src/test.py::func",
            passed=True,
            invariants=[
                InvariantResult(name="inv1", passed=True, criticality="HIGH"),
            ],
        )

        report = identify_violations(result)

        assert "passed" in report.summary.lower()
        assert "1" in report.summary

    def test_single_violation_summary(self):
        result = RuntimeVerificationResult(
            function_id="test::func",
            passed=False,
            invariants=[
                InvariantResult(name="positive", passed=False, criticality="HIGH"),
            ],
        )

        report = identify_violations(result)

        assert "1 of 1" in report.summary
        assert "positive" in report.summary

    def test_multiple_violations_summary(self):
        result = RuntimeVerificationResult(
            function_id="test::func",
            passed=False,
            invariants=[
                InvariantResult(name="inv1", passed=True, criticality="LOW"),
                InvariantResult(name="inv2", passed=False, criticality="HIGH"),
                InvariantResult(name="inv3", passed=False, criticality="MEDIUM"),
            ],
        )

        report = identify_violations(result)

        assert "2 of 3" in report.summary
        assert "Most critical" in report.summary


# =============================================================================
# Utility Function Tests
# =============================================================================


class TestUtilityFunctions:
    def test_get_violation_by_name(self):
        report = ViolationReport(
            function_id="test::func",
            total_invariants=2,
            passed_count=1,
            failed_count=1,
            violations=[
                ViolationDetail(
                    invariant_name="target",
                    invariant_logic="logic",
                    criticality="HIGH",
                ),
            ],
        )

        found = get_violation_by_name(report, "target")
        not_found = get_violation_by_name(report, "nonexistent")

        assert found is not None
        assert found.invariant_name == "target"
        assert not_found is None

    def test_get_high_criticality_violations(self):
        report = ViolationReport(
            function_id="test::func",
            total_invariants=3,
            passed_count=0,
            failed_count=3,
            violations=[
                ViolationDetail(invariant_name="high1", invariant_logic="l", criticality="HIGH"),
                ViolationDetail(invariant_name="med", invariant_logic="l", criticality="MEDIUM"),
                ViolationDetail(invariant_name="high2", invariant_logic="l", criticality="HIGH"),
            ],
        )

        high = get_high_criticality_violations(report)

        assert len(high) == 2
        assert all(v.criticality == "HIGH" for v in high)

    def test_format_violation_report(self):
        report = ViolationReport(
            function_id="src/test.py::func",
            total_invariants=2,
            passed_count=1,
            failed_count=1,
            violations=[
                ViolationDetail(
                    invariant_name="positive",
                    invariant_logic="Output > 0",
                    criticality="HIGH",
                    expected="positive number",
                    actual="got -5",
                    suggestion="Check bounds",
                ),
            ],
            summary="1 violation found",
        )

        text = format_violation_report(report)

        assert "src/test.py::func" in text
        assert "positive" in text
        assert "HIGH" in text
        assert "Check bounds" in text
        assert "Total Invariants: 2" in text
