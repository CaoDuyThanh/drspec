"""C++ declarations and their definitions.

A function is an index entry only where it has a body. A declaration without one
(`int mul(int, int) const;` in a header) whose definition exists elsewhere in the
project is an alias of that definition: it has no artifact of its own, so a
method is listed, searched and given a contract once, and the header it is
declared in can still be used to look it up. A declaration with no definition
anywhere in the project (a library header) stays an entry of its own, marked as
a declaration, and is left out of the lists that ask for dead code.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Tuple

import duckdb

from drspec.core.carry_over import REASON_RENAMED, Pair, restore_snapshots, take_snapshots
from drspec.core.hasher import compute_hash
from drspec.core.scanner import ScannedFunction
from drspec.db.queries import IN_LIST, insert_artifact, insert_artifacts, insert_rows, schedule_removal

_CHUNK = 400


def is_declaration(function: ScannedFunction) -> bool:
    return "declaration" in function.decorators


def split_declarations(functions: Iterable[ScannedFunction]) -> Tuple[List[ScannedFunction], List[ScannedFunction]]:
    """`(definitions, declarations)`."""
    definitions: List[ScannedFunction] = []
    declarations: List[ScannedFunction] = []
    for function in functions:
        (declarations if is_declaration(function) else definitions).append(function)
    return definitions, declarations


def _suffix(function_id: str) -> str:
    """`::qualified#hash`: what a declaration and its definition share, whatever file each is in."""
    return "::" + function_id.split("::", 1)[1]


def find_definition(conn: duckdb.DuckDBPyConnection, declaration_id: str) -> str | None:
    row = conn.execute(
        """
        SELECT a.function_id FROM artifacts a
        WHERE ends_with(a.function_id, ?) AND a.function_id <> ?
          AND a.function_id NOT IN (SELECT function_id FROM pending_removals)
          AND a.function_id NOT IN (SELECT function_id FROM function_declarations WHERE definition_id IS NULL)
        ORDER BY a.function_id LIMIT 1
        """,
        [_suffix(declaration_id), declaration_id],
    ).fetchone()
    return row[0] if row else None


def definition_index(conn: duckdb.DuckDBPyConnection) -> Dict[str, List[str]]:
    """Every function that can be the definition of a declaration, under each `::...` ending of its id, in id order:
    what `find_definition` asks the database for one declaration at a time, read once. Valid while the definitions do
    not change; the declarations that stop or start being entries while a batch is recorded are never candidates."""
    index: Dict[str, List[str]] = {}
    ids = conn.execute(
        """
        SELECT function_id FROM artifacts
        WHERE function_id NOT IN (SELECT function_id FROM pending_removals)
          AND function_id NOT IN (SELECT function_id FROM function_declarations WHERE definition_id IS NULL)
        ORDER BY function_id
        """
    ).fetchall()
    for (function_id,) in ids:
        position = function_id.find("::")
        while position != -1:
            index.setdefault(function_id[position:], []).append(function_id)
            position = function_id.find("::", position + 1)
    return index


def lookup_definition(index: Dict[str, List[str]], declaration_id: str) -> str | None:
    for candidate in index.get(_suffix(declaration_id), ()):
        if candidate != declaration_id:
            return candidate
    return None


_DECLARATION_COLUMNS = (
    "function_id", "file_path", "function_name", "signature", "body", "code_hash", "language", "start_line", "end_line", "parent", "definition_id",
)


def _store_declaration(conn: duckdb.DuckDBPyConnection, f: ScannedFunction, code_hash: str, definition_id: str | None) -> None:
    store_declarations(conn, [(f, code_hash, definition_id)])


def store_declarations(conn: duckdb.DuckDBPyConnection, items: List[Tuple[ScannedFunction, str, str | None]]) -> None:
    """Replace the rows of these declarations (the last of a repeated one wins), a few statements for all of them."""
    rows = {
        f.function_id: (f.function_id, f.file_path, f.name, f.signature, f.body, code_hash, f.language, f.start_line, f.end_line, f.parent, definition_id)
        for f, code_hash, definition_id in items
    }
    if rows:
        conn.execute(f"DELETE FROM function_declarations WHERE function_id {IN_LIST}", [list(rows)])
    insert_rows(conn, "function_declarations", _DECLARATION_COLUMNS, list(rows.values()))


def _materialise(conn: duckdb.DuckDBPyConnection, row: tuple) -> None:
    function_id, file_path, name, signature, body, code_hash, language, start_line, end_line, parent = row
    insert_artifact(
        conn, function_id=function_id, file_path=file_path, function_name=name, signature=signature, body=body, code_hash=code_hash,
        language=language, start_line=start_line, end_line=end_line, parent=parent,
    )


def _become_alias(conn: duckdb.DuckDBPyConnection, declaration_id: str, definition_id: str) -> None:
    """The declaration stops being an entry: what was saved on it (a contract, traces) moves to the definition when
    that has none of its own, and its row goes."""
    if conn.execute("SELECT 1 FROM artifacts WHERE function_id = ?", [declaration_id]).fetchone() is None:
        return
    if conn.execute("SELECT 1 FROM contracts WHERE function_id = ?", [definition_id]).fetchone() is None:
        snapshots = take_snapshots(conn, [Pair(declaration_id, definition_id, REASON_RENAMED, 1.0)])
        restore_snapshots(conn, snapshots)
    schedule_removal(conn, [declaration_id])


def sync_declarations(conn: duckdb.DuckDBPyConnection, declarations: List[ScannedFunction], code_hash_cache: dict) -> int:
    """Record this scan's declarations: an alias where a definition exists, an entry of their own where not.
    Returns how many became or stayed entries. Call after the scan's definitions are written."""
    if not declarations:
        return 0
    index = definition_index(conn)
    items: List[Tuple[ScannedFunction, str, str | None]] = []
    for declaration in declarations:
        items.append((declaration, compute_hash(declaration.body, declaration.language), lookup_definition(index, declaration.function_id)))
    store_declarations(conn, items)

    stored = _load_details(conn, [declaration.function_id for declaration, _hash, _definition in items])
    entries = 0
    new_rows: Dict[str, tuple] = {}
    for declaration, code_hash, definition_id in items:
        if definition_id is not None:
            if declaration.function_id in stored:  # it was an entry before: what was saved on it moves to the definition
                _become_alias(conn, declaration.function_id, definition_id)
            continue
        entries += 1
        code_hash_cache[declaration.function_id] = code_hash
        current = stored.get(declaration.function_id)
        row = (declaration.function_id, declaration.file_path, declaration.name, declaration.signature, declaration.body, code_hash,
               declaration.language, declaration.start_line, declaration.end_line, declaration.parent, "PENDING", False)
        if current is None:
            new_rows[declaration.function_id] = row
        elif current != (code_hash, declaration.signature, declaration.start_line, declaration.end_line, declaration.parent, False, declaration.language):
            insert_artifact(
                conn, function_id=declaration.function_id, file_path=declaration.file_path, function_name=declaration.name,
                signature=declaration.signature, body=declaration.body, code_hash=code_hash, language=declaration.language,
                start_line=declaration.start_line, end_line=declaration.end_line, parent=declaration.parent,
            )
    insert_artifacts(conn, list(new_rows.values()))
    return entries


def _load_details(conn: duckdb.DuckDBPyConnection, function_ids: List[str]) -> Dict[str, tuple]:
    """`{function_id: (code_hash, signature, start_line, end_line, parent, is_property)}` for the stored functions among these."""
    ids = list(dict.fromkeys(function_ids))
    found: Dict[str, tuple] = {}
    if not ids:
        return found
    for row in conn.execute(
        f"SELECT function_id, code_hash, signature, start_line, end_line, parent, is_property, language FROM artifacts WHERE function_id {IN_LIST}", [ids]
    ).fetchall():
        found[row[0]] = tuple(row[1:])
    return found


def match_waiting_declarations(conn: duckdb.DuckDBPyConnection) -> None:
    """A definition scanned after the declaration that waited for it: the declaration becomes its alias."""
    waiting = conn.execute("SELECT function_id FROM function_declarations WHERE definition_id IS NULL").fetchall()
    if not waiting:
        return
    index = definition_index(conn)
    for (declaration_id,) in waiting:
        definition_id = lookup_definition(index, declaration_id)
        if definition_id is None:
            continue
        conn.execute("UPDATE function_declarations SET definition_id = ? WHERE function_id = ?", [definition_id, declaration_id])
        _become_alias(conn, declaration_id, definition_id)


def revert_aliases_of(conn: duckdb.DuckDBPyConnection, removed_definitions: Iterable[str]) -> None:
    """Definitions that left the source: the declarations that were aliases of them are entries again."""
    ids = sorted(set(removed_definitions))
    for start in range(0, len(ids), _CHUNK):
        part = ids[start : start + _CHUNK]
        rows = conn.execute(
            f"SELECT function_id, file_path, function_name, signature, body, code_hash, language, start_line, end_line, parent "
            f"FROM function_declarations WHERE definition_id IN ({', '.join('?' for _ in part)})",
            part,
        ).fetchall()
        for row in rows:
            conn.execute("UPDATE function_declarations SET definition_id = NULL WHERE function_id = ?", [row[0]])
            _materialise(conn, row)


def forget_declarations_not_in(conn: duckdb.DuckDBPyConnection, scanned_file_paths: Iterable[str], fresh_ids: Iterable[str]) -> int:
    """A declaration removed from its header leaves no alias behind, and no entry either: when its definition left in the
    same scan, `revert_aliases_of` has just made the declaration an entry of its own (the definition it was an alias of
    is gone), and an entry for a declaration that is no longer written anywhere would stay in the index, with its edges,
    until the header changed again. Returns how many such entries are scheduled for removal."""
    keep = set(fresh_ids)
    paths = sorted(set(scanned_file_paths))
    scheduled = 0
    for start in range(0, len(paths), _CHUNK):
        part = paths[start : start + _CHUNK]
        stored = conn.execute(
            f"SELECT function_id FROM function_declarations WHERE file_path IN ({', '.join('?' for _ in part)})", part
        ).fetchall()
        gone = [row[0] for row in stored if row[0] not in keep]
        for index in range(0, len(gone), _CHUNK):
            chunk = gone[index : index + _CHUNK]
            marks = ", ".join("?" for _ in chunk)
            entries = [row[0] for row in conn.execute(f"SELECT function_id FROM artifacts WHERE function_id IN ({marks})", chunk).fetchall()]
            conn.execute(f"DELETE FROM function_declarations WHERE function_id IN ({marks})", chunk)
            scheduled += schedule_removal(conn, entries)
    return scheduled


def declared_in(conn: duckdb.DuckDBPyConnection, definition_id: str) -> List[str]:
    """The headers (files) where a definition is declared."""
    return [row[0] for row in conn.execute("SELECT DISTINCT file_path FROM function_declarations WHERE definition_id = ? ORDER BY 1", [definition_id]).fetchall()]


def is_declaration_only(conn: duckdb.DuckDBPyConnection, function_id: str) -> bool:
    return conn.execute("SELECT 1 FROM function_declarations WHERE function_id = ? AND definition_id IS NULL", [function_id]).fetchone() is not None
