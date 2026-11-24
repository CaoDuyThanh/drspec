"""Publishing a scan: one transaction, then the deletions it recorded; and clearing what an interrupted rebuild left."""

from __future__ import annotations

import os
import re
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

from drspec.core import tracing
from drspec.db.queries import complete_pending_removals

STAGING_PREFIX = ".drspec-scan-"
_OWNER_PID = re.compile(r"^" + re.escape(STAGING_PREFIX) + r"(\d+)-")

# A folder with no owner PID in its name predates the naming scheme, so only
# age can say whether a publish is still using it.
UNOWNED_MAX_AGE_SECONDS = 3600.0


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def sweep_orphaned_staging(directory: Path, now: float | None = None) -> int:
    """Delete staging folders a publish left behind when its process was
    killed: each is a full copy of the database. A folder whose owner is
    still running is kept. Returns how many were removed."""
    now = time.time() if now is None else now
    removed = 0
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.startswith(STAGING_PREFIX):
            continue
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
            match = _OWNER_PID.match(entry.name)
            if match:
                if _process_alive(int(match.group(1))):
                    continue
            elif now - entry.stat().st_mtime < UNOWNED_MAX_AGE_SECONDS:
                continue
            shutil.rmtree(entry.path)
            removed += 1
        except OSError:
            continue
    return removed


@contextmanager
def publish_transaction(conn):
    """Everything a scan or a re-scan writes, in one transaction on the live connection: it all commits
    together or, on any error, not at all. Functions that left the source are only recorded in it; once it
    has committed their rows are deleted (`complete_pending_removals`), and whatever a crash leaves of that is
    finished by the next start. No copy of the database file is made."""
    began = time.perf_counter()
    published = False
    conn.begin()
    try:
        yield conn
        synced = time.perf_counter()
        conn.commit()
        committed = time.perf_counter()
        published = True
    except BaseException:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001 - the failure may already have ended the transaction
            pass
        raise
    removed = complete_pending_removals(conn)
    if tracing.enabled():
        ended = time.perf_counter()
        tracing.emit(
            "rescan",
            (ended - began) * 1000.0,
            kind="publish",
            published=published,
            sync_ms=round((synced - began) * 1000.0, 3),
            commit_ms=round((committed - synced) * 1000.0, 3),
            removal_ms=round((ended - committed) * 1000.0, 3),
            functions=removed,
            **tracing.system_snapshot(),
        )
