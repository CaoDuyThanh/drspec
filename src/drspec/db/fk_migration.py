"""Rebuilding a database that still has secondary indexes on `artifacts` and the function vector in its row.

DuckDB rewrites an UPDATE of an indexed column, or of a list column, as a delete plus an insert of the
whole row, which a row other tables reference by foreign key cannot survive. The current layout has no
secondary index on `artifacts`, and keeps the vector in its own table (`artifact_embeddings`), so a
status change or a stored vector is a plain statement. A database from before is rebuilt in a new file
that replaces it in one atomic rename (the machinery of `db.migration`): the three indexes are not
created, every foreign key is kept, and the vectors are copied across (they are derived data, but
copying avoids embedding everything again). Any failure leaves the old file as it was.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb

from drspec.db.migration import MigrationError, copy_database, rebuild_database
from drspec.db.queries import get_config, set_config

MARKER_KEY = "schema:fk_layout"
MARKER_VALUE = "v1"
BACKUP_SUFFIX = ".before-fk-layout"

#: Indexes of `artifacts` that make an UPDATE of the indexed column rewrite the row.
REMOVED_INDEXES = ("idx_artifacts_status", "idx_artifacts_file_path", "idx_artifacts_function_name")


def backup_path(db_path: Path) -> Path:
    return Path(db_path).with_name(Path(db_path).name + BACKUP_SUFFIX)


def _marker(conn: Any):
    try:
        return get_config(conn, MARKER_KEY)
    except duckdb.CatalogException:
        return None


def _has_old_layout(conn: Any) -> bool:
    columns = conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'artifacts'").fetchall()
    if not columns:
        return False  # no artifacts table yet: an empty database
    if any(row[0] == "embedding" for row in columns):
        return True
    names = {row[0] for row in conn.execute("SELECT index_name FROM duckdb_indexes() WHERE table_name = 'artifacts'").fetchall()}
    return bool(names.intersection(REMOVED_INDEXES))


def needs_fk_migration(conn: Any) -> bool:
    return _marker(conn) != MARKER_VALUE and _has_old_layout(conn)


def ensure_fk_layout(db_path: Path) -> str:
    """Rebuild `db_path` if it has the old layout. Call before anything else opens the file. Returns `absent`,
    `current` or `migrated`; raises `MigrationError` if the rebuild failed (the old file is then unchanged)."""
    db_path = Path(db_path)
    if not db_path.exists():
        return "absent"
    try:
        conn = duckdb.connect(str(db_path))
    except duckdb.Error as e:
        raise MigrationError(f"Could not open {db_path} to check its layout: {e}") from e
    try:
        if not needs_fk_migration(conn):
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

    rebuild_database(db_path, backup_path(db_path), _build, "the foreign-key-safe layout")
    return "migrated"


def _remove_backup(db_path: Path) -> None:
    for path in (backup_path(db_path), Path(str(backup_path(db_path)) + ".wal")):
        try:
            path.unlink()
        except OSError:
            pass


def _build(source: Path, target: Path) -> None:
    expected = {"vectors": 0}

    def statement_for(table: str, wanted: list, present: set) -> Any:
        return None  # every table is copied as it is; the old vector column is simply not in the new schema

    def finish(conn: Any) -> None:
        old_columns = {row[0] for row in conn.execute("SELECT column_name FROM duckdb_columns() WHERE database_name = 'old' AND table_name = 'artifacts'").fetchall()}
        if "embedding" in old_columns:
            expected["vectors"] = conn.execute("SELECT COUNT(*) FROM old.artifacts WHERE embedding IS NOT NULL").fetchone()[0]
            conn.execute(
                "INSERT INTO main.artifact_embeddings (function_id, embedding) "
                "SELECT function_id, embedding FROM old.artifacts WHERE embedding IS NOT NULL"
            )
            copied = conn.execute("SELECT COUNT(*) FROM main.artifact_embeddings").fetchone()[0]
            if copied != expected["vectors"]:
                raise MigrationError(f"{copied} vectors were copied but {expected['vectors']} were stored.")
        set_config(conn, MARKER_KEY, MARKER_VALUE)

    def verify(check: Any) -> None:
        if _has_old_layout(check):
            raise MigrationError("The rebuilt database still has the old layout.")
        foreign = check.execute("SELECT COUNT(*) FROM duckdb_constraints() WHERE constraint_type = 'FOREIGN KEY'").fetchone()[0]
        if foreign == 0:
            raise MigrationError("The rebuilt database lost its foreign keys.")

    copy_database(source, target, statement_for, finish, verify)
