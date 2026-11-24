"""Calls that were not tied to a function: why, and who they might be calls to.

An empty list of callers reads as "nobody calls this", which is false when a call site names the function but could
not be resolved (the receiver's type was not known, the name is defined in several places). Two things here:

* the reason "not found" is split by what the project defines under that name: nothing (a library or system call),
  one function (our own function, but the receiver or the base class is not known to the resolver), or several;
* `unresolved_callers` looks the stored calls up by the called name and says how many call sites in how many
  functions name the function, and where.
"""

from __future__ import annotations

import json
import zlib
from typing import Any, Dict, Iterable, List, Optional

import duckdb

from drspec.core.file_records import packed_functions
from drspec.core.languages import family_of, suffixes_of

NO_DEFINITION = "no definition in the project"
RECEIVER_UNKNOWN = "defined in the project, receiver unknown"
SEVERAL_DEFINITIONS = "defined in several places"

#: Most call sites listed in a result; the counts are always complete.
MAX_SITES = 50

_SEPARATORS = ("::", "->", ".")


def called_name(name: str) -> str:
    """The name a call ends in: `Base::baseWork` and `obj.baseWork` call `baseWork`."""
    name = str(name or "")
    for separator in _SEPARATORS:
        if separator in name:
            name = name.rsplit(separator, 1)[1]
    return name


def definition_counts(conn: duckdb.DuckDBPyConnection) -> Dict[str, int]:
    """How many places the project defines each function name in. A place is a class (or namespace), or for a function
    with none the file without its suffix, so a declaration in `a.h` and its definition in `a.cpp` are one place and an
    override in a subclass is another."""
    return {
        name: int(places)
        for name, places in conn.execute(
            "SELECT function_name, COUNT(DISTINCT COALESCE(parent, regexp_replace(file_path, '\\.[^./]+$', ''))) FROM artifacts GROUP BY function_name"
        ).fetchall()
    }


def refine_not_found(name: str, counts: Dict[str, int]) -> str:
    """What a call that was not found is, by the name it calls: (a) no definition of it in the project (a library or
    system call), (b) one place defines it, so the call is to our own function and only the receiver is unknown, (c)
    several places define it."""
    places = counts.get(called_name(name), 0)
    if not places:
        return NO_DEFINITION
    return RECEIVER_UNKNOWN if places == 1 else SEVERAL_DEFINITIONS


def _chunks(items: List[Any], size: int = 400) -> Iterable[List[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _candidate_files(conn: duckdb.DuckDBPyConnection, name: str, suffixes: tuple) -> List[str]:
    """The files of the family that may call `name`: those whose stored mentions include it, and every file whose
    mentions are not known (a language whose calls resolve through other files' exports mentions everything)."""
    mentioned: Dict[str, Optional[str]] = {}
    for key, value in conn.execute("SELECT key, value FROM config WHERE starts_with(key, 'scan:mentions:')").fetchall():
        mentioned[key[len("scan:mentions:"):]] = value
    files = [key[len("scan:file:"):] for (key,) in conn.execute("SELECT key FROM config WHERE starts_with(key, 'scan:file:')").fetchall()]
    chosen = []
    for path in files:
        if not path.endswith(suffixes):
            continue
        raw = mentioned.get(path)
        if raw is None:
            chosen.append(path)
            continue
        try:
            record = json.loads(raw)
            if record.get("all") or name in record.get("n", []):
                chosen.append(path)
        except (ValueError, AttributeError, TypeError):
            chosen.append(path)
    return sorted(chosen)


def _lines_of(body: str, start_line: int, text: str) -> List[int]:
    """The lines of a function's stored source that hold `text`."""
    return [start_line + number for number, line in enumerate(str(body or "").split("\n")) if text and text in line]


def unresolved_callers(conn: duckdb.DuckDBPyConnection, function_id: str, function_name: str, language: str) -> Optional[Dict[str, Any]]:
    """The call sites that name `function_name` and were not tied to any function, in the functions of the same family
    of languages: `{name, call_sites, functions, message, sites: [{function_id, file_path, line, call, reason}]}`; `None`
    when no such call site is stored. Only meant for a function with no resolved caller."""
    family = family_of(language)
    suffixes = suffixes_of(family)
    if not suffixes:
        return None
    matches: List[Dict[str, Any]] = []
    for path in _candidate_files(conn, function_name, suffixes):
        row = conn.execute("SELECT value FROM config WHERE key = ?", ["scan:file:" + path]).fetchone()
        if not row or not row[0]:
            continue
        try:
            functions = packed_functions(row[0], path)
        except (ValueError, zlib.error):
            continue
        for function in functions:
            if function.get("function_id") == function_id:
                continue  # a function that calls itself is not a caller
            for call in function.get("calls", []):
                if called_name(call.get("name", "")) == function_name:
                    matches.append({"function_id": function["function_id"], "file_path": path, "call": call.get("text", "")})
    if not matches:
        return None

    callers = sorted({match["function_id"] for match in matches})
    resolved: Dict[str, set] = {}
    bodies: Dict[str, tuple] = {}
    for chunk in _chunks(callers):
        marks = ", ".join("?" for _ in chunk)
        for caller, sites in conn.execute(f"SELECT caller_id, string_agg(call_site, '; ') FROM dependencies WHERE caller_id IN ({marks}) GROUP BY caller_id", chunk).fetchall():
            resolved[caller] = set(str(sites or "").split("; "))
        for caller, start, body in conn.execute(f"SELECT function_id, start_line, body FROM artifacts WHERE function_id IN ({marks})", chunk).fetchall():
            bodies[caller] = (int(start), body)

    counts = definition_counts(conn)
    reason = refine_not_found(function_name, counts)
    used: Dict[tuple, int] = {}
    sites: List[Dict[str, Any]] = []
    for match in matches:
        if match["call"] in resolved.get(match["function_id"], set()):
            continue  # this site is a call to some function (another one of the same name, say)
        site: Dict[str, Any] = {"function_id": match["function_id"], "file_path": match["file_path"], "call": match["call"], "reason": reason}
        start, body = bodies.get(match["function_id"], (None, ""))
        if start is not None:
            lines = _lines_of(body, start, match["call"])
            seen = used.get((match["function_id"], match["call"]), 0)
            used[(match["function_id"], match["call"])] = seen + 1
            if lines:
                site["line"] = lines[min(seen, len(lines) - 1)]
        sites.append(site)
    if not sites:
        return None
    sites.sort(key=lambda site: (site["file_path"], site.get("line") or 0, site["function_id"], site["call"]))
    functions_named = len({site["function_id"] for site in sites})
    result: Dict[str, Any] = {
        "name": function_name,
        "call_sites": len(sites),
        "functions": functions_named,
        "message": (
            f"no resolved callers; {len(sites)} call site{'' if len(sites) == 1 else 's'} in {functions_named} "
            f"function{'' if functions_named == 1 else 's'} name {function_name} but could not be resolved"
        ),
        "sites": sites[:MAX_SITES],
    }
    if len(sites) > MAX_SITES:
        result["sites_omitted"] = len(sites) - MAX_SITES
    return result


def add_unresolved_callers(conn: duckdb.DuckDBPyConnection, data: Dict[str, Any], artifact: Any, direction: str) -> Dict[str, Any]:
    """`data` of a `deps get` for `artifact`, with `unresolved_callers` right after `callers` when the function has no
    resolved caller (and callers were asked for) but call sites name it. Unchanged otherwise."""
    if direction == "callees" or data.get("callers"):
        return data
    found = unresolved_callers(conn, artifact.function_id, artifact.function_name, artifact.language)
    if not found:
        return data
    result: Dict[str, Any] = {}
    for key, value in data.items():
        result[key] = value
        if key == "callers":
            result["unresolved_callers"] = found
    return result
