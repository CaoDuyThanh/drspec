"""How big the database is, how much of the file is free space, and what the stored text is made of (for `status`)."""

from __future__ import annotations

from typing import Any, Dict, List

from drspec.db.compaction import FREE_SHARE, MIN_BYTES, storage_of

#: One query for the parts: `name, rows, bytes of text` each (the length of the stored text, not of the file blocks).
_PARTS_SQL = """
SELECT 'functions', COUNT(*), COALESCE(SUM(length(function_id) + length(file_path) + length(function_name) + length(signature) + length(body)), 0) FROM artifacts
UNION ALL
SELECT 'per-file facts', COUNT(*), COALESCE(SUM(length(value)), 0) FROM config WHERE starts_with(key, 'scan:file:')
UNION ALL
SELECT 'documents', COUNT(*), COALESCE(SUM(length(text) + length(heading_path) + COALESCE(length(keywords), 0) + COALESCE(length(mentions), 0) + COALESCE(length(code_blocks), 0)), 0) FROM doc_sections
UNION ALL
SELECT 'call edges', COUNT(*), COALESCE(SUM(length(caller_id) + length(callee_id) + COALESCE(length(call_site), 0) + COALESCE(length(edge_hash), 0)), 0) FROM dependencies
UNION ALL
SELECT 'other settings and state', COUNT(*), COALESCE(SUM(length(key) + length(value)), 0) FROM config WHERE NOT starts_with(key, 'scan:file:')
"""


def database_report(conn: Any) -> Dict[str, Any]:
    """`{file_mb, used_mb, free_mb, free_percent, largest: [{name, rows, mb}], note?}`, or `{}` when the size cannot be read."""
    try:
        info = storage_of(conn)
    except Exception:  # noqa: BLE001 - a status call never fails over a missing figure
        return {}
    parts: List[Dict[str, Any]] = []
    try:
        found = conn.execute(_PARTS_SQL).fetchall()
    except Exception:  # noqa: BLE001 - a table that does not exist yet
        found = []
    for name, rows, size in found:
        if rows:
            parts.append({"name": name, "rows": int(rows), "mb": round(int(size) / 1e6, 1)})
    parts.sort(key=lambda part: -part["mb"])
    report: Dict[str, Any] = {key: info[key] for key in ("file_mb", "used_mb", "free_mb", "free_percent")}
    report["largest"] = parts[:3]
    if info["bytes"] >= MIN_BYTES and info["free_share"] > FREE_SHARE:
        report["note"] = (
            f"{info['free_percent']}% of the file is free space; the worker gives it back when it has been idle for "
            "worker.compact_idle_seconds (10 minutes by default)."
        )
    return report
