"""What background maintenance still has to do, for `status`."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

from drspec.core.embedding_backlog import embedding_status
from drspec.core.settings import read_all_settings
from drspec.db.queries import get_rescan_backlog, list_unreadable_files, pending_removal_count


def project_folder(db_path: Any) -> Path:
    """The project folder of a database (where the queued files are relative to), or the current folder."""
    from drspec.core.discovery import project_root_for

    return project_root_for(Path(db_path)) or Path.cwd()


def maintenance_backlog(conn: Any, now: Optional[float] = None, root: Optional[Path] = None) -> dict[str, Any]:
    """Files waiting to be re-scanned or discovered, files that cannot be read as they are, and embedding progress. `root` is the
    project folder (where the files are checked for an edit)."""
    now = time.time() if now is None else now
    backlog = {**get_rescan_backlog(conn, now, root), "embedding": embedding_status(conn, read_all_settings(conn), now)}
    if backlog.get("files_unreadable"):
        backlog["unreadable"] = list_unreadable_files(conn, root)  # the files and their last error (`status --details`)
    pending = pending_removal_count(conn)
    if pending:
        backlog["removals_pending"] = pending  # functions recorded as gone whose rows are still being deleted
    return backlog
