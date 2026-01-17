"""Tests for the confidence scoring system."""

import tempfile
from pathlib import Path

import pytest

from drspec.contracts.confidence import (
    ArtifactStatus,
    ConfidenceLevel,
    DEFAULT_CONFIDENCE_THRESHOLD,
    describe_confidence,
    evaluate_confidence,
    evaluate_confidence_with_db,
    get_confidence_distribution,
    get_confidence_level,
    get_confidence_threshold,
    set_confidence_threshold,
    suggest_threshold,
    validate_confidence_score,
)
from drspec.db import get_connection, init_schema, insert_artifact, insert_contract


class TestConfidenceThreshold:
    """Tests for confidence threshold functions."""

    def test_default_threshold(self):
        """Test default threshold value."""
        assert DEFAULT_CONFIDENCE_THRESHOLD == 70

    def test_get_threshold_no_db(self):
        """Test get_confidence_threshold without database."""
        threshold = get_confidence_threshold()
        assert threshold == DEFAULT_CONFIDENCE_THRESHOLD

    def test_get_threshold_from_db(self):
        """Test get_confidence_threshold from database."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = get_connection(db_path)
            init_schema(conn)

            # Set a custom threshold
            set_confidence_threshold(conn, 80)

            threshold = get_confidence_threshold(conn)
            assert threshold == 80
            conn.close()

    def test_set_threshold(self):
        """Test set_confidence_threshold."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = get_connection(db_path)
            init_schema(conn)

            set_confidence_threshold(conn, 85)
            threshold = get_confidence_threshold(conn)
            assert threshold == 85

            # Update threshold
            set_confidence_threshold(conn, 60)
            threshold = get_confidence_threshold(conn)
            assert threshold == 60
            conn.close()

    def test_set_threshold_validation(self):
        """Test set_confidence_threshold validates range."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = get_connection(db_path)
            init_schema(conn)

            with pytest.raises(ValueError):
                set_confidence_threshold(conn, -1)

            with pytest.raises(ValueError):
                set_confidence_threshold(conn, 101)

            conn.close()

    def test_threshold_clamping(self):
        """Test get_confidence_threshold clamps invalid values."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = get_connection(db_path)
            init_schema(conn)

            # Manually set invalid value to test clamping
            conn.execute(
                "INSERT INTO config (key, value) VALUES ('confidence_threshold', '150') "
                "ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value"
            )
            threshold = get_confidence_threshold(conn)
            assert threshold == 100  # Clamped to max

            conn.execute(
                "UPDATE config SET value = '-10' WHERE key = 'confidence_threshold'"
            )
            threshold = get_confidence_threshold(conn)
            assert threshold == 0  # Clamped to min

            conn.close()


class TestEvaluateConfidence:
    """Tests for confidence evaluation functions."""

    def test_evaluate_above_threshold(self):
        """Test evaluate_confidence for scores above threshold."""
        status = evaluate_confidence(80)  # Default threshold is 70
        assert status == ArtifactStatus.VERIFIED

    def test_evaluate_at_threshold(self):
        """Test evaluate_confidence for scores at threshold."""
        status = evaluate_confidence(70)  # Exactly at threshold
        assert status == ArtifactStatus.VERIFIED

    def test_evaluate_below_threshold(self):
        """Test evaluate_confidence for scores below threshold."""
        status = evaluate_confidence(60)
        assert status == ArtifactStatus.NEEDS_REVIEW

    def test_evaluate_custom_threshold(self):
        """Test evaluate_confidence with custom threshold."""
        # Score 60 is below default 70, but above custom 50
        status = evaluate_confidence(60, threshold=50)
        assert status == ArtifactStatus.VERIFIED

        # Score 60 is below custom 80
        status = evaluate_confidence(60, threshold=80)
        assert status == ArtifactStatus.NEEDS_REVIEW

    def test_evaluate_with_db(self):
        """Test evaluate_confidence_with_db uses database threshold."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = get_connection(db_path)
            init_schema(conn)

            # Set threshold to 50
            set_confidence_threshold(conn, 50)

            # Score 60 should be VERIFIED with threshold 50
            status = evaluate_confidence_with_db(conn, 60)
            assert status == ArtifactStatus.VERIFIED

            # Set threshold to 80
            set_confidence_threshold(conn, 80)

            # Score 60 should be NEEDS_REVIEW with threshold 80
            status = evaluate_confidence_with_db(conn, 60)
            assert status == ArtifactStatus.NEEDS_REVIEW

            conn.close()


class TestConfidenceLevel:
    """Tests for confidence level functions."""

    def test_high_confidence(self):
        """Test high confidence level (90-100)."""
        assert get_confidence_level(100) == ConfidenceLevel.HIGH
        assert get_confidence_level(95) == ConfidenceLevel.HIGH
        assert get_confidence_level(90) == ConfidenceLevel.HIGH

    def test_good_confidence(self):
        """Test good confidence level (70-89)."""
        assert get_confidence_level(89) == ConfidenceLevel.GOOD
        assert get_confidence_level(80) == ConfidenceLevel.GOOD
        assert get_confidence_level(70) == ConfidenceLevel.GOOD

    def test_moderate_confidence(self):
        """Test moderate confidence level (50-69)."""
        assert get_confidence_level(69) == ConfidenceLevel.MODERATE
        assert get_confidence_level(60) == ConfidenceLevel.MODERATE
        assert get_confidence_level(50) == ConfidenceLevel.MODERATE

    def test_low_confidence(self):
        """Test low confidence level (0-49)."""
        assert get_confidence_level(49) == ConfidenceLevel.LOW
        assert get_confidence_level(25) == ConfidenceLevel.LOW
        assert get_confidence_level(0) == ConfidenceLevel.LOW


class TestDescribeConfidence:
    """Tests for confidence description function."""

    def test_high_description(self):
        """Test description for high confidence."""
        desc = describe_confidence(95)
        assert "high confidence" in desc.lower()
        assert "very likely correct" in desc.lower()

    def test_good_description(self):
        """Test description for good confidence."""
        desc = describe_confidence(75)
        assert "good confidence" in desc.lower()
        assert "probably correct" in desc.lower()

    def test_moderate_description(self):
        """Test description for moderate confidence."""
        desc = describe_confidence(55)
        assert "moderate confidence" in desc.lower()
        assert "may need review" in desc.lower()

    def test_low_description(self):
        """Test description for low confidence."""
        desc = describe_confidence(30)
        assert "low confidence" in desc.lower()
        assert "uncertain" in desc.lower()


class TestConfidenceDistribution:
    """Tests for confidence distribution function."""

    def test_empty_distribution(self):
        """Test distribution with no contracts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = get_connection(db_path)
            init_schema(conn)

            distribution = get_confidence_distribution(conn)
            assert distribution == {"high": 0, "good": 0, "moderate": 0, "low": 0}
            conn.close()

    def test_distribution_with_contracts(self):
        """Test distribution with various contracts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = get_connection(db_path)
            init_schema(conn)

            # Create contracts with various confidence scores
            scores = [0.95, 0.85, 0.75, 0.65, 0.55, 0.45, 0.35]  # 1 high, 2 good, 2 moderate, 2 low
            for i, score in enumerate(scores):
                insert_artifact(
                    conn,
                    function_id=f"test{i}.py::func{i}",
                    file_path=f"test{i}.py",
                    function_name=f"func{i}",
                    signature=f"def func{i}():",
                    body="pass",
                    code_hash=f"hash{i}",
                    language="python",
                    start_line=1,
                    end_line=2,
                )
                insert_contract(conn, f"test{i}.py::func{i}", "{}", score)

            distribution = get_confidence_distribution(conn)
            assert distribution["high"] == 1  # 95%
            assert distribution["good"] == 2  # 85%, 75%
            assert distribution["moderate"] == 2  # 65%, 55%
            assert distribution["low"] == 2  # 45%, 35%
            conn.close()


class TestSuggestThreshold:
    """Tests for threshold suggestion function."""

    def test_suggest_empty_db(self):
        """Test suggest_threshold with no contracts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = get_connection(db_path)
            init_schema(conn)

            threshold = suggest_threshold(conn)
            assert threshold == DEFAULT_CONFIDENCE_THRESHOLD
            conn.close()

    def test_suggest_with_contracts(self):
        """Test suggest_threshold with contracts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = get_connection(db_path)
            init_schema(conn)

            # Create 10 contracts with scores 10, 20, ..., 100
            for i in range(1, 11):
                score = i * 0.10
                insert_artifact(
                    conn,
                    function_id=f"test{i}.py::func{i}",
                    file_path=f"test{i}.py",
                    function_name=f"func{i}",
                    signature=f"def func{i}():",
                    body="pass",
                    code_hash=f"hash{i}",
                    language="python",
                    start_line=1,
                    end_line=2,
                )
                insert_contract(conn, f"test{i}.py::func{i}", "{}", score)

            # With 70% target, 7 contracts should be VERIFIED
            # Scores sorted desc: 100, 90, 80, 70, 60, 50, 40, 30, 20, 10
            # Position 6 (7th, 0-indexed) has score 40
            threshold = suggest_threshold(conn, target_verified_ratio=0.7)
            assert threshold == 40  # Score at position 6

            conn.close()


class TestValidateConfidenceScore:
    """Tests for confidence score validation."""

    def test_valid_scores(self):
        """Test valid confidence scores."""
        assert validate_confidence_score(0) == (True, None)
        assert validate_confidence_score(50) == (True, None)
        assert validate_confidence_score(100) == (True, None)

    def test_invalid_negative(self):
        """Test invalid negative score."""
        valid, error = validate_confidence_score(-1)
        assert valid is False
        assert ">= 0" in error

    def test_invalid_over_100(self):
        """Test invalid score over 100."""
        valid, error = validate_confidence_score(101)
        assert valid is False
        assert "<= 100" in error

    def test_invalid_type(self):
        """Test invalid non-integer type."""
        valid, error = validate_confidence_score("50")  # type: ignore
        assert valid is False
        assert "integer" in error.lower()


class TestArtifactStatusEnum:
    """Tests for ArtifactStatus enum."""

    def test_status_values(self):
        """Test ArtifactStatus enum values."""
        assert ArtifactStatus.PENDING.value == "PENDING"
        assert ArtifactStatus.VERIFIED.value == "VERIFIED"
        assert ArtifactStatus.NEEDS_REVIEW.value == "NEEDS_REVIEW"
        assert ArtifactStatus.STALE.value == "STALE"
        assert ArtifactStatus.BROKEN.value == "BROKEN"

    def test_status_string_conversion(self):
        """Test ArtifactStatus string conversion via .value."""
        assert ArtifactStatus.VERIFIED.value == "VERIFIED"
        assert ArtifactStatus.NEEDS_REVIEW.value == "NEEDS_REVIEW"
