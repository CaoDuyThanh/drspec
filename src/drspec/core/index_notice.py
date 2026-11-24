"""Saying so when the index is not complete yet.

While the worker is still scanning a project it found, a search can miss a
function simply because its file has not been reached. The notice makes that
visible instead of looking like "no match".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from drspec.core.identity import split_function_id
from drspec.core.staleness import queue_files_for_rescan
from drspec.db.queries import index_completeness, list_scanned_file_paths

NOTE = "Index incomplete: {pending} of {total} files are not scanned yet; results may be missing. Run 'drspec scan <path>' to scan now."
#: What a line search says about the files it read that are not in the graph yet (`unscanned_files.hint`).
UNSCANNED_HINT = "Searched, but not in the graph yet, so hits in them are unlabelled. Run 'drspec scan' to add them."
#: What the MCP tool `unused` says about its list (`caveat`).
UNUSED_CAVEAT = (
    "Includes both functions with no caller/reference at all, and ones whose "
    "*only* caller/reference is itself a test (design.md §23 — see "
    "'only_test_callers' per function) — a dead-code candidate list, not proof "
    "either way. CLI entry points and anything reflection/framework-invoked are "
    "not excluded automatically (only dunder/magic methods are); use path_prefix "
    "to exclude a known entry-point directory."
)


def add_index_notice(conn: Any, data: dict) -> dict:
    """Add `index: {pending, total}` and a sentence to `note` when files are waiting for their first scan.
    Nothing is added when none are, and a failure to count never fails the command."""
    try:
        status = index_completeness(conn)
    except Exception:  # noqa: BLE001 - a notice must never fail a command
        return data
    if status["pending"]:
        data["index"] = status
        sentence = NOTE.format(**status)
        data["note"] = f"{data['note']} {sentence}" if data.get("note") else sentence
    return data


def queue_if_unindexed(conn: Any, function_id: str) -> None:
    """A command was given a function that is not stored. If its file exists, is a language drspec reads and
    has not been scanned, queue it ahead of the rest, so that asking again soon finds it."""
    try:
        if conn.execute("SELECT 1 FROM artifacts WHERE function_id = ?", [function_id]).fetchone():
            return
        file_path = split_function_id(function_id)[0]
        if not file_path:
            return
        from drspec.core.scanner import Scanner

        path = Path.cwd() / file_path
        if not path.is_file() or not Scanner().is_supported(path):
            return
        if file_path in list_scanned_file_paths(conn):
            return
        queue_files_for_rescan(conn, [(file_path, "search_unscanned")])
    except Exception:  # noqa: BLE001 - best effort
        return
