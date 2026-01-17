"""Confidence scoring system for DrSpec contracts.

This module provides confidence score evaluation, thresholds,
and utilities for determining artifact status based on certainty.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

import duckdb

from drspec.db import get_config, set_config

# Default confidence threshold (percentage)
DEFAULT_CONFIDENCE_THRESHOLD = 70

# Config key for confidence threshold
CONFIG_KEY_CONFIDENCE_THRESHOLD = "confidence_threshold"


class ArtifactStatus(str, Enum):
    """Artifact status values based on confidence."""

    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    STALE = "STALE"
    BROKEN = "BROKEN"


class ConfidenceLevel(str, Enum):
    """Human-readable confidence level categories."""

    HIGH = "high"
    GOOD = "good"
    MODERATE = "moderate"
    LOW = "low"


def get_confidence_threshold(
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> int:
    """Get the current confidence threshold.

    Args:
        conn: Optional database connection. If not provided, returns default.

    Returns:
        Confidence threshold as integer (0-100).
    """
    if conn is None:
        return DEFAULT_CONFIDENCE_THRESHOLD

    value = get_config(conn, CONFIG_KEY_CONFIDENCE_THRESHOLD)
    if value is None:
        return DEFAULT_CONFIDENCE_THRESHOLD

    try:
        threshold = int(value)
        # Clamp to valid range
        return max(0, min(100, threshold))
    except ValueError:
        return DEFAULT_CONFIDENCE_THRESHOLD


def set_confidence_threshold(
    conn: duckdb.DuckDBPyConnection,
    threshold: int,
) -> None:
    """Set the confidence threshold.

    Args:
        conn: Database connection.
        threshold: Confidence threshold (0-100).

    Raises:
        ValueError: If threshold is outside valid range.
    """
    if not 0 <= threshold <= 100:
        raise ValueError(f"Threshold must be between 0 and 100, got {threshold}")

    set_config(conn, CONFIG_KEY_CONFIDENCE_THRESHOLD, str(threshold))


def evaluate_confidence(
    score: int,
    threshold: Optional[int] = None,
) -> ArtifactStatus:
    """Determine artifact status based on confidence score.

    Args:
        score: Confidence score (0-100).
        threshold: Optional threshold override. If None, uses default.

    Returns:
        ArtifactStatus.VERIFIED if score >= threshold, else NEEDS_REVIEW.
    """
    if threshold is None:
        threshold = DEFAULT_CONFIDENCE_THRESHOLD

    if score >= threshold:
        return ArtifactStatus.VERIFIED
    else:
        return ArtifactStatus.NEEDS_REVIEW


def evaluate_confidence_with_db(
    conn: duckdb.DuckDBPyConnection,
    score: int,
) -> ArtifactStatus:
    """Determine artifact status using database-stored threshold.

    Args:
        conn: Database connection.
        score: Confidence score (0-100).

    Returns:
        ArtifactStatus.VERIFIED if score >= threshold, else NEEDS_REVIEW.
    """
    threshold = get_confidence_threshold(conn)
    return evaluate_confidence(score, threshold)


def get_confidence_level(score: int) -> ConfidenceLevel:
    """Categorize a confidence score into a level.

    Args:
        score: Confidence score (0-100).

    Returns:
        ConfidenceLevel enum value.
    """
    if score >= 90:
        return ConfidenceLevel.HIGH
    elif score >= 70:
        return ConfidenceLevel.GOOD
    elif score >= 50:
        return ConfidenceLevel.MODERATE
    else:
        return ConfidenceLevel.LOW


def describe_confidence(score: int) -> str:
    """Return human-readable confidence description.

    Args:
        score: Confidence score (0-100).

    Returns:
        Human-readable description string.
    """
    if score >= 90:
        return "High confidence - contract is very likely correct"
    elif score >= 70:
        return "Good confidence - contract is probably correct"
    elif score >= 50:
        return "Moderate confidence - contract may need review"
    else:
        return "Low confidence - contract is uncertain"


def get_confidence_distribution(
    conn: duckdb.DuckDBPyConnection,
) -> dict[str, int]:
    """Get distribution of contracts by confidence level.

    Args:
        conn: Database connection.

    Returns:
        Dictionary with confidence level counts.
    """
    result = conn.execute(
        """
        SELECT
            CASE
                WHEN confidence_score >= 0.9 THEN 'high'
                WHEN confidence_score >= 0.7 THEN 'good'
                WHEN confidence_score >= 0.5 THEN 'moderate'
                ELSE 'low'
            END as level,
            COUNT(*) as count
        FROM contracts
        GROUP BY level
        """
    ).fetchall()

    # Initialize all levels with 0
    distribution = {
        "high": 0,
        "good": 0,
        "moderate": 0,
        "low": 0,
    }

    for row in result:
        distribution[row[0]] = row[1]

    return distribution


def suggest_threshold(
    conn: duckdb.DuckDBPyConnection,
    target_verified_ratio: float = 0.7,
) -> int:
    """Suggest a confidence threshold based on current data.

    Analyzes the distribution of confidence scores to suggest a threshold
    that would result in approximately target_verified_ratio of contracts
    being marked as VERIFIED.

    Args:
        conn: Database connection.
        target_verified_ratio: Target ratio of verified contracts (0.0-1.0).

    Returns:
        Suggested threshold (0-100).
    """
    # Get all confidence scores
    result = conn.execute(
        "SELECT confidence_score FROM contracts ORDER BY confidence_score DESC"
    ).fetchall()

    if not result:
        return DEFAULT_CONFIDENCE_THRESHOLD

    scores = [int(row[0] * 100) for row in result]
    total = len(scores)

    # Find threshold that gives approximately target_verified_ratio
    target_verified_count = int(total * target_verified_ratio)

    if target_verified_count >= total:
        # All should be verified, use minimum score
        return min(scores)
    elif target_verified_count <= 0:
        # None should be verified, use above maximum
        return min(100, max(scores) + 1)
    else:
        # Return the score at the target position
        return scores[target_verified_count - 1]


def validate_confidence_score(score: int) -> tuple[bool, Optional[str]]:
    """Validate a confidence score.

    Args:
        score: Score to validate.

    Returns:
        Tuple of (is_valid, error_message).
    """
    if not isinstance(score, int):
        return False, f"Confidence score must be an integer, got {type(score).__name__}"

    if score < 0:
        return False, f"Confidence score must be >= 0, got {score}"

    if score > 100:
        return False, f"Confidence score must be <= 100, got {score}"

    return True, None
