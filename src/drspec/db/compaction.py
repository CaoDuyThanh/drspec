"""Giving back the free space of the database file.

DuckDB does not return the blocks a rewrite frees: after a project has been indexed a few times, a third or more of the
file can be blocks no table uses. They are reused by the next writes, so the file stops growing, but it stays as large as
its peak. When more than `FREE_SHARE` of the blocks are free (and the file is worth the trouble), the running worker
rebuilds the file once it has been idle for a while (`WorkerIPCServer.compact_if_needed`: writers wait, reads are
answered as during a publish, the connection is swapped in a moment), and never when it starts, so that a start does not
wait for it. `ensure_compact` does the same for a file nobody has open, with the machinery the layout migrations use:
a new file is written from the old one, checked, and swapped in with one rename, and any failure leaves the old file as
it was.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict

import duckdb

from drspec.db.migration import MigrationError, copy_database, fsync_file, rebuild_database

#: Rebuild when more than this share of the blocks is free.
FREE_SHARE = 0.30
#: ... and the file is at least this large (a small file is not worth a rebuild).
MIN_BYTES = 32 * 1024 * 1024
BACKUP_SUFFIX = ".before-compaction"


def backup_path(db_path: Path) -> Path:
    return Path(db_path).with_name(Path(db_path).name + BACKUP_SUFFIX)


def storage_of(conn: Any) -> Dict[str, Any]:
    """The size of the database file and how much of it no table uses, from `PRAGMA database_size`."""
    row = conn.execute("PRAGMA database_size").fetchone()
    # database_name, database_size, block_size, total_blocks, used_blocks, free_blocks, wal_size, ...
    block_size, total, used, free = int(row[2]), int(row[3]), int(row[4]), int(row[5])
    return {
        "file_mb": round(total * block_size / 1e6, 1),
        "used_mb": round(used * block_size / 1e6, 1),
        "free_mb": round(free * block_size / 1e6, 1),
        "free_percent": round(100.0 * free / total, 1) if total else 0.0,
        "bytes": total * block_size,
        "free_share": (free / total) if total else 0.0,
    }


def needs_compaction(conn: Any, free_share: float = FREE_SHARE, min_bytes: int = MIN_BYTES) -> bool:
    info = storage_of(conn)
    return info["bytes"] >= min_bytes and info["free_share"] > free_share


def ensure_compact(db_path: Path, free_share: float = FREE_SHARE, min_bytes: int = MIN_BYTES) -> str:
    """Rebuild `db_path` if too much of it is free space. Call before anything else opens the file. Returns `absent`,
    `compact` or `compacted`; raises `MigrationError` if the rebuild failed (the file is then unchanged)."""
    db_path = Path(db_path)
    if not db_path.exists():
        return "absent"
    try:
        conn = duckdb.connect(str(db_path))
    except duckdb.Error as e:
        raise MigrationError(f"Could not open {db_path} to check its free space: {e}") from e
    try:
        if not needs_compaction(conn, free_share, min_bytes):
            return "compact"
    finally:
        conn.close()
    rebuild_database(db_path, backup_path(db_path), _build, "compaction")
    for path in (backup_path(db_path), Path(str(backup_path(db_path)) + ".wal")):
        try:
            path.unlink()  # the rebuild only copies the rows, so the old file has nothing the new one lacks
        except OSError:
            pass
    return "compacted"


def build_replacement(db_path: Path, work: Path) -> Path:
    """A compact copy of the database at `db_path`, made in the folder `work`, for a process that holds the file open
    and keeps writers out meanwhile: the file is checkpointed first (by the caller), so the file alone is the whole
    database. Returns the new file; the caller swaps it in."""
    source, target = work / "old.db", work / "contracts.db"
    shutil.copy2(db_path, source)
    _build(source, target)
    fsync_file(target)
    return target


def _build(source: Path, target: Path) -> None:
    def verify(check: Any) -> None:
        left = storage_of(check)["free_share"]
        if left > 0.9:
            raise MigrationError("The rebuilt database is mostly empty space.")

    copy_database(source, target, lambda table, wanted, present: None, lambda conn: None, verify)
