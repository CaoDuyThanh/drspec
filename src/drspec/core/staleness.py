"""Read-time freshness checks, and best-effort queueing of what they find.

Search reads the index, which can be behind the files on disk. The
background worker only notices on its next sweep, so a search checks the
files it is about to report itself, then asks the worker to re-scan them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import duckdb

from drspec.db.queries import enqueue_staleness_many, scanned_file_snapshots, stale_file_paths_among

# A search queues at most this many files per call, so a wide search can't
# flood the queue.
MAX_QUEUED_PER_CALL = 20


def drifted_file_paths(conn: duckdb.DuckDBPyConnection, file_paths: list[str], root: Path) -> set[str]:
    """Files whose size or mtime no longer matches the last scan, or that
    are gone. A file with no scan snapshot can't be judged and is left out."""
    drifted: set[str] = set()
    for file_path, (mtime, size) in scanned_file_snapshots(conn, file_paths).items():
        try:
            stat = (root / file_path).stat()
        except OSError:
            drifted.add(file_path)
            continue
        if stat.st_size != size or stat.st_mtime != mtime:
            drifted.add(file_path)
    return drifted


def queue_files_for_rescan(
    conn: duckdb.DuckDBPyConnection,
    items: list[tuple[str, str]],
    limit: int = MAX_QUEUED_PER_CALL,
) -> int:
    """Ask the worker to re-scan `(file_path, reason)` pairs. Best effort:
    one batched write, errors swallowed, never slows or fails the search.
    Skipped on a genuinely read-only connection. Returns how many were queued."""
    items = items[:limit]
    if not items or getattr(conn, "read_only", False):
        return 0
    try:
        enqueue_staleness_many(conn, items, overwrite=True)
    except Exception:  # noqa: BLE001 - a search must never fail because queueing did
        return 0
    return len(items)


def stale_paths_checked(
    conn: duckdb.DuckDBPyConnection,
    file_paths: list[str],
    root: Optional[Path] = None,
) -> set[str]:
    """Which of `file_paths` are possibly stale right now: flagged by the
    worker, or drifted on disk since the last scan (checked here, so an
    edit is visible before the worker's next sweep). Drifted files are also
    queued for the worker to re-scan."""
    paths = sorted(set(file_paths))
    if not paths:
        return set()
    flagged = stale_file_paths_among(conn, paths)
    drifted = drifted_file_paths(conn, paths, root or Path.cwd())
    queue_files_for_rescan(conn, [(path, "search_drift") for path in sorted(drifted)])
    return flagged | drifted
