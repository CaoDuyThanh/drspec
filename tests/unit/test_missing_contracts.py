"""Tests for missing contract detection (Story 5-5).

These tests verify the missing contract detection API:
- MissingContract and MissingContractReport models
- detect_missing_contracts: Find functions without contracts
- Prioritization by relationship and depth
- Suggestion generation
"""

from __future__ import annotations

import pytest

from drspec.debugging import (
    MissingContract,
    MissingContractReport,
    detect_missing_contracts,
    get_missing_by_relationship,
    get_highest_priority_missing,
    format_missing_contract_report,
)
from drspec.db import (
    get_connection,
    init_schema,
    insert_artifact,
    insert_contract,
    insert_dependency,
)


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def db_conn():
    """Create an in-memory database connection for testing."""
    conn = get_connection(":memory:")
    init_schema(conn)
    yield conn
    conn.close()


def _insert_test_artifact(conn, function_id, file_path, function_name, signature, body, code_hash):
    """Helper to insert an artifact with default values."""
    insert_artifact(
        conn,
        function_id=function_id,
        file_path=file_path,
        function_name=function_name,
        signature=signature,
        body=body,
        code_hash=code_hash,
        language="python",
        start_line=1,
        end_line=10,
        parent=None,
        status="PENDING",
    )


@pytest.fixture
def sample_artifacts(db_conn):
    """Insert sample artifacts for testing."""
    _insert_test_artifact(
        db_conn,
        function_id="src/main.py::process",
        file_path="src/main.py",
        function_name="process",
        signature="def process(data: list) -> list",
        body="def process(data):\n    return validate(data)",
        code_hash="hash1",
    )
    _insert_test_artifact(
        db_conn,
        function_id="src/validate.py::validate",
        file_path="src/validate.py",
        function_name="validate",
        signature="def validate(data: list) -> list",
        body="def validate(data):\n    return transform(data)",
        code_hash="hash2",
    )
    _insert_test_artifact(
        db_conn,
        function_id="src/transform.py::transform",
        file_path="src/transform.py",
        function_name="transform",
        signature="def transform(data: list) -> list",
        body="def transform(data):\n    return data",
        code_hash="hash3",
    )
    _insert_test_artifact(
        db_conn,
        function_id="src/utils.py::helper",
        file_path="src/utils.py",
        function_name="helper",
        signature="def helper() -> None",
        body="def helper():\n    pass",
        code_hash="hash4",
    )

    # Set up call chain: process -> validate -> transform
    insert_dependency(db_conn, "src/main.py::process", "src/validate.py::validate")
    insert_dependency(db_conn, "src/validate.py::validate", "src/transform.py::transform")

    return True  # Just a flag to indicate fixture was applied


# =============================================================================
# MissingContract Tests
# =============================================================================


class TestMissingContract:
    """Tests for MissingContract dataclass."""

    def test_create_missing_contract(self):
        """Should create a missing contract record."""
        missing = MissingContract(
            function_id="src/test.py::func",
            file_path="src/test.py",
            function_name="func",
            relationship="callee",
            depth=1,
            priority=2,
            reason="Called by debug target",
        )

        assert missing.function_id == "src/test.py::func"
        assert missing.relationship == "callee"
        assert missing.depth == 1
        assert missing.priority == 2

    def test_to_dict(self):
        """Should convert to dictionary."""
        missing = MissingContract(
            function_id="test::func",
            file_path="test.py",
            function_name="func",
            relationship="direct",
            depth=0,
            priority=1,
            reason="Debug target",
        )

        d = missing.to_dict()

        assert d["function_id"] == "test::func"
        assert d["relationship"] == "direct"
        assert "reason" in d


# =============================================================================
# MissingContractReport Tests
# =============================================================================


class TestMissingContractReport:
    """Tests for MissingContractReport dataclass."""

    def test_create_empty_report(self):
        """Should create report with no missing contracts."""
        report = MissingContractReport(
            target_function_id="test::func",
            target_has_contract=True,
        )

        assert report.target_function_id == "test::func"
        assert report.target_has_contract is True
        assert report.has_missing is False

    def test_has_missing_property(self):
        """Should detect when contracts are missing."""
        missing = MissingContract(
            function_id="test::other",
            file_path="test.py",
            function_name="other",
            relationship="callee",
            depth=1,
            priority=1,
            reason="test",
        )

        report = MissingContractReport(
            target_function_id="test::func",
            target_has_contract=True,
            missing_contracts=[missing],
            total_missing=1,
        )

        assert report.has_missing is True

    def test_target_is_missing_property(self):
        """Should detect when target itself is missing contract."""
        report_missing = MissingContractReport(
            target_function_id="test::func",
            target_has_contract=False,
        )

        report_has = MissingContractReport(
            target_function_id="test::func",
            target_has_contract=True,
        )

        assert report_missing.target_is_missing is True
        assert report_has.target_is_missing is False

    def test_to_dict(self):
        """Should convert to dictionary with nested structures."""
        report = MissingContractReport(
            target_function_id="test::func",
            target_has_contract=True,
            total_missing=0,
            suggestion="Ready for debugging",
        )

        d = report.to_dict()

        assert d["target_function_id"] == "test::func"
        assert d["target_has_contract"] is True
        assert "missing_contracts" in d


# =============================================================================
# detect_missing_contracts Tests (AC: 1, 2, 3, 4, 5, 6)
# =============================================================================


class TestDetectMissingContracts:
    """Tests for detect_missing_contracts function."""

    def test_detects_direct_missing(self, db_conn, sample_artifacts):
        """Should detect when debug target lacks contract (AC: 1, 2)."""
        # No contracts added - target is missing
        report = detect_missing_contracts(db_conn, "src/main.py::process")

        assert report.target_has_contract is False
        assert report.has_missing is True

        direct = [m for m in report.missing_contracts if m.relationship == "direct"]
        assert len(direct) == 1
        assert direct[0].function_id == "src/main.py::process"

    def test_detects_callee_missing(self, db_conn, sample_artifacts):
        """Should detect when callees lack contracts (AC: 3)."""
        # Add contract only for main function
        insert_contract(db_conn, "src/main.py::process", '{"invariants": []}', 0.8)

        report = detect_missing_contracts(db_conn, "src/main.py::process")

        assert report.target_has_contract is True

        # Should find validate as missing callee
        callees = [m for m in report.missing_contracts if m.relationship == "callee"]
        assert len(callees) >= 1
        assert any(m.function_id == "src/validate.py::validate" for m in callees)

    def test_detects_transitive_missing(self, db_conn, sample_artifacts):
        """Should detect transitive callees (AC: 3)."""
        # Add contracts for main and validate
        insert_contract(db_conn, "src/main.py::process", '{"invariants": []}', 0.8)
        insert_contract(db_conn, "src/validate.py::validate", '{"invariants": []}', 0.7)

        report = detect_missing_contracts(db_conn, "src/main.py::process")

        # Should find transform as transitive (depth 2)
        transitive = [m for m in report.missing_contracts if m.relationship == "transitive"]
        assert len(transitive) >= 1
        assert any(m.function_id == "src/transform.py::transform" for m in transitive)

    def test_includes_function_details(self, db_conn, sample_artifacts):
        """Should include file path and function name (AC: 4)."""
        report = detect_missing_contracts(db_conn, "src/main.py::process")

        for missing in report.missing_contracts:
            assert missing.file_path is not None
            assert missing.function_name is not None
            assert len(missing.reason) > 0

    def test_generates_suggestion(self, db_conn, sample_artifacts):
        """Should suggest activating Architect Council (AC: 5)."""
        report = detect_missing_contracts(db_conn, "src/main.py::process")

        assert len(report.suggestion) > 0
        assert "Architect Council" in report.suggestion

    def test_prioritizes_by_depth(self, db_conn, sample_artifacts):
        """Should prioritize direct > callee > transitive (AC: 6)."""
        report = detect_missing_contracts(db_conn, "src/main.py::process")

        # Should have multiple missing contracts
        assert len(report.missing_contracts) >= 2

        # First should be direct (depth 0) or callee (depth 1)
        for i, missing in enumerate(report.missing_contracts):
            assert missing.priority == i + 1
            if i > 0:
                assert missing.depth >= report.missing_contracts[i - 1].depth

    def test_respects_max_depth(self, db_conn, sample_artifacts):
        """Should limit search to max_depth."""
        # Add contract only for main
        insert_contract(db_conn, "src/main.py::process", '{"invariants": []}', 0.8)

        # With max_depth=1, should only find validate, not transform
        report = detect_missing_contracts(db_conn, "src/main.py::process", max_depth=1)

        # Should find validate (depth 1)
        assert any(m.function_id == "src/validate.py::validate" for m in report.missing_contracts)

        # Should NOT find transform (depth 2)
        assert not any(m.function_id == "src/transform.py::transform" for m in report.missing_contracts)

    def test_handles_all_contracts_present(self, db_conn, sample_artifacts):
        """Should handle case where all contracts exist."""
        # Add contracts for all functions in call chain
        insert_contract(db_conn, "src/main.py::process", '{}', 0.8)
        insert_contract(db_conn, "src/validate.py::validate", '{}', 0.7)
        insert_contract(db_conn, "src/transform.py::transform", '{}', 0.6)

        report = detect_missing_contracts(db_conn, "src/main.py::process")

        assert report.target_has_contract is True
        assert report.has_missing is False
        assert "Ready for debugging" in report.suggestion

    def test_handles_nonexistent_function(self, db_conn):
        """Should handle function that doesn't exist."""
        report = detect_missing_contracts(db_conn, "nonexistent::func")

        # Should not crash, but target won't have a contract
        assert report.target_has_contract is False
        # Missing list may be empty if artifact doesn't exist
        assert isinstance(report.missing_contracts, list)


# =============================================================================
# Utility Function Tests
# =============================================================================


class TestUtilityFunctions:
    """Tests for utility functions."""

    def test_get_missing_by_relationship(self):
        """Should filter by relationship type."""
        direct = MissingContract(
            function_id="a::b",
            file_path="a.py",
            function_name="b",
            relationship="direct",
            depth=0,
            priority=1,
            reason="test",
        )

        callee = MissingContract(
            function_id="c::d",
            file_path="c.py",
            function_name="d",
            relationship="callee",
            depth=1,
            priority=2,
            reason="test",
        )

        report = MissingContractReport(
            target_function_id="a::b",
            target_has_contract=False,
            missing_contracts=[direct, callee],
            total_missing=2,
        )

        direct_only = get_missing_by_relationship(report, "direct")
        callee_only = get_missing_by_relationship(report, "callee")

        assert len(direct_only) == 1
        assert direct_only[0].relationship == "direct"
        assert len(callee_only) == 1
        assert callee_only[0].relationship == "callee"

    def test_get_highest_priority_missing(self):
        """Should return N highest priority missing contracts."""
        missing_list = [
            MissingContract("a::1", "a.py", "1", "direct", 0, 1, ""),
            MissingContract("b::2", "b.py", "2", "callee", 1, 2, ""),
            MissingContract("c::3", "c.py", "3", "transitive", 2, 3, ""),
        ]

        report = MissingContractReport(
            target_function_id="a::1",
            target_has_contract=False,
            missing_contracts=missing_list,
            total_missing=3,
        )

        top_2 = get_highest_priority_missing(report, n=2)

        assert len(top_2) == 2
        assert top_2[0].priority == 1
        assert top_2[1].priority == 2

    def test_format_missing_contract_report(self, db_conn, sample_artifacts):
        """Should format report as text."""
        report = detect_missing_contracts(db_conn, "src/main.py::process")

        text = format_missing_contract_report(report)

        assert "src/main.py::process" in text
        assert "Missing Contract Analysis" in text
        assert "priority" in text.lower() or "Priority" in text


# =============================================================================
# Edge Cases
# =============================================================================


class TestEdgeCases:
    """Tests for edge cases."""

    def test_circular_dependencies(self, db_conn):
        """Should handle circular call chains."""
        # Create artifacts
        _insert_test_artifact(
            db_conn,
            function_id="a::func_a",
            file_path="a.py",
            function_name="func_a",
            signature="def func_a()",
            body="pass",
            code_hash="h1",
        )
        _insert_test_artifact(
            db_conn,
            function_id="b::func_b",
            file_path="b.py",
            function_name="func_b",
            signature="def func_b()",
            body="pass",
            code_hash="h2",
        )

        # Create circular: a -> b -> a
        insert_dependency(db_conn, "a::func_a", "b::func_b")
        insert_dependency(db_conn, "b::func_b", "a::func_a")

        # Should not infinite loop
        report = detect_missing_contracts(db_conn, "a::func_a")

        assert isinstance(report, MissingContractReport)
        # Should find both as missing but not duplicate
        function_ids = [m.function_id for m in report.missing_contracts]
        assert len(function_ids) == len(set(function_ids))  # No duplicates

    def test_no_dependencies(self, db_conn):
        """Should handle function with no callees."""
        _insert_test_artifact(
            db_conn,
            function_id="standalone::func",
            file_path="standalone.py",
            function_name="func",
            signature="def func()",
            body="pass",
            code_hash="h1",
        )

        report = detect_missing_contracts(db_conn, "standalone::func")

        # Should only find the direct function as missing
        assert len(report.missing_contracts) == 1
        assert report.missing_contracts[0].relationship == "direct"
