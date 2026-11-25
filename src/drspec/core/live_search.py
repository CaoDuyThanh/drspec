"""Live, grep-shaped line search over the files drspec has indexed.

Function matches from the database carry no line numbers and miss any
line outside a function body (module-level imports and constants). Line
mode instead reads the files themselves, so those lines are reachable,
and reports each hit as `file:line:text`. The database is only used to
pick which files to read and to name the function a hit sits in.

The files searched are the supported source files on disk under the
requested scope, plus any file the database still lists. Files drspec
doesn't scan (docs, config, unsupported languages) are out of reach. A
file added since the last scan is searched too, but can't be labelled,
and is listed so its absence from the graph isn't silent.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import duckdb

from drspec.core.scanner import Scanner
from drspec.core import usage
from drspec.core.index_notice import UNSCANNED_HINT
from drspec.core.staleness import drifted_file_paths, queue_files_for_rescan
from drspec.db.queries import (
    index_completeness,
    list_function_ranges,
    list_indexed_files,
    list_scanned_file_paths,
    stale_file_paths_among,
)

MAX_LISTED_UNSCANNED = 10

# A single line can be enormous (a minified bundle is one line), and a hit
# returns its whole line.
MAX_LINE_CHARS = 300


class InvalidPattern(ValueError):
    """The search pattern isn't a valid regex."""


def compile_pattern(pattern: str, *, ignore_case: bool, regex: bool) -> "re.Pattern[str]":
    flags = re.IGNORECASE if ignore_case else 0
    try:
        return re.compile(pattern if regex else re.escape(pattern), flags)
    except re.error as e:
        raise InvalidPattern(f"Invalid regex: {e}") from e


def _read_lines(path: Path) -> list[str]:
    """The file's lines as grep and editors count them: split on newline
    only. `str.splitlines()` also splits on form feed, vertical tab,
    U+2028 and others, which shifts every later line number (and the
    function ranges used for labelling come from a parser that counts
    newlines only)."""
    lines = path.read_bytes().decode("utf-8").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def _clip(line: str, compiled: "re.Pattern[str]") -> dict[str, Any]:
    """`{"text": ...}` for `line`, or, if it is longer than
    `MAX_LINE_CHARS`, a window of that width around the first match plus
    `line_length`, so the match stays visible."""
    if len(line) <= MAX_LINE_CHARS:
        return {"text": line}
    match = compiled.search(line)
    start = max(0, match.start() - MAX_LINE_CHARS // 3) if match else 0
    end = min(len(line), start + MAX_LINE_CHARS)
    start = max(0, end - MAX_LINE_CHARS)
    text = ("…" if start > 0 else "") + line[start:end] + ("…" if end < len(line) else "")
    return {"text": text, "line_length": len(line)}


def _enclosing_function(ranges: list[tuple[str, int, int]], line: int) -> Optional[str]:
    """The innermost function whose line range contains `line`."""
    best: Optional[tuple[str, int, int]] = None
    for function_id, start, end in ranges:
        if start <= line <= end and (best is None or (start, -end) > (best[1], -best[2])):
            best = (function_id, start, end)
    return best[0] if best else None


def _supported_files_on_disk(
    root: Path, language: Optional[str], path_prefix: Optional[str]
) -> set[str]:
    """Project-relative paths of the files a scan would process under the
    requested scope."""
    scanner = Scanner()
    base = root / path_prefix if path_prefix else root
    if base.is_file():
        candidates = [base.resolve()] if scanner.is_supported(base) else []
    elif base.is_dir():
        candidates = list(scanner.iter_supported_files(base))
    else:
        # a prefix that is a partial name, e.g. "src/dr": search its parent
        candidates = list(scanner.iter_supported_files(base.parent)) if base.parent.is_dir() else []

    resolved_root = root.resolve()
    found: set[str] = set()
    for path in candidates:
        try:
            relative = path.relative_to(resolved_root).as_posix()
        except ValueError:
            continue
        if path_prefix and not relative.startswith(path_prefix):
            continue
        if language is not None and scanner.detect_language(path) != language:
            continue
        found.add(relative)
    return found


def _merge_windows(matched: list[int], before: int, after: int, last_index: int) -> list[tuple[int, int, list[int]]]:
    """Per-match context windows merged where they overlap or touch, as
    grep does. Returns `(start_index, end_index, matched_indices)`."""
    windows = [(max(0, i - before), min(last_index, i + after), i) for i in matched]
    blocks: list[tuple[int, int, list[int]]] = []
    cur_start, cur_end, cur_matched = windows[0][0], windows[0][1], [windows[0][2]]
    for start, end, index in windows[1:]:
        if start <= cur_end + 1:
            cur_end = max(cur_end, end)
            cur_matched.append(index)
        else:
            blocks.append((cur_start, cur_end, cur_matched))
            cur_start, cur_end, cur_matched = start, end, [index]
    blocks.append((cur_start, cur_end, cur_matched))
    return blocks


def _files_waiting(conn: Any) -> bool:
    """Nothing is indexed yet, and either files are queued for their first scan or the worker has not yet looked
    through the project at all."""
    try:
        from drspec.core.discovery import CURSOR_KEY
        from drspec.db.queries import get_config

        status = index_completeness(conn)
        if status["total"] - status["pending"] > 0:
            return False  # something is indexed: a search that found no file in its scope searched nothing, and says so
        return status["pending"] > 0 or get_config(conn, CURSOR_KEY) is None
    except Exception:  # noqa: BLE001 - a notice must never fail a search
        return False


def known_languages() -> frozenset:
    """The `language` values a file can be indexed under (the scanner's own list), which `--include` takes."""
    from drspec.core.scanner import LANGUAGE_MAP

    return frozenset(LANGUAGE_MAP.values())


def search_scope(conn: Any, language: Optional[str], path_prefix: Optional[str]) -> dict[str, Any]:
    """What a function-level search could see: how many indexed files fall under the
    language and path asked for, and a note when that is none. Without it a path that
    matched nothing (a config file, a folder drspec never scanned) looks the same as a
    path that was searched and held no hits."""
    searched = len(list_indexed_files(conn, language=language, path_prefix=path_prefix))
    scope: dict[str, Any] = {"files_searched": searched}
    if searched == 0 and _files_waiting(conn):
        # Right after `init` the worker has not reached any file yet: that is not "nothing to search".
        status = index_completeness(conn)
        scope["index"] = status
        scanned = status["total"] - status["pending"]
        of = f" of {status['total']}" if status["total"] else ""
        scope["note"] = f"Index being built: {scanned}{of} files scanned; try again in a moment."
        return scope
    if searched == 0:
        scope["note"] = (
            "Nothing was searched: no file drspec has indexed matches this "
            + ("path" if path_prefix else "language filter" if language else "project")
            + ". drspec searches only the source files it scans (not docs, config or data files); "
            + "use grep for those."
        )
    from drspec.core.index_notice import add_index_notice

    return add_index_notice(conn, scope)


def live_search(
    conn: duckdb.DuckDBPyConnection,
    pattern: str,
    *,
    ignore_case: bool = False,
    regex: bool = False,
    language: Optional[str] = None,
    path_prefix: Optional[str] = None,
    limit: int = 50,
    before: int = 0,
    after: int = 0,
    root: Optional[Path] = None,
) -> dict[str, Any]:
    """Search the live contents of every indexed file (narrowed by
    `language`/`path_prefix`) and return up to `limit` matching lines.

    With `before`/`after` set, matches come back as merged context
    `blocks`; otherwise as a flat list of `hits`. A hit is labelled with
    its enclosing function, except in a file that has drifted since the
    last scan, where the stored line ranges can't be trusted.

    Raises `InvalidPattern` for a bad regex.
    """
    compiled = compile_pattern(pattern, ignore_case=ignore_case, regex=regex)
    root = root or Path.cwd()

    indexed = set(list_indexed_files(conn, language=language, path_prefix=path_prefix))
    on_disk = _supported_files_on_disk(root, language, path_prefix)
    unscanned = sorted(on_disk - indexed - list_scanned_file_paths(conn))

    found: list[tuple[str, list[str], list[int]]] = []
    unreadable: list[dict[str, str]] = []
    files_searched = 0
    total_found = 0
    for file_path in sorted(indexed | on_disk):
        try:
            source_lines = _read_lines(root / file_path)
        except (OSError, UnicodeDecodeError) as e:
            unreadable.append({"file_path": file_path, "error": str(e)})
            continue
        files_searched += 1

        matched = [i for i, line in enumerate(source_lines) if compiled.search(line)]
        if not matched:
            continue
        matched = matched[: limit + 1 - total_found]
        found.append((file_path, source_lines, matched))
        total_found += len(matched)
        if total_found > limit:
            break

    truncated = total_found > limit
    budget = limit
    kept: list[tuple[str, list[str], list[int]]] = []
    for file_path, source_lines, matched in found:
        matched = matched[:budget]
        if matched:
            kept.append((file_path, source_lines, matched))
            budget -= len(matched)

    paths = [file_path for file_path, _, _ in kept]
    stale = (stale_file_paths_among(conn, paths) | drifted_file_paths(conn, paths, root)) if paths else set()
    ranges = list_function_ranges(conn, [p for p in paths if p not in stale])

    def label(file_path: str, line: int) -> Optional[str]:
        return _enclosing_function(ranges.get(file_path, []), line)

    data: dict[str, Any] = {
        "pattern": pattern,
        "semantic": False,
        "files_searched": files_searched,
        "total_hits": sum(len(matched) for _, _, matched in kept),
        "truncated": truncated,
    }

    if before > 0 or after > 0:
        blocks = []
        for file_path, source_lines, matched in kept:
            for start, end, block_matched in _merge_windows(matched, before, after, len(source_lines) - 1):
                functions = []
                for index in block_matched:
                    function_id = label(file_path, index + 1)
                    if function_id is not None and function_id not in functions:
                        functions.append(function_id)
                blocks.append({
                    "file_path": file_path,
                    "start_line": start + 1,
                    "end_line": end + 1,
                    "matched_lines": [i + 1 for i in block_matched],
                    "functions": functions,
                    "context": [{"line": i + 1, **_clip(source_lines[i], compiled)} for i in range(start, end + 1)],
                })
        data["blocks"] = blocks
    else:
        data["hits"] = [
            {
                "file_path": file_path,
                "line": index + 1,
                **_clip(source_lines[index], compiled),
                "function_id": label(file_path, index + 1),
            }
            for file_path, source_lines, matched in kept
            for index in matched
        ]

    queue_files_for_rescan(
        conn,
        [(path, "search_drift") for path in sorted(stale)] + [(path, "search_unscanned") for path in unscanned],
    )

    labelled = [h["function_id"] for h in data.get("hits", []) if h["function_id"]]
    for block in data.get("blocks", []):
        labelled.extend(block["functions"])
    usage.record_usage(conn, usage.nodes(labelled))

    if unscanned:
        data["unscanned_files"] = {
            "count": len(unscanned),
            "examples": unscanned[:MAX_LISTED_UNSCANNED],
            "hint": UNSCANNED_HINT,
        }
    if stale:
        data["possibly_stale_files"] = sorted(stale)
    if unreadable:
        data["unreadable_files"] = unreadable
    return data
