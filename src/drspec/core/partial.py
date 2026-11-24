"""Files that parsed only in part.

A file with a syntax error the grammar cannot get past still has functions that
parsed cleanly. Those are indexed; the functions whose own text holds the error
are left out, and nothing already stored for the file is removed (a half-typed
edit must never read as "every function was deleted"). Which files are in that
state, and how much of each was skipped, is kept here so `scan` and `status` can
say so.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List

import duckdb

from drspec.db.queries import list_scanned_file_paths, set_config_many

KEY_PREFIX = "partial:"
MAX_EXAMPLES = 5


def record_partial_files(conn: duckdb.DuckDBPyConnection, scanned: Iterable[str], partial: Dict[str, Dict[str, Any]]) -> None:
    """For every file scanned: remember what was skipped if it was partial, forget it otherwise."""
    scanned = sorted(set(scanned) | set(partial))
    clear = [path for path in scanned if path not in partial]
    for start in range(0, len(clear), 500):
        chunk = clear[start : start + 500]
        conn.execute(f"DELETE FROM config WHERE key IN ({', '.join('?' for _ in chunk)})", [KEY_PREFIX + path for path in chunk])
    from drspec.core import parser_version  # (it reads this module too)

    version = parser_version.current()  # the parser that made the record: a later one may read the file without the loss
    set_config_many(conn, [(KEY_PREFIX + path, json.dumps({**info, "parser": version})) for path, info in partial.items()])


SKIPPED_PREFIX = "skipped:"


def record_skipped_constructs(conn: duckdb.DuckDBPyConnection, scanned: Iterable[str], by_file: Dict[str, int]) -> None:
    """How many harmless constructs the parser skipped in each file scanned (forgotten when none)."""
    scanned = sorted(set(scanned) | set(by_file))
    clear = [path for path in scanned if path not in by_file]
    for start in range(0, len(clear), 500):
        chunk = clear[start : start + 500]
        conn.execute(f"DELETE FROM config WHERE key IN ({', '.join('?' for _ in chunk)})", [SKIPPED_PREFIX + path for path in chunk])
    set_config_many(conn, [(SKIPPED_PREFIX + path, str(count)) for path, count in by_file.items()])


def parse_problems(conn: duckdb.DuckDBPyConnection, files: Iterable[str]) -> List[Dict[str, Any]]:
    """The files among `files` that the parser had problems with (a syntax error it recovered from: a record of `partial:`), each with
    the line of the first error, the functions it skipped or kept in part, and the constructs it skipped, and one line that says what
    that means for what is stored of it. A clean file has no entry. Used where "nothing recorded" must be told from "never done"."""
    files = sorted(set(files))
    values: Dict[str, str] = {}
    for start in range(0, len(files), 400):
        keys = [prefix + path for path in files[start : start + 400] for prefix in (KEY_PREFIX, SKIPPED_PREFIX)]
        values.update(dict(conn.execute(f"SELECT key, value FROM config WHERE key IN ({', '.join('?' for _ in keys)})", keys).fetchall()))
    found: List[Dict[str, Any]] = []
    for path in files:
        raw = values.get(KEY_PREFIX + path)
        if raw is None:
            continue
        try:
            info = json.loads(raw)
        except (ValueError, TypeError):
            continue
        constructs = int(values.get(SKIPPED_PREFIX + path) or 0)
        skipped, partial_count, line = len(info.get("skipped", [])), len(info.get("partial", [])), info.get("first_error_line")
        found.append({
            "file": path,
            "first_error_line": line,
            "functions_skipped": skipped,
            "functions_partial": partial_count,
            "skipped_constructs": constructs,
            "note": (
                f"{path} had parse problems (first error at line {line}; {skipped} function(s) skipped, {partial_count} read in part, "
                f"{constructs} construct(s) skipped): an access in it may be missing, so no access recorded is not proof of none"
            ),
        })
    return found


def forget_unknown_files(conn: duckdb.DuckDBPyConnection) -> int:
    """Remove the partial and skipped-construct records of files the index does not know (not in `scanned_files`): a file that was
    deleted or became ignored before its records were removed with it. Run at the worker's start. Nothing is removed from a database
    whose `scanned_files` is empty (a project not scanned by this version: its files are not known yet, not gone). Returns how
    many records were removed."""
    known = list_scanned_file_paths(conn)
    if not known:
        return 0
    removed = 0
    for prefix in (KEY_PREFIX, SKIPPED_PREFIX):
        stale = [key for (key,) in conn.execute("SELECT key FROM config WHERE starts_with(key, ?)", [prefix]).fetchall() if key[len(prefix):] not in known]
        for start in range(0, len(stale), 500):
            chunk = stale[start : start + 500]
            conn.execute(f"DELETE FROM config WHERE key IN ({', '.join('?' for _ in chunk)})", chunk)
        removed += len(stale)
    return removed


def _loss(info: Dict[str, Any]) -> int:
    return len(info.get("skipped", [])) + len(info.get("partial", []))


def lost_something(info: Dict[str, Any]) -> bool:
    """Whether a function of the file was left out or kept although only partly read. A file with a syntax error and no
    such function is *read with warnings*: nothing stored for it is removed (the error may hide something), but it is
    not counted as partially parsed."""
    return _loss(info) > 0


def worst_files(infos: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """The files that lose the most, worst first: `{file, first_error_line, functions_skipped, functions_partial}`.
    Only files that lost something are listed."""
    ranked = sorted((info for info in infos if lost_something(info)), key=lambda info: (-_loss(info), info["file"]))
    return [
        {
            "file": info["file"],
            "first_error_line": info.get("first_error_line"),
            "functions_skipped": len(info.get("skipped", [])),
            "functions_partial": len(info.get("partial", [])),
        }
        for info in ranked[:limit]
    ]


def totals(infos: List[Dict[str, Any]]) -> Dict[str, int]:
    """`files` counts the files that lost something; the others are `files_read_with_warnings`."""
    lossy = [info for info in infos if lost_something(info)]
    return {
        "files": len(lossy),
        "files_read_with_warnings": len(infos) - len(lossy),
        "functions_skipped": sum(len(info.get("skipped", [])) for info in infos),
        "functions_partial": sum(len(info.get("partial", [])) for info in infos),
    }


MAX_LISTED_FILES = 100


def partial_summary(conn: duckdb.DuckDBPyConnection, limit: int = MAX_EXAMPLES) -> Dict[str, Any]:
    """`{"files", "functions_skipped", "functions_partial", "examples": [the files that lose the most]}`, and the whole
    list (capped, with the cap stated) as `all_files`; empty when every file parsed fully. A function is *skipped* when
    it could not even be recognised as one, *partial* when it was kept although its body was read in part."""
    rows = conn.execute("SELECT key, value FROM config WHERE starts_with(key, ?) ORDER BY key", [KEY_PREFIX]).fetchall()
    infos: List[Dict[str, Any]] = []
    for key, value in rows:
        try:
            info = json.loads(value)
        except ValueError:
            continue
        info["file"] = key[len(KEY_PREFIX):]
        infos.append(info)
    lossy = [info for info in infos if lost_something(info)]
    if not lossy:
        return {}
    summary: Dict[str, Any] = {**totals(infos), "examples": worst_files(infos, limit)}
    if not summary["files_read_with_warnings"]:
        summary.pop("files_read_with_warnings")
    summary["all_files"] = worst_files(infos, MAX_LISTED_FILES)
    if len(lossy) > MAX_LISTED_FILES:
        summary["all_files_omitted"] = len(lossy) - MAX_LISTED_FILES
    return summary


def read_with_warnings(conn: duckdb.DuckDBPyConnection) -> int:
    """How many files have a syntax error somewhere but lost no function to it."""
    count = 0
    for (value,) in conn.execute("SELECT value FROM config WHERE starts_with(key, ?)", [KEY_PREFIX]).fetchall():
        try:
            count += 0 if lost_something(json.loads(value)) else 1
        except ValueError:
            continue
    return count


def partial_function_ids(conn: duckdb.DuckDBPyConnection, file_paths: Iterable[str]) -> set:
    """The ids of the functions among those files that were kept although read in part."""
    paths = sorted(set(file_paths))
    found: set = set()
    for start in range(0, len(paths), 500):
        chunk = paths[start : start + 500]
        for _key, value in conn.execute(
            f"SELECT key, value FROM config WHERE key IN ({', '.join('?' for _ in chunk)})", [KEY_PREFIX + path for path in chunk]
        ).fetchall():
            try:
                found.update(item["function_id"] for item in json.loads(value).get("partial", []) if item.get("function_id"))
            except ValueError:
                continue
    return found


# --- what was left out, for the commands that look a function up -------------------------------------------------


def _last_read(conn: duckdb.DuckDBPyConnection, paths: Iterable[str]) -> Dict[str, Any]:
    """When each file was last read (`scanned_files.last_scanned_at`), as a timestamp."""
    paths = sorted(set(paths))
    found: Dict[str, Any] = {}
    for start in range(0, len(paths), 500):
        chunk = paths[start : start + 500]
        for path, when in conn.execute(
            f"SELECT file_path, last_scanned_at FROM scanned_files WHERE file_path IN ({', '.join('?' for _ in chunk)})", chunk
        ).fetchall():
            found[path] = when
    return found


def _records(conn: duckdb.DuckDBPyConnection) -> List[Dict[str, Any]]:
    """Every partial record that lost something, with its file (`file`) and when the file was last read (`last_read`)."""
    records: List[Dict[str, Any]] = []
    for key, value in conn.execute("SELECT key, value FROM config WHERE starts_with(key, ?) ORDER BY key", [KEY_PREFIX]).fetchall():
        try:
            info = json.loads(value)
        except ValueError:
            continue
        if isinstance(info, dict) and lost_something(info):
            info["file"] = key[len(KEY_PREFIX):]
            records.append(info)
    when = _last_read(conn, (record["file"] for record in records))
    for record in records:
        stamp = when.get(record["file"])
        record["last_read"] = stamp.isoformat(sep=" ", timespec="seconds") if hasattr(stamp, "isoformat") else (str(stamp) if stamp else None)
    return records


def partly_parsed_count(conn: duckdb.DuckDBPyConnection) -> int:
    """How many files lost a function to a parse error (the files `status` counts as partially parsed)."""
    count = 0
    for (value,) in conn.execute("SELECT value FROM config WHERE starts_with(key, ?)", [KEY_PREFIX]).fetchall():
        try:
            count += 1 if lost_something(json.loads(value)) else 0
        except (ValueError, TypeError, AttributeError):
            continue
    return count


#: The skipped names a file shows in the text view of `status --partial` (the JSON view keeps every name).
MAX_NAMES_IN_TEXT = 10


def cap_names_for_text(result: Dict[str, Any], most: int = MAX_NAMES_IN_TEXT) -> Dict[str, Any]:
    """`partial_files(...)` for the text view: each file's `skipped` list is cut at `most` names and ends with `... and N more (use
    --json for all)`, so one file that lost hundreds of functions does not push the others off the screen. The counts stay whole."""
    files = []
    for row in result.get("files", []):
        names = row.get("skipped", [])
        if len(names) > most:
            row = {**row, "skipped": [*names[:most], f"... and {len(names) - most} more (use --json for all)"]}
        files.append(row)
    return {**result, "files": files}


def partial_files(conn: duckdb.DuckDBPyConnection, limit: int = 0) -> Dict[str, Any]:
    """All the files that lost something, worst first: `{total, shown, files: [{file, first_error_line, skipped: [names],
    functions_skipped, functions_partial, last_read}], files_read_with_warnings}`. `limit` 0 lists all."""
    records = _records(conn)
    records.sort(key=lambda record: (-_loss(record), record["file"]))
    shown = records[:limit] if limit else records
    rows = [
        {
            "file": record["file"],
            "first_error_line": record.get("first_error_line"),
            "skipped": [item.get("name") for item in record.get("skipped", [])],
            "functions_skipped": len(record.get("skipped", [])),
            "functions_partial": len(record.get("partial", [])),
            "last_read": record["last_read"],
        }
        for record in shown
    ]
    result: Dict[str, Any] = {"total": len(records), "shown": len(rows), "files": rows}
    warnings = read_with_warnings(conn)
    if warnings:
        result["files_read_with_warnings"] = warnings
    return result


def _boundary_suffix(name: str, target: str) -> bool:
    """Whether `target` is `name`, or the end of it after a scope separator (`update` in `Cls::update` and in `Cls.update`)."""
    return name == target or any(name.endswith(separator + target) for separator in ("::", ".", "->"))


def _entry(record: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, Any]:
    """One skipped function: where it was, why it is not indexed, and when the file was read."""
    read_on = (record.get("last_read") or "")[:10] or None
    at = record.get("first_error_line") or item.get("line")
    entry: Dict[str, Any] = {
        "name": item.get("name"),
        "file_path": record["file"],
        "line": item.get("line"),
        "first_error_line": record.get("first_error_line"),
        "read_on": read_on,
    }
    entry["message"] = (
        f"not indexed: skipped by a parse error at line {at} of {record['file']}" + (f" (read on {read_on})" if read_on else "")
        if at
        else f"not indexed: skipped by a parse error in {record['file']}" + (f" (read on {read_on})" if read_on else "")
    )
    return entry


def skipped_functions(conn: duckdb.DuckDBPyConnection, matches) -> List[Dict[str, Any]]:
    """The skipped functions (`{name, file_path, line, first_error_line, read_on, message}`) whose name `matches(name, file)`."""
    found = []
    for record in _records(conn):
        for item in record.get("skipped", []):
            if item.get("name") and matches(str(item["name"]), record["file"]):
                found.append(_entry(record, item))
    return sorted(found, key=lambda entry: (entry["file_path"], entry["line"] or 0, entry["name"]))


def skipped_matching_pattern(conn: duckdb.DuckDBPyConnection, pattern: str, regex: bool = False, ignore_case: bool = False) -> List[Dict[str, Any]]:
    """The skipped functions a `search` for `pattern` would have found had they been indexed."""
    import re

    if not pattern:
        return []
    if regex:
        try:
            compiled = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error:
            return []
        return skipped_functions(conn, lambda name, _file: bool(compiled.search(name)))
    needle = pattern.lower() if ignore_case else pattern
    return skipped_functions(conn, lambda name, _file: needle in (name.lower() if ignore_case else name))


def skipped_for_id(conn: duckdb.DuckDBPyConnection, function_id: str) -> List[Dict[str, Any]]:
    """The skipped functions a function name or id (`file::Class::name#hash`, `Class::name`, `name`) stands for."""
    text = str(function_id or "").split("#", 1)[0]
    if not text:
        return []
    files = {record["file"] for record in _records(conn)}
    file_path = None
    target = text
    parts = text.split("::")
    for cut in range(len(parts) - 1, 0, -1):  # the longest leading part that is a file with a skipped function
        if "::".join(parts[:cut]) in files:
            file_path, target = "::".join(parts[:cut]), "::".join(parts[cut:])
            break
    return skipped_functions(conn, lambda name, path: (file_path is None or path == file_path) and _boundary_suffix(name, target))


MAX_NOT_INDEXED = 20


def add_not_indexed(conn: Any, data: Dict[str, Any], entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """`data` with `not_indexed` (the skipped functions that match, at most `MAX_NOT_INDEXED`) and its first message
    as `not_indexed_note`; unchanged when there are none."""
    if not entries:
        return data
    data["not_indexed"] = entries[:MAX_NOT_INDEXED]
    if len(entries) > MAX_NOT_INDEXED:
        data["not_indexed_omitted"] = len(entries) - MAX_NOT_INDEXED
    data["not_indexed_note"] = (
        entries[0]["message"] if len(entries) == 1 else f"{len(entries)} function(s) matching this were skipped by a parse error and are not indexed (first: {entries[0]['message']})"
    )
    return data


def not_found_with_reason(conn: duckdb.DuckDBPyConnection, message: str, function_id: str, details: Dict[str, Any]) -> tuple:
    """`(message, details)` of a lookup that found no function: when the name or id is in a skipped list, the message says
    the function is not indexed and why, and `details` carries the entries."""
    entries = skipped_for_id(conn, function_id)
    if not entries:
        return message, details
    add_not_indexed(conn, details, entries)
    return f"{message.rstrip('.')}. {details['not_indexed_note']}", details


def add_callers_caveat(conn: duckdb.DuckDBPyConnection, data: Dict[str, Any], direction: str) -> Dict[str, Any]:
    """`data` of a `deps get`, with how many files of the project are partly parsed when callers were asked for: a caller
    skipped in one of them is not in the list, so "no callers" is not "none". Unchanged when no file is."""
    if direction == "callees":
        return data
    count = partly_parsed_count(conn)
    if not count:
        return data
    note = f"{count} file(s) in this project are partly parsed: a caller skipped there is not in this list (drspec status --partial lists them)"
    result: Dict[str, Any] = {}
    after = "unresolved_callers" if "unresolved_callers" in data else "callers"
    for key, value in data.items():
        result[key] = value
        if key == after:
            result["partially_parsed_files"] = count
            result["callers_note"] = note
    if "callers_note" not in result:
        result["partially_parsed_files"], result["callers_note"] = count, note
    return result
