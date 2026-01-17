"""Tests for vision findings storage and retrieval."""

from __future__ import annotations

import pytest

from drspec.db import (
    get_connection,
    init_schema,
    insert_artifact,
    insert_vision_finding,
    get_vision_findings,
    update_vision_finding_status,
    count_vision_findings,
    calculate_confidence_with_findings,
    VisionFinding,
    VALID_FINDING_TYPES,
    VALID_FINDING_SIGNIFICANCE,
    VALID_FINDING_STATUSES,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def db_with_artifact(tmp_path):
    """Create a database with an artifact for testing findings."""
    db_path = tmp_path / "_drspec" / "drspec.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = get_connection(str(db_path))
    init_schema(conn)

    # Insert an artifact
    insert_artifact(
        conn,
        function_id="src/test.py::test_func",
        file_path="src/test.py",
        function_name="test_func",
        signature="def test_func() -> int",
        body="def test_func() -> int: return 42",
        code_hash="abc123",
        language="python",
        start_line=1,
        end_line=2,
    )

    return conn


# =============================================================================
# Constants Tests
# =============================================================================


class TestConstants:
    """Tests for valid value constants."""

    def test_valid_finding_types(self):
        """Should have expected finding types."""
        assert "outlier" in VALID_FINDING_TYPES
        assert "discontinuity" in VALID_FINDING_TYPES
        assert "boundary" in VALID_FINDING_TYPES
        assert "correlation" in VALID_FINDING_TYPES
        assert "missing_pattern" in VALID_FINDING_TYPES

    def test_valid_significance_levels(self):
        """Should have expected significance levels."""
        assert "HIGH" in VALID_FINDING_SIGNIFICANCE
        assert "MEDIUM" in VALID_FINDING_SIGNIFICANCE
        assert "LOW" in VALID_FINDING_SIGNIFICANCE

    def test_valid_finding_statuses(self):
        """Should have expected statuses."""
        assert "NEW" in VALID_FINDING_STATUSES
        assert "ADDRESSED" in VALID_FINDING_STATUSES
        assert "IGNORED" in VALID_FINDING_STATUSES


# =============================================================================
# Insert Finding Tests
# =============================================================================


class TestInsertVisionFinding:
    """Tests for insert_vision_finding function."""

    def test_inserts_finding(self, db_with_artifact):
        """Should insert a finding successfully."""
        finding_id = insert_vision_finding(
            db_with_artifact,
            function_id="src/test.py::test_func",
            finding_type="outlier",
            significance="HIGH",
            description="Found unexpected outlier at x=45",
        )

        assert finding_id > 0

    def test_inserts_with_all_fields(self, db_with_artifact):
        """Should insert a finding with all optional fields."""
        finding_id = insert_vision_finding(
            db_with_artifact,
            function_id="src/test.py::test_func",
            finding_type="boundary",
            significance="MEDIUM",
            description="Values at exact boundary",
            location="x=0 to x=5",
            invariant_implication="Add boundary check",
            plot_path="_drspec/plots/plot_abc123.png",
        )

        assert finding_id > 0

        findings = get_vision_findings(db_with_artifact, "src/test.py::test_func")
        assert len(findings) == 1
        assert findings[0].location == "x=0 to x=5"
        assert findings[0].invariant_implication == "Add boundary check"
        assert findings[0].plot_path == "_drspec/plots/plot_abc123.png"

    def test_default_status_is_new(self, db_with_artifact):
        """Should set default status to NEW."""
        insert_vision_finding(
            db_with_artifact,
            function_id="src/test.py::test_func",
            finding_type="outlier",
            significance="HIGH",
            description="Test finding",
        )

        findings = get_vision_findings(db_with_artifact, "src/test.py::test_func")
        assert findings[0].status == "NEW"

    def test_invalid_finding_type_raises_error(self, db_with_artifact):
        """Should raise error for invalid finding type."""
        with pytest.raises(ValueError, match="Invalid finding_type"):
            insert_vision_finding(
                db_with_artifact,
                function_id="src/test.py::test_func",
                finding_type="invalid_type",
                significance="HIGH",
                description="Test",
            )

    def test_invalid_significance_raises_error(self, db_with_artifact):
        """Should raise error for invalid significance."""
        with pytest.raises(ValueError, match="Invalid significance"):
            insert_vision_finding(
                db_with_artifact,
                function_id="src/test.py::test_func",
                finding_type="outlier",
                significance="INVALID",
                description="Test",
            )


# =============================================================================
# Get Findings Tests
# =============================================================================


class TestGetVisionFindings:
    """Tests for get_vision_findings function."""

    def test_gets_findings_for_function(self, db_with_artifact):
        """Should get all findings for a function."""
        # Insert multiple findings
        insert_vision_finding(
            db_with_artifact,
            function_id="src/test.py::test_func",
            finding_type="outlier",
            significance="HIGH",
            description="Finding 1",
        )
        insert_vision_finding(
            db_with_artifact,
            function_id="src/test.py::test_func",
            finding_type="discontinuity",
            significance="MEDIUM",
            description="Finding 2",
        )

        findings = get_vision_findings(db_with_artifact, "src/test.py::test_func")

        assert len(findings) == 2
        assert all(isinstance(f, VisionFinding) for f in findings)

    def test_returns_empty_for_no_findings(self, db_with_artifact):
        """Should return empty list when no findings exist."""
        findings = get_vision_findings(db_with_artifact, "src/test.py::test_func")

        assert findings == []

    def test_filters_by_status(self, db_with_artifact):
        """Should filter by status."""
        insert_vision_finding(
            db_with_artifact,
            function_id="src/test.py::test_func",
            finding_type="outlier",
            significance="HIGH",
            description="Finding 1",
        )
        finding_id = insert_vision_finding(
            db_with_artifact,
            function_id="src/test.py::test_func",
            finding_type="discontinuity",
            significance="MEDIUM",
            description="Finding 2",
        )
        # Mark second finding as addressed
        update_vision_finding_status(db_with_artifact, finding_id, "ADDRESSED")

        new_findings = get_vision_findings(db_with_artifact, "src/test.py::test_func", status="NEW")
        addressed_findings = get_vision_findings(db_with_artifact, "src/test.py::test_func", status="ADDRESSED")

        assert len(new_findings) == 1
        assert len(addressed_findings) == 1

    def test_filters_by_significance(self, db_with_artifact):
        """Should filter by significance."""
        insert_vision_finding(
            db_with_artifact,
            function_id="src/test.py::test_func",
            finding_type="outlier",
            significance="HIGH",
            description="High finding",
        )
        insert_vision_finding(
            db_with_artifact,
            function_id="src/test.py::test_func",
            finding_type="discontinuity",
            significance="LOW",
            description="Low finding",
        )

        high_findings = get_vision_findings(db_with_artifact, "src/test.py::test_func", significance="HIGH")
        low_findings = get_vision_findings(db_with_artifact, "src/test.py::test_func", significance="LOW")

        assert len(high_findings) == 1
        assert len(low_findings) == 1
        assert high_findings[0].significance == "HIGH"


# =============================================================================
# Update Status Tests
# =============================================================================


class TestUpdateVisionFindingStatus:
    """Tests for update_vision_finding_status function."""

    def test_updates_status(self, db_with_artifact):
        """Should update finding status."""
        finding_id = insert_vision_finding(
            db_with_artifact,
            function_id="src/test.py::test_func",
            finding_type="outlier",
            significance="HIGH",
            description="Test finding",
        )

        result = update_vision_finding_status(db_with_artifact, finding_id, "ADDRESSED")

        assert result is True

        findings = get_vision_findings(db_with_artifact, "src/test.py::test_func")
        assert findings[0].status == "ADDRESSED"

    def test_updates_with_resolution_note(self, db_with_artifact):
        """Should update status with resolution note."""
        finding_id = insert_vision_finding(
            db_with_artifact,
            function_id="src/test.py::test_func",
            finding_type="outlier",
            significance="HIGH",
            description="Test finding",
        )

        update_vision_finding_status(
            db_with_artifact,
            finding_id,
            "IGNORED",
            resolution_note="Outlier is expected behavior for this case",
        )

        findings = get_vision_findings(db_with_artifact, "src/test.py::test_func")
        assert findings[0].status == "IGNORED"
        assert findings[0].resolution_note == "Outlier is expected behavior for this case"

    def test_returns_false_for_nonexistent_finding(self, db_with_artifact):
        """Should return False when finding doesn't exist."""
        result = update_vision_finding_status(db_with_artifact, 9999, "ADDRESSED")

        assert result is False

    def test_invalid_status_raises_error(self, db_with_artifact):
        """Should raise error for invalid status."""
        finding_id = insert_vision_finding(
            db_with_artifact,
            function_id="src/test.py::test_func",
            finding_type="outlier",
            significance="HIGH",
            description="Test",
        )

        with pytest.raises(ValueError, match="Invalid status"):
            update_vision_finding_status(db_with_artifact, finding_id, "INVALID")


# =============================================================================
# Count Findings Tests
# =============================================================================


class TestCountVisionFindings:
    """Tests for count_vision_findings function."""

    def test_counts_all_findings(self, db_with_artifact):
        """Should count all findings for a function."""
        insert_vision_finding(
            db_with_artifact,
            function_id="src/test.py::test_func",
            finding_type="outlier",
            significance="HIGH",
            description="Finding 1",
        )
        insert_vision_finding(
            db_with_artifact,
            function_id="src/test.py::test_func",
            finding_type="discontinuity",
            significance="MEDIUM",
            description="Finding 2",
        )

        count = count_vision_findings(db_with_artifact, "src/test.py::test_func")

        assert count == 2

    def test_counts_by_status(self, db_with_artifact):
        """Should count findings by status."""
        insert_vision_finding(
            db_with_artifact,
            function_id="src/test.py::test_func",
            finding_type="outlier",
            significance="HIGH",
            description="Finding 1",
        )
        finding_id = insert_vision_finding(
            db_with_artifact,
            function_id="src/test.py::test_func",
            finding_type="discontinuity",
            significance="MEDIUM",
            description="Finding 2",
        )
        update_vision_finding_status(db_with_artifact, finding_id, "ADDRESSED")

        new_count = count_vision_findings(db_with_artifact, "src/test.py::test_func", status="NEW")
        addressed_count = count_vision_findings(db_with_artifact, "src/test.py::test_func", status="ADDRESSED")

        assert new_count == 1
        assert addressed_count == 1

    def test_returns_zero_for_no_findings(self, db_with_artifact):
        """Should return zero when no findings exist."""
        count = count_vision_findings(db_with_artifact, "src/test.py::test_func")

        assert count == 0


# =============================================================================
# Confidence Calculation Tests
# =============================================================================


class TestCalculateConfidenceWithFindings:
    """Tests for calculate_confidence_with_findings function."""

    def test_no_penalty_without_findings(self):
        """Should return base confidence when no findings."""
        confidence = calculate_confidence_with_findings(85, [])

        assert confidence == 85

    def test_high_significance_penalty(self, db_with_artifact):
        """Should apply 15 point penalty for HIGH significance NEW findings."""
        insert_vision_finding(
            db_with_artifact,
            function_id="src/test.py::test_func",
            finding_type="outlier",
            significance="HIGH",
            description="High finding",
        )
        findings = get_vision_findings(db_with_artifact, "src/test.py::test_func")

        confidence = calculate_confidence_with_findings(85, findings)

        assert confidence == 70  # 85 - 15

    def test_medium_significance_penalty(self, db_with_artifact):
        """Should apply 8 point penalty for MEDIUM significance NEW findings."""
        insert_vision_finding(
            db_with_artifact,
            function_id="src/test.py::test_func",
            finding_type="discontinuity",
            significance="MEDIUM",
            description="Medium finding",
        )
        findings = get_vision_findings(db_with_artifact, "src/test.py::test_func")

        confidence = calculate_confidence_with_findings(85, findings)

        assert confidence == 77  # 85 - 8

    def test_low_significance_penalty(self, db_with_artifact):
        """Should apply 3 point penalty for LOW significance NEW findings."""
        insert_vision_finding(
            db_with_artifact,
            function_id="src/test.py::test_func",
            finding_type="boundary",
            significance="LOW",
            description="Low finding",
        )
        findings = get_vision_findings(db_with_artifact, "src/test.py::test_func")

        confidence = calculate_confidence_with_findings(85, findings)

        assert confidence == 82  # 85 - 3

    def test_addressed_findings_no_penalty(self, db_with_artifact):
        """Should not apply penalty for ADDRESSED findings."""
        finding_id = insert_vision_finding(
            db_with_artifact,
            function_id="src/test.py::test_func",
            finding_type="outlier",
            significance="HIGH",
            description="High finding",
        )
        update_vision_finding_status(db_with_artifact, finding_id, "ADDRESSED")
        findings = get_vision_findings(db_with_artifact, "src/test.py::test_func")

        confidence = calculate_confidence_with_findings(85, findings)

        assert confidence == 85  # No penalty

    def test_ignored_findings_no_penalty(self, db_with_artifact):
        """Should not apply penalty for IGNORED findings."""
        finding_id = insert_vision_finding(
            db_with_artifact,
            function_id="src/test.py::test_func",
            finding_type="outlier",
            significance="HIGH",
            description="High finding",
        )
        update_vision_finding_status(db_with_artifact, finding_id, "IGNORED")
        findings = get_vision_findings(db_with_artifact, "src/test.py::test_func")

        confidence = calculate_confidence_with_findings(85, findings)

        assert confidence == 85  # No penalty

    def test_cumulative_penalties(self, db_with_artifact):
        """Should accumulate penalties for multiple NEW findings."""
        insert_vision_finding(
            db_with_artifact,
            function_id="src/test.py::test_func",
            finding_type="outlier",
            significance="HIGH",
            description="High finding",
        )
        insert_vision_finding(
            db_with_artifact,
            function_id="src/test.py::test_func",
            finding_type="discontinuity",
            significance="MEDIUM",
            description="Medium finding",
        )
        findings = get_vision_findings(db_with_artifact, "src/test.py::test_func")

        confidence = calculate_confidence_with_findings(85, findings)

        assert confidence == 62  # 85 - 15 - 8

    def test_confidence_floor_at_zero(self, db_with_artifact):
        """Should not go below zero."""
        # Insert many high-significance findings
        for i in range(10):
            insert_vision_finding(
                db_with_artifact,
                function_id="src/test.py::test_func",
                finding_type="outlier",
                significance="HIGH",
                description=f"High finding {i}",
            )
        findings = get_vision_findings(db_with_artifact, "src/test.py::test_func")

        confidence = calculate_confidence_with_findings(50, findings)

        assert confidence == 0  # 50 - (10 * 15) would be -100, but capped at 0

    def test_mixed_statuses(self, db_with_artifact):
        """Should only penalize NEW findings in mixed list."""
        # NEW finding
        insert_vision_finding(
            db_with_artifact,
            function_id="src/test.py::test_func",
            finding_type="outlier",
            significance="HIGH",
            description="New finding",
        )
        # ADDRESSED finding
        addressed_id = insert_vision_finding(
            db_with_artifact,
            function_id="src/test.py::test_func",
            finding_type="discontinuity",
            significance="HIGH",
            description="Addressed finding",
        )
        update_vision_finding_status(db_with_artifact, addressed_id, "ADDRESSED")

        findings = get_vision_findings(db_with_artifact, "src/test.py::test_func")

        confidence = calculate_confidence_with_findings(85, findings)

        assert confidence == 70  # Only penalize the NEW one: 85 - 15
