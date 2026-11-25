"""Files that lost something to an older parser are read again when the parser changes.

A file that parsed only in part (`core.partial`) is read again only when it changes: its hash and time say nothing
about the parser that read it. A later parser may read the same file without loss, but the index keeps the old
result and a user sees "callers: none" for a function that was never indexed. So each partial record carries the
version of the parser that made it, and when the version changes the files whose record has an older version, or none,
are queued at background priority: a file with a syntax error the old parser could not get past may be read whole by
the new one, and one it only warned about may lose a function that the old one did not report. Nothing else is
touched: a file that parsed whole (it has no record) is not read again.

The version is a hash of the sources that decide what a file parse yields (the parsers, the scanner that finds the
skipped functions) and of the tree-sitter packages, so a release that does not change them reads nothing again.
"""

from __future__ import annotations

import hashlib
import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import duckdb

from drspec.core.partial import KEY_PREFIX, lost_something
from drspec.db.queries import enqueue_staleness_many, get_config, list_scanned_file_paths, set_config

logger = logging.getLogger(__name__)

#: The queue reason of the files read again because of the parser. It has the priority of a changed file (after what a
#: scan or a search asked for, before new files), and is not the reason of a scan, so a cancelled scan leaves it alone.
REASON = "parser_upgrade"

MARKER_KEY = "parser:requeued"
REREAD_KEY = "parser:reread"
MAX_RECORDED_PATHS = 20000

_PACKAGES = ("tree-sitter", "tree-sitter-python", "tree-sitter-javascript", "tree-sitter-typescript", "tree-sitter-cpp", "tree-sitter-rust")


@lru_cache(maxsize=1)
def _computed() -> str:
    digest = hashlib.sha256()
    package = Path(__file__).resolve().parent.parent
    sources = sorted((package / "parsers").glob("*.py")) + [package / "core" / "scanner.py"]
    read = 0
    for source in sources:
        try:
            digest.update(source.name.encode())
            digest.update(source.read_bytes())
            read += 1
        except OSError:
            continue
    if not read:  # a build that carries no sources: the release stands for them
        from drspec import __version__

        digest.update(("release:" + __version__).encode())
    try:
        from importlib import metadata

        for name in _PACKAGES:
            try:
                digest.update(f"{name}=={metadata.version(name)}".encode())
            except metadata.PackageNotFoundError:
                continue
    except ImportError:  # no importlib.metadata
        pass
    return digest.hexdigest()[:12]


def current() -> str:
    """The version of the parsers this process runs."""
    return _computed()


def _outdated(info: Dict[str, Any], version: str) -> bool:
    return info.get("parser") != version


def outdated_files(conn: duckdb.DuckDBPyConnection, version: Optional[str] = None) -> List[str]:
    """The files whose partial record was made by another parser (or by none that was recorded)."""
    version = version or current()
    found: List[str] = []
    for key, value in conn.execute("SELECT key, value FROM config WHERE starts_with(key, ?) ORDER BY key", [KEY_PREFIX]).fetchall():
        try:
            info = json.loads(value)
        except ValueError:
            continue
        if isinstance(info, dict) and _outdated(info, version):
            found.append(key[len(KEY_PREFIX):])
    return found


def requeue_outdated_partial_files(conn: duckdb.DuckDBPyConnection) -> int:
    """Queue the files that lost something to an older parser, once per parser version (the version is remembered when it
    was looked at). Returns how many were queued. Cheap when the version is the one already looked at: one lookup."""
    version = current()
    if get_config(conn, MARKER_KEY) == version:
        return 0
    known = set(list_scanned_file_paths(conn))
    files = [path for path in outdated_files(conn, version) if path in known]
    if files:
        enqueue_staleness_many(conn, [(path, REASON) for path in files], overwrite=False)
        logger.info("Queued %d file(s) that an older parser read in part for a re-read", len(files))
    set_config(conn, REREAD_KEY, json.dumps({"version": version, "paths": files[:MAX_RECORDED_PATHS], "queued": len(files)}))
    set_config(conn, MARKER_KEY, version)
    return len(files)


def reread_summary(conn: duckdb.DuckDBPyConnection) -> Dict[str, Any]:
    """What became of the files queued because of the parser, for `status`; empty when none was queued.

    `waiting` have not been read again yet; `now_clean` parsed whole; `read_with_warnings` have a syntax error but lost
    no function; `still_partial` still lose functions with the new parser."""
    raw = get_config(conn, REREAD_KEY)
    if not raw:
        return {}
    try:
        record = json.loads(raw)
    except ValueError:
        return {}
    paths: List[str] = record.get("paths", [])
    queued = record.get("queued", len(paths))
    if not queued:
        return {}
    records = _records_of(conn, paths)
    version = record.get("version")
    summary = {"parser_version": version, "files_queued": queued, "waiting": 0, "now_clean": 0, "read_with_warnings": 0, "still_partial": 0}
    for path in paths:
        info = records.get(path)
        if info is None:
            summary["now_clean"] += 1
        elif info.get("parser") != version:
            summary["waiting"] += 1
        elif lost_something(info):
            summary["still_partial"] += 1
        else:
            summary["read_with_warnings"] += 1
    if queued > len(paths):
        summary["files_not_followed"] = queued - len(paths)
    return summary


def _records_of(conn: duckdb.DuckDBPyConnection, paths: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    paths = list(paths)
    found: Dict[str, Dict[str, Any]] = {}
    for start in range(0, len(paths), 500):
        chunk = paths[start : start + 500]
        for key, value in conn.execute(
            f"SELECT key, value FROM config WHERE key IN ({', '.join('?' for _ in chunk)})", [KEY_PREFIX + path for path in chunk]
        ).fetchall():
            try:
                info = json.loads(value)
            except ValueError:
                continue
            if isinstance(info, dict):
                found[key[len(KEY_PREFIX):]] = info
    return found
