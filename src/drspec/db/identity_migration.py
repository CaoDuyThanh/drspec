"""Rebuilding a database whose function identifiers have no parameter hash.

Identifiers used to be `<file>::<qualified name>`; they are now
`<file>::<qualified name>#<hash of the parameters>`. Every table and key that holds
one is rewritten in a new file built beside the old one, which replaces it in a
single atomic rename (the machinery is the one used for the embedding layout in
`db.migration`): any failure leaves the old file as it was, and a copy is kept
until the next successful start.

The new identifiers are computed from what is stored, without the source files:
each function's stored body is parsed again by the same parser a scan uses, which
gives the same parameters, hence the same hash, as a fresh parse of the file. A
function whose body cannot be read back gets the hash of an empty parameter list;
the next scan then sees a function of the same name with other parameters, and the
contract is carried over to it as a changed signature.
"""

from __future__ import annotations

import hashlib
import json
import re
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import duckdb

from drspec.core.file_records import is_packed, pack, unpack
from drspec.core.identity import function_identity, split_function_id
from drspec.db.migration import MigrationError, copy_database, rebuild_database
from drspec.db.queries import get_config, set_config

MARKER_KEY = "schema:identity"
MARKER_VALUE = "v1"
BACKUP_SUFFIX = ".before-hashed-ids"

_HASHED = re.compile(r"#[0-9a-f]{8}$")
_SINGLE_ID_KEY_PREFIXES = (
    "contract:source:",
    "contract:context:",
    "artifact:context:",
    "artifact:stale_reason:",
    "verification:",
    "carryover:",
)
_EMBEDDING_KEY_PREFIX = "embedding:"
_SNAPSHOT_KEY_PREFIX = "scan:file:"

#: table -> the columns that hold a function identifier (the tables a scan fills).
ID_COLUMNS: Dict[str, Tuple[str, ...]] = {
    "artifacts": ("function_id",),
    "contracts": ("function_id",),
    "reasoning_traces": ("function_id",),
    "vision_findings": ("function_id",),
    "dependencies": ("caller_id", "callee_id"),
    "code_references": ("caller_id", "callee_id"),
    "learning_history": ("function_id",),
    "artifact_embeddings": ("function_id",),
    "pending_removals": ("function_id",),
    "function_declarations": ("function_id", "definition_id"),
}


def backup_path(db_path: Path) -> Path:
    return Path(db_path).with_name(Path(db_path).name + BACKUP_SUFFIX)


def _marker(conn: Any) -> Optional[str]:
    try:
        return get_config(conn, MARKER_KEY)
    except duckdb.CatalogException:
        return None


def needs_identity_migration(conn: Any) -> bool:
    """True when a stored function has an identifier without a parameter hash."""
    if _marker(conn) == MARKER_VALUE:
        return False
    try:
        rows = conn.execute("SELECT function_id FROM artifacts").fetchall()
    except duckdb.CatalogException:
        return False
    return any(not _HASHED.search(row[0]) for row in rows)


def ensure_identity_layout(db_path: Path) -> str:
    """Rebuild `db_path` if its identifiers have no parameter hash. Call before anything else
    opens the file, and after the embedding layout was checked. Returns `absent`, `current` or
    `migrated`; raises `MigrationError` if the rebuild failed (the old file is then unchanged)."""
    db_path = Path(db_path)
    if not db_path.exists():
        return "absent"
    try:
        conn = duckdb.connect(str(db_path))
    except duckdb.Error as e:
        raise MigrationError(f"Could not open {db_path} to check its identifiers: {e}") from e
    try:
        if not needs_identity_migration(conn):
            if _marker(conn) == MARKER_VALUE:
                _remove_backup(db_path)  # an earlier start migrated it and this one is the next success
            else:
                try:
                    set_config(conn, MARKER_KEY, MARKER_VALUE)
                except duckdb.CatalogException:
                    pass  # no config table yet: an empty database
            return "current"
    finally:
        conn.close()

    rebuild_database(db_path, backup_path(db_path), _build, "hashed function identifiers")
    return "migrated"


def _remove_backup(db_path: Path) -> None:
    for path in (backup_path(db_path), Path(str(backup_path(db_path)) + ".wal")):
        try:
            path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Computing the new identifiers
# ---------------------------------------------------------------------------


def _parsers() -> Dict[str, Any]:
    from drspec.parsers import CppParser, JavaScriptParser, PythonParser, RustParser

    return {
        "python": PythonParser(),
        "javascript": JavaScriptParser(),
        "typescript": JavaScriptParser("typescript"),
        "tsx": JavaScriptParser("tsx"),
        "cpp": CppParser(),
        "c": CppParser(),
        "rust": RustParser(),
    }


def parameter_tokens_from_body(parsers: Dict[str, Any], language: str, name: str, body: str) -> Optional[List[str]]:
    """The parameter tokens of the function called `name` whose stored text is `body`, read by
    parsing the text again; `None` when it cannot be read back."""
    parser = parsers.get(language)
    if parser is None:
        return None
    # A C++ declaration is only recognised in a header; a method needs its class in JS.
    attempts: List[Tuple[str, Optional[str]]] = [(body, "migration.h" if language in ("cpp", "c") else None)]
    if language in ("javascript", "typescript", "tsx"):
        attempts.append((f"class __Holder {{\n{body}\n}}", None))
    elif language in ("cpp", "c"):  # a constructor or a method declared inside a class
        attempts.append((f"class __Holder {{ public:\n{body}\n}};", "migration.h"))
    for text, file_path in attempts:
        try:
            result = parser.parse(text, file_path=file_path)
        except Exception:  # noqa: BLE001 - a body that cannot be parsed just falls back
            continue
        for function in result.functions:
            if function.name == name:
                return list(function.parameter_tokens)
    return None


def compute_new_ids(rows: Sequence[Tuple[str, str, str, str]]) -> Tuple[Dict[str, str], Dict[str, List[str]], List[str]]:
    """`rows` are `(function_id, function_name, language, body)`. Returns the old-to-new identifier
    map, each old identifier's parameter tokens, and the identifiers whose body could not be read
    back (they get the hash of no parameters)."""
    parsers = _parsers()
    mapping: Dict[str, str] = {}
    tokens: Dict[str, List[str]] = {}
    unread: List[str] = []
    for function_id, name, language, body in rows:
        if _HASHED.search(function_id):
            mapping[function_id] = function_id
            tokens[function_id] = []
            continue
        file_path, qualified, _ = split_function_id(function_id)
        found = parameter_tokens_from_body(parsers, language, name, body)
        if found is None:
            found = []
            unread.append(function_id)
        tokens[function_id] = found
        mapping[function_id] = function_identity(file_path, qualified, found)
    return mapping, tokens, unread


# ---------------------------------------------------------------------------
# Copying
# ---------------------------------------------------------------------------


def _remap_key(key: str, mapping: Dict[str, str]) -> str:
    for prefix in _SINGLE_ID_KEY_PREFIXES:
        if key.startswith(prefix):
            old = key[len(prefix):]
            return prefix + mapping.get(old, old)
    if key.startswith(_EMBEDDING_KEY_PREFIX):
        try:
            parts = json.loads(key[len(_EMBEDDING_KEY_PREFIX):])
        except ValueError:
            return key
        if isinstance(parts, list) and parts:
            parts = [parts[0]] + [mapping.get(item, item) for item in parts[1:]]
            return _EMBEDDING_KEY_PREFIX + json.dumps(parts)
    return key


def _remap_value(key: str, value: str, mapping: Dict[str, str], tokens: Dict[str, List[str]]) -> str:
    if key.startswith("carryover:"):
        try:
            record = json.loads(value)
            record["from"] = mapping.get(record.get("from"), record.get("from"))
            record["earlier"] = [mapping.get(item, item) for item in record.get("earlier", [])]
            return json.dumps(record)
        except (ValueError, AttributeError):
            return value
    if key.startswith(_SNAPSHOT_KEY_PREFIX):
        try:
            path = key[len(_SNAPSHOT_KEY_PREFIX):]
            snapshot = unpack(value, path)
            for function in snapshot.get("functions", []):
                old = function.get("function_id")
                if old in mapping:
                    function["function_id"] = mapping[old]
                    if tokens.get(old):  # (a function with no parameters has none stored: the record leaves out an empty list)
                        function.setdefault("parameter_tokens", tokens[old])
            return pack(snapshot, path) if is_packed(value) else json.dumps(snapshot)
        except (ValueError, AttributeError, zlib.error):
            return value
    return value


def _insert_many(conn: Any, sql_prefix: str, rows: Sequence[Tuple[Any, ...]], chunk: int = 400) -> None:
    """Insert rows with a few multi-row statements; `executemany` costs a round trip per row."""
    for start in range(0, len(rows), chunk):
        part = rows[start : start + chunk]
        marks = ", ".join("(" + ", ".join("?" for _ in part[0]) + ")" for _ in part)
        conn.execute(f"{sql_prefix} VALUES {marks}", [value for row in part for value in row])


def _copy_config(conn: Any, mapping: Dict[str, str], tokens: Dict[str, List[str]], fingerprint: str) -> None:
    """Copy the settings with their identifiers rewritten. The fingerprint the scan stored (and that its
    per-file facts were taken under) is replaced by the one the new identifiers give, so nothing is
    re-resolved only because the identifiers changed."""
    rows = conn.execute("SELECT key, value, updated_at FROM old.config").fetchall()
    converted = []
    for key, value, updated in rows:
        new_value = _remap_value(key, value, mapping, tokens)
        if key == "scan:symbols":
            new_value = fingerprint
        elif key.startswith(_SNAPSHOT_KEY_PREFIX):
            if not is_packed(new_value):  # (a packed record is of the current form: it holds none)
                snapshot = json.loads(new_value)
                if "symbols_hash" in snapshot:  # (the fingerprint is the one global entry)
                    snapshot["symbols_hash"] = fingerprint
                    new_value = json.dumps(snapshot)
        converted.append((_remap_key(key, mapping), new_value, updated))
    _insert_many(conn, "INSERT INTO main.config (key, value, updated_at)", converted)


def _id_statement(table: str, wanted: List[str], present: set) -> str:
    """INSERT that replaces the identifier columns of `table` through the `id_map` table."""
    id_columns = [column for column in ID_COLUMNS[table] if column in present]
    selects, joins = [], []
    for column in wanted:
        if column not in present:
            continue
        if column in id_columns:
            alias = f"m_{column}"
            joins.append(f"LEFT JOIN id_map AS {alias} ON t.{column} = {alias}.old_id")
            selects.append(f"COALESCE({alias}.new_id, t.{column})")
        else:
            selects.append(f"t.{column}")
    columns = ", ".join(column for column in wanted if column in present)
    return f"INSERT INTO main.{table} ({columns}) SELECT {', '.join(selects)} FROM old.{table} AS t {' '.join(joins)}"


def _fingerprint(mapping: Dict[str, str], facts: Sequence[Tuple[str, Optional[str], bool]]) -> str:
    """What `graph_sync.symbols_hash` will give once the functions carry their new identifiers."""
    rows = sorted(((mapping[old], parent, is_property) for old, parent, is_property in facts), key=lambda row: row[0].encode("utf-8"))
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def _usage_statement(wanted: List[str], present: set) -> str:
    columns = ", ".join(column for column in wanted if column in present)

    def select(item: str, related: str) -> str:
        return ", ".join(
            item if column == "item_id" else related if column == "related_id" else f"t.{column}" for column in wanted if column in present
        )

    nodes = select("COALESCE(m.new_id, t.item_id)", "t.related_id")
    edges = select("COALESCE(m1.new_id, t.item_id)", "COALESCE(m2.new_id, t.related_id)")
    return (
        f"INSERT INTO main.usage_stats ({columns}) "
        f"SELECT {nodes} FROM old.usage_stats AS t LEFT JOIN id_map AS m ON t.item_id = m.old_id WHERE t.kind = 'node' "
        f"UNION ALL "
        f"SELECT {edges} FROM old.usage_stats AS t LEFT JOIN id_map AS m1 ON t.item_id = m1.old_id "
        f"LEFT JOIN id_map AS m2 ON t.related_id = m2.old_id WHERE t.kind <> 'node'"
    )


def _build(source: Path, target: Path) -> None:
    probe = duckdb.connect(str(source), read_only=True)
    try:
        rows = probe.execute("SELECT function_id, function_name, language, body FROM artifacts").fetchall()
        facts = probe.execute("SELECT function_id, parent, is_property FROM artifacts").fetchall()
        try:  # C++ declarations that stand for a definition have no artifact but an identifier of their own
            rows += probe.execute("SELECT function_id, function_name, language, body FROM function_declarations").fetchall()
        except duckdb.CatalogException:
            pass
    finally:
        probe.close()
    mapping, tokens, _unread = compute_new_ids(rows)
    fingerprint = _fingerprint(mapping, facts)
    seen: Dict[str, str] = {}
    for old_id, new_id in mapping.items():
        if new_id in seen:
            raise MigrationError(
                f"Two stored functions would receive the identifier {new_id}: {seen[new_id]} and {old_id}. "
                "Remove one of them (for example by scanning that file again) and restart."
            )
        seen[new_id] = old_id

    def prepare(conn: Any) -> None:
        conn.execute("CREATE TEMP TABLE id_map (old_id TEXT PRIMARY KEY, new_id TEXT NOT NULL)")
        _insert_many(conn, "INSERT INTO id_map", list(mapping.items()))

    def statement_for(table: str, wanted: List[str], present: set) -> Any:
        if table == "config":
            return lambda conn: _copy_config(conn, mapping, tokens, fingerprint)
        if table == "usage_stats":
            return _usage_statement(wanted, present)
        if table in ID_COLUMNS:
            return _id_statement(table, wanted, present)
        return None

    def finish(conn: Any) -> None:
        from drspec.core.graph_sync import symbols_hash

        set_config(conn, MARKER_KEY, MARKER_VALUE)
        if symbols_hash(conn) != fingerprint:
            raise MigrationError("The fingerprint computed for the new identifiers does not match the rebuilt functions.")
        _check_references(conn)

    def verify(check: Any) -> None:
        unhashed = [row[0] for row in check.execute("SELECT function_id FROM artifacts").fetchall() if not _HASHED.search(row[0])]
        if unhashed:
            raise MigrationError(f"The rebuilt database still has identifiers without a hash, such as {unhashed[0]}.")

    copy_database(source, target, statement_for, finish, verify, prepare)


def _check_references(conn: Any) -> None:
    """Every identifier any table refers to must belong to a stored function."""
    problems = []
    for table, columns in ID_COLUMNS.items():
        if table in ("artifacts", "learning_history"):
            continue
        for column in columns:
            if table == "function_declarations" and column == "function_id":
                continue  # an alias names a declaration that has no artifact of its own
            missing = conn.execute(
                f"SELECT COUNT(*) FROM main.{table} WHERE {column} IS NOT NULL AND {column} NOT IN (SELECT function_id FROM main.artifacts)"
            ).fetchone()[0]
            if missing:
                problems.append(f"{table}.{column}: {missing}")
    if problems:
        raise MigrationError("Rows refer to functions that do not exist after the rebuild: " + ", ".join(problems))
