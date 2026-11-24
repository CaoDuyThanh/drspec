"""A scan, as the worker runs it: files queued in a top tier, read in batches, results gathered per scan.

`drspec scan` does not read or publish anything itself. It asks the worker to queue the files that changed (found
with the one change check of `changes.detect_changes`) and then watches the scan's progress. The worker reads them
in its ordinary batches, each published in one transaction, ahead of everything a search or lookup queued, and adds
what each batch did to the scan's own record so the command can print the same summary a single publish gave.

State lives in two places: `scan_requests` (one row per file still to be read, with the scan's options) and a
`scan:run:<id>` entry in `config` (counts and results so far).
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections import Counter
from itertools import islice
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


from drspec.core.changes import Changes, detect_changes, document_suffixes
from drspec.core.discovery import project_root_for
from drspec.core.parser_version import requeue_outdated_partial_files
from drspec.core.docs import extensions
from drspec.core.scanner import Scanner
from drspec.core.settings import read_all_settings
from drspec.db.queries import enqueue_staleness_many, get_config, set_config

RUN_PREFIX = "scan:run:"
SCAN_REASON = "scan"
"""Reason of a file a waiting scan queued: the top tier, ahead of what a search or lookup asked for."""
BACKGROUND_REASON = "scan_queued"
"""Reason of a file a `--no-wait` scan queued: the ordinary background tier."""
MAX_LISTED = 20
RUN_KEEP_SECONDS = 86400.0
_CHUNK = 400


def new_scan_id() -> str:
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# Summaries of a publish, and adding them up across batches
# ---------------------------------------------------------------------------


def summarize_by_reason(items: Dict[str, str], limit: int = MAX_LISTED) -> Dict[str, Any]:
    """Counts per reason plus the first `limit` entries, instead of one entry per function."""
    return {
        "total": len(items),
        "by_reason": dict(Counter(items.values()).most_common()),
        "examples": dict(islice(items.items(), limit)),
    }


def bounded_carried_over(carried: Dict[str, str], limit: int = MAX_LISTED) -> Dict[str, Any]:
    """The contracts a scan carried over, as a summary: the total and the first `limit` pairs. The whole mapping is
    kept in the stored stats (batches are merged from it); only the summary a person or an assistant reads is cut."""
    return {"total": len(carried), "examples": dict(islice(carried.items(), limit))}


def stats_dict(sync_stats: Any) -> Dict[str, Any]:
    """What one publish did, in the fields a scan's summary has always had."""
    return {
        "functions_new": sync_stats.functions_new,
        "functions_changed": sync_stats.functions_changed,
        "functions_unchanged": sync_stats.functions_unchanged,
        "functions_removed": sync_stats.functions_removed,
        "carried_over": dict(sync_stats.carried_over),
        "resolution_reasons": summarize_by_reason(sync_stats.resolution_reasons),
        "contracts_invalidated": summarize_by_reason(sync_stats.context_invalidated),
        "embeddings_updated": sync_stats.embeddings_updated,
        "edges_inserted": sync_stats.edges_inserted,
        "edges_updated": sync_stats.edges_updated,
        "edges_promoted": sync_stats.edges_promoted,
        "edges_deleted": sync_stats.edges_deleted,
        "references_inserted": sync_stats.references_inserted,
        "references_updated": sync_stats.references_updated,
        "references_deleted": sync_stats.references_deleted,
        "neighbours_re_resolved": getattr(sync_stats, "neighbours_re_resolved", 0),
    }


EMPTY_STATS: Dict[str, Any] = stats_dict(type("Empty", (), {
    "functions_new": 0, "functions_changed": 0, "functions_unchanged": 0, "functions_removed": 0, "carried_over": {},
    "resolution_reasons": {}, "context_invalidated": {}, "embeddings_updated": 0, "edges_inserted": 0, "edges_updated": 0,
    "edges_promoted": 0, "edges_deleted": 0, "references_inserted": 0, "references_updated": 0, "references_deleted": 0,
    "neighbours_re_resolved": 0,
})())


def _merge_reasons(total: Dict[str, Any], more: Dict[str, Any]) -> Dict[str, Any]:
    by_reason = Counter(total.get("by_reason", {}))
    by_reason.update(more.get("by_reason", {}))
    examples = dict(total.get("examples", {}))
    for key, value in more.get("examples", {}).items():
        if len(examples) >= MAX_LISTED:
            break
        examples.setdefault(key, value)
    return {"total": total.get("total", 0) + more.get("total", 0), "by_reason": dict(by_reason.most_common()), "examples": examples}


def merge_stats(total: Dict[str, Any], more: Dict[str, Any]) -> Dict[str, Any]:
    """`total` plus one batch's `more`: counts add up, the per-reason tables are combined."""
    merged = dict(total)
    for key, value in more.items():
        if key in ("resolution_reasons", "contracts_invalidated"):
            merged[key] = _merge_reasons(merged.get(key, {}), value)
        elif key == "carried_over":
            merged[key] = {**merged.get(key, {}), **value}
        else:
            merged[key] = merged.get(key, 0) + value
    return merged


# ---------------------------------------------------------------------------
# The record of one scan
# ---------------------------------------------------------------------------


def new_run(scan_id: str, queued: int, **extra: Any) -> Dict[str, Any]:
    return {
        "scan_id": scan_id,
        "started_at": time.time(),
        "queued": queued,
        "done": 0,
        "done_code": 0,
        "done_documents": 0,
        "failed": 0,
        "cancelled": 0,
        "functions_found": 0,
        "documents_indexed": 0,
        "documentation_sections_to_review": 0,
        "errors": [],
        "partial_files": {},
        "skipped_constructs": 0,
        "stats": dict(EMPTY_STATS),
        "detached": False,
        **extra,
    }


def load_run(conn, scan_id: str) -> Optional[Dict[str, Any]]:
    raw = get_config(conn, RUN_PREFIX + scan_id)
    return json.loads(raw) if raw else None


def save_run(conn, run: Dict[str, Any]) -> None:
    set_config(conn, RUN_PREFIX + run["scan_id"], json.dumps(run))


def forget_run(conn, scan_id: str) -> None:
    conn.execute("DELETE FROM config WHERE key = ?", [RUN_PREFIX + scan_id])


def _prune_old_runs(conn) -> None:
    for key, raw in conn.execute("SELECT key, value FROM config WHERE starts_with(key, ?)", [RUN_PREFIX]).fetchall():
        try:
            started = float(json.loads(raw).get("started_at", 0))
        except (ValueError, TypeError):
            started = 0.0
        if time.time() - started > RUN_KEEP_SECONDS:
            conn.execute("DELETE FROM config WHERE key = ?", [key])


def remaining(conn, scan_id: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM scan_requests WHERE scan_id = ?", [scan_id]).fetchone()[0]


def scanning_now(conn) -> bool:
    """A waiting scan has files queued: the worker then runs flat out."""
    return conn.execute("SELECT 1 FROM staleness_queue WHERE reason = ? LIMIT 1", [SCAN_REASON]).fetchone() is not None


def batch_budget(conn, settings: Dict[str, Any]) -> int:
    """How many files the next batch may hold: `worker.scan_budget` while a scan is being waited for (the cost of a
    publish grows with the index, so few large batches are much faster than many small ones), else `worker.rescan_budget`."""
    return settings["worker.scan_budget"] if scanning_now(conn) else settings["worker.rescan_budget"]


def request_flags(conn, file_paths: Iterable[str]) -> Dict[str, Tuple[str, bool, bool, bool]]:
    """`{path: (scan_id, deep, embed, full)}` for the queued files a scan asked for."""
    paths = list(file_paths)
    found: Dict[str, Tuple[str, bool, bool, bool]] = {}
    for start in range(0, len(paths), _CHUNK):
        part = paths[start : start + _CHUNK]
        marks = ", ".join("?" for _ in part)
        for path, scan_id, deep, embed, full in conn.execute(
            f"SELECT file_path, scan_id, deep, embed, full_scan FROM scan_requests WHERE file_path IN ({marks})", part
        ).fetchall():
            found[path] = (scan_id, bool(deep), bool(embed), bool(full))
    return found


# ---------------------------------------------------------------------------
# Starting, watching and cancelling a scan
# ---------------------------------------------------------------------------


def _target_prefixes(targets: List[str]) -> Tuple[List[str], List[str]]:
    """Split what a scan was asked about into paths, and the file part of any function or section identifier."""
    paths = []
    for target in targets:
        file_part = target.split("::", 1)[0] if "::" in target else target
        paths.append(file_part.rstrip("/") or ".")
    return paths, [t for t in targets if "::" in t]


def _scope(paths: List[str], recursive: bool):
    if not paths:
        return None
    if "." in paths:
        paths = [""] + [p for p in paths if p != "."]

    def in_scope(candidate: str) -> bool:
        for path in paths:
            if path == "":
                return recursive or "/" not in candidate
            if candidate == path:
                return True
            if candidate.startswith(path + "/") and (recursive or "/" not in candidate[len(path) + 1 :]):
                return True
        return False

    return in_scope


GENERATED_FOLDERS_KEY = "scan:generated_folders"


def _remember_generated_folders(conn, found: Dict[str, str]) -> None:
    """Keep what the last whole-project listing left out as generated folders, for `status` (a write only when it changed)."""
    text = json.dumps(dict(sorted(found.items())))
    stored = get_config(conn, GENERATED_FOLDERS_KEY)
    if stored != text and (stored is not None or found):
        set_config(conn, GENERATED_FOLDERS_KEY, text)


def generated_folders_left_out(conn) -> Dict[str, str]:
    """The generated folders (folder -> kind) the last whole-project scan did not read, as `scan.include` would read them."""
    try:
        return dict(json.loads(get_config(conn, GENERATED_FOLDERS_KEY) or "{}"))
    except ValueError:
        return {}


def start_scan(conn, db_path: Path, spec: Dict[str, Any]) -> Dict[str, Any]:
    """Find what a scan has to read and queue it. `spec`: `scan_id`, `targets` (paths, function or section
    identifiers; empty means the whole project), `recursive`, `full`, `deep`, `embed`, `code_only`, `docs_only`,
    `wait` (False queues at background priority)."""
    started = time.perf_counter()
    scan_id = spec.get("scan_id") or new_scan_id()
    settings = read_all_settings(conn)
    root = project_root_for(db_path) or Path.cwd()
    targets = list(spec.get("targets") or [])
    requeue_outdated_partial_files(conn)  # a parser that changed since the files were read (cheap when it did not)
    paths, _ = _target_prefixes(targets)
    in_scope = _scope(paths, bool(spec.get("recursive", True)))
    full = bool(spec.get("full"))
    named = paths if paths and not {".", ""} & set(paths) else None  # a scan of named paths lists those, not the project
    changes = detect_changes(conn, root, settings, Scanner(), in_scope, full, only=named)
    if named is None:  # only a listing of the whole project knows every generated folder (`status` names them)
        _remember_generated_folders(conn, changes.listing.generated)
    _add_explicit_targets(conn, root, settings, changes, paths, full)
    # A file that is still there but no longer part of the project (ignored now) is dropped from the index
    # on the spot; a deleted one goes through the queue like any other change.
    excluded = [path for path in changes.removed if (root / path).exists()]
    functions_removed_as_ignored = 0
    if excluded:
        for start in range(0, len(excluded), _CHUNK):
            part = excluded[start : start + _CHUNK]
            functions_removed_as_ignored += conn.execute(f"SELECT COUNT(*) FROM artifacts WHERE file_path IN ({', '.join('?' for _ in part)})", part).fetchone()[0]
        forget_excluded(conn, excluded, settings)
        changes.removed = [path for path in changes.removed if path not in set(excluded)]

    doc_suffixes = extensions(settings)

    def keep(path: str) -> bool:
        is_doc = path.lower().endswith(doc_suffixes) and bool(document_suffixes(settings))
        if spec.get("code_only") and is_doc:
            return False
        if spec.get("docs_only") and not is_doc:
            return False
        return True

    ordered = [path for path in changes.to_read if keep(path)]
    wait = bool(spec.get("wait", True))
    reason = SCAN_REASON if wait else BACKGROUND_REASON
    if ordered:
        _prune_old_runs(conn)
        for start in range(0, len(ordered), _CHUNK):
            part = ordered[start : start + _CHUNK]
            enqueue_staleness_many(conn, [(path, reason) for path in part], overwrite=True)
            marks = ", ".join("(?, ?, ?, ?, ?)" for _ in part)
            params: List[Any] = []
            for path in part:
                params += [path, scan_id, bool(spec.get("deep")), bool(spec.get("embed")), full]
            conn.execute(
                f"INSERT INTO scan_requests (file_path, scan_id, deep, embed, full_scan) VALUES {marks} "
                "ON CONFLICT (file_path) DO UPDATE SET scan_id = excluded.scan_id, deep = excluded.deep, "
                "embed = excluded.embed, full_scan = excluded.full_scan",
                params,
            )
    run = new_run(
        scan_id, len(ordered),
        up_to_date=changes.unchanged,
        files_skipped_by_gitignore=changes.listing.skipped_by_gitignore,
        used_git=changes.listing.used_git,
        detached=not wait,
    )
    if ordered:
        save_run(conn, run)
    return {
        "scan_id": scan_id,
        "queued": len(ordered),
        "removed_as_ignored": len(excluded),
        "functions_removed_as_ignored": functions_removed_as_ignored,
        "documents_off": bool(spec.get("docs_only")) and not document_suffixes(settings),
        "up_to_date": changes.unchanged,
        "files_skipped_by_gitignore": changes.listing.skipped_by_gitignore,
        "generated_folders": dict(changes.listing.generated),
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1),
    }


def forget_excluded(conn, paths: List[str], settings: Dict[str, Any]) -> None:
    """Remove from the index files that exist but are no longer part of the project: their functions, edges and
    document sections, and their snapshots."""
    from drspec.core.config_keys import is_config_path, publish_configs
    from drspec.core.docs import is_document, remove_document
    from drspec.core.graph_sync import sync_scan_results
    from drspec.db.staging import publish_transaction

    documents = [path for path in paths if is_document(path, settings)]
    configs = [path for path in paths if is_config_path(path)]
    code = [path for path in paths if path not in set(documents) and path not in set(configs)]
    with publish_transaction(conn) as write_conn:
        if code:
            sync_scan_results(write_conn, functions=[], scanned_file_paths=set(code), deleted_paths=set(code))
        for path in documents:
            remove_document(write_conn, path)
        if configs:
            publish_configs(write_conn, {}, configs)
        for start in range(0, len(paths), _CHUNK):
            part = paths[start : start + _CHUNK]
            marks = ", ".join("?" for _ in part)
            for table in ("scanned_files", "staleness_queue", "rescan_backoff", "scan_requests"):
                write_conn.execute(f"DELETE FROM {table} WHERE file_path IN ({marks})", part)


def _add_explicit_targets(conn, root: Path, settings: Dict[str, Any], changes: Changes, paths: List[str], full: bool) -> None:
    """A file named on the command line is read even when the project's own listing leaves it out (a file git
    ignores, say): asking for it by name is a request."""
    from drspec.db.queries import list_scanned_files
    import os

    scanner = Scanner()
    known = set(changes.added) | set(changes.changed) | set(changes.removed)
    listed = set(changes.listing.files)
    stored = {path: (mtime, size) for path, mtime, size in list_scanned_files(conn)}
    suffixes = document_suffixes(settings)
    for path in paths:
        if path in (".", "") or path in listed or path in known:
            continue
        absolute = root / path
        if not absolute.is_file():
            continue
        from drspec.core.config_keys import is_config_path

        if not (scanner.is_supported(path) or (suffixes and path.lower().endswith(suffixes)) or (is_config_path(path) and settings.get("strings.enabled", True))):
            continue
        snapshot = stored.get(path)
        if snapshot is None:
            changes.added.append(path)
            continue
        stat = os.stat(absolute)
        if full or stat.st_size != snapshot[1] or stat.st_mtime != snapshot[0]:
            changes.changed.append(path)
        else:
            changes.unchanged += 1


def progress(conn, scan_id: str) -> Dict[str, Any]:
    """The scan's record with what is still waiting. `finished` once none of its files are left."""
    run = load_run(conn, scan_id)
    left = remaining(conn, scan_id)
    if run is None:
        return {"scan_id": scan_id, "known": False, "finished": True, "remaining": 0}
    run["remaining"] = left
    run["known"] = True
    run["finished"] = left == 0
    live = batch_progress(scan_id)
    if live is not None and not run["finished"]:
        run["batch"] = live  # what the batch that holds this scan's files is doing now
    return run


#: What the batch this process is running has done so far, for the progress of the scans that have files in it:
#: `{"phase": "reading" | "publishing", "since": time.monotonic(), "scans": {scan_id: [files read, files in the batch]}}`,
#: or None between batches. The batch is the only writer; `progress` only reads.
_batch: Optional[Dict[str, Any]] = None


def begin_batch(requests: Dict[str, Any], file_paths: Iterable[str]) -> None:
    """A batch starts reading `file_paths`; `requests` says which scan asked for each file."""
    global _batch
    scans: Dict[str, List[int]] = {}
    for path in file_paths:
        if path in requests:
            scans.setdefault(requests[path][0], [0, 0])[1] += 1
    _batch = {"phase": "reading", "since": time.monotonic(), "scans": scans} if scans else None


def note_read(file_path: str, requests: Dict[str, Any]) -> None:
    """The batch has come to `file_path` (read, or left alone: it is counted either way)."""
    batch = _batch
    if batch is not None and file_path in requests:
        counts = batch["scans"].get(requests[file_path][0])
        if counts is not None:
            counts[0] += 1


def batch_progress(scan_id: str) -> Optional[Dict[str, Any]]:
    """`{phase, files_read, files_total, seconds}` for the scan, while a batch with its files is running: `reading` (the
    files are being parsed) or `publishing` (what was read is being written; `files_read` is then all of them)."""
    batch = _batch
    counts = None if batch is None else batch["scans"].get(scan_id)
    if counts is None:
        return None
    return {
        "phase": batch["phase"],
        "files_read": counts[1] if batch["phase"] == "publishing" else min(counts[0], counts[1]),
        "files_total": counts[1],
        "seconds": round(time.monotonic() - batch["since"], 1),
    }


#: Scans a client has cancelled while a batch was being read: that batch leaves their files alone (the process holds
#: it, so it is a set of this process). Emptied by `cancel`, which takes the files off the queue.
_CANCELLING: set = set()


#: Whether the batch the process is running has begun to publish. Until then a cancel is in time: the batch leaves the
#: cancelled scan's files out, read or not. From then on the batch is published whole. Both are decided under one lock,
#: so a cancel is either in time or it is not, never half.
_PHASE = threading.Lock()
_publish_begun = False


def mark_cancelling(scan_id: str) -> bool:
    """A client cancelled the scan while a batch is running. True when the batch will leave the scan's files out;
    False when it has already begun to publish them (they will be done, not cancelled)."""
    with _PHASE:
        _CANCELLING.add(scan_id)
        return not _publish_begun


def begin_publish(requests: Dict[str, Any], file_paths: Iterable[str]) -> set:
    """The batch has read its files and is about to publish. Returns the files of cancelled scans, which it must leave
    out (their scan's cancel takes them off the queue); every cancel that comes after this is too late."""
    global _publish_begun
    with _PHASE:
        left_out = {path for path in file_paths if path in requests and requests[path][0] in _CANCELLING}
        _publish_begun = True
        if _batch is not None:
            _set_phase("publishing")
        return left_out


def _set_phase(phase: str) -> None:
    global _batch
    batch = _batch
    if batch is not None:
        _batch = {**batch, "phase": phase, "since": time.monotonic()}  # (a new dict: a reader sees one phase or the other)


def end_batch() -> None:
    global _publish_begun, _batch
    with _PHASE:
        _publish_begun = False
        _batch = None


def is_cancelling(scan_id: str) -> bool:
    return scan_id in _CANCELLING


def cancel(conn, scan_id: str) -> Dict[str, Any]:
    """Take the scan's unread files off the queue. What earlier batches published stays."""
    _CANCELLING.discard(scan_id)
    rows = [row[0] for row in conn.execute("SELECT file_path FROM scan_requests WHERE scan_id = ?", [scan_id]).fetchall()]
    for start in range(0, len(rows), _CHUNK):
        part = rows[start : start + _CHUNK]
        marks = ", ".join("?" for _ in part)
        conn.execute(f"DELETE FROM staleness_queue WHERE reason IN (?, ?) AND file_path IN ({marks})", [SCAN_REASON, BACKGROUND_REASON, *part])
        conn.execute(f"DELETE FROM scan_requests WHERE file_path IN ({marks})", part)
    run = load_run(conn, scan_id)
    if run is not None:
        run["cancelled"] = run.get("cancelled", 0) + len(rows)
        save_run(conn, run)
    return {"scan_id": scan_id, "cancelled": len(rows)}


def detach(conn, scan_id: str) -> Dict[str, Any]:
    """Nobody is waiting for this scan any more: it goes on at background priority and its record is dropped
    when it is done."""
    run = load_run(conn, scan_id)
    left = remaining(conn, scan_id)
    if left:
        conn.execute("UPDATE staleness_queue SET reason = ? WHERE reason = ? AND file_path IN (SELECT file_path FROM scan_requests WHERE scan_id = ?)", [BACKGROUND_REASON, SCAN_REASON, scan_id])
        if run is not None:
            run["detached"] = True
            save_run(conn, run)
    else:
        forget_run(conn, scan_id)
    return {"scan_id": scan_id, "remaining": left}


# ---------------------------------------------------------------------------
# What the worker reports back after a batch
# ---------------------------------------------------------------------------


def record_batch(
    conn,
    group_stats: Dict[str, Dict[str, Any]],
    finished: Dict[str, List[Tuple[str, bool, int]]],
    failures: Dict[str, List[Dict[str, Any]]],
    partial: Dict[str, Dict[str, Any]],
    skipped_constructs: Dict[str, int],
    documents_indexed: Dict[str, int],
    to_review: int,
    declarations: Optional[Dict[str, int]] = None,
) -> None:
    """Add a batch to the record of each scan that had files in it, and drop those files' request rows.

    `finished[scan_id]` lists `(path, is_document, functions)` for each file published (or removed);
    `failures[scan_id]` the errors of files that could not be read; `group_stats[scan_id]` what the publish did."""
    scan_ids = set(finished) | set(failures)
    for scan_id in scan_ids:
        run = load_run(conn, scan_id)
        if run is None:
            continue
        files = finished.get(scan_id, [])
        run["done"] += len(files)
        run["done_documents"] += sum(1 for _path, is_doc, _n in files if is_doc)
        run["done_code"] += sum(1 for _path, is_doc, _n in files if not is_doc)
        run["functions_found"] += sum(count for _path, _is_doc, count in files)
        run["declarations_found"] = run.get("declarations_found", 0) + sum((declarations or {}).get(path, 0) for path, _d, _n in files)
        run["documents_indexed"] += documents_indexed.get(scan_id, 0)
        run["documentation_sections_to_review"] = to_review
        run["failed"] += len(failures.get(scan_id, []))
        run["errors"] = (run["errors"] + failures.get(scan_id, []))
        run["partial_files"].update({path: info for path, info in partial.items() if path in {p for p, _d, _n in files}})
        run["skipped_constructs"] += sum(count for path, count in skipped_constructs.items() if path in {p for p, _d, _n in files})
        if scan_id in group_stats:
            run["stats"] = merge_stats(run["stats"], group_stats[scan_id])
        save_run(conn, run)
    paths = [path for files in finished.values() for path, _d, _n in files] + [
        error["file"] for errors in failures.values() for error in errors
    ]
    for start in range(0, len(paths), _CHUNK):
        part = paths[start : start + _CHUNK]
        conn.execute(f"DELETE FROM scan_requests WHERE file_path IN ({', '.join('?' for _ in part)})", part)
    for scan_id in scan_ids:
        run = load_run(conn, scan_id)
        if run is not None and run.get("detached") and remaining(conn, scan_id) == 0:
            forget_run(conn, scan_id)


def fail_batch(conn, requests: Dict[str, Tuple[str, bool, bool, bool]], paths: Iterable[str], message: str) -> None:
    """A batch could not be published (it rolled back whole). The scans that had files in it get the reason and
    drop those files; the files go back to the ordinary queue, where the worker tries them again later."""
    by_scan: Dict[str, List[str]] = {}
    for path in paths:
        if path in requests:
            by_scan.setdefault(requests[path][0], []).append(path)
    for scan_id, files in by_scan.items():
        run = load_run(conn, scan_id)
        if run is not None:
            run["failed"] += len(files)
            run["publish_error"] = message
            save_run(conn, run)
        for start in range(0, len(files), _CHUNK):
            part = files[start : start + _CHUNK]
            marks = ", ".join("?" for _ in part)
            conn.execute(f"DELETE FROM scan_requests WHERE file_path IN ({marks})", part)
            conn.execute(f"UPDATE staleness_queue SET reason = 'mtime_changed' WHERE reason IN (?, ?) AND file_path IN ({marks})", [SCAN_REASON, BACKGROUND_REASON, *part])


# ---------------------------------------------------------------------------
# Test mode: no worker loop, so the scan's files are read right away
# ---------------------------------------------------------------------------


def drain(conn, db_path: Path, scan_id: str) -> None:
    """Read every file of the scan now, in the batches the worker would use. Used where there is no worker loop
    (the in-process connection of the test suite)."""
    from drspec.db.queries import next_rescan_batch
    from drspec.staleness_worker import _remediate_queued_files

    settings = read_all_settings(conn)
    root = project_root_for(db_path)
    while remaining(conn, scan_id):
        batch = next_rescan_batch(conn, batch_budget(conn, settings), time.time(), root)
        mine =[path for path in batch if conn.execute("SELECT 1 FROM scan_requests WHERE file_path = ? AND scan_id = ?", [path, scan_id]).fetchone()]
        if not mine:
            # Files left in retry wait; they were given up on (recorded as failed) or belong to nobody.
            break
        try:
            _remediate_queued_files(conn, db_path, mine, None)
        except Exception:  # noqa: BLE001 - recorded in the scan's record, which the caller reports
            break


# ---------------------------------------------------------------------------
# Running a scan from a client: ask, wait, summarise (the CLI and the MCP tool share this)
# ---------------------------------------------------------------------------


def bounded_errors(errors: List[Dict[str, Any]], limit: int = MAX_LISTED) -> Dict[str, Any]:
    """The first `limit` parse/read errors with the full count."""
    data: Dict[str, Any] = {"errors": errors[:limit], "error_count": len(errors)}
    if len(errors) > limit:
        data["errors_omitted"] = len(errors) - limit
    return data


def partial_report(partial_files: Dict[str, Any], skipped_constructs: int) -> Dict[str, Any]:
    """The part of a scan's output that says some files were read only in part: the totals, and the files that lose the
    most (five, worst first); the whole list is capped, with the cap stated."""
    from drspec.core.partial import MAX_EXAMPLES, MAX_LISTED_FILES, totals, worst_files

    report: Dict[str, Any] = {}
    if partial_files:
        infos = [{**info, "file": path} for path, info in partial_files.items()]
        counts = totals(infos)
        if counts["files_read_with_warnings"]:
            report["files_read_with_warnings"] = counts["files_read_with_warnings"]
        if counts["files"]:
            report["files_partially_parsed"] = counts["files"]
            report["functions_skipped"] = counts["functions_skipped"]
            report["functions_partial"] = counts["functions_partial"]
            report["partial_examples"] = worst_files(infos, MAX_EXAMPLES)
            report["partial_files"] = worst_files(infos, MAX_LISTED_FILES)
            if counts["files"] > MAX_LISTED_FILES:
                report["partial_files_omitted"] = counts["files"] - MAX_LISTED_FILES
    if skipped_constructs:
        report["skipped_constructs"] = skipped_constructs
    return report


def summarize(started: Dict[str, Any], run: Optional[Dict[str, Any]], state: str) -> Dict[str, Any]:
    """The summary a scan prints. Every field a scan has always reported keeps its name and meaning; the files
    queued, done, failed and cancelled, and the neighbours re-resolved, are added.

    `state`: `done`, `queued` (a `--no-wait` scan), `still_queued` (the wait limit passed, the worker goes on) or
    `cancelled`."""
    run = run or new_run(started.get("scan_id", ""), started.get("queued", 0))
    stats = run.get("stats") or EMPTY_STATS
    queued = started.get("queued", 0)
    files_scanned = run.get("done_code", 0)
    data: Dict[str, Any] = {
        "files_scanned": files_scanned,
        "functions_found": run.get("functions_found", 0),
        **(
            {"declarations_found": run["declarations_found"], "definitions_found": run.get("functions_found", 0) - run["declarations_found"]}
            if run.get("declarations_found")
            else {}
        ),
        **{**stats, "carried_over": bounded_carried_over(stats.get("carried_over", {}))},
        "documents_indexed": run.get("documents_indexed", 0),
        "documentation_sections_to_review": run.get("documentation_sections_to_review", 0),
        **partial_report(run.get("partial_files", {}), run.get("skipped_constructs", 0)),
        "files_queued": queued,
        "files_done": run.get("done", 0),
        "files_failed": run.get("failed", 0),
        "files_cancelled": run.get("cancelled", 0),
        "files_unchanged": started.get("up_to_date", 0),
        "files_skipped_by_gitignore": started.get("files_skipped_by_gitignore", 0),
        "generated_folders": started.get("generated_folders", {}),
        "files_removed_as_ignored": started.get("removed_as_ignored", 0),
        "functions_removed_as_ignored": started.get("functions_removed_as_ignored", 0),
        "up_to_date": queued == 0 and not started.get("removed_as_ignored", 0),
        "state": state,
    }
    if started.get("removed_as_ignored"):
        data["removed_note"] = (
            f"{started['removed_as_ignored']} file(s) with {started.get('functions_removed_as_ignored', 0)} function(s) were removed from the index: "
            "they are ignored now (.gitignore, scan.ignore or a generated folder; scan.include reads one anyway), so the total went down by that"
        )
    if run.get("remaining"):
        data["files_remaining"] = run["remaining"]
    if state == "queued":
        data["message"] = f"{queued} file(s) queued; the worker reads them in the background"
    elif queued == 0 and started.get("removed_as_ignored"):
        data["message"] = f"Removed {started['removed_as_ignored']} file(s) that are now ignored from the index; nothing else to do"
    elif queued == 0:
        data["message"] = "Up to date, nothing to do"
    elif state == "still_queued":
        data["message"] = f"{run.get('remaining', 0)} of {queued} file(s) still queued; the worker continues"
    elif state == "cancelled":
        if run.get("batch_in_flight"):
            data["batch_in_flight"] = True
            size = f" of {run['batch_files']} file(s)" if run.get("batch_files") else ""
            data["message"] = (
                f"Cancelled: {data['files_done']} of {queued} file(s) were done when you cancelled. The batch{size} the worker was "
                f"publishing is still being finished, not undone: its files are indexed, the counts here do not include them yet"
            )
            if run.get("batch_files"):
                data["files_in_flight"] = run["batch_files"]  # published when the worker is done with them; not in `files_done` yet
        else:
            data["message"] = f"Cancelled: {data['files_done']} of {queued} file(s) were done, {data['files_cancelled']} not read"
            if run.get("published_after_cancel"):
                data["message"] += f" ({run['published_after_cancel']} of the done files were in a batch already being published, which finished)"
                data["files_published_after_cancel"] = run["published_after_cancel"]
    else:
        detail = f" ({data['definitions_found']} definitions, {data['declarations_found']} declarations)" if data.get("declarations_found") else ""
        data["message"] = f"Scanned {files_scanned} file(s), found {data['functions_found']} function(s){detail}"
    if started.get("documents_off"):
        from drspec.core.docs import OFF_NOTE

        data["note"] = OFF_NOTE
    if run.get("errors"):
        data.update(bounded_errors(run["errors"]))
    if run.get("publish_error"):
        data["publish_error"] = run["publish_error"]
    return data


def run_scan(
    conn,
    spec: Dict[str, Any],
    on_progress=None,
    max_wait: Optional[float] = None,
    poll_seconds: float = 0.2,
) -> Dict[str, Any]:
    """Ask the worker for a scan (`conn` is a worker connection), wait for it unless `spec["wait"]` is false,
    and return its summary. Ctrl-C cancels what has not been read; `max_wait` seconds without the scan
    finishing leaves it running and says so."""
    started = conn.scan_start(**spec)
    scan_id = started["scan_id"]
    if not spec.get("wait", True):
        return summarize(started, None, "queued")
    if started["queued"] == 0:
        return summarize(started, None, "done")
    deadline = None if max_wait is None else time.monotonic() + max_wait
    run: Optional[Dict[str, Any]] = None
    try:
        while True:
            run = conn.scan_progress(scan_id)
            if on_progress is not None:
                on_progress(run)
            if run.get("finished"):
                break
            if deadline is not None and time.monotonic() >= deadline:
                conn.scan_detach(scan_id)
                return summarize(started, run, "still_queued")
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        cancelled = conn.scan_cancel(scan_id)
        if cancelled.get("deferred"):
            # The worker is in the middle of a batch. If it is still reading, it leaves this scan's files out and the
            # cancel takes them off the queue when the batch ends: nothing to wait for. If it is already publishing, the
            # batch is published whole and the record says what became of the files only when it ends, so wait for that
            # (a second Ctrl-C stops waiting).
            if cancelled.get("publishing"):
                final, settled = _wait_for_the_batch(conn, scan_id, on_progress, poll_seconds)
                if not settled:
                    in_flight = (final.get("batch") or (run or {}).get("batch") or {}).get("files_total")
                    left = cancelled.get("cancelled", 0)
                    # the files of the batch being published are neither done nor cancelled yet: they will be done
                    final = {**final, "cancelled": max(left - in_flight, 0) if in_flight else left, "batch_in_flight": True, "batch_files": in_flight}
                elif final.get("done", 0) > cancelled.get("done", 0):
                    final = {**final, "published_after_cancel": final["done"] - cancelled.get("done", 0)}
            else:
                final = {**conn.scan_progress(scan_id), "cancelled": cancelled.get("cancelled", 0)}  # the batch leaves them out
        else:
            final = conn.scan_progress(scan_id)
        try:
            conn.scan_detach(scan_id)
        except KeyboardInterrupt:
            pass  # the worker finishes with the scan's record when the batch ends
        return summarize(started, final, "cancelled")
    conn.scan_detach(scan_id)
    return summarize(started, run, "done")


#: How long a cancelled scan waits for the batch the worker was in the middle of publishing, for the summary to say what
#: became of its files. Short: Ctrl-C returns within a second wherever it comes. A publish that is not over by then is
#: said to be still being finished (its files are indexed, not undone).
CANCEL_WAIT_SECONDS = 0.5


def _wait_for_the_batch(conn, scan_id: str, on_progress, poll_seconds: float) -> Tuple[Dict[str, Any], bool]:
    """The scan's record once the batch that was running at the cancel has ended: `(record, True)`. `(record, False)`
    when the wait ran out or was interrupted, the record being as the last poll saw it."""
    deadline = time.monotonic() + CANCEL_WAIT_SECONDS
    run = conn.scan_progress(scan_id)
    try:
        while not run.get("finished"):
            if time.monotonic() >= deadline:
                return run, False
            if on_progress is not None:
                on_progress({**run, "cancelling": True})
            time.sleep(poll_seconds)
            run = conn.scan_progress(scan_id)
    except KeyboardInterrupt:
        return run, False
    return run, True
