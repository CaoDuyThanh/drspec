"""Reasoning trace models and utilities.

This module provides models for storing and retrieving reasoning traces
from the Architect Council debate process (Proposer, Critic, Judge).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Optional

import duckdb


class AgentType(str, Enum):
    """Types of agents that can produce reasoning traces."""

    PROPOSER = "proposer"
    CRITIC = "critic"
    JUDGE = "judge"
    VISION_ANALYST = "vision_analyst"
    LIBRARIAN = "librarian"
    DEBUGGER = "debugger"


@dataclass
class ReasoningTrace:
    """Represents a reasoning trace from an agent.

    Attributes:
        id: Database ID (None for new traces).
        function_id: Function ID this trace is for.
        agent: Agent type that produced this trace.
        trace: Structured trace content as dictionary.
        created_at: When the trace was created.
    """

    function_id: str
    agent: str
    trace: dict[str, Any]
    id: Optional[int] = None
    created_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: tuple) -> "ReasoningTrace":
        """Create ReasoningTrace from database row.

        Args:
            row: Database row tuple (id, function_id, agent, trace_json, created_at).

        Returns:
            ReasoningTrace instance.
        """
        trace_dict = json.loads(row[3]) if isinstance(row[3], str) else row[3]
        return cls(
            id=row[0],
            function_id=row[1],
            agent=row[2],
            trace=trace_dict,
            created_at=row[4],
        )

    def to_json(self) -> str:
        """Convert trace dict to JSON string.

        Returns:
            JSON string representation of trace.
        """
        return json.dumps(self.trace)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API responses.

        Returns:
            Dictionary representation.
        """
        return {
            "id": self.id,
            "function_id": self.function_id,
            "agent": self.agent,
            "trace": self.trace,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


def store_trace(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
    agent: str,
    trace: dict[str, Any],
) -> None:
    """Store a reasoning trace in the database.

    Args:
        conn: DuckDB connection.
        function_id: Function ID the trace is for.
        agent: Agent type (proposer, critic, judge, etc.).
        trace: Structured trace content.

    Raises:
        ValueError: If agent is not a valid agent type.
    """
    # Validate agent type
    valid_agents = {t.value for t in AgentType}
    if agent not in valid_agents:
        raise ValueError(f"Invalid agent type: {agent}. Must be one of {valid_agents}")

    trace_json = json.dumps(trace)
    conn.execute(
        """
        INSERT INTO reasoning_traces (function_id, agent, trace_json)
        VALUES (?, ?, ?)
        """,
        [function_id, agent, trace_json],
    )


def get_traces(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
    agent: Optional[str] = None,
    limit: int = 100,
) -> list[ReasoningTrace]:
    """Get reasoning traces for a function.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to get traces for.
        agent: Optional agent type filter.
        limit: Maximum number of traces to return.

    Returns:
        List of ReasoningTrace objects, ordered by created_at descending.
    """
    if agent:
        result = conn.execute(
            """
            SELECT id, function_id, agent, trace_json, created_at
            FROM reasoning_traces
            WHERE function_id = ? AND agent = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            [function_id, agent, limit],
        ).fetchall()
    else:
        result = conn.execute(
            """
            SELECT id, function_id, agent, trace_json, created_at
            FROM reasoning_traces
            WHERE function_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            [function_id, limit],
        ).fetchall()

    return [ReasoningTrace.from_row(row) for row in result]


def get_latest_trace(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
    agent: Optional[str] = None,
) -> Optional[ReasoningTrace]:
    """Get the most recent reasoning trace for a function.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to get trace for.
        agent: Optional agent type filter.

    Returns:
        Most recent ReasoningTrace or None if none found.
    """
    traces = get_traces(conn, function_id, agent, limit=1)
    return traces[0] if traces else None


def count_traces(
    conn: duckdb.DuckDBPyConnection,
    function_id: Optional[str] = None,
    agent: Optional[str] = None,
) -> int:
    """Count reasoning traces.

    Args:
        conn: DuckDB connection.
        function_id: Optional function ID filter.
        agent: Optional agent type filter.

    Returns:
        Number of traces matching filters.
    """
    query = "SELECT COUNT(*) FROM reasoning_traces WHERE 1=1"
    params: list[Any] = []

    if function_id:
        query += " AND function_id = ?"
        params.append(function_id)

    if agent:
        query += " AND agent = ?"
        params.append(agent)

    result = conn.execute(query, params).fetchone()
    return result[0] if result else 0


def delete_traces(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
    agent: Optional[str] = None,
) -> int:
    """Delete reasoning traces for a function.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to delete traces for.
        agent: Optional agent type filter.

    Returns:
        Number of traces deleted.
    """
    count = count_traces(conn, function_id, agent)

    if agent:
        conn.execute(
            "DELETE FROM reasoning_traces WHERE function_id = ? AND agent = ?",
            [function_id, agent],
        )
    else:
        conn.execute(
            "DELETE FROM reasoning_traces WHERE function_id = ?",
            [function_id],
        )

    return count
