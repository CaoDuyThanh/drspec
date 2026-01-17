"""Learning history storage and queries.

This module provides functionality to:
- Store learning events from bug analysis
- Query learning history
- Track contract modifications
- Generate analytics on learning effectiveness
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

import duckdb

from drspec.learning.patterns import PatternType


# Schema for learning history table
# Note: DuckDB requires explicit sequence for auto-increment
LEARNING_HISTORY_SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS learning_history_seq;

CREATE TABLE IF NOT EXISTS learning_history (
    id INTEGER DEFAULT nextval('learning_history_seq') PRIMARY KEY,
    commit_sha TEXT NOT NULL,
    commit_message TEXT,
    function_id TEXT,
    pattern_type TEXT,
    pattern_description TEXT,
    contract_modified BOOLEAN DEFAULT FALSE,
    confidence_boost REAL DEFAULT 0.0,
    new_invariants_added INTEGER DEFAULT 0,
    invariants_validated INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_learning_commit ON learning_history(commit_sha);
CREATE INDEX IF NOT EXISTS idx_learning_function ON learning_history(function_id);
CREATE INDEX IF NOT EXISTS idx_learning_pattern ON learning_history(pattern_type);
CREATE INDEX IF NOT EXISTS idx_learning_date ON learning_history(created_at);
"""


@dataclass
class LearningEvent:
    """A single learning event from bug analysis.

    Attributes:
        commit_sha: The commit that was analyzed.
        commit_message: The commit message (truncated).
        function_id: The function that was affected.
        pattern_type: Type of pattern detected.
        pattern_description: Description of the pattern.
        contract_modified: Whether the contract was modified.
        confidence_boost: Confidence boost applied.
        new_invariants_added: Number of new invariants added.
        invariants_validated: Number of invariants validated.
        created_at: When this event was recorded.
        id: Database ID (None if not persisted).
    """

    commit_sha: str
    function_id: Optional[str] = None
    pattern_type: Optional[PatternType] = None
    pattern_description: str = ""
    commit_message: str = ""
    contract_modified: bool = False
    confidence_boost: float = 0.0
    new_invariants_added: int = 0
    invariants_validated: int = 0
    created_at: Optional[datetime] = None
    id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "commit_sha": self.commit_sha,
            "commit_message": self.commit_message,
            "function_id": self.function_id,
            "pattern_type": self.pattern_type.value if self.pattern_type else None,
            "pattern_description": self.pattern_description,
            "contract_modified": self.contract_modified,
            "confidence_boost": self.confidence_boost,
            "new_invariants_added": self.new_invariants_added,
            "invariants_validated": self.invariants_validated,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    @classmethod
    def from_row(cls, row: tuple) -> "LearningEvent":
        """Create from database row."""
        return cls(
            id=row[0],
            commit_sha=row[1],
            commit_message=row[2] or "",
            function_id=row[3],
            pattern_type=PatternType(row[4]) if row[4] else None,
            pattern_description=row[5] or "",
            contract_modified=bool(row[6]),
            confidence_boost=float(row[7]) if row[7] else 0.0,
            new_invariants_added=int(row[8]) if row[8] else 0,
            invariants_validated=int(row[9]) if row[9] else 0,
            created_at=row[10] if row[10] else None,
        )


def init_learning_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Initialize the learning history schema.

    Args:
        conn: DuckDB connection.
    """
    conn.execute(LEARNING_HISTORY_SCHEMA)


def insert_learning_event(
    conn: duckdb.DuckDBPyConnection,
    event: LearningEvent,
) -> int:
    """Insert a learning event into the database.

    Args:
        conn: DuckDB connection.
        event: The learning event to insert.

    Returns:
        The ID of the inserted event.
    """
    # Ensure schema exists
    init_learning_schema(conn)

    result = conn.execute(
        """
        INSERT INTO learning_history (
            commit_sha, commit_message, function_id, pattern_type,
            pattern_description, contract_modified, confidence_boost,
            new_invariants_added, invariants_validated, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
        RETURNING id
        """,
        [
            event.commit_sha,
            event.commit_message[:500] if event.commit_message else None,  # Truncate
            event.function_id,
            event.pattern_type.value if event.pattern_type else None,
            event.pattern_description[:1000] if event.pattern_description else None,
            event.contract_modified,
            event.confidence_boost,
            event.new_invariants_added,
            event.invariants_validated,
            event.created_at,
        ],
    ).fetchone()

    return result[0] if result else 0


def get_learning_history(
    conn: duckdb.DuckDBPyConnection,
    function_id: Optional[str] = None,
    commit_sha: Optional[str] = None,
    pattern_type: Optional[PatternType] = None,
    limit: int = 100,
    offset: int = 0,
) -> List[LearningEvent]:
    """Query learning history with optional filters.

    Args:
        conn: DuckDB connection.
        function_id: Filter by function ID.
        commit_sha: Filter by commit SHA (prefix match).
        pattern_type: Filter by pattern type.
        limit: Maximum number of results.
        offset: Number of results to skip.

    Returns:
        List of learning events.
    """
    conditions = []
    params = []

    if function_id:
        conditions.append("function_id = ?")
        params.append(function_id)

    if commit_sha:
        conditions.append("commit_sha LIKE ?")
        params.append(f"{commit_sha}%")

    if pattern_type:
        conditions.append("pattern_type = ?")
        params.append(pattern_type.value)

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)

    params.extend([limit, offset])

    result = conn.execute(
        f"""
        SELECT id, commit_sha, commit_message, function_id, pattern_type,
               pattern_description, contract_modified, confidence_boost,
               new_invariants_added, invariants_validated, created_at
        FROM learning_history
        {where_clause}
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
        """,
        params,
    ).fetchall()

    return [LearningEvent.from_row(row) for row in result]


def get_learning_stats(
    conn: duckdb.DuckDBPyConnection,
) -> Dict[str, Any]:
    """Get statistics on learning history.

    Args:
        conn: DuckDB connection.

    Returns:
        Dictionary with learning statistics.
    """
    # Ensure schema exists
    init_learning_schema(conn)

    # Overall counts
    counts = conn.execute(
        """
        SELECT
            COUNT(*) as total_events,
            COUNT(DISTINCT commit_sha) as unique_commits,
            COUNT(DISTINCT function_id) as unique_functions,
            SUM(CASE WHEN contract_modified THEN 1 ELSE 0 END) as contracts_modified,
            SUM(new_invariants_added) as total_invariants_added,
            SUM(invariants_validated) as total_invariants_validated,
            AVG(confidence_boost) as avg_confidence_boost
        FROM learning_history
        """
    ).fetchone()

    # Pattern type distribution
    pattern_dist = conn.execute(
        """
        SELECT pattern_type, COUNT(*) as count
        FROM learning_history
        WHERE pattern_type IS NOT NULL
        GROUP BY pattern_type
        ORDER BY count DESC
        """
    ).fetchall()

    # Recent activity (last 7 days)
    recent = conn.execute(
        """
        SELECT
            DATE(created_at) as date,
            COUNT(*) as events
        FROM learning_history
        WHERE created_at >= CURRENT_DATE - INTERVAL '7 days'
        GROUP BY DATE(created_at)
        ORDER BY date DESC
        """
    ).fetchall()

    return {
        "total_events": counts[0] if counts else 0,
        "unique_commits": counts[1] if counts else 0,
        "unique_functions": counts[2] if counts else 0,
        "contracts_modified": counts[3] if counts else 0,
        "total_invariants_added": counts[4] if counts else 0,
        "total_invariants_validated": counts[5] if counts else 0,
        "avg_confidence_boost": float(counts[6]) if counts and counts[6] else 0.0,
        "pattern_distribution": {
            row[0]: row[1] for row in pattern_dist
        },
        "recent_activity": [
            {"date": str(row[0]), "events": row[1]} for row in recent
        ],
    }


def get_function_learning_history(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
) -> Dict[str, Any]:
    """Get learning history for a specific function.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to query.

    Returns:
        Dictionary with function-specific learning info.
    """
    events = get_learning_history(conn, function_id=function_id, limit=50)

    if not events:
        return {
            "function_id": function_id,
            "total_events": 0,
            "patterns_detected": [],
            "total_confidence_boost": 0.0,
            "invariants_added": 0,
            "invariants_validated": 0,
        }

    patterns = set()
    total_boost = 0.0
    invariants_added = 0
    invariants_validated = 0

    for event in events:
        if event.pattern_type:
            patterns.add(event.pattern_type.value)
        total_boost += event.confidence_boost
        invariants_added += event.new_invariants_added
        invariants_validated += event.invariants_validated

    return {
        "function_id": function_id,
        "total_events": len(events),
        "patterns_detected": list(patterns),
        "total_confidence_boost": total_boost,
        "invariants_added": invariants_added,
        "invariants_validated": invariants_validated,
        "recent_events": [e.to_dict() for e in events[:5]],
    }


def export_learning_report(
    conn: duckdb.DuckDBPyConnection,
    format: str = "json",
) -> str:
    """Export learning history as a report.

    Args:
        conn: DuckDB connection.
        format: Export format ("json" or "markdown").

    Returns:
        Report as string.
    """
    import json

    stats = get_learning_stats(conn)
    events = get_learning_history(conn, limit=1000)

    if format == "json":
        return json.dumps({
            "stats": stats,
            "events": [e.to_dict() for e in events],
        }, indent=2, default=str)

    # Markdown format
    lines = [
        "# DrSpec Learning Report",
        "",
        "## Summary",
        "",
        f"- **Total learning events:** {stats['total_events']}",
        f"- **Unique commits analyzed:** {stats['unique_commits']}",
        f"- **Functions affected:** {stats['unique_functions']}",
        f"- **Contracts modified:** {stats['contracts_modified']}",
        f"- **Invariants added:** {stats['total_invariants_added']}",
        f"- **Invariants validated:** {stats['total_invariants_validated']}",
        f"- **Average confidence boost:** {stats['avg_confidence_boost']:.2%}",
        "",
        "## Pattern Distribution",
        "",
    ]

    for pattern, count in stats["pattern_distribution"].items():
        lines.append(f"- {pattern}: {count}")

    lines.extend([
        "",
        "## Recent Activity",
        "",
    ])

    for day in stats["recent_activity"][:7]:
        lines.append(f"- {day['date']}: {day['events']} events")

    return "\n".join(lines)
