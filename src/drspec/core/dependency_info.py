"""Caller/callee lookup shared by `drspec deps get` and the MCP server's
`deps_get` tool, so both reuse the exact same BFS rather than maintaining
two separate copies of it.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from drspec.core.identity import display_name
from drspec.db import (
    get_artifact,
    get_callees,
    get_callers,
    get_reference_callees,
    get_reference_callers,
)
from drspec.core.staleness import stale_paths_checked


def _callee_relations(conn: Any, function_id: str) -> list[tuple[str, str]]:
    """Every function `function_id` depends on, tagged by relation type —
    `calls` and `references` (currently just Python `@property` reads).
    Combined by default, since "zero callers" from `calls` alone can
    misleadingly read as "unused" for something with real reference-only
    dependents.
    """
    return [(cid, "calls") for cid in get_callees(conn, function_id)] + [
        (cid, "references") for cid in get_reference_callees(conn, function_id)
    ]


def _caller_relations(conn: Any, function_id: str) -> list[tuple[str, str]]:
    """Every function that depends on `function_id`, tagged by relation
    type. See `_callee_relations`."""
    return [(cid, "calls") for cid in get_callers(conn, function_id)] + [
        (cid, "references") for cid in get_reference_callers(conn, function_id)
    ]


def _confidence(conn: Any, caller_id: str, callee_id: str, relation: str) -> Any:
    """How sure the resolver was of the edge between two functions (`EXTRACTED`, `INFERRED` or `AMBIGUOUS`), or `None`."""
    table = "code_references" if relation == "references" else "dependencies"
    row = conn.execute(f"SELECT confidence FROM {table} WHERE caller_id = ? AND callee_id = ?", [caller_id, callee_id]).fetchone()
    return row[0] if row else None


def get_dependency_info(
    conn: Any,
    function_id: str,
    depth: int = 1,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Get callees and callers with contract status.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to get dependencies for.
        depth: Maximum depth of dependency traversal.

    Returns:
        Tuple of (callees, callers) lists with dependency info. Each entry
        includes a `"relation"` field (`"calls"` or `"references"`) — BFS
        traversal follows both relation types uniformly, since a property
        read is just as real a dependency as a call for this purpose.
    """
    callees = []
    callers = []

    visited_callees = {function_id}
    queue: deque[tuple[str, int, str, str]] = deque()  # (function, depth, relation, the function it was reached from)

    for callee_id, relation in _callee_relations(conn, function_id):
        if callee_id not in visited_callees:
            visited_callees.add(callee_id)
            queue.append((callee_id, 1, relation, function_id))

    while queue:
        current_id, current_depth, relation, reached_from = queue.popleft()

        artifact = get_artifact(conn, current_id)

        contract_result = conn.execute(
            "SELECT function_id FROM contracts WHERE function_id = ?",
            [current_id],
        ).fetchone()
        has_contract = contract_result is not None

        callee_info = {
            "function_id": current_id,
            "function_name": artifact.function_name if artifact else display_name(current_id),
            "file_path": artifact.file_path if artifact else "",
            "depth": current_depth,
            "relation": relation,
            "confidence": _confidence(conn, reached_from, current_id, relation),
            "has_contract": has_contract,
            "status": artifact.status if artifact else "UNKNOWN",
        }
        callees.append(callee_info)

        if current_depth < depth:
            for next_callee, next_relation in _callee_relations(conn, current_id):
                if next_callee not in visited_callees:
                    visited_callees.add(next_callee)
                    queue.append((next_callee, current_depth + 1, next_relation, current_id))

    visited_callers = {function_id}
    queue = deque()

    for caller_id, relation in _caller_relations(conn, function_id):
        if caller_id not in visited_callers:
            visited_callers.add(caller_id)
            queue.append((caller_id, 1, relation, function_id))

    while queue:
        current_id, current_depth, relation, reached_from = queue.popleft()

        artifact = get_artifact(conn, current_id)

        contract_result = conn.execute(
            "SELECT function_id FROM contracts WHERE function_id = ?",
            [current_id],
        ).fetchone()
        has_contract = contract_result is not None

        caller_info = {
            "function_id": current_id,
            "function_name": artifact.function_name if artifact else display_name(current_id),
            "file_path": artifact.file_path if artifact else "",
            "depth": current_depth,
            "relation": relation,
            "confidence": _confidence(conn, current_id, reached_from, relation),
            "has_contract": has_contract,
            "status": artifact.status if artifact else "UNKNOWN",
        }
        callers.append(caller_info)

        if current_depth < depth:
            for next_caller, next_relation in _caller_relations(conn, current_id):
                if next_caller not in visited_callers:
                    visited_callers.add(next_caller)
                    queue.append((next_caller, current_depth + 1, next_relation, current_id))

    callees.sort(key=lambda x: (x["depth"], x["function_id"]))
    callers.sort(key=lambda x: (x["depth"], x["function_id"]))

    # Staleness reflects whatever the background worker already knows, not a live recheck here.
    stale_paths = stale_paths_checked(conn, [e["file_path"] for e in callees + callers])
    for entry in callees + callers:
        entry["possibly_stale"] = entry["file_path"] in stale_paths

    return callees, callers


def find_similar_functions(conn: Any, function_id: str, limit: int = 5) -> list[str]:
    """Function ids to suggest for one that was not found: the functions with the same last name (the ones of the file named first, so
    an id that only lacks its namespace, `file::Class::name` for `file::ns::Class::name`, finds it), then names that contain it or
    are contained in it, then the other functions of the file. Asked of the whole index, not of its first thousand functions.

    Args:
        conn: DuckDB connection.
        function_id: Function ID that wasn't found.
        limit: Maximum number of suggestions.

    Returns:
        List of similar function IDs.
    """
    name = display_name(function_id)
    file_part = function_id.partition("::")[0] if "::" in function_id else ""
    found: list[str] = []

    def add(rows: list) -> None:
        for (candidate,) in rows:
            if candidate not in found and len(found) < limit:
                found.append(candidate)

    add(conn.execute("SELECT function_id FROM artifacts WHERE function_name = ? ORDER BY (file_path = ?) DESC, function_id LIMIT ?", [name, file_part, limit]).fetchall())
    if len(found) < limit:
        add(conn.execute(
            "SELECT function_id FROM artifacts WHERE contains(lower(function_name), lower(?)) OR (length(function_name) >= 3 AND contains(lower(?), lower(function_name))) "
            "ORDER BY (file_path = ?) DESC, function_id LIMIT ?",
            [name, name, file_part, limit * 3],
        ).fetchall())
    if len(found) < limit and file_part:
        add(conn.execute("SELECT function_id FROM artifacts WHERE file_path = ? ORDER BY function_id LIMIT ?", [file_part, limit]).fetchall())
    return found
