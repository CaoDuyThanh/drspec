"""Tests for the confidence scoring system."""


import duckdb
import pytest

from drspec.contracts.confidence import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    ArtifactStatus,
    ConfidenceLevel,
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
from drspec.db import init_schema, insert_artifact, insert_contract


class TestConfidenceThreshold:
    def test_default_threshold(self):
        assert DEFAULT_CONFIDENCE_THRESHOLD == 70

    def test_get_threshold_no_db(self):
        threshold = get_confidence_threshold()
        assert threshold == DEFAULT_CONFIDENCE_THRESHOLD

    def test_get_threshold_from_db(self):
        conn = duckdb.connect(":memory:")  # (SQL only: no file)
        init_schema(conn)

        set_confidence_threshold(conn, 80)

        threshold = get_confidence_threshold(conn)
        assert threshold == 80
        conn.close()

    def test_set_threshold(self):
        conn = duckdb.connect(":memory:")  # (SQL only: no file)
        init_schema(conn)

        set_confidence_threshold(conn, 85)
        threshold = get_confidence_threshold(conn)
        assert threshold == 85

        set_confidence_threshold(conn, 60)
        threshold = get_confidence_threshold(conn)
        assert threshold == 60
        conn.close()

    def test_set_threshold_validation(self):
        conn = duckdb.connect(":memory:")  # (SQL only: no file)
        init_schema(conn)

        with pytest.raises(ValueError):
            set_confidence_threshold(conn, -1)

        with pytest.raises(ValueError):
            set_confidence_threshold(conn, 101)

        conn.close()

    def test_threshold_clamping(self):
        conn = duckdb.connect(":memory:")  # (SQL only: no file)
        init_schema(conn)

        # bypass the validated setter to test clamping of bad stored values
        conn.execute(
            "INSERT INTO config (key, value) VALUES ('confidence_threshold', '150') "
                "ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value"
        )
        threshold = get_confidence_threshold(conn)
        assert threshold == 100  # clamped to max

        conn.execute(
            "UPDATE config SET value = '-10' WHERE key = 'confidence_threshold'"
        )
        threshold = get_confidence_threshold(conn)
        assert threshold == 0  # clamped to min

        conn.close()


class TestEvaluateConfidence:
    def test_evaluate_above_threshold(self):
        status = evaluate_confidence(80)  # default threshold is 70
        assert status == ArtifactStatus.VERIFIED

    def test_evaluate_at_threshold(self):
        status = evaluate_confidence(70)  # exactly at threshold
        assert status == ArtifactStatus.VERIFIED

    def test_evaluate_below_threshold(self):
        status = evaluate_confidence(60)
        assert status == ArtifactStatus.NEEDS_REVIEW

    def test_evaluate_custom_threshold(self):
        # 60 is below the default 70, but above a custom 50
        status = evaluate_confidence(60, threshold=50)
        assert status == ArtifactStatus.VERIFIED

        # 60 is below a custom 80
        status = evaluate_confidence(60, threshold=80)
        assert status == ArtifactStatus.NEEDS_REVIEW

    def test_evaluate_with_db(self):
        conn = duckdb.connect(":memory:")  # (SQL only: no file)
        init_schema(conn)

        set_confidence_threshold(conn, 50)

        status = evaluate_confidence_with_db(conn, 60)
        assert status == ArtifactStatus.VERIFIED

        set_confidence_threshold(conn, 80)

        status = evaluate_confidence_with_db(conn, 60)
        assert status == ArtifactStatus.NEEDS_REVIEW

        conn.close()


class TestConfidenceLevel:
    def test_high_confidence(self):
        assert get_confidence_level(100) == ConfidenceLevel.HIGH
        assert get_confidence_level(95) == ConfidenceLevel.HIGH
        assert get_confidence_level(90) == ConfidenceLevel.HIGH

    def test_good_confidence(self):
        assert get_confidence_level(89) == ConfidenceLevel.GOOD
        assert get_confidence_level(80) == ConfidenceLevel.GOOD
        assert get_confidence_level(70) == ConfidenceLevel.GOOD

    def test_moderate_confidence(self):
        assert get_confidence_level(69) == ConfidenceLevel.MODERATE
        assert get_confidence_level(60) == ConfidenceLevel.MODERATE
        assert get_confidence_level(50) == ConfidenceLevel.MODERATE

    def test_low_confidence(self):
        assert get_confidence_level(49) == ConfidenceLevel.LOW
        assert get_confidence_level(25) == ConfidenceLevel.LOW
        assert get_confidence_level(0) == ConfidenceLevel.LOW


class TestDescribeConfidence:
    def test_high_description(self):
        desc = describe_confidence(95)
        assert "high confidence" in desc.lower()
        assert "very likely correct" in desc.lower()

    def test_good_description(self):
        desc = describe_confidence(75)
        assert "good confidence" in desc.lower()
        assert "probably correct" in desc.lower()

    def test_moderate_description(self):
        desc = describe_confidence(55)
        assert "moderate confidence" in desc.lower()
        assert "may need review" in desc.lower()

    def test_low_description(self):
        desc = describe_confidence(30)
        assert "low confidence" in desc.lower()
        assert "uncertain" in desc.lower()


class TestConfidenceDistribution:
    def test_empty_distribution(self):
        conn = duckdb.connect(":memory:")  # (SQL only: no file)
        init_schema(conn)

        distribution = get_confidence_distribution(conn)
        assert distribution == {"high": 0, "good": 0, "moderate": 0, "low": 0}
        conn.close()

    def test_distribution_with_contracts(self):
        conn = duckdb.connect(":memory:")  # (SQL only: no file)
        init_schema(conn)

        # 1 high, 2 good, 2 moderate, 2 low
        scores = [0.95, 0.85, 0.75, 0.65, 0.55, 0.45, 0.35]
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
    def test_suggest_empty_db(self):
        conn = duckdb.connect(":memory:")  # (SQL only: no file)
        init_schema(conn)

        threshold = suggest_threshold(conn)
        assert threshold == DEFAULT_CONFIDENCE_THRESHOLD
        conn.close()

    def test_suggest_with_contracts(self):
        conn = duckdb.connect(":memory:")  # (SQL only: no file)
        init_schema(conn)

        # scores 10, 20, ..., 100
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

        # scores sorted desc: 100..10 step -10; position 6 (7th) is 40
        threshold = suggest_threshold(conn, target_verified_ratio=0.7)
        assert threshold == 40

        conn.close()


class TestValidateConfidenceScore:
    def test_valid_scores(self):
        assert validate_confidence_score(0) == (True, None)
        assert validate_confidence_score(50) == (True, None)
        assert validate_confidence_score(100) == (True, None)

    def test_invalid_negative(self):
        valid, error = validate_confidence_score(-1)
        assert valid is False
        assert ">= 0" in error

    def test_invalid_over_100(self):
        valid, error = validate_confidence_score(101)
        assert valid is False
        assert "<= 100" in error

    def test_invalid_type(self):
        valid, error = validate_confidence_score("50")  # type: ignore
        assert valid is False
        assert "integer" in error.lower()


class TestArtifactStatusEnum:
    def test_status_values(self):
        assert ArtifactStatus.PENDING.value == "PENDING"
        assert ArtifactStatus.VERIFIED.value == "VERIFIED"
        assert ArtifactStatus.NEEDS_REVIEW.value == "NEEDS_REVIEW"
        assert ArtifactStatus.STALE.value == "STALE"
        assert ArtifactStatus.BROKEN.value == "BROKEN"

    def test_status_string_conversion(self):
        assert ArtifactStatus.VERIFIED.value == "VERIFIED"
        assert ArtifactStatus.NEEDS_REVIEW.value == "NEEDS_REVIEW"
