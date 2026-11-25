"""Saying so when the files an answer comes from have changed since the last scan.

`refs get`, `deps get` and a search read the index, which the worker brings up to date on its next sweep (about a minute later). Until
then a live read of the same files (`search --lines`) and the index disagree. The check compares the stored size and modification time of
each file with the file on disk (no file is opened; the same comparison `core/changes.py` makes for a scan) and, for a file that is new,
lists the folders the answer's files are in. One line in `note` and a `changed_since_scan` field say which files; nothing else in the
answer changes. A failure to check never fails the command.

The stored snapshots and the staleness queue are read BEFORE the answer (`take_before`): the worker publishes a re-read file's new
snapshot and its index rows in one commit, so a publish that lands while the answer is read would otherwise hide the change from the check
(the answer still old, the snapshot already new). A file in the queue is not reported as changed: the worker is reading it again, so the
answer says that instead (edited or deleted files), and a new queued file is counted by the index-incomplete note.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple

from drspec.db.queries import list_scanned_files, list_staleness_queue

#: Folders looked into for new files, and names listed in the line, at most.
_MAX_FOLDERS = 400
_MAX_NAMES = 5
_MAX_LISTED = 20

NOTE = "{count} indexed file(s) changed since the last scan ({names}): results may be outdated; drspec scan <paths> updates them."
BEING_READ = "{count} file(s) are being read again by the worker ({names}): ask again in a moment."


@dataclass(frozen=True)
class Before:
    """What the index and the queue held before the answer was read: `snapshots` maps each stored path to its (mtime, size), `queued` is
    the set of paths the staleness queue holds."""

    snapshots: Dict[str, Tuple[float, int]]
    queued: FrozenSet[str]


def take_before(conn: Any) -> Before:
    """Read the stored snapshots and the queue. Call it before the answer's own queries."""
    snapshots = {path: (mtime, size) for path, mtime, size in list_scanned_files(conn)}
    return Before(snapshots, frozenset(list_staleness_queue(conn)))


def changed_since_scan(
    conn: Any, root: Path, files: Iterable[str], *, look_for_new: bool = True, before: Optional[Before] = None
) -> Dict[str, List[str]]:
    """`{edited, deleted, added, being_read}`: the files of `files` whose size or mtime differs from the snapshot, or that are gone; the
    files beside them that have no snapshot; and the changed files the queue already holds (`being_read`). Each file is in one list only.
    `before` is the result of `take_before` taken before the answer (taken here, now, when it is not given)."""
    before = take_before(conn) if before is None else before
    paths = sorted({path for path in files if path})
    edited: List[str] = []
    deleted: List[str] = []
    being_read: List[str] = []
    for path in paths:
        if path not in before.snapshots:
            continue
        mtime, size = before.snapshots[path]
        try:
            stat = os.stat(root / path)
        except OSError:
            gone = True
        else:
            gone = False
            if stat.st_size == size and stat.st_mtime == mtime:
                continue
        if path in before.queued:
            being_read.append(path)
        elif gone:
            deleted.append(path)
        else:
            edited.append(path)
    added: List[str] = []
    if look_for_new:
        added = [path for path in _new_beside(root, before, [p for p in paths if p in before.snapshots]) if path not in before.queued]
    return {"edited": sorted(edited), "deleted": sorted(deleted), "added": added, "being_read": sorted(being_read)}


def _new_beside(root: Path, before: Before, known: List[str]) -> List[str]:
    """The files in the folders of `known` that drspec reads and has no snapshot of: a folder listed, nothing opened. A name that could
    be a file of the project is then checked against the project's own rules (`.gitignore`, `scan.ignore`, generated folders)."""
    from drspec.core.scanner import Scanner

    scanner = Scanner()
    folders = sorted({os.path.dirname(path) for path in known})[:_MAX_FOLDERS]
    candidates: List[str] = []
    for folder in folders:
        try:
            with os.scandir(root / folder if folder else root) as entries:
                names = [entry.name for entry in entries if entry.is_file(follow_symlinks=False)]
        except OSError:
            continue
        candidates += [f"{folder}/{name}" if folder else name for name in names if scanner.is_supported(Path(name))]
    unknown = [path for path in candidates if path not in before.snapshots]  # (no stored snapshot: not in the index)
    if not unknown:
        return []
    from drspec.core.project_files import list_project_files

    return list_project_files(root, scanner, (), None, count_ignored=False, only=unknown).files


def _listed(names: List[str]) -> str:
    return ", ".join(names[:_MAX_NAMES]) + (", ..." if len(names) > _MAX_NAMES else "")


def changed_note(changes: Dict[str, List[str]]) -> Optional[str]:
    """The sentences for `changes`, or `None` when nothing changed. A file is named in one sentence only."""
    marked = [*changes["edited"], *(f"{path} (deleted)" for path in changes["deleted"]), *(f"{path} (new)" for path in changes["added"])]
    sentences = []
    if marked:
        sentences.append(NOTE.format(count=len(marked), names=_listed(marked)))
    reading = list(changes.get("being_read") or [])
    if reading:
        sentences.append(BEING_READ.format(count=len(reading), names=_listed(reading)))
    return " ".join(sentences) or None


def add_changed_notice(
    conn: Any, data: dict, root: Optional[Path], files: Iterable[str], *, look_for_new: bool = True, before: Optional[Before] = None
) -> dict:
    """Add `changed_since_scan` and a sentence to `note` when a file of `files` has changed since the last scan (nothing when none has,
    when `root` is not known, or when the check fails). Pass `before` (from `take_before`, taken before the answer) so that the check
    compares with what the index held when the answer was read."""
    if root is None:
        return data
    try:
        changes = changed_since_scan(conn, Path(root), files, look_for_new=look_for_new, before=before)
        sentence = changed_note(changes)
    except Exception:  # noqa: BLE001 - a notice must never fail a command
        return data
    if sentence is None:
        return data
    data["changed_since_scan"] = {
        "count": sum(len(names) for names in changes.values()),
        **{key: names[:_MAX_LISTED] for key, names in changes.items() if names},
    }
    data["note"] = f"{data['note']} {sentence}" if data.get("note") else sentence
    return data
