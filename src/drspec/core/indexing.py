"""What `drspec init` does about indexing: start the worker, so the background index begins at once, and optionally run a
waiting scan of the whole project (`init --scan`)."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

#: How long `init` waits to see the worker it started before it says it could not.
START_WAIT_SECONDS = 5.0

ALREADY_RUNNING_MESSAGE = "indexing is already running in the background; 'drspec scan' finishes it at full speed"
BACKGROUND_MESSAGE = (
    "indexing started in the background (about 20% of the machine); 'drspec scan' finishes it at full speed"
)


def start_indexing(db_path: Path, wait_seconds: float = START_WAIT_SECONDS) -> Dict[str, Any]:
    """Start the worker for `db_path` (it finds every file of the project by itself and indexes them at its own pace) and
    say what happened: `{started, message}` and, when it could not be started, a `warning`. Never raises: a project
    that is initialised but not indexed yet is not a failed `init`."""
    from drspec.staleness_worker import _DISABLE_ENV_VAR, is_worker_running, spawn_worker_if_not_running

    if os.environ.get(_DISABLE_ENV_VAR):
        return {"started": False, "message": f"indexing not started ({_DISABLE_ENV_VAR} is set)"}
    if is_worker_running(db_path) is not None:
        return {"started": True, "already_running": True, "message": ALREADY_RUNNING_MESSAGE}
    try:
        spawn_worker_if_not_running(db_path)
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if is_worker_running(db_path) is not None:
                return {"started": True, "message": BACKGROUND_MESSAGE}
            time.sleep(0.1)
    except Exception as e:  # noqa: BLE001 - reported, not raised
        return {"started": False, "warning": f"could not start the indexing worker: {e}. Run 'drspec scan' to start it."}
    return {
        "started": False,
        "warning": f"the indexing worker did not appear within {wait_seconds:.0f} s. Run 'drspec scan' to start it.",
    }


def scan_whole_project(
    db_path: Path,
    wait: bool = True,
    on_progress: Optional[Callable[[dict], None]] = None,
    max_wait: Optional[float] = None,
) -> Dict[str, Any]:
    """The summary of a scan of every file of the project, run as `drspec scan` runs it (the same worker call, flat out).
    `wait` false queues the files at background priority and returns at once. Raises `WorkerIPCError` when the worker
    cannot be reached."""
    from drspec.core.scan_service import new_scan_id, run_scan
    from drspec.worker_ipc import get_worker_connection

    spec = {
        "scan_id": new_scan_id(),
        "targets": [],
        "recursive": True,
        "full": False,
        "deep": False,
        "embed": False,
        "code_only": False,
        "docs_only": False,
        "wait": wait,
    }
    connection = get_worker_connection(db_path)
    try:
        return run_scan(connection, spec, on_progress=on_progress, max_wait=max_wait)
    finally:
        connection.close()


def scan_message(summary: Dict[str, Any]) -> str:
    """One line for the result of `init --scan`."""
    state = summary.get("state")
    if state == "queued":
        return f"scan queued: {summary.get('files_queued', 0)} files, read in the background"
    if state == "cancelled":
        done = summary.get("files_done", 0)
        return f"scan cancelled after {done} file(s) were indexed; the project stays initialised"
    if state == "still_queued":
        return "scan still running in the background; 'drspec scan' waits for it"
    done = summary.get("files_done", 0) + summary.get("files_unchanged", 0)
    return f"scan finished: {done} files, {summary.get('functions_found', 0)} functions"
