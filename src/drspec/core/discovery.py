"""Finding files that were added to the project after the last scan.

The worker only watches files it has already scanned, so a new file stays
invisible until someone runs `drspec scan`. This walks the project root with
the scanner's own rules (supported languages, `DEFAULT_IGNORES`) and queues
anything not yet scanned.

The walk is incremental: files are visited in a fixed order, a pass looks at
a bounded number, and a cursor remembers where it stopped, so a large tree
is covered over several passes instead of one long stall.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Iterator, Optional

import duckdb

from drspec.core.project_files import list_project_files
from drspec.core.scanner import Scanner
from drspec.db.queries import (
    RESCAN_REASON_NEW_FILE,
    RESCAN_REASON_NEW_FILE_RECENT,
    enqueue_staleness_many,
    get_config,
    list_scanned_file_paths,
    set_config,
)

CURSOR_KEY = "worker:discovery_cursor"
STATE_DIR = "_drspec"


def project_root_for(db_path: Path) -> Optional[Path]:
    """The project root, when the database sits in the usual `<root>/_drspec/`.

    With a `--db` override somewhere else there is no way to tell which
    directory the project is, so discovery is off rather than guessing.
    """
    db_path = Path(db_path)
    if db_path.parent.name == STATE_DIR:
        return db_path.parent.parent
    return None


def iter_supported_sorted(root: Path, scanner: Scanner, after: tuple[str, ...] = (), extra_suffixes: tuple[str, ...] = ()) -> Iterator[str]:
    """Project-relative paths of scannable files (and files with one of `extra_suffixes`, the documents) in a
    fixed order, starting after the `after` path (given as its components)."""

    def walk(directory: Path, parts: tuple[str, ...]) -> Iterator[str]:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError:
            return
        for entry in entries:
            entry_parts = parts + (entry.name,)
            path = Path(entry.path)
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if not parts and entry.name == STATE_DIR:
                    continue
                if scanner.should_ignore(path, root):
                    continue
                if after and entry_parts < after[: len(entry_parts)]:
                    continue  # the whole directory sorts before the cursor
                yield from walk(path, entry_parts)
            elif is_file:
                if not (scanner.is_supported(path) or (extra_suffixes and path.name.lower().endswith(extra_suffixes))) or scanner.should_ignore(path, root):
                    continue
                if after and entry_parts <= after:
                    continue
                yield "/".join(entry_parts)

    yield from walk(root, ())


def discover_new_files(
    conn: duckdb.DuckDBPyConnection,
    root: Path,
    budget: int,
    recent_days: float = 14.0,
    document_suffixes: tuple[str, ...] = (),
    settings: Optional[dict] = None,
) -> dict[str, int]:
    """Examine up to `budget` files after the saved cursor and queue those
    never scanned (a file already queued is left alone). The files are the project's own list
    (`project_files`: what git tracks or does not ignore, narrowed by `scan.ignore`/`scan.include`). A file modified within
    `recent_days` is queued as `new_file_recent`, which is re-scanned before the
    rest (`new_file`): what someone worked on lately is the likelier question.

    Returns `{"examined": n, "queued": n}`. When the walk reaches the end of
    the tree the cursor resets, so the next pass starts over.
    """
    raw = get_config(conn, CURSOR_KEY)
    try:
        cursor = tuple(json.loads(raw)) if raw else ()
    except (TypeError, ValueError):
        cursor = ()

    known = list_scanned_file_paths(conn)
    scanner = Scanner()
    new_files: list[str] = []
    examined = 0
    last: Optional[str] = None
    exhausted = True
    listing = list_project_files(root, scanner, document_suffixes, settings, count_ignored=False)
    for relative in listing.files:
        if cursor and tuple(relative.split("/")) <= cursor:
            continue
        if examined >= budget:
            exhausted = False
            break
        examined += 1
        last = relative
        if relative not in known:
            new_files.append(relative)

    if new_files:
        cutoff = time.time() - recent_days * 86400.0

        def reason_for(relative: str) -> str:
            if document_suffixes and relative.lower().endswith(document_suffixes) and not scanner.is_supported(Path(relative)):
                return "new_doc"  # documents come after the code
            try:
                recent = (root / relative).stat().st_mtime >= cutoff
            except OSError:
                recent = False
            return RESCAN_REASON_NEW_FILE_RECENT if recent else RESCAN_REASON_NEW_FILE

        enqueue_staleness_many(conn, [(path, reason_for(path)) for path in new_files], overwrite=False)
    next_cursor = [] if exhausted or last is None else last.split("/")
    set_config(conn, CURSOR_KEY, json.dumps(next_cursor))
    return {"examined": examined, "queued": len(new_files)}
