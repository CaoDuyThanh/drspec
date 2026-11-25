"""Rebuilding a database whose embedding columns are fixed-size arrays.

DuckDB 1.3.2 mis-reads some rows of a `FLOAT[384]` column after a vector is
cleared and written back, so the three embedding columns are now variable-length
lists. DuckDB cannot alter the type in place (foreign keys and indexes depend on
the columns), so an old database is rebuilt: a new file with the current schema
receives every table except the vectors, and replaces the old one in a single
atomic rename. The vectors are derived data and are left empty; the `embedding:`
bookkeeping rows are copied, so the background process re-embeds exactly the
items that had a vector.

The old file is only read until the final rename, so any failure leaves it as
it was. A copy is kept beside it until the next successful start.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import duckdb

from drspec.db.connection import init_schema
from drspec.db.queries import set_config
from drspec.db.staging import STAGING_PREFIX

LAYOUT_KEY = "schema:embedding_layout"
LAYOUT_VALUE = "list"
BACKUP_SUFFIX = ".before-list-embeddings"

#: The tables with an `embedding` column, whose vectors are not carried over.
EMBEDDING_TABLES = ("artifacts", "dependencies", "code_references")
_FIXED_ARRAY = re.compile(r"\[\d+\]$")


class MigrationError(RuntimeError):
    """The database could not be rebuilt; the old file is unchanged."""


def backup_path(db_path: Path) -> Path:
    return Path(db_path).with_name(Path(db_path).name + BACKUP_SUFFIX)


def _marker(conn: Any) -> Optional[str]:
    try:
        row = conn.execute("SELECT value FROM config WHERE key = ?", [LAYOUT_KEY]).fetchone()
    except duckdb.CatalogException:
        return None
    return row[0] if row else None


def needs_embedding_migration(conn: Any) -> bool:
    """True when any embedding column is still a fixed-size array."""
    if _marker(conn) == LAYOUT_VALUE:
        return False
    marks = ", ".join("?" for _ in EMBEDDING_TABLES)
    rows = conn.execute(
        f"SELECT data_type FROM information_schema.columns WHERE column_name = 'embedding' AND table_name IN ({marks})",
        list(EMBEDDING_TABLES),
    ).fetchall()
    return any(_FIXED_ARRAY.search(row[0]) for row in rows)


def ensure_embedding_layout(db_path: Path) -> str:
    """Rebuild `db_path` if it uses the old layout. Call before anything else
    opens the file. Returns `absent`, `current` or `migrated`; raises
    `MigrationError` if the rebuild failed (the old file is then unchanged)."""
    db_path = Path(db_path)
    if not db_path.exists():
        return "absent"
    try:
        conn = duckdb.connect(str(db_path))
    except duckdb.Error as e:
        raise MigrationError(f"Could not open {db_path} to check its embedding layout: {e}") from e
    try:
        if not needs_embedding_migration(conn):
            if _marker(conn) == LAYOUT_VALUE:
                # An earlier start migrated it and this one is the next success.
                _remove_backup(db_path)
            else:
                try:
                    set_config(conn, LAYOUT_KEY, LAYOUT_VALUE)
                except duckdb.CatalogException:
                    pass  # no config table yet: an empty database
            return "current"
    finally:
        conn.close()

    migrate_embedding_layout(db_path)
    return "migrated"


def _remove_backup(db_path: Path) -> None:
    for path in (backup_path(db_path), Path(str(backup_path(db_path)) + ".wal")):
        try:
            path.unlink()
        except OSError:
            pass


def fsync_file(path: Path) -> None:
    with open(path, "rb") as stream:
        os.fsync(stream.fileno())


def migrate_embedding_layout(db_path: Path) -> None:
    rebuild_database(db_path, backup_path(db_path), _build, "list-typed embeddings")


def rebuild_database(db_path: Path, backup: Path, build: Callable[[Path, Path], None], purpose: str) -> None:
    """Build a replacement for `db_path` with `build(source_copy, target)` and swap it in
    atomically. The old file is only read until the final rename; any failure raises
    `MigrationError` and leaves it as it was. A copy of it is kept at `backup`."""
    db_path = Path(db_path).resolve()

    # Fold any write-ahead log into the file first, so the file alone is the whole database.
    try:
        conn = duckdb.connect(str(db_path))
        try:
            conn.execute("CHECKPOINT")
        finally:
            conn.close()
    except duckdb.Error as e:
        raise MigrationError(f"Could not read {db_path} for the rebuild: {e}") from e
    if Path(str(db_path) + ".wal").exists():
        raise MigrationError(f"{db_path} still has a write-ahead log after a checkpoint; not rebuilding it.")

    work = Path(tempfile.mkdtemp(prefix=f"{STAGING_PREFIX}{os.getpid()}-", dir=str(db_path.parent)))
    try:
        source = work / "old.db"
        target = work / "contracts.db"
        shutil.copy2(db_path, source)
        try:
            build(source, target)
            fsync_file(target)
            shutil.copy2(db_path, backup)
            fsync_file(backup)
            os.replace(target, db_path)
            directory = os.open(str(db_path.parent), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except MigrationError as e:
            raise MigrationError(f"{e} The database was not changed.") from e
        except Exception as e:  # noqa: BLE001 - any failure leaves the old file in place
            raise MigrationError(f"Rebuilding {db_path} for {purpose} failed: {e}. The database was not changed.") from e
    finally:
        shutil.rmtree(work, ignore_errors=True)


def table_names(conn: Any, database: str) -> List[str]:
    return [row[0] for row in conn.execute("SELECT table_name FROM duckdb_tables() WHERE database_name = ?", [database]).fetchall()]


def column_names(conn: Any, database: str, table: str) -> List[str]:
    rows = conn.execute(
        "SELECT column_name FROM duckdb_columns() WHERE database_name = ? AND table_name = ? ORDER BY column_index",
        [database, table],
    ).fetchall()
    return [row[0] for row in rows]


def copy_order(conn: Any, tables: List[str]) -> List[str]:
    """Tables with the ones a foreign key points at first."""
    parents: Dict[str, set] = {name: set() for name in tables}
    for table, referenced in conn.execute(
        "SELECT table_name, referenced_table FROM duckdb_constraints() WHERE database_name = 'old' AND constraint_type = 'FOREIGN KEY'"
    ).fetchall():
        if table in parents and referenced in parents and referenced != table:
            parents[table].add(referenced)
    ordered: List[str] = []
    remaining = list(tables)
    while remaining:
        ready = [name for name in remaining if parents[name] <= set(ordered)]
        if not ready:
            raise MigrationError("The foreign keys of the old database form a cycle; cannot order the tables to copy.")
        for name in ready:
            ordered.append(name)
            remaining.remove(name)
    return ordered


def next_sequence_values(source: Path) -> Dict[str, int]:
    """What each sequence of the (disposable) source copy returns next. A loaded
    database reports its next value as `last_value`, a live session its last
    one, so asking `nextval` is the only reading that means the same thing."""
    probe = duckdb.connect(str(source))
    try:
        names = [row[0] for row in probe.execute("SELECT sequence_name FROM duckdb_sequences()").fetchall()]
        return {name: int(probe.execute(f"SELECT nextval('{name}')").fetchone()[0]) for name in names}
    finally:
        probe.close()


#: `(table, wanted columns, columns the old table has)` -> an INSERT statement for tables that
#: need their rows changed on the way, a callable `(connection) -> None` that copies them itself, or
#: `None` for a plain copy.
StatementFor = Callable[[str, List[str], set], Any]


def copy_database(
    source: Path,
    target: Path,
    statement_for: StatementFor,
    finish: Callable[[Any], None],
    verify: Callable[[Any], None],
    prepare: Optional[Callable[[Any], None]] = None,
) -> None:
    """Create `target` with the current schema and copy every table of the (disposable) `source`
    into it, in foreign-key order. `prepare` runs before the first table is copied;
    `statement_for` changes how a table is copied; `finish` runs in the new database after the
    copy (markers, derived rows) and `verify` on the finished file.
    Tables the current schema does not define, and the sequences, are carried over."""
    next_values = next_sequence_values(source)
    conn = duckdb.connect(str(target))
    try:
        init_schema(conn)
        conn.execute(f"ATTACH '{source}' AS old (READ_ONLY)")
        current = conn.execute("SELECT current_database()").fetchone()[0]

        old_tables = table_names(conn, "old")
        new_tables = set(table_names(conn, current))

        # Anything the current schema does not define (tables created on demand,
        # such as the learning history) is recreated from the old definition.
        new_sequences = {row[0] for row in conn.execute("SELECT sequence_name FROM duckdb_sequences() WHERE database_name = ?", [current]).fetchall()}
        recreated = set()
        for name, sql in conn.execute("SELECT sequence_name, sql FROM duckdb_sequences() WHERE database_name = 'old'").fetchall():
            if name not in new_sequences:
                conn.execute(sql)  # the definition already starts at the old sequence's next value
                recreated.add(name)
        extra = [name for name in old_tables if name not in new_tables]
        for name in copy_order(conn, extra):
            definition = conn.execute("SELECT sql FROM duckdb_tables() WHERE database_name = 'old' AND table_name = ?", [name]).fetchone()[0]
            conn.execute(definition)
        for _name, table, sql in conn.execute("SELECT index_name, table_name, sql FROM duckdb_indexes() WHERE database_name = 'old'").fetchall():
            if table in extra and sql:
                conn.execute(sql)

        if prepare is not None:
            prepare(conn)

        counts: Dict[str, int] = {}
        copied_by_callback = set()
        for table in copy_order(conn, old_tables):
            wanted = column_names(conn, current, table)
            present = set(column_names(conn, "old", table))
            action = statement_for(table, wanted, present)
            if callable(action):
                action(conn)
                copied_by_callback.add(table)
            else:
                if action is None:
                    columns = ", ".join(column for column in wanted if column in present)
                    action = f"INSERT INTO main.{table} ({columns}) SELECT {columns} FROM old.{table}"
                conn.execute(action)
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM old.{table}").fetchone()[0]

        for table, expected in counts.items():
            if table in copied_by_callback:
                continue  # such a table decides for itself which rows are kept
            copied = conn.execute(f"SELECT COUNT(*) FROM main.{table}").fetchone()[0]
            if copied != expected:
                raise MigrationError(f"Table {table} has {copied} rows after the copy but {expected} before.")

        # Sequences the current schema created start at 1; move them to where the old ones stopped.
        for name, upcoming in next_values.items():
            if upcoming > 1 and name not in recreated:
                conn.execute(f"SELECT nextval('{name}') FROM range({upcoming - 1})")

        finish(conn)
        conn.execute("DETACH old")
        conn.execute("CHECKPOINT")
    finally:
        conn.close()

    check = duckdb.connect(str(target), read_only=True)
    try:
        verify(check)
    finally:
        check.close()


def _build(source: Path, target: Path) -> None:
    def statement_for(table: str, wanted: List[str], present: set) -> Any:
        if table not in EMBEDDING_TABLES:
            return None
        columns = ", ".join(column for column in wanted if column in present and column != "embedding")
        return f"INSERT INTO main.{table} ({columns}) SELECT {columns} FROM old.{table}"

    def verify(check: Any) -> None:
        if needs_embedding_migration(check):
            raise MigrationError("The rebuilt database still has fixed-size embedding columns.")

    copy_database(source, target, statement_for, lambda conn: set_config(conn, LAYOUT_KEY, LAYOUT_VALUE), verify)
