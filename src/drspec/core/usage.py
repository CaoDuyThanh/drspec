"""Recording which graph items clients ask about.

Search and lookup results leave a trace in `usage_stats`; the worker folds
it into a decayed score that picks what is worth embedding. Recording is
best effort: one batched write, errors swallowed, skipped on a read-only
connection, so a read never slows or fails because of it.
"""

from __future__ import annotations

from typing import Any, Iterable

import duckdb

from drspec.core.settings import get_setting
from drspec.db.queries import add_usage_pending

KIND_NODE = "node"
KIND_EDGE = "edge"
KIND_REFERENCE = "reference"

# A single call records at most this many items, so a wide result can't
# turn into a large write.
MAX_ITEMS_PER_CALL = 200


def record_usage(
    conn: duckdb.DuckDBPyConnection,
    items: Iterable[tuple[str, str, str]],
    setting: str = "usage.search_weight",
) -> int:
    """Add the `usage.*` weight named by `setting` to each `(kind, item_id,
    related_id)`. Returns how many items were recorded."""
    batch = list(dict.fromkeys(items))[:MAX_ITEMS_PER_CALL]
    if not batch or getattr(conn, "read_only", False):
        return 0
    try:
        weight = float(get_setting(conn, setting))
        if weight <= 0:
            return 0
        add_usage_pending(conn, [(kind, item_id, related_id, weight) for kind, item_id, related_id in batch])
    except Exception:  # noqa: BLE001 - a read must never fail because bookkeeping did
        return 0
    return len(batch)


def nodes(function_ids: Iterable[str]) -> list[tuple[str, str, str]]:
    return [(KIND_NODE, function_id, "") for function_id in function_ids]


def dependency_items(
    function_id: str, callees: list[dict[str, Any]], callers: list[dict[str, Any]]
) -> list[tuple[str, str, str]]:
    """The function itself, every function listed around it, and the direct
    edges or references joining them to it."""
    items = nodes([function_id] + [entry["function_id"] for entry in callees + callers])
    for entry in callees:
        if entry["depth"] == 1:
            kind = KIND_REFERENCE if entry["relation"] == "references" else KIND_EDGE
            items.append((kind, function_id, entry["function_id"]))
    for entry in callers:
        if entry["depth"] == 1:
            kind = KIND_REFERENCE if entry["relation"] == "references" else KIND_EDGE
            items.append((kind, entry["function_id"], function_id))
    return items
