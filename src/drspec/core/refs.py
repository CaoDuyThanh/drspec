"""Who reads and writes a member (a field of a class): the member references a scan stored (`member_references`, C++ first),
grouped by owner class, file and function, each with its kind, its line and the line of code, and with the accesses left unresolved
(receiver type unknown) counted, so an empty or short list is not read as "nobody uses it"."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import duckdb

from drspec.core.changed_notice import add_changed_notice, take_before
from drspec.core.partial import parse_problems

KINDS = ("read", "write")
#: `--kind read` is every access that reads (a read, and the read of a read-modify-write); `--kind write` every one that writes.
_KINDS_OF = {"read": ("read", "readwrite"), "write": ("write", "readwrite")}
_MAX_UNRESOLVED_FUNCTIONS = 20


def split_symbol(symbol: str) -> Tuple[Optional[str], str]:
    """`Owner::member` (or `Owner.member`) -> (owner, member); a bare `member` -> (None, member)."""
    text = symbol.strip()
    for separator in ("::", "."):
        if separator in text:
            owner, _, member = text.rpartition(separator)
            return (owner.strip() or None), member.strip()
    return None, text


def _source_lines(body: Optional[str], start_line: int, line: int, context: int) -> Dict[str, Any]:
    """The line of code of an access and the lines around it, from the stored text of its function (the lines of a function are
    its own: the context stops at the function's first and last line)."""
    lines = (body or "").split("\n")
    index = line - (start_line or 1)
    if index < 0 or index >= len(lines):
        return {"source": ""}
    found: Dict[str, Any] = {"source": lines[index].strip()}
    if context > 0:
        found["before"] = [{"line": start_line + i, "text": lines[i]} for i in range(max(0, index - context), index)]
        found["after"] = [{"line": start_line + i, "text": lines[i]} for i in range(index + 1, min(len(lines), index + context + 1))]
    return found


def _through(written_through: Optional[str]) -> Dict[str, str]:
    """`written_through` of an access that is the base of a write (`context->avoidanceResult` with `available` written through it),
    as a field of its own; nothing for an access that is not."""
    return {"written_through": written_through} if written_through else {}


def _declares_member(conn: duckdb.DuckDBPyConnection, owner: str, member: str) -> Optional[bool]:
    """Whether the class `owner` (written with or without its namespace) declares a data member `member`, a base's included:
    `True` or `False` for a class the index knows, `None` for one it does not. Read from the classes the scan stored per file."""
    classes: Dict[str, List[Dict[str, Any]]] = {}
    for (value,) in conn.execute("SELECT value FROM config WHERE starts_with(key, 'scan:imports:') AND contains(value, '\"classes\"')").fetchall():
        try:
            found = (json.loads(value).get("facts") or {}).get("classes") or []
        except ValueError:
            continue
        for fact in found:
            if fact.get("name"):
                classes.setdefault(fact["name"], []).append(fact)

    def named(written: str) -> List[Dict[str, Any]]:
        written = re.sub(r"<.*>", "", written).strip().lstrip(":")
        return [fact for name, facts in classes.items() if name == written or name.endswith("::" + written) for fact in facts]

    start = named(owner)
    if not start:
        return None
    seen, pending = set(), list(start)
    while pending:
        fact = pending.pop()
        if id(fact) in seen:
            continue
        seen.add(id(fact))
        if member in (fact.get("members") or []) or member in (fact.get("fields") or {}):
            return True
        for base in fact.get("bases") or []:
            pending.extend(named(base))
    return False


UNRESOLVED_MODES = ("files", "full", "count")


def get_refs(conn: duckdb.DuckDBPyConnection, symbol: str, kind: Optional[str] = None, context: int = 0, limit: int = 50, unresolved: str = "full", root: Optional[Path] = None) -> Dict[str, Any]:
    """The reads and writes of the member `symbol` names. Raises `ValueError` for a kind that is not `read` or `write`, and for an
    `unresolved` mode that is not `files` (the unresolved accesses grouped by file: their counts), `full` (each unresolved function with
    its lines, up to `limit`) or `count` (only the numbers). The totals and the note are in every mode. With the project's `root`, the
    answer says when a file it comes from has changed since the last scan."""
    if kind is not None and kind not in KINDS:
        raise ValueError(f"Invalid kind: {kind!r}. Must be one of {list(KINDS)}.")
    if unresolved not in UNRESOLVED_MODES:
        raise ValueError(f"Invalid unresolved: {unresolved!r}. Must be one of {list(UNRESOLVED_MODES)}.")
    owner, member = split_symbol(symbol)
    if not member:
        raise ValueError("Missing the member: give `Owner::member` or a member name.")
    context = max(0, min(int(context), 50))
    limit = max(1, int(limit))
    wanted = _KINDS_OF[kind] if kind else None
    before = take_before(conn) if root is not None else None  # taken before the answer's reads (see changed_notice)

    clauses, params = ["member = ?"], [member]
    if owner is not None:
        clauses.append("(owner_class = ? OR owner_class LIKE ?)")
        params += [owner, "%::" + owner]
    else:
        clauses.append("owner_class <> ''")
    if wanted:
        clauses.append(f"kind IN ({', '.join('?' for _ in wanted)})")
        params += list(wanted)
    rows = conn.execute(
        "SELECT caller_id, owner_class, kind, file_path, line, site, written_through FROM member_references WHERE " + " AND ".join(clauses)
        + " ORDER BY owner_class, file_path, line, caller_id, kind, site",
        params,
    ).fetchall()

    unresolved_clauses, unresolved_params = ["member = ?", "owner_class = ''"], [member]
    if wanted:
        unresolved_clauses.append(f"kind IN ({', '.join('?' for _ in wanted)})")
        unresolved_params += list(wanted)
    unresolved_rows = conn.execute(
        "SELECT caller_id, file_path, line, site, receiver, kind, written_through FROM member_references WHERE " + " AND ".join(unresolved_clauses)
        + " ORDER BY file_path, line, caller_id",
        unresolved_params,
    ).fetchall()

    shown = rows[:limit]
    callers = sorted({row[0] for row in shown} | {row[0] for row in unresolved_rows})
    info: Dict[str, tuple] = {}
    for start in range(0, len(callers), 400):
        part = callers[start : start + 400]
        for function_id, name, body, start_line in conn.execute(
            f"SELECT function_id, function_name, body, start_line FROM artifacts WHERE function_id IN ({', '.join('?' for _ in part)})", part
        ).fetchall():
            info[function_id] = (name, body, start_line)

    owners: Dict[str, Dict[str, Dict[str, Dict[str, Any]]]] = {}
    for caller_id, owner_class, access_kind, file_path, line, site, through in shown:
        _stored_name, body, start_line = info.get(caller_id, (None, None, 1))
        name = caller_id.split("::", 1)[-1].rsplit("#", 1)[0]  # the qualified name (Holder::run), as the id spells it
        function = owners.setdefault(owner_class, {}).setdefault(file_path, {}).setdefault(
            caller_id, {"function_id": caller_id, "function_name": name, "accesses": []}
        )
        function["accesses"].append({"kind": access_kind, "line": line, "site": site, **_source_lines(body, start_line, line, context), **_through(through)})
    owner_list = []
    for owner_class, files in owners.items():
        count = sum(len(function["accesses"]) for functions in files.values() for function in functions.values())
        owner_list.append({
            "owner_class": owner_class,
            "accesses": count,
            "files": [{"file_path": path, "functions": list(functions.values())} for path, functions in files.items()],
        })

    by_function: Dict[str, Dict[str, Any]] = {}
    for caller_id, file_path, line, site, receiver, access_kind, through in unresolved_rows:
        entry = by_function.setdefault(caller_id, {
            "function_id": caller_id, "function_name": caller_id.split("::", 1)[-1].rsplit("#", 1)[0],
            "file_path": file_path, "lines": [],
        })
        entry["lines"].append({"line": line, "kind": access_kind, "site": site, "receiver": receiver or "", **_through(through)})
    unresolved_out: Dict[str, Any] = {"count": len(unresolved_rows), "functions_total": len(by_function)}
    listed = list(by_function.values())
    if unresolved == "full":
        cap = min(limit, _MAX_UNRESOLVED_FUNCTIONS)
        unresolved_out["functions"] = listed[:cap]
        if len(listed) > cap:
            unresolved_out["functions_omitted"] = len(listed) - cap
    elif unresolved == "files":
        by_file: Dict[str, Dict[str, Any]] = {}
        for caller_id, file_path, *_rest in unresolved_rows:
            entry = by_file.setdefault(file_path, {"file_path": file_path, "accesses": 0, "functions": set()})
            entry["accesses"] += 1
            entry["functions"].add(caller_id)
        files_list = [{"file_path": path, "accesses": entry["accesses"], "functions": len(entry["functions"])} for path, entry in sorted(by_file.items())]
        unresolved_out["files"] = files_list[:limit]
        if len(files_list) > limit:
            unresolved_out["files_omitted"] = len(files_list) - limit
    if unresolved_rows:
        where = {"full": " Listed under `functions`.", "files": " Counted by file under `files`; `unresolved: full` lists each one.", "count": ""}[unresolved]
        unresolved_out["note"] = (
            f"{len(unresolved_rows)} access(es) to a member named '{member}' in {len(by_function)} function(s) were left unresolved: the "
            f"receiver's type is not known, so they may read or write this member.{where}"
        )
    unresolved = unresolved_out

    # The files whose accesses may be missing: the ones that hold a listed access and the ones that define a method of the class
    # (an access inside a function that was read in part may not have been recorded).
    files = {row[3] for row in shown} | {row[1] for row in unresolved_rows}
    classes = sorted({row[1] for row in shown}) or ([owner] if owner is not None else [])
    if classes:
        marks = ", ".join("?" for _ in classes)
        files |= {path for (path,) in conn.execute(f"SELECT DISTINCT file_path FROM artifacts WHERE parent IN ({marks}) OR " + " OR ".join("parent LIKE ?" for _ in classes), [*classes, *("%::" + name for name in classes)]).fetchall()}
    problems = parse_problems(conn, files)

    result: Dict[str, Any] = {
        "symbol": symbol,
        "member": member,
        "kind": kind or "any",
        "total_accesses": len(rows),
        "returned": len(shown),
        "truncated": len(rows) > len(shown),
        "owners": owner_list,
        "unresolved": unresolved,
    }
    if problems:
        result["parse_problems"] = problems
    if owner is not None and rows:
        used = sorted({row[1] for row in rows})
        if owner not in used:  # written without its namespace: say which class the answer is about
            result["owner_resolved"] = {"given": owner, "used": used}
            result["owner_note"] = (
                f"'{owner}' is not a full class name: used {used[0]}" if len(used) == 1
                else f"'{owner}' matches {len(used)} classes: {', '.join(used)}; the accesses of each are listed apart"
            )
    declared = None  # whether the named class declares the member (None: not asked, or the index does not know the class)
    candidates_note = None
    if owner is not None and not rows:
        other = [r[0] for r in conn.execute("SELECT DISTINCT owner_class FROM member_references WHERE member = ? AND owner_class <> '' ORDER BY 1", [member]).fetchall()]
        declared = _declares_member(conn, owner, member)
        if len(other) == 1 and other[0] != owner and not other[0].endswith("::" + owner) and declared is not True:
            # the named class does not exist in the index or does not declare the member, and one class has accesses of that name: it is
            # the one meant, and the answer says it was used (a class that declares the member is never replaced by another)
            answer = get_refs(conn, f"{other[0]}::{member}", kind=kind, context=context, limit=limit, root=root)
            answer["symbol"] = symbol
            answer["owner_resolved"] = {"given": owner, "used": [other[0]]}
            why = f"'{owner}' is not a class the index knows" if declared is None else f"'{owner}' does not declare a member '{member}'"
            answer["owner_note"] = f"{why}; the only class with recorded accesses to a member of that name is {other[0]}: used it"
            return answer
        if other:
            result["other_owners_of_the_name"] = other
            if owner is not None and declared is not True and len(other) >= 2:
                # two or more classes have accesses of that name and none is redirected to: say what is true of the named class
                why = f"'{owner}' is not a class the index knows" if declared is None else f"'{owner}' does not declare a member '{member}'"
                candidates_note = f"{why}; classes with recorded accesses to a member of that name: {', '.join(other)}"
    if not rows and not unresolved_rows:
        if declared is True:
            result["note"] = f"No access to {symbol} is recorded ('{owner}' declares '{member}'; C++ only, a scan records the accesses)."
        elif candidates_note:
            result["note"] = candidates_note
        else:
            result["note"] = f"No access to a member named '{member}' is recorded (C++ only; a scan records them)."
    elif not rows:
        result["note"] = f"No resolved access to '{symbol}'; see `unresolved`."
    return add_changed_notice(conn, result, root, files, before=before)
