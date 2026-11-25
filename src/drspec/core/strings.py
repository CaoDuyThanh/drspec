"""`strings get <key>`: who sets a key of a YAML config file, and which code reads it.

The yaml side is the `config_keys` table (written by the scan). The code side is read at query time: the project's code files are
read as they are now, once per query, and the exact string literals that equal the key are found (comments left out; the key also
written with dots). Each site names the function it is in, or the module level. A literal built at run time (`"avoidance/" + name`) and
a key read through a launch file or a namespace are not found. A key whose first one or two segments are a prefix added at run time
(`high/` + `robot/speed/max`) gets a possible prefix site: a literal equal to the rest of the key, which is not counted as a read.
The answer says what it can and cannot see.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import duckdb

from drspec.core import config_keys
from drspec.core.changed_notice import add_changed_notice
from drspec.core.config_keys import key_exists_under, normalize_key
from drspec.db.queries import list_scanned_file_paths

#: The most hints of a possible prefix a key gets (a literal equal to the rest of the key, in code).
MAX_PREFIX_HINTS = 3
MODULE_LEVEL = "(module level)"


def split_key(text: str) -> tuple[str, bool]:
    """`(normalized key, is_a_prefix)`: a key ending with `/` asks for the sub-keys under it."""
    prefix = text.strip().endswith("/")
    return normalize_key(text), prefix


def tails_of(key: str) -> List[str]:
    """The keys the given key is made of when its first one or two segments are a prefix added at run time: `high/robot/speed/max` gives
    `robot/speed/max` and `speed/max`. A tail needs at least two segments, so a single word is never a hint."""
    segments = key.rstrip("/").split("/")
    tails = []
    for drop in (1, 2):
        if len(segments) - drop >= 2:
            tails.append("/".join(segments[drop:]))
    return tails


def _needles(keys: Set[str]) -> List[bytes]:
    """The bytes a file must hold for one of the keys to be in it (the key, and the key written with dots)."""
    found: Set[bytes] = set()
    for key in keys:
        found.add(key.encode("utf-8"))
        found.add(key.replace("/", ".").encode("utf-8"))
    return sorted(found)


def _functions_by_file(conn: duckdb.DuckDBPyConnection, file_paths: List[str]) -> Dict[str, List[tuple]]:
    """The indexed functions of each file: `(start_line, end_line, function_id, function_name)`, to name the function a line is in."""
    found: Dict[str, List[tuple]] = {}
    for start in range(0, len(file_paths), 400):
        part = file_paths[start : start + 400]
        marks = ", ".join("?" for _ in part)
        for path, start_line, end_line, function_id, function_name, parent in conn.execute(
            f"SELECT file_path, start_line, end_line, function_id, function_name, parent FROM artifacts WHERE file_path IN ({marks})", part
        ).fetchall():
            qualified = f"{parent}::{function_name}" if parent else function_name  # (the name the ids, `refs get` and `deps get` print)
            found.setdefault(path, []).append((start_line or 0, end_line or start_line or 0, function_id, qualified))
    return found


def _enclosing(functions: List[tuple], line: int) -> tuple:
    """`(function_id, function_name)` of the innermost indexed function that holds `line`, or the module level when none does."""
    best = None
    for start, end, function_id, function_name in functions:
        if start <= line <= end and (best is None or (end - start) < (best[1] - best[0])):
            best = (start, end, function_id, function_name)
    return (best[2], best[3]) if best else (None, MODULE_LEVEL)


def code_literals(
    conn: duckdb.DuckDBPyConnection,
    root: Optional[Path],
    keys: Set[str],
    needles: List[bytes],
    per_key: int,
) -> Tuple[Dict[str, Dict[str, Any]], int]:
    """The string literals of the project's code that equal one of `keys`, in ONE read of the code files: `({key: {"total", "sites"}}, files
    read)`. A file is read only when it holds one of `needles` (the keys' bytes, or a prefix's for a listing); each key keeps its first
    `per_key` sites: the function it is in (or the module level), the line and the line of code."""
    from drspec.core.scanner import Scanner

    found: Dict[str, Dict[str, Any]] = {key: {"total": 0, "sites": []} for key in keys}
    if root is None or not keys:
        return found, 0
    scanner = Scanner()
    paths = sorted(path for path in list_scanned_file_paths(conn) if scanner.detect_language(path) is not None)
    hit_files: List[tuple] = []
    read = 0
    for path in paths:
        try:
            with open(root / path, "rb") as stream:
                data = stream.read()
        except OSError:
            continue
        read += 1
        if not any(needle in data for needle in needles):
            continue
        text = data.decode("utf-8", errors="replace")
        matches = config_keys.literal_matches(text, scanner.detect_language(path) or "", keys)
        if matches:
            hit_files.append((path, text, matches))
    functions = _functions_by_file(conn, [path for path, _text, _matches in hit_files])
    for path, text, matches in hit_files:
        lines = text.split("\n")
        for literal, offset in matches:
            entry = found[literal]
            entry["total"] += 1
            if len(entry["sites"]) < per_key:
                line = text.count("\n", 0, offset) + 1
                function_id, function_name = _enclosing(functions.get(path, []), line)
                entry["sites"].append({"function_id": function_id, "function_name": function_name, "file_path": path, "line": line, "source": lines[line - 1].strip()})
    return found, read


def _like_escaped(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _leaves(conn: duckdb.DuckDBPyConnection, root: Optional[Path], prefix: str, limit: int, result: Dict[str, Any]) -> None:
    """Every leaf key under `prefix` (its yaml value and site, and the first code site that reads it, or none): the first `limit` of them,
    with the code read once for all of them. `leaves_total` counts all of them."""
    rows = conn.execute(
        "SELECT key, value, file_path, line FROM config_keys WHERE key LIKE ? ESCAPE '\\' ORDER BY key, file_path, line",
        [_like_escaped(prefix) + "%"],
    ).fetchall()
    shown = rows[:limit]
    found, files_read = code_literals(conn, root, {row[0] for row in shown}, _needles({prefix, prefix.replace("/", ".")}), 1)
    leaves = []
    for key, value, path, line in shown:
        sites = found.get(key, {"sites": []})["sites"]  # the first code site that reads the leaf, flat (a table cell holds it as it is)
        first = sites[0] if sites else None
        leaves.append({
            "key": key, "value": value, "file_path": path, "line": line,
            "read_file": first["file_path"] if first else None,
            "read_function": first["function_name"] if first else None,
            "read_line": first["line"] if first else None,
        })
    result.update({"leaves_total": len(rows), "leaves": leaves, "code_files_read": files_read})
    if len(rows) > limit:
        more = len(rows) - limit
        result["leaves_more"] = more
        result["leaves_note"] = f"{more} more leaves: narrow the prefix (a longer key) or raise --limit."


def get_strings(
    conn: duckdb.DuckDBPyConnection,
    text: str,
    limit: int = 50,
    root: Optional[Path] = None,
    problems: bool = False,
) -> Dict[str, Any]:
    """The YAML sites and the code sites of one key. A key ending with `/` lists each leaf under it with its value and the code that reads
    it. `problems`: list the yaml files that could not be read (otherwise only their count)."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    key, prefix = split_key(text)
    if not key:
        raise ValueError("Missing the key: give a key path such as avoidance/timeout (or avoidance/ for its sub-keys).")
    result: Dict[str, Any] = {"key": key}
    # yaml files a scan met without PyYAML: whether a key is set in them is not known (the counts of every file are in `status`)
    unread = config_keys.config_counts(conn)["files_without_pyyaml"]
    unread_yaml = unread > 0
    if unread_yaml:
        result["files_without_pyyaml"] = unread
    if prefix:
        groups, _sub = config_keys.sub_keys(conn, key, limit)
        result["sub_keys_total"] = groups  # (the groups are the leaves' prefixes: the leaves below say it all)
        _leaves(conn, root, key, limit, result)
        note = f"'{key}' has {groups} sub-key group(s) in yaml files." if groups else f"No key is set under '{key}' in any yaml file."
        if result.get("leaves_note"):
            note = f"{note} {result['leaves_note']}"
        result["note"] = note
    else:
        yaml_total, yaml_sites = config_keys.key_sites(conn, key, limit)
        tails = tails_of(key)
        found, files_read = code_literals(conn, root, {key, *tails}, _needles({key, *tails}), limit)
        code_total = found[key]["total"]
        result.update({
            "yaml_total": yaml_total, "yaml_sites": yaml_sites, "code_total": code_total, "code_sites": found[key]["sites"],
            "code_files_read": files_read,
        })
        hints = [dict(site, literal=tail) for tail in tails for site in found[tail]["sites"]][:MAX_PREFIX_HINTS] if code_total == 0 else []
        if hints:
            result["possible_prefix_sites"] = hints  # hints only: not counted as reads (`code_total` is 0 here)
        hint_note = " A literal equal to part of this key is in code (possible_prefix_sites): a prefix may be added at run time." if hints else ""
        if not yaml_total and not code_total and key_exists_under(conn, key + "/"):
            # (a mapping in yaml: its leaves are the keys, so the answer lists them, as for a prefix)
            groups, _sub = config_keys.sub_keys(conn, key + "/", limit)
            result["sub_keys_total"] = groups
            _leaves(conn, root, key + "/", limit, result)
            result["note"] = f"'{key}' is a mapping in yaml with {groups} sub-key group(s); its leaves are listed."
            if result.get("leaves_note"):
                result["note"] = f"{result['note']} {result['leaves_note']}"
        elif unread_yaml and not yaml_total:
            result["note"] = (
                "not known whether it is set in yaml: the yaml files were not read, because PyYAML is not installed "
                "(pip install pyyaml, then scan again)." + hint_note
            )
        elif yaml_total and not code_total:
            result["note"] = "set in yaml; no string literal equal to this key was found in code (a key built at run time is not found)." + hint_note
        elif code_total and not yaml_total:
            result["note"] = "read in code, not set in any yaml file (it may be a parameter set outside the project, or in a launch file, which is not read)."
        elif not yaml_total and not code_total:
            result["note"] = "No yaml file sets this key, and no string literal equal to it was found in code." + hint_note
    if unread_yaml:
        result["pyyaml_note"] = config_keys.YAML_MISSING + "; the yaml files that were met are counted in problem_files_total."
    _problem_files(conn, result, limit, problems)
    return result


def _problem_files(conn: duckdb.DuckDBPyConnection, result: Dict[str, Any], limit: int, problems: bool) -> None:
    """The yaml files that could not be read: their count and one line on how to list them; the list itself only when asked for."""
    found = config_keys.problem_files(conn)
    if not found:
        return
    result["problem_files_total"] = len(found)
    result["problem_note"] = (
        f"{len(found)} yaml file(s) could not be read (not parsed, too large, or PyYAML missing): their keys are not indexed. "
        "`drspec strings get --problems` lists them."
    )
    if problems:
        result["problem_files"] = found[:limit]
        if len(found) > limit:
            result["problem_files_more"] = len(found) - limit


def add_strings_notice(conn: duckdb.DuckDBPyConnection, data: Dict[str, Any], root: Optional[Any], before: Any = None) -> Dict[str, Any]:
    """The `changed since the last scan` notice for the yaml files and the functions' files the answer lists."""
    files = [site["file_path"] for site in data.get("yaml_sites", [])] + [site["file_path"] for site in data.get("code_sites", [])]
    files += [site["file_path"] for site in data.get("possible_prefix_sites", [])]
    for leaf in data.get("leaves", []):
        files.append(leaf["file_path"])
        if leaf.get("read_file"):
            files.append(leaf["read_file"])
    return add_changed_notice(conn, data, root, files, before=before)
