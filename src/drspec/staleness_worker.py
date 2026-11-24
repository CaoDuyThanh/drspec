"""Background staleness-resolution worker.

Catches drift between stored `scanned_files` snapshots and the real
filesystem asynchronously, off the query path that `search`/
`deps_get`/`unused` use, so `drspec scan` itself stays unaffected
(it only gains one free side effect: recording each scanned file's
mtime/size). Detection diffs `os.stat()` against those stored values,
no parsing; remediation re-runs the same `sync_scan_results` machinery
`drspec scan` uses, scoped to just the drifted files and batched into
one publish per pass.

Holds exactly one connection for its entire run, reached only through
the same IPC path (`worker_ipc.py`) external clients use — an earlier
version opened a fresh connection per pass, which turned out to still
collide with other commands' held-open read-only connections.

Singleton enforcement is a PID file (`_drspec/staleness_worker.pid`) —
not race-proof against a crash that leaves it behind, deliberately
kept simple rather than pre-emptively bulletproof.

Never exits while idle, since every other command now depends on it
being reachable. `idle_timeout` exists only so tests can make the loop
terminate deterministically without a real `SIGTERM`.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional, Tuple, Union

import duckdb

from drspec.core import tracing
from drspec.core.config_keys import ParsedConfig, is_config_path, publish_configs, read_config_file
from drspec.core.docs import count_to_review, extensions, is_document, publish_documents, readable_text
from drspec.core.scan_service import batch_budget, begin_batch, begin_publish, end_batch, note_read, fail_batch, is_cancelling, load_run, record_batch, request_flags, save_run, scanning_now, stats_dict
from drspec.core.discovery import discover_new_files, project_root_for
from drspec.core.embedding_backlog import embedding_status
from drspec.core.graph_sync import sync_scan_results
from drspec.core.scanner import FileImports, ScannedFunction, Scanner
from drspec.core.settings import SETTINGS, read_all_settings
from drspec.db.connection import get_connection, init_schema
from drspec.core.facts_version import requeue_for_new_facts
from drspec.core.parser_version import requeue_outdated_partial_files
from drspec.core.partial import forget_unknown_files
from drspec.db.fk_migration import ensure_fk_layout
from drspec.db.compaction import FREE_SHARE, MIN_BYTES
from drspec.db.identity_migration import ensure_identity_layout
from drspec.db.migration import MigrationError, ensure_embedding_layout
from drspec.db.queries import (
    set_config,
    enqueue_staleness_many,
    get_rescan_backlog,
    list_scanned_files,
    list_staleness_queue,
    next_rescan_batch,
    record_rescan_failure,
    settle_scanned_files,
    upsert_scanned_file,
)
from drspec.db.staging import publish_transaction, sweep_orphaned_staging
from drspec.core.build_id import build_id
from drspec.core.embedding_runner import available_memory_mb, pause_reason
from concurrent.futures.process import BrokenProcessPool
from drspec.core.parse_pool import close_if_idle, close_pool, cpu_seconds as parser_cpu_seconds, get_pool as get_parse_pool, parser_plan, PARSERS_KEY
from drspec.core.partial import record_partial_files, record_skipped_constructs
from drspec.embedding_supervisor import EmbeddingSupervisor
from drspec.worker_ipc import (
    _DISABLE_ENV_VAR,
    WorkerConnection,
    WorkerIPCServer,
    connect_loopback,
    get_worker_connection,
    peek_worker_build,
)

logger = logging.getLogger(__name__)

#: Effectively "never" by default — a finite value is only ever passed
#: by a test that wants the loop to terminate deterministically.
IDLE_TIMEOUT_SECONDS = float("inf")
POLL_INTERVAL_SECONDS = 10

#: Stay alive while idle (above), but drop to minimal CPU usage rather
#: than polling at the fast cadence forever. After this many seconds of
#: a pass detecting and remediating nothing, back off to
#: `DORMANT_POLL_INTERVAL_SECONDS` between passes instead of
#: `POLL_INTERVAL_SECONDS`; any pass that finds real work snaps straight
#: back to the fast cadence.
DORMANT_AFTER_SECONDS = 120
DORMANT_POLL_INTERVAL_SECONDS = 300

# A connection-like object usable for `.execute()`/`.begin()`/etc — either
# a real `duckdb` connection (tests, or anything run outside the worker
# process) or a `WorkerConnection` proxy (the worker's own staleness loop,
# looped back through its own IPC socket so it shares the same lock/
# transaction discipline as every external client, rather than touching
# the real connection by some separate, untested path).
ConnectionLike = Union[duckdb.DuckDBPyConnection, WorkerConnection]


STARTUP_ERROR_NAME = "worker_startup_error.txt"


def startup_error_file(db_path: Path) -> Path:
    return Path(db_path).parent / STARTUP_ERROR_NAME


def record_startup_error(db_path: Path, message: str) -> None:
    try:
        startup_error_file(db_path).write_text(message + "\n")
    except OSError:
        pass


def clear_startup_error(db_path: Path) -> None:
    try:
        startup_error_file(db_path).unlink()
    except OSError:
        pass


def read_startup_error(db_path: Path) -> Optional[str]:
    try:
        return startup_error_file(db_path).read_text().strip() or None
    except OSError:
        return None


def _pid_file(db_path: Path) -> Path:
    return Path(db_path).parent / "staleness_worker.pid"


def _is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else — still alive
    return True


def is_worker_running(db_path: Path) -> Optional[int]:
    """The running worker's PID, or `None` if there isn't one (no PID
    file, or its process is no longer alive)."""
    pid_path = _pid_file(db_path)
    if not pid_path.exists():
        return None
    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError):
        return None
    return pid if _is_process_alive(pid) else None


def acquire_singleton(db_path: Path) -> bool:
    """`True` if this process may proceed as *the* worker (it just
    staked the PID file to its own PID); `False` if another live
    worker already holds it.

    Writes the PID to a per-process temp file first, then `os.link()`s
    that file into place — not `os.open(O_CREAT | O_EXCL)` followed by
    a separate `os.write()`: that leaves a window where the file exists
    but is still empty, so a concurrent `is_worker_running()` can read
    it as "no PID here" and reclaim it out from under the process still
    writing it, letting both believe they're the sole worker. `os.link()`
    has no such window: the path never becomes visible until its content
    is already complete.
    """
    pid_path = _pid_file(db_path)
    # `threading.get_ident()` alongside the PID: this process only ever
    # calls this once from its main thread in practice, but the suffix
    # only needs to be *unique enough not to collide*, and PID alone
    # collides across threads of the same process (same `os.getpid()`)
    # — exactly what a deterministic same-process concurrency test for
    # this function needs to simulate two distinct callers safely.
    temp_path = pid_path.with_name(f"{pid_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temp_path.write_text(str(os.getpid()))
    try:
        try:
            os.link(str(temp_path), str(pid_path))
        except FileExistsError:
            if is_worker_running(db_path) is not None:
                return False
            # Stale PID file from a crashed worker — reclaim it. A second
            # process doing the same thing at the same instant could still
            # both reach here; the next link() decides the real winner,
            # atomically (one of the two `os.link` calls below necessarily
            # raises `FileExistsError` — there is no window where both
            # can succeed, unlike the old create-then-write approach).
            try:
                pid_path.unlink()
                os.link(str(temp_path), str(pid_path))
            except OSError:
                return False
        return True
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def release_singleton(db_path: Path) -> None:
    try:
        _pid_file(db_path).unlink()
    except FileNotFoundError:
        pass


def _detect_drift(conn: ConnectionLike) -> list[tuple[str, str]]:
    """`[(file_path, reason), ...]` for every scanned file whose
    on-disk mtime/size no longer matches its stored snapshot, or that
    has vanished entirely. `conn` is the worker's one long-lived
    connection — see module docstring for why it's not opened/closed
    per call.
    """
    snapshots = list_scanned_files(conn)

    drifted: list[tuple[str, str]] = []
    for file_path, mtime, size in snapshots:
        path = Path(file_path)
        if not path.exists():
            drifted.append((file_path, "missing"))
            continue
        try:
            file_stat = path.stat()
        except OSError:
            continue
        if file_stat.st_size != size:
            drifted.append((file_path, "size_changed"))
        elif file_stat.st_mtime != mtime:
            drifted.append((file_path, "mtime_changed"))
    return drifted


def _describe_failure(result) -> str:
    if result is None:
        return "scan returned no result"
    if result.errors:
        first = result.errors[0]
        where = f" (line {first.line})" if first.line else ""
        return f"{first.message}{where}"
    return f"parse status: {result.parse_status}"


def _remediate_queued_files(conn: duckdb.DuckDBPyConnection, db_path: Path, file_paths: list[str], pass_seconds: Optional[float] = None) -> int:
    """Re-scan every file in `file_paths` for real and publish the
    result through the same `sync_scan_results`/`publish_transaction` path
    every scan uses: one batched publish for the whole list.

    Returns how many files were resolved. A file that fails to parse stays
    queued and is parked (`rescan_backoff`): it is not read again until it is
    edited, so it can't hold the front of the queue or be retried on every pass.

    With `pass_seconds`, parsing stops once that much time has gone (after at
    least one file) and only the files parsed so far are published; the rest
    stay queued. This keeps the time the worker is busy, and so the time a
    waiting request waits, bounded however long the queue is.

    Files a `drspec scan` asked for carry that scan's options (deep resolution,
    embeddings, a full re-resolve); they are published together by option, and
    what the publish did is added to the scan's record (`core.scan_service`).
    """
    scanner = Scanner()
    started_at = time.perf_counter()
    functions: list[ScannedFunction] = []
    file_imports: dict[str, FileImports] = {}
    resolved_paths: set[str] = set()
    gone_paths: set[str] = set()  # files that no longer exist
    partial_files: dict[str, dict] = {}
    skipped_by_file: dict[str, int] = {}
    failures: list[tuple[str, str, Optional[float], Optional[int]]] = []
    doc_settings = read_all_settings(conn)
    documents: dict[str, str] = {}  # document files to (re)index
    gone_documents: set[str] = set()  # document files that are gone, or not to be indexed
    configs: dict[str, ParsedConfig] = {}  # YAML config files to (re)read: their keys replace the stored ones
    gone_configs: set[str] = set()  # YAML config files that are gone, or not to be read (`strings.enabled` off)
    config_on = bool(doc_settings.get("strings.enabled", True))
    config_max_kb = int(doc_settings.get("strings.max_file_kb", 1024))
    requests = request_flags(conn, file_paths)  # the options of the scan that asked for each file, if one did
    begin_batch(requests, file_paths)  # a scan that waits sees how far the reading has come (`scan_progress`)
    file_errors: dict[str, list[dict]] = {}
    function_counts: dict[str, int] = {}
    declaration_counts: dict[str, int] = {}

    documents_off = not doc_settings.get("docs.enabled", False)
    document_suffixes = extensions(doc_settings)
    settle_root = project_root_for(db_path) or Path.cwd()  # the folder the queued paths are relative to (the worker's own)
    pool = _parser_pool(conn, doc_settings, file_paths)
    parsed: dict[str, Any] = {}  # results the pool produced ahead of the loop, by path

    def parse(file_path: str, index: int):
        """One file's scan result: from the pool (which parses the next few files together) or, without one, here."""
        nonlocal pool
        if pool is not None and file_path not in parsed:
            upcoming = []
            for candidate in file_paths[index:]:
                if candidate in parsed or (documents_off and candidate.lower().endswith(document_suffixes)) or is_document(candidate, doc_settings) or is_config_path(candidate):
                    continue
                if Path(candidate).exists():
                    upcoming.append(candidate)
                if len(upcoming) >= pool.workers * 3:
                    break
            try:
                for candidate, (result, error) in zip(upcoming, pool.parse(upcoming)):
                    if result is not None:
                        parsed[candidate] = result  # a file the pool could not read is parsed again below, to report it properly
            except BrokenProcessPool:
                logger.warning("A parser process died; parsing this batch in the worker itself")
                pool = None
        if file_path in parsed:
            return parsed[file_path]
        return scanner.scan_file(str(Path(file_path)), relative_to=Path.cwd())

    parked: set[str] = set()  # queued documents while documents are off: left alone, off the queue
    for index, file_path in enumerate(file_paths):
        if pass_seconds is not None and index and time.perf_counter() - started_at >= pass_seconds:
            break
        note_read(file_path, requests)
        if file_path in requests and is_cancelling(requests[file_path][0]):
            continue  # its scan was cancelled while this batch was being read: left alone, the cancel takes it off the queue
        path = Path(file_path)
        if is_config_path(file_path):  # a YAML file: its keys are read here, not parsed as code
            if config_on and path.exists():
                configs[file_path] = read_config_file(path, config_max_kb)
            else:
                gone_configs.add(file_path)
            resolved_paths.add(file_path)  # its snapshot is written with the batch, like a code file's
            if not path.exists():
                gone_paths.add(file_path)
            continue
        if documents_off and file_path.lower().endswith(document_suffixes) and not scanner.is_supported(path):
            parked.add(file_path)
            continue
        if is_document(file_path, doc_settings):
            text = readable_text(path, doc_settings) if path.exists() else None
            if text is None:
                gone_documents.add(file_path)  # deleted, too large, unreadable or generated: nothing of it stays indexed
            else:
                documents[file_path] = text
            continue
        if not path.exists():
            resolved_paths.add(file_path)  # no fresh functions -> sync removes its old ones
            gone_paths.add(file_path)
            continue
        parse_started = time.perf_counter()
        result = parse(file_path, index)
        if tracing.enabled():
            tracing.emit(
                "rescan",
                (time.perf_counter() - parse_started) * 1000.0,
                kind="parse",
                index=index,
                ok=bool(result and result.parse_status in ("successful", "partial")),
                functions=len(result.functions) if result else 0,
            )
        if not result or result.parse_status not in ("successful", "partial"):
            try:
                stat = path.stat()
                mtime, size = stat.st_mtime, stat.st_size
            except OSError:
                mtime = size = None
            failures.append((file_path, _describe_failure(result), mtime, size))
            if result is not None and result.errors:
                file_errors[file_path] = [error._asdict() for error in result.errors]
            continue
        functions.extend(result.functions)
        function_counts[file_path] = len(result.functions)
        declaration_counts[file_path] = sum(1 for f in result.functions if "declaration" in f.decorators)
        file_imports.update(result.file_imports)
        resolved_paths.add(file_path)
        if result.errors:
            file_errors[file_path] = [error._asdict() for error in result.errors]
        if result.skipped_constructs:
            skipped_by_file[file_path] = result.skipped_constructs
        if result.parse_status == "partial":
            # Read in part: what parsed is indexed, nothing stored for the file is removed, and the file is
            # settled, so it is looked at again only when it changes.
            partial_files[file_path] = {
                "skipped": [{"name": item["name"], "line": item["line"]} for item in result.skipped_functions],
                "partial": [{"name": item["name"], "line": item["line"], "function_id": item["function_id"]} for item in result.partial_functions],
                "first_error_line": result.errors[0].line if result.errors else None,
            }

    # From here a cancel is too late (`mark_cancelling` says so): the files of a scan cancelled until now are left out, and
    # the cancel takes them off the queue.
    left_out = begin_publish(requests, file_paths)
    try:
        if left_out:
            resolved_paths -= left_out
            gone_paths -= left_out
            gone_documents -= left_out
            parked -= left_out
            documents = {path: text for path, text in documents.items() if path not in left_out}
            failures = [failure for failure in failures if failure[0] not in left_out]
        if requests and len(left_out) == len(file_paths):
            return 0  # every file of the batch belongs to a cancelled scan: nothing is published
        # A file read a moment ago that is gone now (created and deleted between the read and the publish) is a gone file: publishing
        # its functions would leave them in the index with nothing to say they are stale (no `scanned_files` row is written for it).
        vanished = {path for path in resolved_paths if path not in gone_paths and not Path(path).exists()}
        if vanished:
            gone_paths |= vanished
            functions = [f for f in functions if f.file_path not in vanished]

        # Written before the publish: the staged copy is taken from this
        # connection afterwards, so it carries these rows.
        now = time.time()
        for file_path, error, mtime, size in failures:
            record_rescan_failure(conn, file_path, error, mtime, size, now)
        _record_scan_failures(conn, failures, file_errors, requests)
        if parked:
            settle_scanned_files(conn, parked, settle_root)

        if not resolved_paths and not documents and not gone_documents and not configs and not gone_configs:
            return len(parked)

        try:
            with publish_transaction(conn) as write_conn:
                group_stats: dict[str, dict] = {}
                if resolved_paths:
                    for flags, group in _by_options(resolved_paths, requests).items():
                        deep, embed, full = flags
                        stats = sync_scan_results(
                            write_conn,
                            functions=[f for f in functions if f.file_path in group],
                            scanned_file_paths=group,
                            file_imports={path: imports for path, imports in file_imports.items() if path in group},
                            deep_scan=deep,
                            embed=embed,
                            force=full,
                            no_removal_paths=set(partial_files) & group,
                            deleted_paths=gone_paths & group,
                        )
                        owner = _owner_of(group, requests)
                        if owner is not None:
                            group_stats[owner] = stats_dict(stats)
                    record_partial_files(write_conn, resolved_paths, partial_files)
                    record_skipped_constructs(write_conn, resolved_paths, skipped_by_file)
                if configs or gone_configs:
                    publish_configs(write_conn, configs, gone_configs)  # the keys of each file, in the publish that writes its snapshot
                if documents or gone_documents:
                    publish_documents(write_conn, documents, gone_documents, doc_settings)  # after the code, so a document sees this batch's functions
                    for document_path in gone_documents:
                        try:  # a document left out on purpose is remembered, so discovery does not offer it again
                            stat = os.stat(document_path)
                            upsert_scanned_file(write_conn, document_path, stat.st_mtime, stat.st_size)
                        except OSError:
                            pass
                finished_paths = resolved_paths | set(documents) | gone_documents
                if any(path in requests for path in finished_paths):
                    _record_scan_batch(write_conn, requests, finished_paths, documents, gone_documents, function_counts, group_stats, partial_files, skipped_by_file, file_errors, declaration_counts)
                settle_scanned_files(write_conn, finished_paths, settle_root)

        except Exception as error:
            # The batch rolled back whole. A scan that asked for its files is told, and they leave its queue, so it
            # does not wait for a publish that will fail again.
            fail_batch(conn, requests, resolved_paths | set(documents) | gone_documents, f"{type(error).__name__}: {error}")
            raise

        return len(resolved_paths) + len(documents) + len(gone_documents)
    finally:
        end_batch()


#: A batch of fewer files than this is parsed in the worker itself: starting work in other processes costs more.
MIN_FILES_FOR_PARALLEL = 6


_last_parsers: Optional[tuple] = None


def _record_parsers(conn, workers: int, held_down: Optional[str]) -> None:
    """Keep what the last batch was parsed with where `status` can show it (`parsers: 1 (machine busy)`), written only
    when it changes: a backlog that drains slowly on one parser is otherwise not visible to anyone."""
    global _last_parsers
    if _last_parsers == (workers, held_down):
        return
    _last_parsers = (workers, held_down)
    try:
        set_config(conn, PARSERS_KEY, json.dumps({"count": workers, "held_down_by": held_down, "at": time.time()}))
    except Exception:  # noqa: BLE001 - reporting must not stop the work
        pass


def _parser_pool(conn, settings: dict, file_paths: list):
    """The pool of parser processes this batch should use, or `None` to parse in the worker itself (a small batch,
    the one-process test mode, or a machine with a single core)."""
    if len(file_paths) < MIN_FILES_FOR_PARALLEL or os.environ.get(_DISABLE_ENV_VAR):
        return None
    scanning = scanning_now(conn)
    workers, held_down = parser_plan(settings, scanning, None if scanning else pause_reason(settings), available_memory_mb())
    _record_parsers(conn, workers, held_down)
    if workers < 2:
        return None
    return get_parse_pool(workers)


def _by_options(paths: set, requests: dict) -> dict:
    """The files of a batch grouped by the options of the scan that asked for them (plain files have none)."""
    groups: dict = {}
    for path in paths:
        flags = requests[path][1:] if path in requests else (False, False, False)
        groups.setdefault(flags, set()).add(path)
    return groups


def _owner_of(group: set, requests: dict) -> Optional[str]:
    owners = sorted({requests[path][0] for path in group if path in requests})
    return owners[0] if owners else None


def _record_scan_failures(conn, failures: list, file_errors: dict, requests: dict) -> None:
    """A file a scan asked for that could not be read leaves the scan (it is reported there) and goes back to the
    ordinary queue, where its retry wait applies."""
    by_scan: dict[str, list[dict]] = {}
    for file_path, message, _mtime, _size in failures:
        if file_path not in requests:
            continue
        errors = file_errors.get(file_path) or [{"file": file_path, "message": message, "line": None, "column": None}]
        by_scan.setdefault(requests[file_path][0], []).extend(errors)
    if not by_scan:
        return
    enqueue_staleness_many(conn, [(file_path, "mtime_changed") for file_path, *_ in failures if file_path in requests], overwrite=True)
    record_batch(conn, {}, {}, by_scan, {}, {}, {}, 0)


def _record_scan_batch(conn, requests, finished_paths, documents, gone_documents, function_counts, group_stats, partial_files, skipped_by_file, file_errors, declaration_counts=None) -> None:
    finished: dict[str, list] = {}
    documents_indexed: dict[str, int] = {}
    for path in finished_paths:
        if path not in requests:
            continue
        scan_id = requests[path][0]
        is_document_file = path in documents or path in gone_documents
        finished.setdefault(scan_id, []).append((path, is_document_file, function_counts.get(path, 0)))
        if path in documents:
            documents_indexed[scan_id] = documents_indexed.get(scan_id, 0) + 1
    # Diagnostics of a file read in part are reported with the scan, as they always were.
    reported: dict[str, list[dict]] = {}
    for path, errors in file_errors.items():
        if path in requests and path in finished_paths:
            reported.setdefault(requests[path][0], []).extend(errors)
    record_batch(conn, group_stats, finished, {}, partial_files, skipped_by_file, documents_indexed, count_to_review(conn), declaration_counts)
    for scan_id, errors in reported.items():
        run = load_run(conn, scan_id)
        if run is not None:
            run["errors"] = run["errors"] + errors
            save_run(conn, run)


#: How long to wait when the machine is busy or short of memory before looking again.
GUARD_PAUSE_SECONDS = 5.0


def _pause_after(settings: dict, cpu_seconds: float, wall_seconds: float, more_queued: bool, scanning: bool = False) -> tuple:
    """`(seconds, reason)` to wait before the next batch of a drain. Zero and `none` when nothing is left.
    The wait comes from the first limit that applies: the machine is busy or short of memory; the worker's
    average CPU over the whole machine (CPU time over wall time and cores) is above `worker.cpu_percent`;
    otherwise the short yield that lets a waiting request take the lock. While a scan is waiting for its
    files, only that short yield applies: someone is at the terminal, so the limits for background work do not."""
    if not more_queued:
        return 0.0, "none"
    if scanning:
        return settings["worker.yield_ms"] / 1000.0, "scan"
    busy = pause_reason(settings)
    if busy is not None:
        return GUARD_PAUSE_SECONDS, "memory" if "memory" in busy else "load"
    cores = os.cpu_count() or 1
    allowed = max(1, settings["worker.cpu_percent"]) / 100.0
    over_cap = cpu_seconds / (cores * allowed) - wall_seconds
    yield_for = settings["worker.yield_ms"] / 1000.0
    if over_cap > yield_for:
        return over_cap, "cap"
    return yield_for, "yield"


def run_one_pass(conn: ConnectionLike, db_path: Path, settings: Optional[dict] = None, detect: bool = True) -> dict:
    """One bounded detect / discover / re-scan cycle against `conn`.

    Detection is a stat sweep of every scanned file (no reads). New-file
    discovery looks at a bounded slice of the project per pass. Re-scanning
    takes files a search or lookup queued first, then drift, then new files
    (those modified lately before the rest), stopping after `worker.rescan_budget`
    files or `worker.pass_seconds` of parsing, so one pass never turns into a
    long stall. With `detect=False` only the re-scan runs: that is the drain
    between two detections, which the loop repeats without waiting.

    Returns counts: `detected` (newly queued drift), `discovered` (newly
    queued files), `remediated` (files re-scanned), and what the loop should do
    next: `queued_after` (files still waiting), `pause` (seconds) and
    `pause_reason`.
    """
    settings = settings or read_all_settings(conn)
    root = project_root_for(db_path)
    pass_started = time.perf_counter()
    cpu_started = time.process_time() + parser_cpu_seconds()

    drifted: list = []
    queued_before: set = set()
    discovered = 0
    detect_ms = discover_ms = 0.0
    if detect:
        queued_before = set(list_staleness_queue(conn))
        drifted = [(path, reason) for path, reason in _detect_drift(conn) if path not in queued_before]
        if drifted:
            enqueue_staleness_many(conn, drifted, overwrite=False)
        detect_ms = (time.perf_counter() - pass_started) * 1000.0

        discover_started = time.perf_counter()
        if root is not None:
            discovered = discover_new_files(
                conn, root, settings["worker.discovery_budget"], settings["worker.recent_days"],
                extensions(settings) if settings.get("docs.enabled", False) else (), settings,
            )["queued"]
        discover_ms = (time.perf_counter() - discover_started) * 1000.0

    batch = next_rescan_batch(conn, batch_budget(conn, settings), time.time(), root)
    remediated = 0
    remediate_started = time.perf_counter()
    try:
        # A scan that waits for its result is cut by the number of files only: a file that takes longer to read than the
        # time budget would otherwise end every batch after one file, and each batch pays the fixed cost of a publish.
        remediated = _remediate_batch(conn, db_path, batch, None if scanning_now(conn) else settings["worker.pass_seconds"])
    finally:
        queued_after = len(list_staleness_queue(conn)) if (batch or detect) else 0
        wall = time.perf_counter() - pass_started
        # Files in retry wait stay queued but cannot be taken now: they are not "more work".
        more = remediated > 0 and bool(next_rescan_batch(conn, 1, time.time(), root))
        pause, pause_why = _pause_after(settings, time.process_time() + parser_cpu_seconds() - cpu_started, wall, more, more and scanning_now(conn))
        if tracing.enabled():
            tracing.emit(
                "pass",
                wall * 1000.0,
                detect_ms=round(detect_ms, 3),
                discover_ms=round(discover_ms, 3),
                rescan_ms=round((time.perf_counter() - remediate_started) * 1000.0, 3),
                queued_before=len(queued_before),
                drifted=len(drifted),
                discovered=discovered,
                batch=len(batch),
                remediated=remediated,
                queued_after=queued_after,
                gap_ms=round(pause * 1000.0, 3),
                pause_reason=pause_why,
                settings={key: settings.get(key) for key in ("worker.poll_interval", "worker.rescan_budget", "worker.scan_budget", "worker.discovery_budget", "worker.wake_min_gap", "worker.pass_seconds", "worker.cpu_percent", "worker.yield_ms")},
                **tracing.system_snapshot(),
            )

    return {
        "detected": len(drifted),
        "discovered": discovered,
        "remediated": remediated,
        "queued_after": queued_after,
        "pause": pause,
        "pause_reason": pause_why,
    }


def _remediate_batch(conn: ConnectionLike, db_path: Path, batch: list, pass_seconds: Optional[float] = None) -> int:
    """Re-scan `batch` through the worker (or directly, for a plain connection)."""
    remediated = 0
    if batch:
        # `WorkerConnection.remediate` runs the publish inside the worker
        # process, which holds the database; a plain `duckdb` connection
        # (tests only) has no worker to delegate to.
        if isinstance(conn, WorkerConnection):
            remediated = conn.remediate(batch, pass_seconds)
        else:
            remediated = _remediate_queued_files(conn, db_path, batch, pass_seconds)
    return remediated


#: Set by the SIGTERM handler. Raising `SystemExit` alone is not enough: DuckDB
#: runs a lazy `import pandas` on its first parameterized query and swallows
#: every exception raised while it does, so a stop that arrives then would be lost.
_stop_requested = threading.Event()
_wake_to_stop: Optional[threading.Event] = None


def _raise_system_exit(signum, frame) -> None:
    _stop_requested.set()
    if _wake_to_stop is not None:
        _wake_to_stop.set()
    raise SystemExit(0)


#: The database file this worker opened, and its identity (device and inode): a worker whose file is deleted, or replaced by another
#: project's, stops at its next check (`database_replaced`); it never creates the file again.
_watched_database: Optional[Tuple[Path, Tuple[int, int]]] = None


def database_replaced(db_path: Path, identity: Tuple[int, int], stat: Callable[[Path], os.stat_result] = os.stat) -> bool:
    """Whether the file the worker opened is gone, or is no longer the file at that path (deleted, or replaced by another project's).
    An unreadable folder is not a reason to stop, and nothing is created."""
    try:
        info = stat(db_path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return (info.st_dev, info.st_ino) != identity


def _exit_if_stop_requested() -> None:
    if _stop_requested.is_set():
        raise SystemExit(0)
    if _watched_database is not None and database_replaced(*_watched_database):
        logger.warning(
            "The database file %s is gone or was replaced by another file: this worker stops. The next command starts a current worker "
            "for the file at that path; nothing was created here.",
            _watched_database[0],
        )
        raise SystemExit(0)


def _monotonic() -> float:
    """The clock of the detection cadence (`run_worker`, `detection_due`). A test replaces it with a clock that moves only when the loop
    waits, so the cadence is checked without real time (the waits the loop asks for are the thing checked)."""
    return time.monotonic()


def _wait_for_wake(event: threading.Event, seconds: float) -> bool:
    """Sleep up to `seconds`, or until a client request sets `event`."""
    return event.wait(seconds)


def _load_settings(conn: ConnectionLike) -> dict:
    """The current settings, or the defaults if they can't be read: a
    settings problem must not stop the worker."""
    try:
        return read_all_settings(conn)
    except Exception:  # noqa: BLE001
        logger.exception("Could not read settings; using defaults")
        return {key: setting.default for key, setting in SETTINGS.items()}


def run_worker(
    db_path: Path,
    idle_timeout: float = IDLE_TIMEOUT_SECONDS,
    poll_interval: Optional[float] = None,
    dormant_after: float = DORMANT_AFTER_SECONDS,
    dormant_poll_interval: float = DORMANT_POLL_INTERVAL_SECONDS,
) -> None:
    """Opens the *one* real connection this process will ever hold on
    `db_path`, serves it to every other command over the IPC socket,
    and runs the staleness-detection loop against that same connection
    through its own loopback client — never a second, separate way of
    touching it. Exits immediately, doing nothing, if another worker is
    already running for this `db_path`.

    Installs a `SIGTERM` handler for the duration of the loop
    (`drspec worker stop` sends one) so the PID file and socket always
    get released on the way out — Python's default disposition for an
    uncaught `SIGTERM` terminates the process without running `finally`
    blocks, which would otherwise leave both behind.

    The pace comes from the `worker.poll_interval` setting (read each
    pass, so a change applies without a restart); `poll_interval` overrides
    it for tests. It backs off while idle (`dormant_after`/
    `dormant_poll_interval`), but any request from a client wakes the
    worker for a pass within seconds (at least `worker.wake_min_gap` after
    the last one), so work a search just queued doesn't wait out a long
    sleep. The loop still runs forever by default (`idle_timeout`).

    A single uncaught exception from one pass must never kill the
    whole process silently — a spawned worker's stderr goes to
    `/dev/null` and nothing else supervises it. One bad pass (a
    transient IPC hiccup, a file that fails to re-parse) costs that one
    pass, never the process, and is treated the same as "nothing
    detected" for the backoff cadence above, so a *repeatedly* failing
    pass still backs off over time instead of busy-looping forever.
    """
    # Installed before the PID file exists: once other processes can see
    # this worker, a `worker stop` must find a handler that cleans up.
    # With the default disposition, a SIGTERM arriving during startup
    # (the heavy import below takes a moment) killed the process and left
    # its PID file behind.
    global _wake_to_stop, _watched_database
    _stop_requested.clear()
    _wake_to_stop = None
    _watched_database = None
    previous_handler = signal.signal(signal.SIGTERM, _raise_system_exit)
    tracing.init_process("worker", honor_env=False)
    tracing.bind(db_path)
    acquired = False
    server: Optional[WorkerIPCServer] = None
    loopback: Optional[WorkerConnection] = None
    supervisor: Optional[EmbeddingSupervisor] = None
    try:
        acquired = acquire_singleton(db_path)
        if not acquired:
            return

        # Paths in the database are relative to the project root; the worker
        # may have been started from anywhere.
        root = project_root_for(db_path)
        if root is not None and root.exists() and root.resolve() != Path.cwd().resolve():
            os.chdir(root)

        sweep_orphaned_staging(Path(db_path).parent)
        try:
            ensure_embedding_layout(db_path)
            ensure_fk_layout(db_path)
            ensure_identity_layout(db_path)
        except MigrationError as e:
            # Never run on the old layout. Nothing reads this process's stderr,
            # so the reason goes where the next command can show it.
            record_startup_error(db_path, str(e))
            raise
        # A rebuild that was killed on an earlier start leaves its working copy of the
        # database behind, and this start may have just finished the job.
        sweep_orphaned_staging(Path(db_path).parent)
        clear_startup_error(db_path)
        _exit_if_stop_requested()
        conn = duckdb.connect(str(db_path))
        try:  # (the file this worker opened: a worker whose file later goes is stopped by `_exit_if_stop_requested`)
            info = os.stat(db_path)
            _watched_database = (Path(db_path), (info.st_dev, info.st_ino))
        except OSError:
            _watched_database = None
        init_schema(conn)  # so the staleness tables exist even before any scan runs
        requeue_for_new_facts(conn)
        try:
            requeue_outdated_partial_files(conn)  # files an older parser read in part: read again, at background priority
        except Exception:  # noqa: BLE001 - never stops the worker: the files are looked at again at the next start
            logger.warning("Could not queue the files an older parser read in part", exc_info=True)
        try:
            removed = forget_unknown_files(conn)  # records of files that were deleted or ignored before they went with the file
            if removed:
                logger.info("Removed %d partial or skipped-construct record(s) of files the index no longer knows", removed)
        except Exception:  # noqa: BLE001 - never stops the worker: they are looked at again at the next start
            logger.warning("Could not remove the records of files the index no longer knows", exc_info=True)
        _exit_if_stop_requested()

        server = WorkerIPCServer(conn, db_path, remediate_fn=_remediate_queued_files)
        server.start()
        _wake_to_stop = server.wake
        loopback = connect_loopback(db_path)
        supervisor = EmbeddingSupervisor()
        idle_elapsed = 0.0
        last_detect: Optional[float] = None
        detect_due = False  # the last idle sleep ran to its end, so detection is due now
        pruned_logs = False
        compaction_tried = False  # once per stretch of idleness
        while idle_elapsed < idle_timeout:
            _exit_if_stop_requested()
            if restart_ready(server, loopback):
                logger.info("Replacing this worker (a client runs another build): the batch in flight is finished and nothing is waited for")
                break  # the loop ends as it does when idle: the connection, the socket and the PID file are released
            settings = _load_settings(loopback)
            compact_after = settings.get("worker.compact_idle_seconds", 600)
            if idle_elapsed < max(1, compact_after):
                compaction_tried = False
            elif compact_after and not compaction_tried and not _scan_waiting(loopback):
                # Idle for a while: the free blocks of the file are given back, in this process, without a restart
                # (a start never waits for it). Writers wait for the rebuild; reads are answered as during a publish.
                compaction_tried = True
                try:
                    server.compact_if_needed(FREE_SHARE, MIN_BYTES)
                except Exception:  # noqa: BLE001 - never fatal: the file stays as it is
                    logger.warning("Compacting the database failed; it is left as it is", exc_info=True)
            tracing.configure_from_settings(settings)
            if not pruned_logs:
                pruned_logs = True
                try:
                    tracing.prune_logs(settings["trace.keep_days"], settings["trace.max_total_mb"], in_use=tracing.log_file_for(db_path))
                except OSError:
                    pass
            tracing.set_cid(tracing.new_cid())
            close_if_idle()
            interval = poll_interval if poll_interval is not None else settings["worker.poll_interval"]
            # Two rhythms. Detecting (the stat sweep of every known file and the walk for new ones) is the
            # heavy part and runs every `interval`, or every dormant interval after a long idle. Re-scanning
            # what is queued does not wait for that: it runs back to back, pausing only as its limits say.
            detect_every = dormant_poll_interval if idle_elapsed >= dormant_after else interval
            detecting = detection_due(last_detect, detect_due, detect_every, _scan_waiting(loopback))
            try:
                result = run_one_pass(loopback, db_path, settings, detect=detecting)
                if detecting:
                    last_detect = _monotonic()
                    detect_due = False
            except Exception:
                # Never silent: a pass that keeps failing should be
                # visible to anyone who's configured logging, not just
                # invisible forever. Still never raises past this point
                # — one bad pass costs a log line, never the process.
                logger.exception("Staleness worker pass failed; will retry next interval")
                result = {"detected": 0, "discovered": 0, "remediated": 0, "pause": 0.0, "pause_reason": "none"}
                last_detect = _monotonic()
            try:
                supervisor.tick(loopback, db_path, settings)
            except Exception:
                logger.exception("Embedding scheduling failed; will retry next interval")
            _exit_if_stop_requested()
            pass_ended = _monotonic()
            if result.get("pause_reason", "none") != "none":
                # More is queued: go on after the pause the limits ask for. A request that arrives meanwhile
                # needs no extra gap; the pause is already short and the lock is free between batches.
                idle_elapsed = 0.0
                if _wait_for_wake(server.wake, result["pause"]):
                    server.wake.clear()
                _exit_if_stop_requested()
                continue
            if result["detected"] or result["discovered"] or result["remediated"]:
                idle_elapsed = 0.0
                sleep_for = interval if detecting else max(0.0, detect_every - (_monotonic() - last_detect))
            else:
                sleep_for = detect_every if detecting else max(0.0, detect_every - (_monotonic() - last_detect))
                idle_elapsed += sleep_for
            woken = _wait_for_wake(server.wake, sleep_for)
            detect_due = True  # a sleep that ran out, or a request that cut it short, both mean: look for changes now
            if woken and not _stop_requested.is_set():
                # A client asked for something: that is activity, and the
                # requests may have queued work. Keep a minimum gap so a
                # burst of requests doesn't trigger a pass each time.
                idle_elapsed = 0.0
                # (a scan that someone waits for has no use for the gap: it is the reason for the wake, and its files
                # may be queued a moment after the wake, so the gap is slept in steps and ends when they are)
                while not _stop_requested.is_set():
                    remaining_gap = settings["worker.wake_min_gap"] - (_monotonic() - pass_ended)
                    if remaining_gap <= 0 or _scan_waiting(loopback) or server.restart_requested.is_set():
                        break
                    time.sleep(min(0.1, remaining_gap))
                server.wake.clear()
            _exit_if_stop_requested()
    finally:
        close_pool()
        if supervisor is not None:
            supervisor.stop()
        if loopback is not None:
            loopback.close()
        if server is not None:
            server.stop()
        _wake_to_stop = None
        signal.signal(signal.SIGTERM, previous_handler)
        if acquired:
            release_singleton(db_path)


def detection_due(last_detect: Optional[float], detect_due: bool, detect_every: float, scan_waiting: bool, now: Optional[float] = None) -> bool:
    """Whether this pass sweeps the project for changes and new files. Not while a scan is waiting: the scan has just
    done that sweep itself and queued what it found, and repeating it (a second or more on a large project) only
    delays the files it is waiting for. The sweep stays due, and the next pass makes it."""
    if scan_waiting and last_detect is not None:
        return False
    now = _monotonic() if now is None else now
    return last_detect is None or detect_due or now - last_detect >= detect_every


def _scan_waiting(conn: ConnectionLike) -> bool:
    """Whether a `drspec scan` has files queued and is waiting for them. A failed question is a "no": the gap only
    spaces out passes, so keeping it is the safe answer."""
    try:
        return scanning_now(conn)
    except Exception:
        return False


def spawn_worker_if_not_running(db_path: Path) -> None:
    """Best-effort, fire-and-forget launch — called by `drspec scan` on
    completion. Never raises: a launch failure just means staleness
    detection doesn't start until something else triggers it (another
    scan, or a manual `drspec worker run`)."""
    if os.environ.get(_DISABLE_ENV_VAR):
        return
    if is_worker_running(db_path) is not None:
        return
    try:
        # `--db` is a *global* option on the main `app` callback, not
        # on `worker run` itself — it must precede the subcommand, or
        # Click rejects it as "No such option."
        root = project_root_for(db_path)
        subprocess.Popen(
            [sys.executable, "-m", "drspec", "--db", str(db_path), "worker", "run"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(root) if root is not None and root.exists() else None,
        )
    except OSError:
        pass


def stop_worker(db_path: Path, timeout: float = 10.0) -> tuple[Optional[int], bool]:
    """SIGTERM the running worker and wait for it to release its PID file.

    Returns `(pid, exited)`: `pid` is `None` when no worker was running;
    `exited` is `False` only if it was still registered after `timeout`.
    Raises `OSError` if the signal can't be delivered.

    Waiting matters because the PID file is the last thing a worker
    releases: a command started right after a non-waiting stop would see
    a half-dead worker, decline to start a replacement, and time out.
    """
    pid = is_worker_running(db_path)
    if pid is None:
        return None, True
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_worker_running(db_path) is None:
            return pid, True
        time.sleep(0.05)
    return pid, False


def wait_for_worker_exit(db_path: Path, timeout: float = 10.0) -> bool:
    """Wait for the worker to release its PID file (it was asked to replace itself and has nothing to finish). False after `timeout`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_worker_running(db_path) is None:
            return True
        time.sleep(0.05)
    return is_worker_running(db_path) is None


def restart_ready(server: Any, conn: ConnectionLike) -> bool:
    """Whether the worker asked to replace itself (a client of another build) may exit now: the batch it was publishing is over
    (this is asked between passes) and no scan is waited for, so nothing a person waits for is cut."""
    return server.restart_requested.is_set() and not _scan_waiting(conn)


def get_worker_status(db_path: Path) -> dict[str, Any]:
    """Whether a worker is running and how many files are flagged stale.

    Goes through the worker only when one is confirmed up:
    `get_worker_connection()` would spawn one just to answer whether one
    exists, the opposite of what this reports.
    """
    pid = is_worker_running(db_path)
    queue_size: Optional[int] = None
    backlog: Optional[dict[str, int]] = None
    embedding: Optional[dict[str, Any]] = None
    if Path(db_path).exists():
        try:
            conn = get_worker_connection(db_path, read_only=True, check_build=False) if pid is not None else get_connection(db_path, read_only=True)
            try:
                backlog = get_rescan_backlog(conn, time.time(), project_root_for(db_path) or Path.cwd())
                queue_size = len(list_staleness_queue(conn)) - backlog["files_unreadable"]  # a file that cannot be read is not work to do
                embedding = embedding_status(conn, read_all_settings(conn), time.time())
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 - status must answer even when the queue can't be read
            queue_size = None
            backlog = None
            embedding = None
    status: dict[str, Any] = {
        "running": pid is not None,
        "pid": pid,
        "staleness_queue_size": queue_size,
        "backlog": backlog,
        "embedding": embedding,
    }
    if pid is not None:
        reported = peek_worker_build(db_path)
        status["build"] = reported or None
        status["current_build"] = build_id()
        status["outdated"] = reported is not None and reported != build_id()
        if status["outdated"]:
            status["note"] = (
                "The worker runs older code than the one installed (it was started before an upgrade). "
                "Run 'drspec worker stop'; the next command starts a current one."
            )
    try:
        from drspec.core.embedder import model_cache_info

        status["model_cache"] = model_cache_info()
    except Exception:  # noqa: BLE001 - status must answer even when the folder cannot be examined
        pass
    failure = read_startup_error(db_path)
    if failure is not None and pid is None:
        status["startup_error"] = failure
    try:
        from drspec.core.ide.mcp_registration import registration_status

        root = project_root_for(db_path)
        mcp = registration_status(root) if root is not None else {}
        if mcp:
            status["mcp_server"] = mcp
    except Exception:  # noqa: BLE001 - status must answer even when a client's files cannot be read
        pass
    return status
