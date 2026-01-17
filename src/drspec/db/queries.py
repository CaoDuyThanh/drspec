"""Typed query functions for DrSpec database operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import duckdb


# =============================================================================
# Status Values
# =============================================================================

VALID_ARTIFACT_STATUSES = frozenset({"PENDING", "VERIFIED", "NEEDS_REVIEW", "STALE", "BROKEN"})
VALID_QUEUE_STATUSES = frozenset({"PENDING", "PROCESSING", "COMPLETED", "FAILED"})
VALID_QUEUE_REASONS = frozenset({"NEW", "HASH_MISMATCH", "DEPENDENCY_CHANGED", "MANUAL_RETRY"})
VALID_FINDING_TYPES = frozenset({"outlier", "discontinuity", "boundary", "correlation", "missing_pattern"})
VALID_FINDING_SIGNIFICANCE = frozenset({"HIGH", "MEDIUM", "LOW"})
VALID_FINDING_STATUSES = frozenset({"NEW", "ADDRESSED", "IGNORED"})


# =============================================================================
# Artifact Model
# =============================================================================


@dataclass
class Artifact:
    """Represents a scanned function artifact.

    Attributes:
        function_id: Unique function ID (filepath::function_name).
        file_path: Relative path from project root.
        function_name: Function/method name.
        signature: Full function signature.
        body: Function source code.
        code_hash: SHA-256 hash of normalized code.
        language: Programming language (python, javascript, cpp).
        start_line: 1-indexed start line.
        end_line: 1-indexed end line.
        parent: Parent class/namespace if applicable.
        status: Status (PENDING, VERIFIED, NEEDS_REVIEW, STALE, BROKEN).
        created_at: Creation timestamp.
        updated_at: Last update timestamp.
    """

    function_id: str
    file_path: str
    function_name: str
    signature: str
    body: str
    code_hash: str
    language: str
    start_line: int
    end_line: int
    parent: Optional[str]
    status: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: tuple) -> "Artifact":
        """Create Artifact from database row.

        Args:
            row: Database row tuple in column order.

        Returns:
            Artifact instance.
        """
        return cls(
            function_id=row[0],
            file_path=row[1],
            function_name=row[2],
            signature=row[3],
            body=row[4],
            code_hash=row[5],
            language=row[6],
            start_line=row[7],
            end_line=row[8],
            parent=row[9],
            status=row[10],
            created_at=row[11],
            updated_at=row[12],
        )


# =============================================================================
# Artifact Queries
# =============================================================================


def insert_artifact(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
    file_path: str,
    function_name: str,
    signature: str,
    body: str,
    code_hash: str,
    language: str,
    start_line: int,
    end_line: int,
    parent: Optional[str] = None,
    status: str = "PENDING",
) -> bool:
    """Insert or update an artifact with hash change detection.

    Uses upsert behavior: if artifact exists and hash differs, updates it.
    If the existing artifact is VERIFIED or NEEDS_REVIEW and the hash changed,
    status is automatically set to STALE.

    Args:
        conn: DuckDB connection.
        function_id: Unique function ID (filepath::function_name).
        file_path: Relative file path.
        function_name: Function name.
        signature: Full function signature.
        body: Function source code.
        code_hash: SHA-256 hash of normalized code.
        language: Programming language (python, javascript, cpp).
        start_line: 1-indexed start line.
        end_line: 1-indexed end line.
        parent: Parent class/namespace if applicable.
        status: Status (PENDING, VERIFIED, NEEDS_REVIEW, STALE, BROKEN).

    Returns:
        True if the artifact was changed (new or hash changed), False otherwise.
    """
    # Check if artifact exists and get its current hash
    existing = conn.execute(
        "SELECT code_hash, status FROM artifacts WHERE function_id = ?",
        [function_id],
    ).fetchone()

    if existing is not None:
        old_hash, old_status = existing
        if old_hash == code_hash:
            # No change in code - skip update to avoid DuckDB FK constraint issue
            # DuckDB has a known limitation where UPDATE on tables with FK references
            # can fail with "still referenced by a foreign key" error
            return False

        # Hash changed - determine new status
        if old_status in ("VERIFIED", "NEEDS_REVIEW"):
            status = "STALE"
        elif old_status == "BROKEN":
            status = "BROKEN"  # Keep broken status, requires manual reset
        # PENDING stays as PENDING

        conn.execute(
            """
            UPDATE artifacts SET
                file_path = ?,
                function_name = ?,
                signature = ?,
                body = ?,
                code_hash = ?,
                language = ?,
                start_line = ?,
                end_line = ?,
                parent = ?,
                status = ?,
                updated_at = now()
            WHERE function_id = ?
            """,
            [file_path, function_name, signature, body, code_hash, language, start_line, end_line, parent, status, function_id],
        )
        return True

    # New artifact - insert
    conn.execute(
        """
        INSERT INTO artifacts (
            function_id, file_path, function_name, signature, body, code_hash,
            language, start_line, end_line, parent, status, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())
        """,
        [function_id, file_path, function_name, signature, body, code_hash, language, start_line, end_line, parent, status],
    )
    return True


def get_artifact(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
) -> Optional[Artifact]:
    """Get an artifact by function ID.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to look up.

    Returns:
        Artifact object or None if not found.
    """
    result = conn.execute(
        """SELECT function_id, file_path, function_name, signature, body, code_hash,
                  language, start_line, end_line, parent, status, created_at, updated_at
           FROM artifacts WHERE function_id = ?""",
        [function_id],
    ).fetchone()

    if result is None:
        return None

    return Artifact.from_row(result)


def list_artifacts(
    conn: duckdb.DuckDBPyConnection,
    status: Optional[str] = None,
    file_path: Optional[str] = None,
    language: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Artifact]:
    """List artifacts with optional filters.

    Args:
        conn: DuckDB connection.
        status: Optional status filter.
        file_path: Optional file path prefix filter.
        language: Optional language filter.
        limit: Maximum number of results.
        offset: Number of results to skip.

    Returns:
        List of Artifact objects.
    """
    query = """SELECT function_id, file_path, function_name, signature, body, code_hash,
                      language, start_line, end_line, parent, status, created_at, updated_at
               FROM artifacts WHERE 1=1"""
    params: list[Any] = []

    if status is not None:
        query += " AND status = ?"
        params.append(status)

    if file_path is not None:
        query += " AND file_path LIKE ?"
        params.append(f"{file_path}%")

    if language is not None:
        query += " AND language = ?"
        params.append(language)

    query += " ORDER BY file_path, start_line LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    results = conn.execute(query, params).fetchall()
    return [Artifact.from_row(row) for row in results]


def count_artifacts(
    conn: duckdb.DuckDBPyConnection,
    status: Optional[str] = None,
) -> int:
    """Count artifacts with optional status filter.

    Args:
        conn: DuckDB connection.
        status: Optional status filter.

    Returns:
        Number of artifacts.
    """
    if status is not None:
        result = conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE status = ?",
            [status],
        ).fetchone()
    else:
        result = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()

    return result[0] if result else 0


def update_artifact_status(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
    status: str,
) -> bool:
    """Update artifact status.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to update.
        status: New status value.

    Returns:
        True if artifact was found and updated.

    Raises:
        ValueError: If status is not a valid status value.
    """
    if status not in VALID_ARTIFACT_STATUSES:
        raise ValueError(f"Invalid status: {status}. Must be one of {VALID_ARTIFACT_STATUSES}")

    # Check if artifact exists first (DuckDB rowcount not reliable)
    existing = conn.execute(
        "SELECT 1 FROM artifacts WHERE function_id = ?", [function_id]
    ).fetchone()
    if existing is None:
        return False

    conn.execute(
        """
        UPDATE artifacts
        SET status = ?, updated_at = now()
        WHERE function_id = ?
        """,
        [status, function_id],
    )
    return True


# =============================================================================
# Contract Queries
# =============================================================================


def insert_contract(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
    contract_json: str,
    confidence_score: float = 0.0,
    verification_script: Optional[str] = None,
) -> None:
    """Insert or update a contract.

    Args:
        conn: DuckDB connection.
        function_id: Function ID.
        contract_json: Contract JSON string (Pydantic validated).
        confidence_score: Confidence score 0.0 to 1.0.
        verification_script: Generated Python verification script.
    """
    conn.execute(
        """
        INSERT INTO contracts (function_id, contract_json, confidence_score, verification_script, updated_at)
        VALUES (?, ?, ?, ?, now())
        ON CONFLICT (function_id) DO UPDATE SET
            contract_json = EXCLUDED.contract_json,
            confidence_score = EXCLUDED.confidence_score,
            verification_script = EXCLUDED.verification_script,
            updated_at = now()
        """,
        [function_id, contract_json, confidence_score, verification_script],
    )


def get_contract(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
) -> Optional[dict[str, Any]]:
    """Get a contract by function ID.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to look up.

    Returns:
        Contract dict or None if not found.
    """
    result = conn.execute(
        "SELECT * FROM contracts WHERE function_id = ?",
        [function_id],
    ).fetchone()

    if result is None:
        return None

    columns = [
        "function_id",
        "contract_json",
        "confidence_score",
        "verification_script",
        "created_at",
        "updated_at",
    ]
    return dict(zip(columns, result))


def list_contracts(
    conn: duckdb.DuckDBPyConnection,
    status: Optional[str] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List contracts with optional status filter.

    Args:
        conn: DuckDB connection.
        status: Optional status filter (VERIFIED, NEEDS_REVIEW, etc.).
        limit: Maximum number of results.

    Returns:
        List of contract dicts.
    """
    if status:
        result = conn.execute(
            """
            SELECT c.*, a.status
            FROM contracts c
            JOIN artifacts a ON c.function_id = a.function_id
            WHERE a.status = ?
            ORDER BY c.confidence_score DESC
            LIMIT ?
            """,
            [status, limit],
        ).fetchall()
    else:
        result = conn.execute(
            """
            SELECT c.*, a.status
            FROM contracts c
            JOIN artifacts a ON c.function_id = a.function_id
            ORDER BY c.confidence_score DESC
            LIMIT ?
            """,
            [limit],
        ).fetchall()

    columns = [
        "function_id",
        "contract_json",
        "confidence_score",
        "created_at",
        "updated_at",
        "status",
    ]
    return [dict(zip(columns, row)) for row in result]


def count_contracts(conn: duckdb.DuckDBPyConnection) -> int:
    """Count total contracts.

    Args:
        conn: DuckDB connection.

    Returns:
        Number of contracts.
    """
    result = conn.execute("SELECT COUNT(*) FROM contracts").fetchone()
    return result[0] if result else 0


def get_contract_confidence_stats(
    conn: duckdb.DuckDBPyConnection,
) -> dict[str, Any]:
    """Get contract confidence score statistics.

    Args:
        conn: DuckDB connection.

    Returns:
        Dictionary with average and distribution.
    """
    result = conn.execute(
        """
        SELECT
            AVG(confidence_score) as average,
            SUM(CASE WHEN confidence_score < 0.5 THEN 1 ELSE 0 END) as below_50,
            SUM(CASE WHEN confidence_score >= 0.5 AND confidence_score < 0.7 THEN 1 ELSE 0 END) as range_50_70,
            SUM(CASE WHEN confidence_score >= 0.7 AND confidence_score < 0.9 THEN 1 ELSE 0 END) as range_70_90,
            SUM(CASE WHEN confidence_score >= 0.9 THEN 1 ELSE 0 END) as above_90
        FROM contracts
        """
    ).fetchone()

    if result is None or result[0] is None:
        return {
            "average": 0.0,
            "distribution": {
                "below_50": 0,
                "50_to_70": 0,
                "70_to_90": 0,
                "above_90": 0,
            },
        }

    return {
        "average": round(result[0] * 100, 1),  # Convert to percentage
        "distribution": {
            "below_50": int(result[1] or 0),
            "50_to_70": int(result[2] or 0),
            "70_to_90": int(result[3] or 0),
            "above_90": int(result[4] or 0),
        },
    }


# =============================================================================
# Contract Details Model (for Debugger Agent)
# =============================================================================


@dataclass
class ContractDetails:
    """Comprehensive contract details for debugger queries.

    Combines contract data with artifact metadata for efficient debugging.

    Attributes:
        function_id: Unique function ID (filepath::function_name).
        contract_json: Contract JSON string (Pydantic validated).
        confidence_score: Confidence score 0.0 to 1.0.
        status: Artifact status (PENDING, VERIFIED, NEEDS_REVIEW, STALE, BROKEN).
        file_path: Relative file path.
        function_name: Function name.
        created_at: Contract creation timestamp.
        updated_at: Contract last update timestamp.
        has_verification_script: Whether a verification script exists.
    """

    function_id: str
    contract_json: str
    confidence_score: float
    status: str
    file_path: str
    function_name: str
    created_at: datetime
    updated_at: datetime
    has_verification_script: bool

    @classmethod
    def from_row(cls, row: tuple) -> "ContractDetails":
        """Create ContractDetails from database row.

        Args:
            row: Database row tuple in column order.

        Returns:
            ContractDetails instance.
        """
        return cls(
            function_id=row[0],
            contract_json=row[1],
            confidence_score=row[2],
            created_at=row[3],
            updated_at=row[4],
            status=row[5],
            file_path=row[6],
            function_name=row[7],
            has_verification_script=row[8],
        )


# =============================================================================
# Contract Query Functions (for Debugger Agent)
# =============================================================================


def query_contract(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
) -> Optional[ContractDetails]:
    """Query a single contract by function ID with full details.

    Optimized for debugger agent access. Returns None for non-existent
    contracts without raising an exception.

    Args:
        conn: DuckDB connection.
        function_id: Exact function ID to look up.

    Returns:
        ContractDetails or None if not found.
    """
    result = conn.execute(
        """
        SELECT
            c.function_id,
            c.contract_json,
            c.confidence_score,
            c.created_at,
            c.updated_at,
            a.status,
            a.file_path,
            a.function_name,
            c.verification_script IS NOT NULL as has_verification_script
        FROM contracts c
        JOIN artifacts a ON c.function_id = a.function_id
        WHERE c.function_id = ?
        """,
        [function_id],
    ).fetchone()

    if result is None:
        return None

    return ContractDetails.from_row(result)


def query_contracts(
    conn: duckdb.DuckDBPyConnection,
    function_ids: list[str],
) -> dict[str, ContractDetails]:
    """Query multiple contracts by function IDs efficiently.

    Uses a single database query for optimal performance.
    Non-existent contracts are silently omitted from results.

    Args:
        conn: DuckDB connection.
        function_ids: List of function IDs to query.

    Returns:
        Dictionary mapping function_id to ContractDetails.
        Missing contracts are not included (no error raised).
    """
    if not function_ids:
        return {}

    # Build parameterized IN clause
    placeholders = ", ".join(["?" for _ in function_ids])
    result = conn.execute(
        f"""
        SELECT
            c.function_id,
            c.contract_json,
            c.confidence_score,
            c.created_at,
            c.updated_at,
            a.status,
            a.file_path,
            a.function_name,
            c.verification_script IS NOT NULL as has_verification_script
        FROM contracts c
        JOIN artifacts a ON c.function_id = a.function_id
        WHERE c.function_id IN ({placeholders})
        """,
        function_ids,
    ).fetchall()

    return {row[0]: ContractDetails.from_row(row) for row in result}


def search_contracts(
    conn: duckdb.DuckDBPyConnection,
    pattern: str,
    limit: int = 10,
) -> list[ContractDetails]:
    """Search contracts by function name pattern (partial match).

    Supports searching by function name only or partial function_id.
    Useful for finding contracts when exact ID is unknown.

    Args:
        conn: DuckDB connection.
        pattern: Search pattern (matches function name or function_id).
        limit: Maximum results to return (default 10).

    Returns:
        List of ContractDetails matching the pattern.
        Returns empty list if no matches found.
    """
    if not pattern:
        return []

    result = conn.execute(
        """
        SELECT
            c.function_id,
            c.contract_json,
            c.confidence_score,
            c.created_at,
            c.updated_at,
            a.status,
            a.file_path,
            a.function_name,
            c.verification_script IS NOT NULL as has_verification_script
        FROM contracts c
        JOIN artifacts a ON c.function_id = a.function_id
        WHERE a.function_name LIKE ? || '%'
           OR c.function_id LIKE '%' || ? || '%'
        ORDER BY
            CASE WHEN a.function_name = ? THEN 0 ELSE 1 END,
            a.function_name
        LIMIT ?
        """,
        [pattern, pattern, pattern, limit],
    ).fetchall()

    return [ContractDetails.from_row(row) for row in result]


# =============================================================================
# Queue Model
# =============================================================================


@dataclass
class QueueItem:
    """Represents a processing queue item.

    Attributes:
        function_id: Function ID in the queue.
        priority: Priority level (lower = higher priority).
        status: Queue status (PENDING, PROCESSING, COMPLETED, FAILED).
        reason: Why it was queued (NEW, HASH_MISMATCH, DEPENDENCY_CHANGED, MANUAL_RETRY).
        attempts: Number of processing attempts.
        max_attempts: Maximum allowed attempts before permanent failure.
        error_message: Last error message if failed.
        created_at: When it was added to the queue.
        updated_at: Last update timestamp.
    """

    function_id: str
    priority: int
    status: str
    reason: str
    attempts: int
    max_attempts: int
    error_message: Optional[str]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: tuple) -> "QueueItem":
        """Create QueueItem from database row.

        Args:
            row: Database row tuple in column order.

        Returns:
            QueueItem instance.
        """
        return cls(
            function_id=row[0],
            priority=row[1],
            status=row[2],
            reason=row[3],
            attempts=row[4],
            max_attempts=row[5],
            error_message=row[6],
            created_at=row[7],
            updated_at=row[8],
        )


# =============================================================================
# Queue Queries
# =============================================================================


def queue_push(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
    priority: int = 100,
    reason: str = "NEW",
) -> None:
    """Add a function to the processing queue.

    If the function is already in the queue, updates priority and reason,
    and resets status to PENDING.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to queue.
        priority: Priority level (lower = higher priority).
        reason: Why it's being queued (NEW, HASH_MISMATCH, DEPENDENCY_CHANGED, MANUAL_RETRY).

    Raises:
        ValueError: If reason is not a valid queue reason.
    """
    if reason not in VALID_QUEUE_REASONS:
        raise ValueError(f"Invalid reason: {reason}. Must be one of {VALID_QUEUE_REASONS}")

    conn.execute(
        """
        INSERT INTO queue (function_id, priority, status, reason, attempts, updated_at)
        VALUES (?, ?, 'PENDING', ?, 0, now())
        ON CONFLICT (function_id) DO UPDATE SET
            priority = EXCLUDED.priority,
            status = 'PENDING',
            reason = EXCLUDED.reason,
            updated_at = now()
        """,
        [function_id, priority, reason],
    )


def queue_pop(
    conn: duckdb.DuckDBPyConnection,
) -> Optional[QueueItem]:
    """Get and mark the next item from the queue as processing.

    Only returns items that haven't exceeded their max attempts.
    Increments the attempt counter.

    Args:
        conn: DuckDB connection.

    Returns:
        QueueItem or None if queue is empty.
    """
    # Get next pending item that hasn't exceeded max attempts
    result = conn.execute(
        """
        SELECT function_id, priority, status, reason, attempts, max_attempts,
               error_message, created_at, updated_at
        FROM queue
        WHERE status = 'PENDING' AND attempts < max_attempts
        ORDER BY priority ASC, created_at ASC
        LIMIT 1
        """,
    ).fetchone()

    if result is None:
        return None

    function_id = result[0]
    attempts = result[4]

    # Mark as processing and increment attempts
    conn.execute(
        """
        UPDATE queue
        SET status = 'PROCESSING', attempts = ?, updated_at = now()
        WHERE function_id = ?
        """,
        [attempts + 1, function_id],
    )

    # Return updated item
    return QueueItem(
        function_id=result[0],
        priority=result[1],
        status="PROCESSING",
        reason=result[3],
        attempts=attempts + 1,
        max_attempts=result[5],
        error_message=result[6],
        created_at=result[7],
        updated_at=result[8],
    )


def queue_peek(
    conn: duckdb.DuckDBPyConnection,
    count: int = 10,
    include_all: bool = False,
) -> list[QueueItem]:
    """Peek at upcoming queue items without consuming them.

    Args:
        conn: DuckDB connection.
        count: Number of items to peek.
        include_all: If True, include all statuses; if False, only PENDING.

    Returns:
        List of QueueItem objects.
    """
    if include_all:
        result = conn.execute(
            """
            SELECT function_id, priority, status, reason, attempts, max_attempts,
                   error_message, created_at, updated_at
            FROM queue
            ORDER BY priority ASC, created_at ASC
            LIMIT ?
            """,
            [count],
        ).fetchall()
    else:
        result = conn.execute(
            """
            SELECT function_id, priority, status, reason, attempts, max_attempts,
                   error_message, created_at, updated_at
            FROM queue
            WHERE status = 'PENDING' AND attempts < max_attempts
            ORDER BY priority ASC, created_at ASC
            LIMIT ?
            """,
            [count],
        ).fetchall()

    return [QueueItem.from_row(row) for row in result]


def queue_complete(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
    success: bool = True,
    error_message: Optional[str] = None,
) -> bool:
    """Mark a queue item as completed or failed.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to mark.
        success: True for COMPLETED, False for FAILED.
        error_message: Error message if failed.

    Returns:
        True if item was found and updated.
    """
    # Check if queue item exists first (DuckDB rowcount not reliable)
    existing = conn.execute(
        "SELECT 1 FROM queue WHERE function_id = ?", [function_id]
    ).fetchone()
    if existing is None:
        return False

    status = "COMPLETED" if success else "FAILED"
    conn.execute(
        """
        UPDATE queue
        SET status = ?, error_message = ?, updated_at = now()
        WHERE function_id = ?
        """,
        [status, error_message, function_id],
    )
    return True


def queue_retry(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
    reason: str = "MANUAL_RETRY",
) -> bool:
    """Reset a failed queue item for retry.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to retry.
        reason: Reason for retry.

    Returns:
        True if item was found and reset.

    Raises:
        ValueError: If reason is not a valid queue reason.
    """
    if reason not in VALID_QUEUE_REASONS:
        raise ValueError(f"Invalid reason: {reason}. Must be one of {VALID_QUEUE_REASONS}")

    # Check if queue item exists
    existing = conn.execute(
        "SELECT 1 FROM queue WHERE function_id = ?", [function_id]
    ).fetchone()
    if existing is None:
        return False

    conn.execute(
        """
        UPDATE queue
        SET status = 'PENDING', reason = ?, error_message = NULL, updated_at = now()
        WHERE function_id = ?
        """,
        [reason, function_id],
    )
    return True


def queue_prioritize(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
    priority: int,
) -> bool:
    """Update priority for a queue item.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to prioritize.
        priority: New priority level.

    Returns:
        True if item was found and updated.
    """
    # Check if queue item exists first (DuckDB rowcount not reliable)
    existing = conn.execute(
        "SELECT 1 FROM queue WHERE function_id = ?", [function_id]
    ).fetchone()
    if existing is None:
        return False

    conn.execute(
        """
        UPDATE queue
        SET priority = ?, updated_at = now()
        WHERE function_id = ?
        """,
        [priority, function_id],
    )
    return True


def queue_remove(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
) -> bool:
    """Remove an item from the queue.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to remove.

    Returns:
        True if item was found and removed.
    """
    existing = conn.execute(
        "SELECT 1 FROM queue WHERE function_id = ?", [function_id]
    ).fetchone()
    if existing is None:
        return False

    conn.execute("DELETE FROM queue WHERE function_id = ?", [function_id])
    return True


def queue_get(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
) -> Optional[QueueItem]:
    """Get a queue item by function ID.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to look up.

    Returns:
        QueueItem or None if not found.
    """
    result = conn.execute(
        """
        SELECT function_id, priority, status, reason, attempts, max_attempts,
               error_message, created_at, updated_at
        FROM queue
        WHERE function_id = ?
        """,
        [function_id],
    ).fetchone()

    if result is None:
        return None

    return QueueItem.from_row(result)


def queue_count(
    conn: duckdb.DuckDBPyConnection,
    status: Optional[str] = None,
) -> int:
    """Count queue items.

    Args:
        conn: DuckDB connection.
        status: Optional status filter.

    Returns:
        Number of queue items.
    """
    if status is not None:
        result = conn.execute(
            "SELECT COUNT(*) FROM queue WHERE status = ?",
            [status],
        ).fetchone()
    else:
        result = conn.execute("SELECT COUNT(*) FROM queue").fetchone()

    return result[0] if result else 0


def queue_clear_completed(
    conn: duckdb.DuckDBPyConnection,
) -> int:
    """Remove all completed items from the queue.

    Args:
        conn: DuckDB connection.

    Returns:
        Number of items removed.
    """
    count = conn.execute(
        "SELECT COUNT(*) FROM queue WHERE status = 'COMPLETED'"
    ).fetchone()[0]

    conn.execute("DELETE FROM queue WHERE status = 'COMPLETED'")
    return count


# =============================================================================
# Dependency Queries
# =============================================================================


def insert_dependency(
    conn: duckdb.DuckDBPyConnection,
    caller_id: str,
    callee_id: str,
) -> None:
    """Insert a caller/callee dependency relationship.

    Args:
        conn: DuckDB connection.
        caller_id: Calling function ID.
        callee_id: Called function ID.
    """
    conn.execute(
        """
        INSERT INTO dependencies (caller_id, callee_id)
        VALUES (?, ?)
        ON CONFLICT DO NOTHING
        """,
        [caller_id, callee_id],
    )


def get_callers(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
) -> list[str]:
    """Get functions that call the given function.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to look up callers for.

    Returns:
        List of caller function IDs.
    """
    result = conn.execute(
        "SELECT caller_id FROM dependencies WHERE callee_id = ?",
        [function_id],
    ).fetchall()
    return [row[0] for row in result]


def get_callees(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
) -> list[str]:
    """Get functions that are called by the given function.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to look up callees for.

    Returns:
        List of callee function IDs.
    """
    result = conn.execute(
        "SELECT callee_id FROM dependencies WHERE caller_id = ?",
        [function_id],
    ).fetchall()
    return [row[0] for row in result]


# =============================================================================
# Reasoning Trace Queries
# =============================================================================


def insert_reasoning_trace(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
    agent: str,
    trace_json: str,
) -> None:
    """Insert a reasoning trace entry.

    Args:
        conn: DuckDB connection.
        function_id: Function ID the trace is for.
        agent: Agent name (librarian, proposer, critic, judge, debugger).
        trace_json: JSON trace data.
    """
    conn.execute(
        """
        INSERT INTO reasoning_traces (function_id, agent, trace_json)
        VALUES (?, ?, ?)
        """,
        [function_id, agent, trace_json],
    )


def get_reasoning_traces(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
    agent: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Get reasoning traces for a function.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to get traces for.
        agent: Optional agent name filter.

    Returns:
        List of trace dicts.
    """
    if agent:
        result = conn.execute(
            """
            SELECT id, function_id, agent, trace_json, created_at
            FROM reasoning_traces
            WHERE function_id = ? AND agent = ?
            ORDER BY created_at DESC
            """,
            [function_id, agent],
        ).fetchall()
    else:
        result = conn.execute(
            """
            SELECT id, function_id, agent, trace_json, created_at
            FROM reasoning_traces
            WHERE function_id = ?
            ORDER BY created_at DESC
            """,
            [function_id],
        ).fetchall()

    columns = ["id", "function_id", "agent", "trace_json", "created_at"]
    return [dict(zip(columns, row)) for row in result]


# =============================================================================
# Config Queries
# =============================================================================


def get_config(
    conn: duckdb.DuckDBPyConnection,
    key: str,
    default: Optional[str] = None,
) -> Optional[str]:
    """Get a configuration value.

    Args:
        conn: DuckDB connection.
        key: Configuration key.
        default: Default value if key not found.

    Returns:
        Configuration value or default.
    """
    result = conn.execute(
        "SELECT value FROM config WHERE key = ?",
        [key],
    ).fetchone()

    if result is None:
        return default

    return result[0]


def set_config(
    conn: duckdb.DuckDBPyConnection,
    key: str,
    value: str,
) -> None:
    """Set a configuration value.

    Args:
        conn: DuckDB connection.
        key: Configuration key.
        value: Configuration value.
    """
    conn.execute(
        """
        INSERT INTO config (key, value, updated_at)
        VALUES (?, ?, now())
        ON CONFLICT (key) DO UPDATE SET
            value = EXCLUDED.value,
            updated_at = now()
        """,
        [key, value],
    )


def get_all_config(
    conn: duckdb.DuckDBPyConnection,
) -> dict[str, str]:
    """Get all configuration values.

    Args:
        conn: DuckDB connection.

    Returns:
        Dictionary of all config key-value pairs.
    """
    result = conn.execute("SELECT key, value FROM config").fetchall()
    return {row[0]: row[1] for row in result}


# =============================================================================
# Vision Finding Model
# =============================================================================


@dataclass
class VisionFinding:
    """Represents a visual analysis finding from Vision Analyst.

    Attributes:
        id: Unique finding ID.
        function_id: Function this finding relates to.
        finding_type: Type of finding (outlier, discontinuity, etc.).
        significance: Severity level (HIGH, MEDIUM, LOW).
        description: Description of the finding.
        location: Where in the plot the finding was observed.
        invariant_implication: Suggested invariant change.
        status: Finding status (NEW, ADDRESSED, IGNORED).
        resolution_note: How the finding was addressed or why ignored.
        plot_path: Path to the plot image.
        created_at: When the finding was created.
    """

    id: Optional[int]
    function_id: str
    finding_type: str
    significance: str
    description: str
    location: Optional[str]
    invariant_implication: Optional[str]
    status: str
    resolution_note: Optional[str]
    plot_path: Optional[str]
    created_at: datetime

    @classmethod
    def from_row(cls, row: tuple) -> "VisionFinding":
        """Create VisionFinding from database row.

        Args:
            row: Database row tuple in column order.

        Returns:
            VisionFinding instance.
        """
        return cls(
            id=row[0],
            function_id=row[1],
            finding_type=row[2],
            significance=row[3],
            description=row[4],
            location=row[5],
            invariant_implication=row[6],
            status=row[7],
            resolution_note=row[8],
            plot_path=row[9],
            created_at=row[10],
        )


# =============================================================================
# Vision Finding Queries
# =============================================================================


def insert_vision_finding(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
    finding_type: str,
    significance: str,
    description: str,
    location: Optional[str] = None,
    invariant_implication: Optional[str] = None,
    plot_path: Optional[str] = None,
) -> int:
    """Insert a vision finding.

    Args:
        conn: DuckDB connection.
        function_id: Function ID the finding relates to.
        finding_type: Type of finding (outlier, discontinuity, boundary, correlation, missing_pattern).
        significance: Severity (HIGH, MEDIUM, LOW).
        description: Description of the finding.
        location: Where in the plot it was observed.
        invariant_implication: Suggested invariant change.
        plot_path: Path to the plot image.

    Returns:
        ID of the inserted finding.

    Raises:
        ValueError: If finding_type or significance is invalid.
    """
    if finding_type not in VALID_FINDING_TYPES:
        raise ValueError(f"Invalid finding_type: {finding_type}. Must be one of {VALID_FINDING_TYPES}")

    if significance not in VALID_FINDING_SIGNIFICANCE:
        raise ValueError(f"Invalid significance: {significance}. Must be one of {VALID_FINDING_SIGNIFICANCE}")

    conn.execute(
        """
        INSERT INTO vision_findings (
            function_id, finding_type, significance, description,
            location, invariant_implication, status, plot_path
        ) VALUES (?, ?, ?, ?, ?, ?, 'NEW', ?)
        """,
        [function_id, finding_type, significance, description, location, invariant_implication, plot_path],
    )

    # Get the last inserted ID
    result = conn.execute("SELECT MAX(id) FROM vision_findings WHERE function_id = ?", [function_id]).fetchone()
    return result[0] if result else 0


def get_vision_findings(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
    status: Optional[str] = None,
    significance: Optional[str] = None,
) -> list[VisionFinding]:
    """Get vision findings for a function.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to get findings for.
        status: Optional status filter (NEW, ADDRESSED, IGNORED).
        significance: Optional significance filter (HIGH, MEDIUM, LOW).

    Returns:
        List of VisionFinding objects.
    """
    query = """
        SELECT id, function_id, finding_type, significance, description,
               location, invariant_implication, status, resolution_note,
               plot_path, created_at
        FROM vision_findings
        WHERE function_id = ?
    """
    params: list[Any] = [function_id]

    if status is not None:
        query += " AND status = ?"
        params.append(status)

    if significance is not None:
        query += " AND significance = ?"
        params.append(significance)

    query += " ORDER BY created_at DESC"

    result = conn.execute(query, params).fetchall()
    return [VisionFinding.from_row(row) for row in result]


def update_vision_finding_status(
    conn: duckdb.DuckDBPyConnection,
    finding_id: int,
    status: str,
    resolution_note: Optional[str] = None,
) -> bool:
    """Update the status of a vision finding.

    Args:
        conn: DuckDB connection.
        finding_id: ID of the finding to update.
        status: New status (NEW, ADDRESSED, IGNORED).
        resolution_note: Note explaining how it was addressed or why ignored.

    Returns:
        True if finding was found and updated.

    Raises:
        ValueError: If status is invalid.
    """
    if status not in VALID_FINDING_STATUSES:
        raise ValueError(f"Invalid status: {status}. Must be one of {VALID_FINDING_STATUSES}")

    # Check if finding exists
    existing = conn.execute(
        "SELECT 1 FROM vision_findings WHERE id = ?", [finding_id]
    ).fetchone()
    if existing is None:
        return False

    conn.execute(
        """
        UPDATE vision_findings
        SET status = ?, resolution_note = ?
        WHERE id = ?
        """,
        [status, resolution_note, finding_id],
    )
    return True


def count_vision_findings(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
    status: Optional[str] = None,
) -> int:
    """Count vision findings for a function.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to count findings for.
        status: Optional status filter.

    Returns:
        Number of findings.
    """
    if status is not None:
        result = conn.execute(
            "SELECT COUNT(*) FROM vision_findings WHERE function_id = ? AND status = ?",
            [function_id, status],
        ).fetchone()
    else:
        result = conn.execute(
            "SELECT COUNT(*) FROM vision_findings WHERE function_id = ?",
            [function_id],
        ).fetchone()

    return result[0] if result else 0


def calculate_confidence_with_findings(
    base_confidence: int,
    findings: list[VisionFinding],
) -> int:
    """Calculate adjusted confidence based on unresolved vision findings.

    Only NEW (unresolved) findings reduce confidence.

    Args:
        base_confidence: Base confidence score (0-100).
        findings: List of VisionFinding objects.

    Returns:
        Adjusted confidence score (0-100).
    """
    penalty = 0

    for finding in findings:
        if finding.status == "NEW":
            if finding.significance == "HIGH":
                penalty += 15
            elif finding.significance == "MEDIUM":
                penalty += 8
            elif finding.significance == "LOW":
                penalty += 3

    return max(0, base_confidence - penalty)


def get_all_vision_findings(
    conn: duckdb.DuckDBPyConnection,
    status: Optional[str] = None,
    significance: Optional[str] = None,
    limit: int = 100,
) -> list[VisionFinding]:
    """Get all vision findings across all functions.

    Args:
        conn: DuckDB connection.
        status: Optional status filter (NEW, ADDRESSED, IGNORED).
        significance: Optional significance filter (HIGH, MEDIUM, LOW).
        limit: Maximum number of results (default 100).

    Returns:
        List of VisionFinding objects.

    Raises:
        ValueError: If status or significance is invalid.
    """
    query = """
        SELECT id, function_id, finding_type, significance, description,
               location, invariant_implication, status, resolution_note,
               plot_path, created_at
        FROM vision_findings
        WHERE 1=1
    """
    params: list[Any] = []

    if status is not None:
        if status.upper() not in VALID_FINDING_STATUSES:
            raise ValueError(f"Invalid status: {status}. Must be one of {VALID_FINDING_STATUSES}")
        query += " AND status = ?"
        params.append(status.upper())

    if significance is not None:
        if significance.upper() not in VALID_FINDING_SIGNIFICANCE:
            raise ValueError(f"Invalid significance: {significance}. Must be one of {VALID_FINDING_SIGNIFICANCE}")
        query += " AND significance = ?"
        params.append(significance.upper())

    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    result = conn.execute(query, params).fetchall()
    return [VisionFinding.from_row(row) for row in result]
