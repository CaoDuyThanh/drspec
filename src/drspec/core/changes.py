"""What changed in a project since it was last indexed: added, changed and removed files, code and documents alike.

One check for every kind of file. A file is *changed* when its size or modification time differs from the snapshot
stored when it was last read (no file is opened), *added* when it has no snapshot, and *removed* when it has one but is
no longer part of the project (deleted, or now ignored). The worker's periodic sweep and `drspec scan` both use it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import duckdb

from drspec.core.docs import extensions
from drspec.core.project_files import ProjectFiles, list_project_files
from drspec.core.scanner import Scanner
from drspec.db.queries import list_scanned_files


@dataclass
class Changes:
    added: List[str] = field(default_factory=list)
    changed: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    unchanged: int = 0
    listing: ProjectFiles = field(default_factory=ProjectFiles)

    @property
    def to_read(self) -> List[str]:
        """Files that need reading: new and edited ones first, then removals."""
        return self.added + self.changed + self.removed

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.changed or self.removed)


def document_suffixes(settings: Dict[str, Any]) -> tuple:
    return extensions(settings) if settings.get("docs.enabled", False) else ()


def detect_changes(
    conn: duckdb.DuckDBPyConnection,
    root: Path,
    settings: Dict[str, Any],
    scanner: Optional[Scanner] = None,
    in_scope: Optional[Callable[[str], bool]] = None,
    full: bool = False,
    count_ignored: bool = True,
    only: Optional[List[str]] = None,
) -> Changes:
    """Compare the project on disk with what is stored.

    `in_scope` narrows the answer to the files a scan was asked about; `only` (the paths themselves) also narrows the
    listing, so that a scan of one file does not list the whole project. With `full`, every file in scope is
    reported as changed (an unchanged one is not skipped)."""
    suffixes = document_suffixes(settings)
    listing = list_project_files(root, scanner, suffixes, settings, count_ignored=count_ignored, only=only)
    stored = {path: (mtime, size) for path, mtime, size in list_scanned_files(conn)}
    docs_off = not settings.get("docs.enabled", False)
    doc_suffixes = extensions(settings)

    def wanted(path: str) -> bool:
        return in_scope is None or in_scope(path)

    changes = Changes(listing=listing)
    present = set(listing.files)
    for path in listing.files:
        if not wanted(path):
            continue
        snapshot = stored.get(path)
        if snapshot is None:
            changes.added.append(path)
            continue
        if full:
            changes.changed.append(path)
            continue
        try:
            stat = os.stat(root / path)
        except OSError:
            continue
        if stat.st_size != snapshot[1] or stat.st_mtime != snapshot[0]:
            changes.changed.append(path)
        else:
            changes.unchanged += 1
    for path in sorted(stored):
        if path in present or not wanted(path):
            continue
        if docs_off and path.lower().endswith(doc_suffixes):
            continue  # documents are switched off, not gone
        if listing.left_the_project(root, path):
            changes.removed.append(path)
    # A file with functions in the index but no row of `scanned_files` is not found through the rows above: its functions would stay for
    # ever (a file created and deleted between a worker's read and its publish leaves exactly this). It is removed like any other.
    known = set(stored) | present
    for (path,) in conn.execute("SELECT DISTINCT file_path FROM artifacts").fetchall():
        if path in known or not wanted(path) or path in changes.removed:
            continue
        if listing.left_the_project(root, path):
            changes.removed.append(path)
    changes.removed.sort()
    return changes
