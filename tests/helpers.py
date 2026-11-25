"""Shared test helpers."""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

import duckdb


def insert_dependency(conn: duckdb.DuckDBPyConnection, caller_id: str, callee_id: str) -> None:
    """Insert a call edge directly. Production edges are written by
    `sync_scan_results`; tests use this to build graphs by hand."""
    conn.execute(
        "INSERT INTO dependencies (caller_id, callee_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
        [caller_id, callee_id],
    )


def fid(file_path: str, qualified_name: str, *parameters: str) -> str:
    """The identifier the scanner gives a function: `fid("a.py", "K.run", "int")` for
    `def run(self, x: int)`; pass no parameters for a function that takes none."""
    from drspec.core.identity import function_identity

    return function_identity(file_path, qualified_name, list(parameters))


def publish(conn, **kwargs):
    """`sync_scan_results` the way a scan runs it: in one transaction, with the removals it records
    carried out once that has committed."""
    from drspec.core.graph_sync import sync_scan_results
    from drspec.db.staging import publish_transaction

    with publish_transaction(conn) as write:
        return sync_scan_results(write, **kwargs)


_IDENTITY_HASH = re.compile(r"#[0-9a-f]{8}")


def strip_identity_hash(project_db: Path, old_db: Path) -> None:
    """Rewrite a database of a real scan as it was before the hash existed."""
    from drspec.core.file_records import pack, unpack
    from drspec.db.identity_migration import MARKER_KEY

    shutil.copy(project_db, old_db)
    connection = duckdb.connect(str(old_db))
    columns = {
        "artifacts": ["function_id"], "artifact_embeddings": ["function_id"], "contracts": ["function_id"],
        "reasoning_traces": ["function_id"], "vision_findings": ["function_id"],
        "dependencies": ["caller_id", "callee_id"], "code_references": ["caller_id", "callee_id"],
    }
    try:
        # Rebuild the tables with the plain identifiers (the schema's foreign keys forbid updating a key in place).
        strip = lambda column: f"regexp_replace({column}, '#[0-9a-f]{{8}}$', '')"  # noqa: E731
        connection.execute("ATTACH ':memory:' AS plain")
        connection.execute("BEGIN TRANSACTION")  # one commit for the copy below, not one per statement (each commit waits for the disk)
        for table in columns:
            connection.execute(f"CREATE TABLE plain.{table} AS SELECT * FROM main.{table}")
            for column in columns[table]:
                connection.execute(f"UPDATE plain.{table} SET {column} = {strip(column)}")
        connection.execute("COMMIT")  # (the copy above: a transaction writes one attached database, so the file below is written statement by statement)
        for table in ("function_declarations",):
            for column in ("function_id", "definition_id"):
                connection.execute(f"UPDATE main.{table} SET {column} = {strip(column)}")
        for table in ("code_references", "dependencies", "vision_findings", "reasoning_traces", "contracts", "artifact_embeddings", "artifacts"):
            connection.execute(f"DELETE FROM main.{table}")
        for table in ("artifacts", "artifact_embeddings", "contracts", "reasoning_traces", "vision_findings", "dependencies", "code_references"):
            connection.execute(f"INSERT INTO main.{table} SELECT * FROM plain.{table}")
        connection.execute("DETACH plain")
        connection.execute("BEGIN TRANSACTION")
        connection.execute("DELETE FROM config WHERE key = ?", [MARKER_KEY])
        connection.execute("UPDATE config SET value = 'fingerprint-of-the-old-identifiers' WHERE key = 'scan:symbols'")
        for key, value in connection.execute("SELECT key, value FROM config").fetchall():
            new_key = _IDENTITY_HASH.sub("", key)
            new_value = value
            if key.startswith("scan:file:"):
                snapshot = unpack(value, key[len("scan:file:"):])
                for function in snapshot["functions"]:
                    function["function_id"] = _IDENTITY_HASH.sub("", function["function_id"])
                    function.pop("parameter_tokens", None)
                new_value = pack(snapshot, key[len("scan:file:"):])  # (what this version stores: the identifiers are what is old)
            if (new_key, new_value) != (key, value):
                connection.execute("DELETE FROM config WHERE key = ?", [key])
                connection.execute("INSERT INTO config (key, value) VALUES (?, ?)", [new_key, new_value])
        connection.execute("COMMIT")
        connection.execute("CHECKPOINT")
    finally:
        connection.close()


# --- proof that what a module shares was only read ------------------------------------------------------------------
# A fixture of module scope is built once and used by several tests; a test that writes into it changes what the next one sees. Each such
# fixture takes a fingerprint after it is built and compares it at the module's end, so the order of the tests cannot matter and a test that
# writes fails the module.


def table_counts(database) -> dict:
    """Rows per table of a database: a path (opened read-only, closed) or an open connection (kept open)."""
    owned = not hasattr(database, "execute")
    connection = duckdb.connect(str(database), read_only=True) if owned else database  # pattern-ok: duckdb-file the fingerprint of a shared FILE
    try:
        names = [row[0] for row in connection.execute("SELECT table_name FROM duckdb_tables() ORDER BY 1").fetchall()]
        return {name: connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in names}
    finally:
        if owned:
            connection.close()


def file_hash(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def folder_listing(root: Path) -> list:
    """Every file under `root` with its size, sorted: a folder shared by tests must list the same at the end (a database file is
    left out: its size changes when it is only opened; use `table_counts` for it)."""
    return sorted((str(path.relative_to(root)), path.stat().st_size) for path in Path(root).rglob("*") if path.is_file() and path.suffix not in (".db", ".wal"))
