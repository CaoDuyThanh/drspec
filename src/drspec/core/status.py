"""Status tracking system for DrSpec artifacts.

Provides functions for tracking, querying, and managing artifact statuses.
Artifact statuses represent the lifecycle state of contract generation:

- PENDING: Newly discovered, awaiting contract generation
- VERIFIED: Contract generated and validated successfully
- NEEDS_REVIEW: Low confidence contract, requires human review
- STALE: Code changed after verification, needs re-processing
- BROKEN: Contract generation failed after max retries
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import duckdb

from drspec.db.queries import (
    VALID_ARTIFACT_STATUSES,
    Artifact,
    list_artifacts,
    count_artifacts,
    update_artifact_status,
)


@dataclass
class StatusSummary:
    """Summary of artifact statuses.

    Attributes:
        total: Total number of artifacts.
        pending: Number of pending artifacts.
        verified: Number of verified artifacts.
        needs_review: Number needing review.
        stale: Number of stale artifacts.
        broken: Number of broken artifacts.
    """

    total: int
    pending: int
    verified: int
    needs_review: int
    stale: int
    broken: int

    @property
    def completion_rate(self) -> float:
        """Calculate completion rate (verified / total).

        Returns:
            Completion rate as a float between 0.0 and 1.0.
        """
        if self.total == 0:
            return 0.0
        return self.verified / self.total

    @property
    def success_rate(self) -> float:
        """Calculate success rate ((verified + needs_review) / total).

        Returns:
            Success rate as a float between 0.0 and 1.0.
        """
        if self.total == 0:
            return 0.0
        return (self.verified + self.needs_review) / self.total

    @property
    def actionable(self) -> int:
        """Number of artifacts that need attention.

        Returns:
            Count of pending + stale + needs_review + broken.
        """
        return self.pending + self.stale + self.needs_review + self.broken

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization.

        Returns:
            Dictionary representation.
        """
        return {
            "total": self.total,
            "pending": self.pending,
            "verified": self.verified,
            "needs_review": self.needs_review,
            "stale": self.stale,
            "broken": self.broken,
            "completion_rate": round(self.completion_rate, 4),
            "success_rate": round(self.success_rate, 4),
            "actionable": self.actionable,
        }


def get_status_summary(conn: duckdb.DuckDBPyConnection) -> StatusSummary:
    """Get a summary of artifact statuses.

    Args:
        conn: DuckDB connection.

    Returns:
        StatusSummary with counts for each status.
    """
    return StatusSummary(
        total=count_artifacts(conn),
        pending=count_artifacts(conn, status="PENDING"),
        verified=count_artifacts(conn, status="VERIFIED"),
        needs_review=count_artifacts(conn, status="NEEDS_REVIEW"),
        stale=count_artifacts(conn, status="STALE"),
        broken=count_artifacts(conn, status="BROKEN"),
    )


def get_artifacts_by_status(
    conn: duckdb.DuckDBPyConnection,
    status: str,
    limit: int = 100,
    offset: int = 0,
) -> list[Artifact]:
    """Get artifacts with a specific status.

    Args:
        conn: DuckDB connection.
        status: Status to filter by.
        limit: Maximum number of results.
        offset: Number of results to skip.

    Returns:
        List of artifacts with the specified status.

    Raises:
        ValueError: If status is not a valid artifact status.
    """
    if status not in VALID_ARTIFACT_STATUSES:
        raise ValueError(f"Invalid status: {status}. Must be one of {VALID_ARTIFACT_STATUSES}")
    return list_artifacts(conn, status=status, limit=limit, offset=offset)


def get_stale_artifacts(
    conn: duckdb.DuckDBPyConnection,
    limit: int = 100,
) -> list[Artifact]:
    """Get stale artifacts that need re-processing.

    Args:
        conn: DuckDB connection.
        limit: Maximum number of results.

    Returns:
        List of stale artifacts.
    """
    return list_artifacts(conn, status="STALE", limit=limit)


def get_pending_artifacts(
    conn: duckdb.DuckDBPyConnection,
    limit: int = 100,
) -> list[Artifact]:
    """Get pending artifacts awaiting contract generation.

    Args:
        conn: DuckDB connection.
        limit: Maximum number of results.

    Returns:
        List of pending artifacts.
    """
    return list_artifacts(conn, status="PENDING", limit=limit)


def get_broken_artifacts(
    conn: duckdb.DuckDBPyConnection,
    limit: int = 100,
) -> list[Artifact]:
    """Get broken artifacts that failed contract generation.

    Args:
        conn: DuckDB connection.
        limit: Maximum number of results.

    Returns:
        List of broken artifacts.
    """
    return list_artifacts(conn, status="BROKEN", limit=limit)


def get_review_artifacts(
    conn: duckdb.DuckDBPyConnection,
    limit: int = 100,
) -> list[Artifact]:
    """Get artifacts that need human review.

    Args:
        conn: DuckDB connection.
        limit: Maximum number of results.

    Returns:
        List of artifacts needing review.
    """
    return list_artifacts(conn, status="NEEDS_REVIEW", limit=limit)


def mark_verified(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
) -> bool:
    """Mark an artifact as verified.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to mark.

    Returns:
        True if artifact was found and updated.
    """
    return update_artifact_status(conn, function_id, "VERIFIED")


def mark_needs_review(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
) -> bool:
    """Mark an artifact as needing review.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to mark.

    Returns:
        True if artifact was found and updated.
    """
    return update_artifact_status(conn, function_id, "NEEDS_REVIEW")


def mark_broken(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
) -> bool:
    """Mark an artifact as broken.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to mark.

    Returns:
        True if artifact was found and updated.
    """
    return update_artifact_status(conn, function_id, "BROKEN")


def mark_pending(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
) -> bool:
    """Reset an artifact to pending status.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to mark.

    Returns:
        True if artifact was found and updated.
    """
    return update_artifact_status(conn, function_id, "PENDING")


def get_file_status_summary(
    conn: duckdb.DuckDBPyConnection,
    file_path: str,
) -> StatusSummary:
    """Get status summary for a specific file.

    Args:
        conn: DuckDB connection.
        file_path: File path prefix to filter by.

    Returns:
        StatusSummary for artifacts in the file.
    """
    # Count by querying with file_path filter
    total = len(list_artifacts(conn, file_path=file_path, limit=10000))

    pending = len(list_artifacts(conn, file_path=file_path, status="PENDING", limit=10000))
    verified = len(list_artifacts(conn, file_path=file_path, status="VERIFIED", limit=10000))
    needs_review = len(
        list_artifacts(conn, file_path=file_path, status="NEEDS_REVIEW", limit=10000)
    )
    stale = len(list_artifacts(conn, file_path=file_path, status="STALE", limit=10000))
    broken = len(list_artifacts(conn, file_path=file_path, status="BROKEN", limit=10000))

    return StatusSummary(
        total=total,
        pending=pending,
        verified=verified,
        needs_review=needs_review,
        stale=stale,
        broken=broken,
    )


def get_language_status_summary(
    conn: duckdb.DuckDBPyConnection,
    language: str,
) -> StatusSummary:
    """Get status summary for a specific language.

    Args:
        conn: DuckDB connection.
        language: Language to filter by (python, javascript, cpp).

    Returns:
        StatusSummary for artifacts in the language.
    """
    # Count by querying with language filter
    total = len(list_artifacts(conn, language=language, limit=10000))

    pending = len(list_artifacts(conn, language=language, status="PENDING", limit=10000))
    verified = len(list_artifacts(conn, language=language, status="VERIFIED", limit=10000))
    needs_review = len(
        list_artifacts(conn, language=language, status="NEEDS_REVIEW", limit=10000)
    )
    stale = len(list_artifacts(conn, language=language, status="STALE", limit=10000))
    broken = len(list_artifacts(conn, language=language, status="BROKEN", limit=10000))

    return StatusSummary(
        total=total,
        pending=pending,
        verified=verified,
        needs_review=needs_review,
        stale=stale,
        broken=broken,
    )


def bulk_update_status(
    conn: duckdb.DuckDBPyConnection,
    function_ids: list[str],
    status: str,
) -> int:
    """Update status for multiple artifacts.

    Args:
        conn: DuckDB connection.
        function_ids: List of function IDs to update.
        status: New status value.

    Returns:
        Number of artifacts updated.

    Raises:
        ValueError: If status is not a valid artifact status.
    """
    if status not in VALID_ARTIFACT_STATUSES:
        raise ValueError(f"Invalid status: {status}. Must be one of {VALID_ARTIFACT_STATUSES}")

    updated = 0
    for function_id in function_ids:
        if update_artifact_status(conn, function_id, status):
            updated += 1

    return updated


def reset_stale_to_pending(
    conn: duckdb.DuckDBPyConnection,
) -> int:
    """Reset all stale artifacts to pending status.

    This is useful when re-processing the codebase after changes.

    Args:
        conn: DuckDB connection.

    Returns:
        Number of artifacts reset.
    """
    stale = list_artifacts(conn, status="STALE", limit=10000)
    return bulk_update_status(conn, [a.function_id for a in stale], "PENDING")


def reset_broken_to_pending(
    conn: duckdb.DuckDBPyConnection,
) -> int:
    """Reset all broken artifacts to pending status.

    This is useful for retrying failed contract generation.

    Args:
        conn: DuckDB connection.

    Returns:
        Number of artifacts reset.
    """
    broken = list_artifacts(conn, status="BROKEN", limit=10000)
    return bulk_update_status(conn, [a.function_id for a in broken], "PENDING")
