"""Typed query functions for DrSpec database operations."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import duckdb


# =============================================================================
# Status Values
# =============================================================================

VALID_ARTIFACT_STATUSES = frozenset({"PENDING", "VERIFIED", "NEEDS_REVIEW", "STALE", "BROKEN"})
VALID_FINDING_TYPES = frozenset({"outlier", "discontinuity", "boundary", "correlation", "missing_pattern"})
VALID_FINDING_SIGNIFICANCE = frozenset({"HIGH", "MEDIUM", "LOW"})
VALID_FINDING_STATUSES = frozenset({"NEW", "ADDRESSED", "IGNORED"})


# =============================================================================
# Artifact Model
# =============================================================================


@dataclass
class Artifact:
    """Represents a scanned function artifact.

    Attributes:
        function_id: Unique function ID (filepath::function_name).
        file_path: Relative path from project root.
        function_name: Function/method name.
        signature: Full function signature.
        body: Function source code.
        code_hash: SHA-256 hash of normalized code.
        language: Programming language (python, javascript, cpp).
        start_line: 1-indexed start line.
        end_line: 1-indexed end line.
        parent: Parent class/namespace if applicable.
        status: Status (PENDING, VERIFIED, NEEDS_REVIEW, STALE, BROKEN).
        is_property: True if this is a Python `@property` method — always
            False for JS/C++, which this project doesn't detect properties
            for yet.
        created_at: Creation timestamp.
        updated_at: Last update timestamp.
    """

    function_id: str
    file_path: str
    function_name: str
    signature: str
    body: str
    code_hash: str
    language: str
    start_line: int
    end_line: int
    parent: Optional[str]
    status: str
    is_property: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: tuple) -> "Artifact":
        """Create Artifact from database row.

        Args:
            row: Database row tuple in column order.

        Returns:
            Artifact instance.
        """
        return cls(
            function_id=row[0],
            file_path=row[1],
            function_name=row[2],
            signature=row[3],
            body=row[4],
            code_hash=row[5],
            language=row[6],
            start_line=row[7],
            end_line=row[8],
            parent=row[9],
            status=row[10],
            is_property=row[11],
            created_at=row[12],
            updated_at=row[13],
        )


# =============================================================================
# Artifact Queries
# =============================================================================

def language_filter(language: str) -> tuple:
    """`(sql, parameters)` for `language IN (...)`: the stored values a `--include` name stands for (`cpp` is C++ and C)."""
    from drspec.core.languages import languages_matching

    values = list(languages_matching(language))
    return "language IN (" + ", ".join("?" for _ in values) + ")", values


#: `column IN_LIST` with one list parameter: a lookup of many keys in one statement. (Many placeholders in one `IN (...)`,
#: in chunks, cost about ten times as much for the same rows.)
IN_LIST = "IN (SELECT unnest(?::VARCHAR[]))"

#: Parameters in one statement of the multi-row writes below.
_PARAMETERS_PER_STATEMENT = 3000

#: The columns of `artifacts` a new function is written with (`created_at` has its default; `updated_at` is `now()`).
ARTIFACT_COLUMNS = (
    "function_id", "file_path", "function_name", "signature", "body", "code_hash", "language",
    "start_line", "end_line", "parent", "status", "is_property",
)


def insert_rows(conn: duckdb.DuckDBPyConnection, table: str, columns: Sequence[str], rows: Sequence[Sequence[Any]], now_columns: Sequence[str] = (), conflict: str = "") -> None:
    """Write many rows with a few statements, each with many rows, instead of one statement per row.

    The cost of a write to a table with a key and foreign keys is mostly the cost of the statement, so a publish of a
    large project spent most of its time here. `now_columns` are filled with the current time; `conflict` is appended
    (an `ON CONFLICT ... DO UPDATE` clause, whose rows must then have distinct keys)."""
    if not rows:
        return
    names = list(columns) + list(now_columns)
    marks = ", ".join(["?"] * len(columns) + ["now()"] * len(now_columns))
    size = max(1, _PARAMETERS_PER_STATEMENT // max(1, len(columns)))
    for start in range(0, len(rows), size):
        chunk = rows[start : start + size]
        values = ", ".join(f"({marks})" for _ in chunk)
        parameters = [value for row in chunk for value in row]
        conn.execute(f"INSERT INTO {table} ({', '.join(names)}) VALUES {values} {conflict}", parameters)


def insert_artifacts(conn: duckdb.DuckDBPyConnection, rows: Sequence[Sequence[Any]]) -> None:
    """New functions, one row each in the order of `ARTIFACT_COLUMNS` (see `insert_artifact` for one at a time)."""
    insert_rows(conn, "artifacts", ARTIFACT_COLUMNS, rows, now_columns=("updated_at",))


def upsert_edges(conn: duckdb.DuckDBPyConnection, table: str, site_column: str, rows: Sequence[Sequence[Any]]) -> None:
    """`(caller_id, callee_id, site, edge_hash, confidence)` rows of `dependencies` or `code_references`, each inserted or,
    where the pair exists at any tier, updated. Later rows of the same pair replace earlier ones."""
    unique = {(row[0], row[1]): row for row in rows}
    insert_rows(
        conn, table, ("caller_id", "callee_id", site_column, "edge_hash", "confidence"), list(unique.values()),
        conflict=f"ON CONFLICT (caller_id, callee_id) DO UPDATE SET {site_column} = excluded.{site_column}, "
        "edge_hash = excluded.edge_hash, embedding = NULL, confidence = excluded.confidence",
    )


def check_embedding_length(vector: Optional[list[float]]) -> None:
    """Refuse a vector of the wrong size. The column is a variable-length list,
    so the database no longer enforces the model's dimension."""
    from drspec.core.embedder import EMBEDDING_DIM

    if vector is not None and len(vector) != EMBEDDING_DIM:
        raise ValueError(f"An embedding must have {EMBEDDING_DIM} values, got {len(vector)}.")


def set_artifact_embedding(conn: duckdb.DuckDBPyConnection, function_id: str, vector: Optional[list[float]]) -> None:
    """Store a function's vector, or remove it with `None`. It is its own table, written as a delete plus an
    insert, because a list column of a row other tables reference cannot be updated in place."""
    check_embedding_length(vector)
    conn.execute("DELETE FROM artifact_embeddings WHERE function_id = ?", [function_id])
    if vector is not None:
        conn.execute("INSERT INTO artifact_embeddings (function_id, embedding) VALUES (?, ?)", [function_id, vector])


def insert_artifact(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
    file_path: str,
    function_name: str,
    signature: str,
    body: str,
    code_hash: str,
    language: str,
    start_line: int,
    end_line: int,
    parent: Optional[str] = None,
    status: str = "PENDING",
    embedding: Optional[list[float]] = None,
    is_property: bool = False,
) -> bool:
    """Insert or update an artifact with hash change detection.

    Uses upsert behavior: if artifact exists and hash differs, updates it.
    If the existing artifact is VERIFIED or NEEDS_REVIEW and the hash changed,
    status is automatically set to STALE.

    Args:
        conn: DuckDB connection.
        function_id: Unique function ID (filepath::function_name).
        file_path: Relative file path.
        function_name: Function name.
        signature: Full function signature.
        body: Function source code.
        code_hash: SHA-256 hash of normalized code.
        language: Programming language (python, javascript, cpp).
        start_line: 1-indexed start line.
        end_line: 1-indexed end line.
        parent: Parent class/namespace if applicable.
        status: Status (PENDING, VERIFIED, NEEDS_REVIEW, STALE, BROKEN).
        embedding: Local embedding of `body`, 384 floats. Always written
            as given — `None` writes `NULL`, since a changed node scanned
            without `--embed` must not keep a stale embedding from before
            the change.
        is_property: True if this is a Python `@property` method — the
            reference resolver's `build_properties_by_name` reads this
            column to know which attribute reads are property reads.

    Returns:
        True if the artifact was changed (new or hash changed), False otherwise.
    """
    check_embedding_length(embedding)
    existing = conn.execute(
        "SELECT code_hash, status, file_path, function_name FROM artifacts WHERE function_id = ?",
        [function_id],
    ).fetchone()

    if existing is not None:
        old_hash, old_status, old_file_path, old_function_name = existing
        if old_hash == code_hash:
            current = conn.execute(
                "SELECT signature, body, start_line, end_line, parent, is_property, language FROM artifacts WHERE function_id = ?",
                [function_id],
            ).fetchone()
            values = (signature, body, start_line, end_line, parent, is_property, language)
            if current != values:
                conn.execute("UPDATE artifacts SET signature=?, body=?, start_line=?, end_line=?, parent=?, is_property=?, language=?, updated_at=now() WHERE function_id=?",
                             [*values, function_id])
            return False

        if old_status in ("VERIFIED", "NEEDS_REVIEW"):
            status = "STALE"
        elif old_status == "BROKEN":
            status = "BROKEN"  # Keep broken status, requires manual reset
        # PENDING stays as PENDING

        conn.execute(
            "UPDATE artifacts SET file_path=?, function_name=?, signature=?, body=?, code_hash=?, language=?, start_line=?, end_line=?, "
            "parent=?, status=?, is_property=?, updated_at=now() WHERE function_id=?",
            [file_path, function_name, signature, body, code_hash, language, start_line, end_line, parent, status, is_property, function_id],
        )
        # A changed function must not keep a vector from before the change.
        set_artifact_embedding(conn, function_id, embedding)
        return True

    conn.execute(
        """
        INSERT INTO artifacts (
            function_id, file_path, function_name, signature, body, code_hash,
            language, start_line, end_line, parent, status, is_property, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())
        """,
        [function_id, file_path, function_name, signature, body, code_hash, language, start_line, end_line, parent, status, is_property],
    )
    if embedding is not None:
        set_artifact_embedding(conn, function_id, embedding)
    return True


def get_artifact(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
) -> Optional[Artifact]:
    """Get an artifact by function ID.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to look up.

    Returns:
        Artifact object or None if not found.
    """
    result = conn.execute(
        """SELECT function_id, file_path, function_name, signature, body, code_hash,
                  language, start_line, end_line, parent, status, is_property, created_at, updated_at
           FROM artifacts WHERE function_id = ?""",
        [function_id],
    ).fetchone()

    if result is None:
        return None

    return Artifact.from_row(result)


class AmbiguousFunctionId(ValueError):
    """A `file::name` that several functions of that file share (overloads, a property
    and its setter). `candidates` are `(function_id, signature)` pairs."""

    def __init__(self, given: str, candidates: list[tuple[str, str]]) -> None:
        self.given = given
        self.candidates = candidates
        listing = "; ".join(f"{function_id} ({signature})" for function_id, signature in candidates)
        super().__init__(f"'{given}' matches {len(candidates)} functions; use the full identifier: {listing}")


def resolve_function_id(conn: duckdb.DuckDBPyConnection, function_id: str) -> str:
    """The stored identifier a user-supplied one stands for.

    An identifier that is stored is returned as it is. `file::name` without the
    trailing `#hash` stands for the one function with that qualified name in that
    file; if several share it, `AmbiguousFunctionId` lists them. Anything else
    (including an unknown identifier) is returned unchanged for the caller to report."""
    if conn.execute("SELECT 1 FROM artifacts WHERE function_id = ?", [function_id]).fetchone():
        return function_id
    alias = conn.execute("SELECT definition_id FROM function_declarations WHERE function_id = ? AND definition_id IS NOT NULL", [function_id]).fetchone()
    if alias:
        return alias[0]  # a header's declaration stands for the definition
    prefix = function_id + "#"
    alias_rows = conn.execute(
        "SELECT DISTINCT definition_id FROM function_declarations WHERE starts_with(function_id, ?) AND length(function_id) = ? AND definition_id IS NOT NULL",
        [prefix, len(prefix) + 8],
    ).fetchall()
    if len(alias_rows) == 1:
        return alias_rows[0][0]
    rows = conn.execute(
        "SELECT function_id, signature FROM artifacts WHERE starts_with(function_id, ?) AND length(function_id) = ? ORDER BY function_id",
        [prefix, len(prefix) + 8],
    ).fetchall()
    if len(rows) == 1:
        return rows[0][0]
    if len(rows) > 1:
        raise AmbiguousFunctionId(function_id, [(row[0], row[1]) for row in rows])
    return _without_its_namespace(conn, function_id)


def _without_its_namespace(conn: duckdb.DuckDBPyConnection, function_id: str) -> str:
    """`file::Class::name` for the function stored as `file::ns::Class::name#hash` (a C++ name written without its namespace): the one
    function of that file whose qualified name ends with what was written. Several (overloads, the same name in two namespaces) are
    `AmbiguousFunctionId`; none leaves the identifier as it is."""
    file_path, separator, written = function_id.partition("::")
    if not separator or not written or "#" in written:
        return function_id
    name = written.replace(".", "::").rsplit("::", 1)[-1]
    found = []
    for candidate, signature in conn.execute(
        "SELECT function_id, signature FROM artifacts WHERE file_path = ? AND function_name = ? ORDER BY function_id", [file_path, name]
    ).fetchall():
        qualified = candidate[len(file_path) + 2 : candidate.rfind("#")]
        if qualified.endswith("::" + written) or qualified.endswith("." + written):
            found.append((candidate, signature))
    if len(found) == 1:
        return found[0][0]
    if len(found) > 1:
        raise AmbiguousFunctionId(function_id, found)
    return function_id


def get_artifact_code_hash(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
) -> Optional[str]:
    """Get just an artifact's `code_hash`, or `None` if it doesn't exist —
    avoids pulling the full row (`body`/`signature` can be large) when
    the caller only needs this one field to compare.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to look up.

    Returns:
        The stored `code_hash`, or `None` if no such artifact exists.
    """
    result = conn.execute(
        "SELECT code_hash FROM artifacts WHERE function_id = ?",
        [function_id],
    ).fetchone()
    return result[0] if result is not None else None


def list_artifacts(
    conn: duckdb.DuckDBPyConnection,
    status: Optional[str] = None,
    file_path: Optional[str] = None,
    language: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Artifact]:
    """List artifacts with optional filters.

    Args:
        conn: DuckDB connection.
        status: Optional status filter.
        file_path: Optional file path prefix filter.
        language: Optional language filter.
        limit: Maximum number of results.
        offset: Number of results to skip.

    Returns:
        List of Artifact objects.
    """
    query = """SELECT function_id, file_path, function_name, signature, body, code_hash,
                      language, start_line, end_line, parent, status, is_property, created_at, updated_at
               FROM artifacts WHERE 1=1"""
    params: list[Any] = []

    if status is not None:
        query += " AND status = ?"
        params.append(status)

    if file_path is not None:
        query += " AND file_path LIKE ?"
        params.append(f"{file_path}%")

    if language is not None:
        fragment, values = language_filter(language)
        query += " AND " + fragment
        params.extend(values)

    query += " ORDER BY file_path, start_line LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    results = conn.execute(query, params).fetchall()
    return [Artifact.from_row(row) for row in results]


# Matches this project's own pytest config (`test_*.py` under `tests/`),
# not every possible test-naming convention — e.g. `*_test.py` is
# deliberately excluded.
_TEST_FILE_PATH_SQL = (
    "({col} LIKE 'tests/%'"
    " OR {col} LIKE 'test/%'"
    " OR {col} LIKE '%/tests/%'"
    " OR {col} LIKE '%/test/%'"
    " OR {col} LIKE '%/\\_\\_tests\\_\\_/%' ESCAPE '\\'"
    " OR {col} LIKE '%.test.%'"
    " OR {col} LIKE '%.spec.%'"
    " OR {col} LIKE 'test\\_%.py' ESCAPE '\\'"
    " OR {col} LIKE '%/test\\_%.py' ESCAPE '\\')"
)

# Included if it has no caller/reference at all, OR every
# caller/reference is itself a test file — a function called only by
# its own tests is nearly as strong a dead-code signal as zero callers.
# Which case applies is resolved separately by
# `only_test_caller_function_ids`, since `Artifact.from_row` expects a
# fixed column shape.
_UNUSED_WHERE = f"""
    WHERE NOT (starts_with(function_name, '__') AND ends_with(function_name, '__'))
      AND function_id NOT IN (SELECT function_id FROM function_declarations WHERE definition_id IS NULL)
      AND NOT (language IN ('javascript', 'typescript', 'tsx', 'rust') AND {_TEST_FILE_PATH_SQL.format(col="file_path")})
      AND NOT (language = 'rust' AND function_id LIKE '%::tests::%')
      AND NOT EXISTS (
          SELECT 1 FROM dependencies d
          JOIN artifacts caller ON caller.function_id = d.caller_id
          WHERE d.callee_id = artifacts.function_id
            AND NOT {_TEST_FILE_PATH_SQL.format(col="caller.file_path")}
      )
      AND NOT EXISTS (
          SELECT 1 FROM code_references r
          JOIN artifacts caller ON caller.function_id = r.caller_id
          WHERE r.callee_id = artifacts.function_id
            AND NOT {_TEST_FILE_PATH_SQL.format(col="caller.file_path")}
      )
"""


def list_unused_artifacts(
    conn: duckdb.DuckDBPyConnection,
    path_prefix: Optional[str] = None,
    limit: int = 100,
) -> list[Artifact]:
    """Functions with no real-code caller/reference anywhere, project-wide
    — a dead-code *candidate* list, not proof. Includes both zero-caller
    functions and ones called only from test files; see
    `only_test_caller_function_ids` to tell those apart.

    Dunder/magic methods are excluded in SQL since they're invoked
    implicitly by the language runtime, never via a resolved static call.
    CLI entry points and reflection-invoked functions are deliberately
    NOT excluded — auto-detecting "looks like an entry point" would be a
    guess; use `path_prefix` to exclude a known entry-point directory
    instead.

    Requires package-re-export import resolution to be correct, or a
    function only ever imported via `from package import X` would show
    up here as a false positive.
    """
    query = f"SELECT {_ARTIFACT_SEARCH_COLUMNS} FROM artifacts {_UNUSED_WHERE}"
    params: list[Any] = []
    if path_prefix is not None:
        query += " AND file_path LIKE ?"
        params.append(f"{path_prefix}%")
    query += " ORDER BY file_path, start_line LIMIT ?"
    params.append(limit)
    results = conn.execute(query, params).fetchall()
    return [Artifact.from_row(row) for row in results]


def count_unused_artifacts(
    conn: duckdb.DuckDBPyConnection,
    path_prefix: Optional[str] = None,
) -> int:
    """Total count for `list_unused_artifacts`, not capped by its `limit`."""
    query = f"SELECT COUNT(*) FROM artifacts {_UNUSED_WHERE}"
    params: list[Any] = []
    if path_prefix is not None:
        query += " AND file_path LIKE ?"
        params.append(f"{path_prefix}%")
    return conn.execute(query, params).fetchone()[0]


def only_test_caller_function_ids(conn: duckdb.DuckDBPyConnection, function_ids: list[str]) -> set[str]:
    """Among `function_ids` (expected to already have no non-test
    caller/reference, e.g. from `list_unused_artifacts`), which ones have
    at least one real caller/reference at all — necessarily a test one,
    given that precondition.
    """
    if not function_ids:
        return set()
    placeholders = ", ".join("?" for _ in function_ids)
    rows = conn.execute(
        f"""
        SELECT function_id FROM artifacts
        WHERE function_id IN ({placeholders})
          AND (
            EXISTS (SELECT 1 FROM dependencies d WHERE d.callee_id = artifacts.function_id)
            OR EXISTS (SELECT 1 FROM code_references r WHERE r.callee_id = artifacts.function_id)
          )
        """,
        list(function_ids),
    ).fetchall()
    return {row[0] for row in rows}


def count_artifacts(
    conn: duckdb.DuckDBPyConnection,
    status: Optional[str] = None,
) -> int:
    """Count artifacts with optional status filter.

    Args:
        conn: DuckDB connection.
        status: Optional status filter.

    Returns:
        Number of artifacts.
    """
    if status is not None:
        result = conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE status = ?",
            [status],
        ).fetchone()
    else:
        result = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()

    return result[0] if result else 0


def update_artifact_status(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
    status: str,
) -> bool:
    """Update artifact status.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to update.
        status: New status value.

    Returns:
        True if artifact was found and updated.

    Raises:
        ValueError: If status is not a valid status value.
    """
    if status not in VALID_ARTIFACT_STATUSES:
        raise ValueError(f"Invalid status: {status}. Must be one of {VALID_ARTIFACT_STATUSES}")

    # Check if artifact exists first (DuckDB rowcount not reliable)
    existing = conn.execute(
        "SELECT 1 FROM artifacts WHERE function_id = ?", [function_id]
    ).fetchone()
    if existing is None:
        return False

    conn.execute("UPDATE artifacts SET status=?, updated_at=now() WHERE function_id=?", [status, function_id])
    return True


def get_artifacts_by_file(
    conn: duckdb.DuckDBPyConnection,
    file_path: str,
) -> list[Artifact]:
    """Get all artifacts stored for an exact file path.

    Unlike `list_artifacts(file_path=...)`, this matches `file_path` exactly
    (not as a prefix) — used for the node-removal diff, which needs every
    artifact currently stored for *this* file, not files whose path happens
    to start with the same string.

    Args:
        conn: DuckDB connection.
        file_path: Exact relative file path.

    Returns:
        List of Artifact objects stored for this file.
    """
    results = conn.execute(
        """SELECT function_id, file_path, function_name, signature, body, code_hash,
                  language, start_line, end_line, parent, status, is_property, created_at, updated_at
           FROM artifacts WHERE file_path = ?""",
        [file_path],
    ).fetchall()
    return [Artifact.from_row(row) for row in results]


# =============================================================================
# Search Queries (`drspec search`, grep-flag-compatible)
# =============================================================================

_ARTIFACT_SEARCH_COLUMNS = (
    "function_id, file_path, function_name, signature, body, code_hash, "
    "language, start_line, end_line, parent, status, is_property, created_at, updated_at"
)


#: A function's qualified name as the ids and `refs get` / `deps get` print it (`ns::Class::method`): its owner, then its name. A function
#: with no owner (a free function outside any namespace) is its own name.
_QUALIFIED_NAME = "(CASE WHEN parent IS NULL OR parent = '' THEN function_name ELSE parent || '::' || function_name END)"


def qualified_name_of(artifact: "Artifact") -> str:
    """The same name in Python: `ns::Class::method` for a function with an owner, its own name otherwise."""
    return f"{artifact.parent}::{artifact.function_name}" if artifact.parent else artifact.function_name


def matched_only_by_qualified_name(artifact: "Artifact", pattern: str, ignore_case: bool = False, regex: bool = False) -> bool:
    """Whether `pattern` found `artifact` through its qualified name alone (not its name, signature or body): `Producer` finds
    the methods of `Producer` that way. A pattern the Python engine cannot read says no."""
    if not pattern:
        return False
    if regex:
        try:
            compiled = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error:
            return False
        found = lambda text: compiled.search(text or "") is not None  # noqa: E731
    else:
        needle = pattern.lower() if ignore_case else pattern
        found = lambda text: needle in ((text or "").lower() if ignore_case else (text or ""))  # noqa: E731
    return found(qualified_name_of(artifact)) and not (found(artifact.function_name) or found(artifact.signature) or found(artifact.body))


def _build_search_where(
    pattern: str,
    ignore_case: bool,
    regex: bool,
    invert_match: bool,
    language: Optional[str],
    path_prefix: Optional[str],
    status: Optional[str],
    confidence: Optional[str],
) -> tuple[str, list[Any]]:
    """Build the shared `WHERE` clause + params for literal/regex search
    over `function_name`/`signature`/`body`, used by `search_artifacts`,
    `count_search_artifacts`, and `list_search_file_paths` alike — kept as
    one function so the regex-flag/LIKE-escaping logic isn't duplicated
    three times with room for the copies to drift.
    """
    if regex:
        flags = "i" if ignore_case else ""
        match_clause = (
            "(regexp_matches(function_name, ?, ?) OR regexp_matches(signature, ?, ?) "
            f"OR regexp_matches(body, ?, ?) OR regexp_matches({_QUALIFIED_NAME}, ?, ?))"
        )
        params: list[Any] = [pattern, flags, pattern, flags, pattern, flags, pattern, flags]
    else:
        # Literal substring match, not a general LIKE pattern — `%`/`_`
        # are escaped so a pattern containing them (common in real code:
        # `__init__`, `100%`) is matched literally, not as LIKE wildcards.
        escaped = pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like_pattern = f"%{escaped}%"
        operator = "ILIKE" if ignore_case else "LIKE"
        match_clause = (
            f"(function_name {operator} ? ESCAPE '\\' OR signature {operator} ? ESCAPE '\\' "
            f"OR body {operator} ? ESCAPE '\\' OR {_QUALIFIED_NAME} {operator} ? ESCAPE '\\')"
        )
        params = [like_pattern, like_pattern, like_pattern, like_pattern]

    if invert_match:
        match_clause = f"NOT {match_clause}"

    where = f"WHERE {match_clause}"

    if language is not None:
        fragment, values = language_filter(language)
        where += " AND " + fragment
        params.extend(values)
    if path_prefix is not None:
        where += " AND file_path LIKE ?"
        params.append(f"{path_prefix}%")
    if status is not None:
        where += " AND status = ?"
        params.append(status)
    if confidence is not None:
        # "This function has at least one outgoing edge resolved at this
        # confidence tier" — the only sense `--confidence` can sensibly
        # have for a function-level search (confidence is an edge
        # property, not a function property); e.g. "show me functions
        # with an AMBIGUOUS dependency worth reviewing."
        where += " AND EXISTS (SELECT 1 FROM dependencies d WHERE d.caller_id = artifacts.function_id AND d.confidence = ?)"
        params.append(confidence)

    return where, params


def search_artifacts(
    conn: duckdb.DuckDBPyConnection,
    pattern: str,
    ignore_case: bool = False,
    regex: bool = False,
    invert_match: bool = False,
    language: Optional[str] = None,
    path_prefix: Optional[str] = None,
    status: Optional[str] = None,
    confidence: Optional[str] = None,
    limit: int = 100,
) -> list[Artifact]:
    """Search artifacts by `pattern` against `function_name`/`signature`/
    `body` — grep-flag-compatible: `ignore_case` is `-i`, `regex` is
    `-E`, `invert_match` is `-v`.

    Returns:
        Matching Artifact objects, ordered by file_path/start_line.
    """
    where, params = _build_search_where(
        pattern, ignore_case, regex, invert_match, language, path_prefix, status, confidence
    )
    rank, rank_params = _relevance_order(pattern, ignore_case, regex, invert_match)
    query = f"SELECT {_ARTIFACT_SEARCH_COLUMNS} FROM artifacts {where} ORDER BY {rank}file_path, start_line LIMIT ?"
    params = [*params, *rank_params, limit]
    results = conn.execute(query, params).fetchall()
    return [Artifact.from_row(row) for row in results]


def _relevance_order(pattern: str, ignore_case: bool, regex: bool, invert_match: bool) -> tuple[str, list[Any]]:
    """`ORDER BY` terms that put the function a name search is for first: the exact name, then names that start
    with the pattern, then names that contain it, then a signature that does, then a body that does; within a
    rank the order is by file and line. Nothing to rank for an empty pattern or a `-v` search."""
    if not pattern or invert_match:
        return "", []
    if regex:
        flags = "i" if ignore_case else ""
        full = f"^(?:{pattern})$"
        return (
            f"CASE WHEN regexp_matches(function_name, ?, ?) OR regexp_matches({_QUALIFIED_NAME}, ?, ?) THEN 0 "
            f"WHEN regexp_matches(function_name, ?, ?) THEN 2 WHEN regexp_matches({_QUALIFIED_NAME}, ?, ?) THEN 3 "
            "WHEN regexp_matches(signature, ?, ?) THEN 4 ELSE 5 END, ",
            [full, flags, full, flags, pattern, flags, pattern, flags, pattern, flags],
        )
    escaped = pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    operator = "ILIKE" if ignore_case else "LIKE"
    exact = "lower(function_name) = lower(?)" if ignore_case else "function_name = ?"
    exact_qualified = f"lower({_QUALIFIED_NAME}) = lower(?)" if ignore_case else f"{_QUALIFIED_NAME} = ?"
    return (
        f"CASE WHEN {exact} OR {exact_qualified} THEN 0 WHEN function_name {operator} ? ESCAPE '\\' THEN 1 "
        f"WHEN function_name {operator} ? ESCAPE '\\' THEN 2 WHEN {_QUALIFIED_NAME} {operator} ? ESCAPE '\\' THEN 3 "
        f"WHEN signature {operator} ? ESCAPE '\\' THEN 4 ELSE 5 END, ",
        [pattern, pattern, f"{escaped}%", f"%{escaped}%", f"%{escaped}%", f"%{escaped}%"],
    )


def count_search_artifacts(
    conn: duckdb.DuckDBPyConnection,
    pattern: str,
    ignore_case: bool = False,
    regex: bool = False,
    invert_match: bool = False,
    language: Optional[str] = None,
    path_prefix: Optional[str] = None,
    status: Optional[str] = None,
    confidence: Optional[str] = None,
) -> int:
    """`-c`/`--count`: total matches, not capped by `search_artifacts`'s
    `limit` — a `COUNT(*)` query, not `len(search_artifacts(...))`."""
    where, params = _build_search_where(
        pattern, ignore_case, regex, invert_match, language, path_prefix, status, confidence
    )
    result = conn.execute(f"SELECT COUNT(*) FROM artifacts {where}", params).fetchone()
    return result[0] if result else 0


def list_search_file_paths(
    conn: duckdb.DuckDBPyConnection,
    pattern: str,
    ignore_case: bool = False,
    regex: bool = False,
    invert_match: bool = False,
    language: Optional[str] = None,
    path_prefix: Optional[str] = None,
    status: Optional[str] = None,
    confidence: Optional[str] = None,
) -> list[str]:
    """`-l`/`--files-with-matches`: distinct file paths with at least one
    match, not capped by `search_artifacts`'s `limit`."""
    where, params = _build_search_where(
        pattern, ignore_case, regex, invert_match, language, path_prefix, status, confidence
    )
    results = conn.execute(f"SELECT DISTINCT file_path FROM artifacts {where} ORDER BY file_path", params).fetchall()
    return [row[0] for row in results]


def list_indexed_files(
    conn: duckdb.DuckDBPyConnection,
    language: Optional[str] = None,
    path_prefix: Optional[str] = None,
) -> list[str]:
    """Every file with at least one indexed function, ordered by path —
    the set of files a live line search can reach."""
    clauses: list[str] = []
    params: list[Any] = []
    if language is not None:
        fragment, values = language_filter(language)
        clauses.append(fragment)
        params.extend(values)
    if path_prefix is not None:
        clauses.append("starts_with(file_path, ?)")
        params.append(path_prefix)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(f"SELECT DISTINCT file_path FROM artifacts {where} ORDER BY file_path", params).fetchall()
    return [row[0] for row in rows]


def list_function_ranges(
    conn: duckdb.DuckDBPyConnection,
    file_paths: list[str],
) -> dict[str, list[tuple[str, int, int]]]:
    """`{file_path: [(function_id, start_line, end_line), ...]}` for the
    given files, as of the last scan."""
    if not file_paths:
        return {}
    placeholders = ", ".join("?" for _ in file_paths)
    rows = conn.execute(
        f"SELECT file_path, function_id, start_line, end_line FROM artifacts WHERE file_path IN ({placeholders})",
        list(file_paths),
    ).fetchall()
    ranges: dict[str, list[tuple[str, int, int]]] = {}
    for file_path, function_id, start_line, end_line in rows:
        ranges.setdefault(file_path, []).append((function_id, start_line, end_line))
    return ranges


@dataclass
class SemanticSearchResult:
    """One semantic search hit: an artifact plus its cosine-similarity
    score against the query embedding."""

    artifact: Artifact
    score: float


def count_semantic_scope(
    conn: duckdb.DuckDBPyConnection,
    language: Optional[str] = None,
    path_prefix: Optional[str] = None,
    status: Optional[str] = None,
) -> tuple[int, int]:
    """`(searched, total)`: the functions in the scope of a `--semantic` search that have an embedding (the only ones it can find) and all
    the functions in that scope. The filters are `search_artifacts_semantic`'s."""
    query = (
        "SELECT COUNT(*), COUNT(e.function_id) FROM artifacts LEFT JOIN artifact_embeddings e ON e.function_id = artifacts.function_id WHERE TRUE"
    )
    params: list[Any] = []
    if language is not None:
        fragment, values = language_filter(language)
        query += " AND " + fragment
        params.extend(values)
    if path_prefix is not None:
        query += " AND file_path LIKE ?"
        params.append(f"{path_prefix}%")
    if status is not None:
        query += " AND status = ?"
        params.append(status)
    total, embedded = conn.execute(query, params).fetchone()
    return int(embedded), int(total)


def search_artifacts_semantic(
    conn: duckdb.DuckDBPyConnection,
    query_embedding: list[float],
    language: Optional[str] = None,
    path_prefix: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 20,
) -> list[SemanticSearchResult]:
    """`--semantic`: nearest-neighbor search over `artifact_embeddings`
    by cosine similarity, using DuckDB's native `list_cosine_similarity`
    — no separate vector store. No `-i`/`-E`/`-v`/`--confidence`
    equivalent here; see `search.py`'s own validation for why those are
    rejected together with `--semantic` instead of silently ignored.

    Returns:
        Matches ordered by similarity score, descending (most similar first).
    """
    check_embedding_length(query_embedding)
    query = (
        f"SELECT {_ARTIFACT_SEARCH_COLUMNS.replace('function_id,', 'artifacts.function_id,', 1)}, list_cosine_similarity(e.embedding, ?::FLOAT[]) AS score "
        "FROM artifacts JOIN artifact_embeddings e ON e.function_id = artifacts.function_id WHERE TRUE"
    )
    params: list[Any] = [query_embedding]

    if language is not None:
        fragment, values = language_filter(language)
        query += " AND " + fragment
        params.extend(values)
    if path_prefix is not None:
        query += " AND file_path LIKE ?"
        params.append(f"{path_prefix}%")
    if status is not None:
        query += " AND status = ?"
        params.append(status)

    query += " ORDER BY score DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    return [SemanticSearchResult(artifact=Artifact.from_row(row[:-1]), score=row[-1]) for row in rows]


def any_artifact_has_embedding(conn: duckdb.DuckDBPyConnection) -> bool:
    """True if at least one artifact in the whole project has an
    embedding. Since `drspec scan` no longer embeds by default, an empty
    `--semantic` result is now the *normal* state for a project that's
    never run `--embed`, not a transient or
    surprising one — `search.py` uses this to tell those two "zero
    matches" cases apart in its response, rather than returning an
    unexplained empty list either way.
    """
    return conn.execute("SELECT 1 FROM artifact_embeddings LIMIT 1").fetchone() is not None


def delete_artifact(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
) -> bool:
    """Delete an artifact row.

    Callers must delete any `dependencies`/`reasoning_traces`/
    `vision_findings`/`contracts` rows that reference this
    `function_id` first — there is no `ON DELETE CASCADE` in
    `schema.sql`, so DuckDB rejects this delete outright (a
    `ConstraintException`) while a referencing row still exists.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to delete.

    Returns:
        True if a row was found and deleted.
    """
    existing = conn.execute(
        "SELECT 1 FROM artifacts WHERE function_id = ?", [function_id]
    ).fetchone()
    if existing is None:
        return False

    conn.execute("DELETE FROM artifacts WHERE function_id = ?", [function_id])
    return True


def delete_dependencies_for_function(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
) -> None:
    """Delete every `dependencies` row where `function_id` is the caller or callee.

    Part of the cascade for hard-deleting a node.

    Args:
        conn: DuckDB connection.
        function_id: Function ID being removed.
    """
    conn.execute(
        "DELETE FROM dependencies WHERE caller_id = ? OR callee_id = ?",
        [function_id, function_id],
    )


def delete_references_for_function(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
) -> None:
    """Delete every `code_references` row where `function_id` is the caller
    or callee — the same cascade `dependencies` gets, mirrored for the
    separate non-call relation table.

    Args:
        conn: DuckDB connection.
        function_id: Function ID being removed.
    """
    conn.execute(
        "DELETE FROM code_references WHERE caller_id = ? OR callee_id = ?",
        [function_id, function_id],
    )
    delete_member_references_for_function(conn, function_id)


def delete_member_references_for_function(conn: duckdb.DuckDBPyConnection, function_id: str) -> None:
    """Delete the member references of the function `function_id` (the cascade of a removed function, and the first half of
    replacing them when it is read again)."""
    conn.execute("DELETE FROM member_references WHERE caller_id = ?", [function_id])


def replace_member_references(conn: duckdb.DuckDBPyConnection, caller_id: str, rows: Sequence[Sequence[Any]]) -> None:
    """The member references of one function: what it had is deleted and `rows` are written, each
    `(owner_class, member, kind, file_path, line, site, receiver[, written_through])`; a repeated row (same class, member, kind, line, site
    and written-through members) is one."""
    conn.execute("DELETE FROM member_references WHERE caller_id = ?", [caller_id])
    unique = {_key(row): _full(row) for row in rows}
    if unique:
        conn.executemany(
            "INSERT INTO member_references (caller_id, owner_class, member, kind, file_path, line, site, receiver, written_through) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [[caller_id, *row] for row in unique.values()],
        )


def _full(row: Sequence[Any]) -> tuple:
    """A member reference row as stored: the seven columns, and `written_through` ('' for a row given without it)."""
    owner, member, kind, file_path, line, site, receiver, *rest = row
    return (owner, member, kind, file_path, line, site, receiver, rest[0] if rest else "")


def _key(row: Sequence[Any]) -> tuple:
    owner, member, kind, _file, line, site, _receiver, *rest = row
    return (owner, member, kind, line, site, rest[0] if rest else "")


def replace_member_references_many(conn: duckdb.DuckDBPyConnection, rows_by_caller: dict) -> None:
    """`replace_member_references` for many functions at once (a scan's batch): a few statements, not one pair per function.
    `rows_by_caller` is `{caller_id: [(owner_class, member, kind, file_path, line, site, receiver[, written_through]), ...]}`; a caller with no rows
    has its old ones removed."""
    if not rows_by_caller:
        return
    callers = list(rows_by_caller)
    for start in range(0, len(callers), 500):
        part = callers[start : start + 500]
        conn.execute(f"DELETE FROM member_references WHERE caller_id IN ({', '.join('?' for _ in part)})", part)
    rows = []
    for caller, items in rows_by_caller.items():
        unique = {_key(row): _full(row) for row in items}
        rows.extend([caller, *row] for row in unique.values())
    for start in range(0, len(rows), 2000):
        conn.executemany(
            "INSERT INTO member_references (caller_id, owner_class, member, kind, file_path, line, site, receiver, written_through) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows[start : start + 2000],
        )


def get_member_reference_stats(conn: duckdb.DuckDBPyConnection) -> dict:
    """What `status` says of the member references: how many there are, how many of them were tied to a class by kind, and how many
    were not (a receiver whose type is unknown: stored by member name, never guessed). Empty when there are none."""
    rows = conn.execute("SELECT kind, owner_class = '' AS unresolved, COUNT(*) FROM member_references GROUP BY kind, owner_class = ''").fetchall()
    by_kind = {"read": 0, "write": 0, "readwrite": 0}
    unresolved = 0
    for kind, is_unresolved, count in rows:
        if is_unresolved:
            unresolved += count
        else:
            by_kind[kind] = by_kind.get(kind, 0) + count
    resolved = sum(by_kind.values())
    if not resolved and not unresolved:
        return {}
    return {"total": resolved + unresolved, "resolved": resolved, "by_kind": by_kind, "unresolved": unresolved}


def get_member_references(conn: duckdb.DuckDBPyConnection, owner_class: Optional[str] = None, member: Optional[str] = None) -> list[dict]:
    """The member references, optionally of one class and/or one member, in a stable order."""
    clauses, params = [], []
    if owner_class is not None:
        clauses.append("owner_class = ?")
        params.append(owner_class)
    if member is not None:
        clauses.append("member = ?")
        params.append(member)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        "SELECT caller_id, owner_class, member, kind, file_path, line, site, receiver, written_through FROM member_references" + where
        + " ORDER BY file_path, line, caller_id, member, kind, site",
        params,
    ).fetchall()
    names = ("caller_id", "owner_class", "member", "kind", "file_path", "line", "site", "receiver", "written_through")
    return [dict(zip(names, row)) for row in rows]


def delete_reasoning_traces_for_function(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
) -> None:
    """Delete every `reasoning_traces` row for `function_id`."""
    conn.execute(
        "DELETE FROM reasoning_traces WHERE function_id = ?", [function_id]
    )


def delete_vision_findings_for_function(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
) -> None:
    """Delete every `vision_findings` row for `function_id`."""
    conn.execute(
        "DELETE FROM vision_findings WHERE function_id = ?", [function_id]
    )


def delete_contract(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
) -> bool:
    """Delete the `contracts` row for `function_id`, if any.

    Returns:
        True if a row was found and deleted.
    """
    existing = conn.execute(
        "SELECT 1 FROM contracts WHERE function_id = ?", [function_id]
    ).fetchone()
    if existing is None:
        return False

    conn.execute("DELETE FROM contracts WHERE function_id = ?", [function_id])
    return True


_FUNCTION_CONFIG_PREFIXES = ("contract:source:", "contract:context:", "artifact:context:", "artifact:stale_reason:", "verification:", "carryover:")
_CHUNK = 400


def _rollback_quietly(conn: duckdb.DuckDBPyConnection) -> None:
    """Roll back after a failure; when the failure already ended the transaction there is nothing to undo."""
    try:
        conn.rollback()
    except duckdb.Error:
        pass


def schedule_removal(conn: duckdb.DuckDBPyConnection, function_ids: Iterable[str]) -> int:
    """Record functions whose rows are to be deleted. Nothing else is touched: the deletion is
    `complete_pending_removals`, after the transaction that records them has committed."""
    ids = sorted(set(function_ids))
    for start in range(0, len(ids), _CHUNK):
        part = ids[start : start + _CHUNK]
        marks = ", ".join("(?)" for _ in part)
        conn.execute(f"INSERT INTO pending_removals (function_id) VALUES {marks} ON CONFLICT DO NOTHING", part)
    return len(ids)


def pending_removal_count(conn: duckdb.DuckDBPyConnection) -> int:
    return conn.execute("SELECT COUNT(*) FROM pending_removals").fetchone()[0]


def complete_pending_removals(conn: duckdb.DuckDBPyConnection) -> int:
    """Delete every function recorded by `schedule_removal`, with all that references it: edges, references,
    traces, findings, contract, vector, and the records kept under its identifier. Returns how many functions.

    Two transactions, because DuckDB does not honour a delete of the referencing rows made in the same
    transaction when it checks the delete of the row they reference: first the referencing rows go and
    that commits, then the functions. Every statement can be repeated, so a crash between them is finished
    by the next call (worker start, and every pass), and what is deleted always belongs to a function that
    has left the source or whose contract was already copied to its new identifier."""
    ids = [row[0] for row in conn.execute("SELECT function_id FROM pending_removals").fetchall()]
    if not ids:
        return 0
    doomed = set(ids)
    conn.begin()
    try:
        for key, in conn.execute("SELECT key FROM config WHERE starts_with(key, 'embedding:')").fetchall():
            try:
                named = json.loads(key[len("embedding:"):])[1:]
            except ValueError:
                continue
            if doomed.intersection(named):
                conn.execute("DELETE FROM config WHERE key = ?", [key])
        for start in range(0, len(ids), _CHUNK):
            part = ids[start : start + _CHUNK]
            keys = [prefix + function_id for function_id in part for prefix in _FUNCTION_CONFIG_PREFIXES]
            conn.execute(f"DELETE FROM config WHERE key IN ({', '.join('?' for _ in keys)})", keys)
        pending = "(SELECT function_id FROM pending_removals)"
        conn.execute(f"DELETE FROM dependencies WHERE caller_id IN {pending} OR callee_id IN {pending}")
        conn.execute(f"DELETE FROM code_references WHERE caller_id IN {pending} OR callee_id IN {pending}")
        conn.execute(f"DELETE FROM member_references WHERE caller_id IN {pending}")
        for table in ("reasoning_traces", "vision_findings", "contracts", "artifact_embeddings"):
            conn.execute(f"DELETE FROM {table} WHERE function_id IN {pending}")
        conn.execute(f"DELETE FROM usage_stats WHERE (kind = 'node' AND item_id IN {pending}) OR (kind <> 'node' AND (item_id IN {pending} OR related_id IN {pending}))")
        conn.commit()
    except BaseException:
        _rollback_quietly(conn)
        raise
    conn.begin()
    try:
        conn.execute("DELETE FROM artifacts WHERE function_id IN (SELECT function_id FROM pending_removals)")
        conn.execute("DELETE FROM pending_removals")
        conn.commit()
    except BaseException:
        _rollback_quietly(conn)
        raise
    return len(ids)


def cascade_delete_artifact(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
) -> bool:
    """Hard-delete one function and everything that references it (see `complete_pending_removals`).
    Returns True if the artifact existed. Not for use inside a transaction: it commits."""
    existed = conn.execute("SELECT 1 FROM artifacts WHERE function_id = ?", [function_id]).fetchone() is not None
    schedule_removal(conn, [function_id])
    complete_pending_removals(conn)
    return existed


# =============================================================================
# Contract Queries
# =============================================================================


def insert_contract(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
    contract_json: str,
    confidence_score: float = 0.0,
    verification_script: Optional[str] = None,
) -> None:
    """Insert or update a contract.

    Args:
        conn: DuckDB connection.
        function_id: Function ID.
        contract_json: Contract JSON string (Pydantic validated).
        confidence_score: Confidence score 0.0 to 1.0.
        verification_script: Generated Python verification script.
    """
    conn.execute(
        """
        INSERT INTO contracts (function_id, contract_json, confidence_score, verification_script, updated_at)
        VALUES (?, ?, ?, ?, now())
        ON CONFLICT (function_id) DO UPDATE SET
            contract_json = EXCLUDED.contract_json,
            confidence_score = EXCLUDED.confidence_score,
            verification_script = EXCLUDED.verification_script,
            updated_at = now()
        """,
        [function_id, contract_json, confidence_score, verification_script],
    )

    from drspec.core.provenance import current_context

    context_hash = current_context(conn, function_id)
    set_config(conn, "contract:context:" + function_id, context_hash)
    set_config(conn, "artifact:context:" + function_id, context_hash)
    conn.execute("DELETE FROM config WHERE key=?", ["artifact:stale_reason:" + function_id])
    conn.execute("DELETE FROM config WHERE key=?", ["carryover:" + function_id])  # a fresh save replaces what was carried
    set_config(conn, "contract:source:" + function_id, get_artifact_code_hash(conn, function_id) or "unknown")
    conn.execute("DELETE FROM config WHERE key=?", ["verification:" + function_id])


def get_contract(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
) -> Optional[dict[str, Any]]:
    """Get a contract by function ID.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to look up.

    Returns:
        Contract dict or None if not found.
    """
    result = conn.execute(
        "SELECT * FROM contracts WHERE function_id = ?",
        [function_id],
    ).fetchone()

    if result is None:
        return None

    columns = [
        "function_id",
        "contract_json",
        "confidence_score",
        "verification_script",
        "created_at",
        "updated_at",
    ]
    contract = dict(zip(columns, result))
    contract["context_hash"] = get_config(conn, "contract:context:" + function_id)
    contract["source_hash"] = get_config(conn, "contract:source:" + function_id)
    evidence = get_config(conn, "verification:" + function_id)
    contract["verification"] = json.loads(evidence) if evidence else None
    return contract


def count_contracts(conn: duckdb.DuckDBPyConnection) -> int:
    """Count total contracts.

    Args:
        conn: DuckDB connection.

    Returns:
        Number of contracts.
    """
    result = conn.execute("SELECT COUNT(*) FROM contracts").fetchone()
    return result[0] if result else 0


def get_contract_confidence_stats(
    conn: duckdb.DuckDBPyConnection,
) -> dict[str, Any]:
    """Get contract confidence score statistics.

    Args:
        conn: DuckDB connection.

    Returns:
        Dictionary with average and distribution.
    """
    result = conn.execute(
        """
        SELECT
            AVG(confidence_score) as average,
            SUM(CASE WHEN confidence_score < 0.5 THEN 1 ELSE 0 END) as below_50,
            SUM(CASE WHEN confidence_score >= 0.5 AND confidence_score < 0.7 THEN 1 ELSE 0 END) as range_50_70,
            SUM(CASE WHEN confidence_score >= 0.7 AND confidence_score < 0.9 THEN 1 ELSE 0 END) as range_70_90,
            SUM(CASE WHEN confidence_score >= 0.9 THEN 1 ELSE 0 END) as above_90
        FROM contracts
        """
    ).fetchone()

    if result is None or result[0] is None:
        return {
            "average": 0.0,
            "distribution": {
                "below_50": 0,
                "50_to_70": 0,
                "70_to_90": 0,
                "above_90": 0,
            },
        }

    return {
        "average": round(result[0] * 100, 1),  # Convert to percentage
        "distribution": {
            "below_50": int(result[1] or 0),
            "50_to_70": int(result[2] or 0),
            "70_to_90": int(result[3] or 0),
            "above_90": int(result[4] or 0),
        },
    }


# `code_references` only ever produces EXTRACTED rows today (receiver
# type is never guessed), but both tables are padded with every tier
# here — same as `artifacts.by_status` padding every valid status — so
# the shape stays stable if that ever changes.
VALID_EDGE_CONFIDENCE_TIERS = ("EXTRACTED", "INFERRED", "AMBIGUOUS")


def get_edge_stats(conn: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """Edge counts for `dependencies` (`calls`) and `code_references`
    (`references`), broken down by confidence tier.

    Args:
        conn: DuckDB connection.

    Returns:
        `{"calls": {"total": N, "by_confidence": {...}}, "references": {...}}`.
    """

    def _counts(table: str) -> dict[str, Any]:
        rows = conn.execute(f"SELECT confidence, COUNT(*) FROM {table} GROUP BY confidence").fetchall()
        by_confidence = dict.fromkeys(VALID_EDGE_CONFIDENCE_TIERS, 0)
        total = 0
        for confidence, count in rows:
            if confidence in by_confidence:
                by_confidence[confidence] = count
            total += count
        return {"total": total, "by_confidence": by_confidence}

    return {
        "calls": _counts("dependencies"),
        "references": _counts("code_references"),
    }


# =============================================================================
# Dependency Queries
# =============================================================================


def get_callers(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
) -> list[str]:
    """Get functions that call the given function.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to look up callers for.

    Returns:
        List of caller function IDs.
    """
    result = conn.execute(
        "SELECT caller_id FROM dependencies WHERE callee_id = ?",
        [function_id],
    ).fetchall()
    return [row[0] for row in result]


def get_callees(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
) -> list[str]:
    """Get functions that are called by the given function.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to look up callees for.

    Returns:
        List of callee function IDs.
    """
    result = conn.execute(
        "SELECT callee_id FROM dependencies WHERE caller_id = ?",
        [function_id],
    ).fetchall()
    return [row[0] for row in result]


# =============================================================================
# Dependency Edge Queries
# =============================================================================
#
# Operate on the full edge row (call_site/edge_hash/embedding/confidence),
# unlike the plain caller/callee-id helpers above, which predate the code
# graph and stay as-is for existing callers (tests, grapher).


@dataclass
class EdgeRecord:
    """A full `dependencies` row, including the code-graph columns.

    Named `EdgeRecord` (not `DependencyEdge`) to avoid colliding with
    `drspec.db.graph.DependencyEdge`, which is a different, simpler
    traversal-output type (just caller/callee/is_cyclic).
    """

    caller_id: str
    callee_id: str
    call_site: Optional[str]
    edge_hash: Optional[str]
    embedding: Optional[list[float]]
    confidence: Optional[str]
    created_at: datetime

    @classmethod
    def from_row(cls, row: tuple) -> "EdgeRecord":
        embedding = list(row[4]) if row[4] is not None else None
        return cls(
            caller_id=row[0],
            callee_id=row[1],
            call_site=row[2],
            edge_hash=row[3],
            embedding=embedding,
            confidence=row[5],
            created_at=row[6],
        )


_EDGE_RECORD_COLUMNS = "caller_id, callee_id, call_site, edge_hash, embedding, confidence, created_at"


def get_dependency_edge(
    conn: duckdb.DuckDBPyConnection,
    caller_id: str,
    callee_id: str,
) -> Optional[EdgeRecord]:
    """Get the full edge row for a `(caller_id, callee_id)` pair, at
    whatever confidence tier it currently holds (there is only ever one,
    since confidence isn't part of the primary key).
    """
    result = conn.execute(
        f"SELECT {_EDGE_RECORD_COLUMNS} FROM dependencies WHERE caller_id = ? AND callee_id = ?",
        [caller_id, callee_id],
    ).fetchone()
    return EdgeRecord.from_row(result) if result is not None else None


def get_dependencies_by_caller(
    conn: duckdb.DuckDBPyConnection,
    caller_id: str,
    confidences: Optional[list[str]] = None,
) -> list[EdgeRecord]:
    """Get a caller's outgoing edges, optionally scoped to specific
    confidence tiers (a plain scan only considers `EXTRACTED` rows;
    `--deep-scan` considers all tiers).

    Args:
        conn: DuckDB connection.
        caller_id: Caller function ID.
        confidences: If given, only rows whose `confidence` is in this
            list are returned. If `None`, all of this caller's rows are
            returned regardless of confidence.
    """
    if confidences is None:
        result = conn.execute(
            f"SELECT {_EDGE_RECORD_COLUMNS} FROM dependencies WHERE caller_id = ?",
            [caller_id],
        ).fetchall()
    else:
        placeholders = ", ".join("?" for _ in confidences)
        result = conn.execute(
            f"SELECT {_EDGE_RECORD_COLUMNS} FROM dependencies "
            f"WHERE caller_id = ? AND confidence IN ({placeholders})",
            [caller_id, *confidences],
        ).fetchall()
    return [EdgeRecord.from_row(row) for row in result]


def get_dependencies_by_callee(
    conn: duckdb.DuckDBPyConnection,
    callee_id: str,
) -> list[EdgeRecord]:
    """Get every edge where `callee_id` is the callee (used when
    refreshing `edge_hash`/`embedding` after a callee's own hash change).
    """
    result = conn.execute(
        f"SELECT {_EDGE_RECORD_COLUMNS} FROM dependencies WHERE callee_id = ?",
        [callee_id],
    ).fetchall()
    return [EdgeRecord.from_row(row) for row in result]


def upsert_dependency_edge(
    conn: duckdb.DuckDBPyConnection,
    caller_id: str,
    callee_id: str,
    call_site: str,
    edge_hash: str,
    embedding: Optional[list[float]],
    confidence: str,
) -> None:
    """Insert a new edge row, or update an existing one at any confidence
    tier (a "promotion" — the `PRIMARY KEY (caller_id, callee_id)` has no
    confidence component, so there is never more than one row for this
    pair regardless of which tier it's at).

    Args:
        conn: DuckDB connection.
        caller_id: Calling function ID.
        callee_id: Called function ID.
        call_site: Call-site text (concatenated if multiple occurrences).
        edge_hash: `compute_edge_hash(caller.code_hash, callee.code_hash, call_site)`.
        embedding: 384-float local embedding of `call_site`.
        confidence: `EXTRACTED`, `INFERRED`, or `AMBIGUOUS`.
    """
    check_embedding_length(embedding)
    conn.execute(
        """
        INSERT INTO dependencies (caller_id, callee_id, call_site, edge_hash, embedding, confidence)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (caller_id, callee_id) DO UPDATE SET
            call_site = excluded.call_site,
            edge_hash = excluded.edge_hash,
            embedding = excluded.embedding,
            confidence = excluded.confidence
        """,
        [caller_id, callee_id, call_site, edge_hash, embedding, confidence],
    )


def update_dependency_edge_hash(
    conn: duckdb.DuckDBPyConnection,
    caller_id: str,
    callee_id: str,
    edge_hash: str,
    embedding: Optional[list[float]],
) -> None:
    """Refresh just `edge_hash`/`embedding` on an existing edge, without
    touching `call_site`/`confidence` — a callee's own hash changed, so
    the edge hash must change too, but the call-site text and how it was
    originally resolved haven't.
    """
    check_embedding_length(embedding)
    conn.execute(
        """
        UPDATE dependencies SET edge_hash = ?, embedding = ?
        WHERE caller_id = ? AND callee_id = ?
        """,
        [edge_hash, embedding, caller_id, callee_id],
    )


def set_edge_hashes(
    conn: duckdb.DuckDBPyConnection,
    table: str,
    callee_id: str,
    hashes: Iterable[tuple[str, str]],
) -> None:
    """Give edges into `callee_id` their new `edge_hash` and clear their embedding
    (the text it described is gone), with one UPDATE per 500 edges instead of one
    per edge. `hashes` is `(caller_id, edge_hash)`; `table` is `dependencies` or
    `code_references`."""
    if table not in ("dependencies", "code_references"):
        raise ValueError(f"Not an edge table: {table}")
    rows = list(hashes)
    for start in range(0, len(rows), 500):
        chunk = rows[start : start + 500]
        values = ", ".join("(?, ?)" for _ in chunk)
        params: list[Any] = [value for row in chunk for value in row]
        conn.execute(
            f"UPDATE {table} SET edge_hash = v.edge_hash, embedding = NULL "
            f"FROM (VALUES {values}) AS v(caller_id, edge_hash) "
            f"WHERE {table}.callee_id = ? AND {table}.caller_id = v.caller_id",
            [*params, callee_id],
        )


def delete_dependency_edge(
    conn: duckdb.DuckDBPyConnection,
    caller_id: str,
    callee_id: str,
) -> None:
    """Delete one `(caller_id, callee_id)` edge row."""
    conn.execute(
        "DELETE FROM dependencies WHERE caller_id = ? AND callee_id = ?",
        [caller_id, callee_id],
    )


# =============================================================================
# Reference Queries (non-call dependencies, e.g. Python @property reads.
# Separate table, separate functions, from `dependencies`/the functions
# above — deliberately not unified, so the proven `calls` schema/queries
# stay untouched. Confidence is always `EXTRACTED` here (the resolver
# never guesses a receiver type), so there's no scoped-diff-by-tier
# question the way `--deep-scan` raises for `dependencies`.)
# =============================================================================


def get_reference_callers(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
) -> list[str]:
    """Get functions whose body reads `function_id` as a property.

    Mirrors `get_callers`, for `code_references` instead of `dependencies`.
    """
    result = conn.execute(
        "SELECT caller_id FROM code_references WHERE callee_id = ?",
        [function_id],
    ).fetchall()
    return [row[0] for row in result]


def get_reference_callees(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
) -> list[str]:
    """Get properties that `function_id`'s body reads.

    Mirrors `get_callees`, for `code_references` instead of `dependencies`.
    """
    result = conn.execute(
        "SELECT callee_id FROM code_references WHERE caller_id = ?",
        [function_id],
    ).fetchall()
    return [row[0] for row in result]


@dataclass
class ReferenceRecord:
    """A full `code_references` row — mirrors `EdgeRecord`, for the
    separate non-call relation table."""

    caller_id: str
    callee_id: str
    reference_site: Optional[str]
    edge_hash: Optional[str]
    embedding: Optional[list[float]]
    confidence: Optional[str]
    created_at: datetime

    @classmethod
    def from_row(cls, row: tuple) -> "ReferenceRecord":
        embedding = list(row[4]) if row[4] is not None else None
        return cls(
            caller_id=row[0],
            callee_id=row[1],
            reference_site=row[2],
            edge_hash=row[3],
            embedding=embedding,
            confidence=row[5],
            created_at=row[6],
        )


_REFERENCE_RECORD_COLUMNS = "caller_id, callee_id, reference_site, edge_hash, embedding, confidence, created_at"


def get_references_by_caller(
    conn: duckdb.DuckDBPyConnection,
    caller_id: str,
) -> list[ReferenceRecord]:
    """Get a caller's outgoing `code_references` rows (its property reads)."""
    result = conn.execute(
        f"SELECT {_REFERENCE_RECORD_COLUMNS} FROM code_references WHERE caller_id = ?",
        [caller_id],
    ).fetchall()
    return [ReferenceRecord.from_row(row) for row in result]


def get_references_by_callee(
    conn: duckdb.DuckDBPyConnection,
    callee_id: str,
) -> list[ReferenceRecord]:
    """Get every `code_references` row where `callee_id` is the property
    being read (used when refreshing a callee's own hash change)."""
    result = conn.execute(
        f"SELECT {_REFERENCE_RECORD_COLUMNS} FROM code_references WHERE callee_id = ?",
        [callee_id],
    ).fetchall()
    return [ReferenceRecord.from_row(row) for row in result]


def upsert_reference_edge(
    conn: duckdb.DuckDBPyConnection,
    caller_id: str,
    callee_id: str,
    reference_site: str,
    edge_hash: str,
    embedding: Optional[list[float]],
    confidence: str,
) -> None:
    """Insert a new `code_references` row, or update an existing one.

    Mirrors `upsert_dependency_edge`. Unlike that function, there is no
    cross-tier "promotion" case to worry about in practice — `confidence`
    is always `EXTRACTED` for references today — but the `ON CONFLICT ...
    DO UPDATE` is still correct either way.
    """
    check_embedding_length(embedding)
    conn.execute(
        """
        INSERT INTO code_references (caller_id, callee_id, reference_site, edge_hash, embedding, confidence)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (caller_id, callee_id) DO UPDATE SET
            reference_site = excluded.reference_site,
            edge_hash = excluded.edge_hash,
            embedding = excluded.embedding,
            confidence = excluded.confidence
        """,
        [caller_id, callee_id, reference_site, edge_hash, embedding, confidence],
    )


def update_reference_edge_hash(
    conn: duckdb.DuckDBPyConnection,
    caller_id: str,
    callee_id: str,
    edge_hash: str,
    embedding: Optional[list[float]],
) -> None:
    """Refresh just `edge_hash`/`embedding` on an existing reference edge.
    Mirrors `update_dependency_edge_hash`."""
    check_embedding_length(embedding)
    conn.execute(
        """
        UPDATE code_references SET edge_hash = ?, embedding = ?
        WHERE caller_id = ? AND callee_id = ?
        """,
        [edge_hash, embedding, caller_id, callee_id],
    )


def delete_reference_edge(
    conn: duckdb.DuckDBPyConnection,
    caller_id: str,
    callee_id: str,
) -> None:
    """Delete one `(caller_id, callee_id)` row from `code_references`."""
    conn.execute(
        "DELETE FROM code_references WHERE caller_id = ? AND callee_id = ?",
        [caller_id, callee_id],
    )


# =============================================================================
# Reasoning Trace Queries
# =============================================================================


def insert_reasoning_trace(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
    agent: str,
    trace_json: str,
) -> None:
    """Insert a reasoning trace entry.

    Args:
        conn: DuckDB connection.
        function_id: Function ID the trace is for.
        agent: Agent name (librarian, proposer, critic, judge, debugger).
        trace_json: JSON trace data.
    """
    conn.execute(
        """
        INSERT INTO reasoning_traces (function_id, agent, trace_json)
        VALUES (?, ?, ?)
        """,
        [function_id, agent, trace_json],
    )


def get_reasoning_traces(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
    agent: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Get reasoning traces for a function.

    The only reader of stored traces; no command exposes it yet.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to get traces for.
        agent: Optional agent name filter.

    Returns:
        List of trace dicts.
    """
    if agent:
        result = conn.execute(
            """
            SELECT id, function_id, agent, trace_json, created_at
            FROM reasoning_traces
            WHERE function_id = ? AND agent = ?
            ORDER BY created_at DESC
            """,
            [function_id, agent],
        ).fetchall()
    else:
        result = conn.execute(
            """
            SELECT id, function_id, agent, trace_json, created_at
            FROM reasoning_traces
            WHERE function_id = ?
            ORDER BY created_at DESC
            """,
            [function_id],
        ).fetchall()

    columns = ["id", "function_id", "agent", "trace_json", "created_at"]
    return [dict(zip(columns, row)) for row in result]


# =============================================================================
# Config Queries
# =============================================================================


def get_config(
    conn: duckdb.DuckDBPyConnection,
    key: str,
    default: Optional[str] = None,
) -> Optional[str]:
    """Get a configuration value.

    Args:
        conn: DuckDB connection.
        key: Configuration key.
        default: Default value if key not found.

    Returns:
        Configuration value or default.
    """
    result = conn.execute(
        "SELECT value FROM config WHERE key = ?",
        [key],
    ).fetchone()

    if result is None:
        return default

    return result[0]


def set_config(
    conn: duckdb.DuckDBPyConnection,
    key: str,
    value: str,
) -> None:
    """Set a configuration value.

    Args:
        conn: DuckDB connection.
        key: Configuration key.
        value: Configuration value.
    """
    conn.execute(
        """
        INSERT INTO config (key, value, updated_at)
        VALUES (?, ?, now())
        ON CONFLICT (key) DO UPDATE SET
            value = EXCLUDED.value,
            updated_at = now()
        """,
        [key, value],
    )


_CONFIG_WRITE_CHUNK = 500


def set_config_many(conn: duckdb.DuckDBPyConnection, pairs: list[tuple[str, str]]) -> None:
    """`set_config` for many keys, a few statements instead of one each. A
    publish holds the worker's lock, so a write per function makes it last as
    long as the project is big."""
    for start in range(0, len(pairs), _CONFIG_WRITE_CHUNK):
        chunk = pairs[start:start + _CONFIG_WRITE_CHUNK]
        values = ", ".join("(?, ?, now())" for _ in chunk)
        conn.execute(
            f"INSERT INTO config (key, value, updated_at) VALUES {values} "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            [part for pair in chunk for part in pair],
        )


# =============================================================================
# Staleness tracking — `scanned_files` is a per-file on-disk snapshot
# written by `drspec scan` itself; `staleness_queue` is populated only by
# the background worker's own drift check against it.
# =============================================================================


def upsert_scanned_file(conn: duckdb.DuckDBPyConnection, file_path: str, mtime: float, size: int) -> None:
    """Record `file_path`'s on-disk mtime/size as of *this* scan — the
    snapshot the background worker later compares against to detect
    drift. Called once per file `drspec scan` actually processes, as a
    side effect of its own normal file-walking (no extra file I/O: the
    stat info comes from the same read the scan already did)."""
    conn.execute(
        """
        INSERT INTO scanned_files (file_path, mtime, size, last_scanned_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT (file_path) DO UPDATE SET
            mtime = excluded.mtime,
            size = excluded.size,
            last_scanned_at = excluded.last_scanned_at
        """,
        [file_path, mtime, size],
    )


def list_scanned_files(conn: duckdb.DuckDBPyConnection) -> list[tuple[str, float, int]]:
    """Every file's last-known-scanned (path, mtime, size) snapshot —
    what the background worker diffs against the real filesystem."""
    return conn.execute("SELECT file_path, mtime, size FROM scanned_files").fetchall()


def scanned_file_snapshots(
    conn: duckdb.DuckDBPyConnection,
    file_paths: list[str],
) -> dict[str, tuple[float, int]]:
    """`{file_path: (mtime, size)}` as of the last scan, for those of
    `file_paths` that have a snapshot. Empty if the table doesn't exist
    yet (a project not rescanned since it was added)."""
    if not file_paths:
        return {}
    placeholders = ", ".join("?" for _ in file_paths)
    try:
        rows = conn.execute(
            f"SELECT file_path, mtime, size FROM scanned_files WHERE file_path IN ({placeholders})",
            list(file_paths),
        ).fetchall()
    except duckdb.CatalogException:
        return {}
    return {file_path: (mtime, size) for file_path, mtime, size in rows}


def list_scanned_file_paths(conn: duckdb.DuckDBPyConnection) -> set[str]:
    """Every file `drspec scan` has processed, whether or not it held functions."""
    try:
        return {row[0] for row in conn.execute("SELECT file_path FROM scanned_files").fetchall()}
    except duckdb.CatalogException:
        return set()


def delete_scanned_file(conn: duckdb.DuckDBPyConnection, file_path: str) -> None:
    """Drop a file's snapshot — e.g. once the worker finds it deleted
    from disk entirely, so it's never checked again."""
    conn.execute("DELETE FROM scanned_files WHERE file_path = ?", [file_path])


def enqueue_staleness(conn: duckdb.DuckDBPyConnection, file_path: str, reason: str) -> None:
    """Flag `file_path` as possibly stale. Idempotent — re-flagging an
    already-queued file just refreshes `reason`/`flagged_at`, never
    duplicates a row (`file_path` is the primary key)."""
    conn.execute(
        """
        INSERT INTO staleness_queue (file_path, flagged_at, reason)
        VALUES (?, CURRENT_TIMESTAMP, ?)
        ON CONFLICT (file_path) DO UPDATE SET
            flagged_at = excluded.flagged_at,
            reason = excluded.reason
        """,
        [file_path, reason],
    )


def dequeue_staleness(conn: duckdb.DuckDBPyConnection, file_path: str) -> None:
    """Remove `file_path` from the staleness queue — the worker calls
    this once it has re-verified/re-synced the file for real."""
    conn.execute("DELETE FROM staleness_queue WHERE file_path = ?", [file_path])


def list_staleness_queue(conn: duckdb.DuckDBPyConnection) -> list[str]:
    """Every currently-flagged file path, oldest flag first."""
    rows = conn.execute("SELECT file_path FROM staleness_queue ORDER BY flagged_at").fetchall()
    return [row[0] for row in rows]


# Order in which queued files are re-scanned: files a search just looked at,
# then drift the worker found, then newly discovered files.
RESCAN_REASON_SEARCH = ("search_drift", "search_unscanned")
RESCAN_REASON_NEW_FILE = "new_file"
RESCAN_REASON_NEW_FILE_RECENT = "new_file_recent"
# Asked for by the user first, then drift and changes, then new files: those modified recently before the rest.
_RESCAN_PRIORITY_SQL = (
    "CASE WHEN reason = 'scan' THEN -1 WHEN reason LIKE 'search\\_%' ESCAPE '\\' THEN 0 WHEN reason = 'new_file_recent' THEN 2 WHEN reason = 'new_file' THEN 3 WHEN reason = 'new_doc' THEN 4 ELSE 1 END"
)

def enqueue_staleness_many(
    conn: duckdb.DuckDBPyConnection,
    items: list[tuple[str, str]],
    overwrite: bool = False,
) -> None:
    """Queue `(file_path, reason)` pairs for re-scanning in one statement.

    `overwrite=False` keeps an existing row as it is, so the worker's own
    sweep cannot downgrade a file a search already queued. `overwrite=True`
    replaces its reason and flag time (a search seeing a file wins).
    """
    if not items:
        return
    values = ", ".join("(?, now(), ?)" for _ in items)
    params: list[Any] = [value for item in items for value in item]
    conflict = (
        "DO UPDATE SET flagged_at = excluded.flagged_at, reason = excluded.reason" if overwrite else "DO NOTHING"
    )
    conn.execute(
        f"INSERT INTO staleness_queue (file_path, flagged_at, reason) VALUES {values} ON CONFLICT (file_path) {conflict}",
        params,
    )


def add_usage_pending(conn: duckdb.DuckDBPyConnection, rows: list[tuple[str, str, str, float]]) -> None:
    """Add `(kind, item_id, related_id, weight)` to each item's pending
    counter in one statement. Clients only touch `pending`; the worker folds
    it into `score`."""
    totals: dict[tuple[str, str, str], float] = {}
    for kind, item_id, related_id, weight in rows:
        key = (kind, item_id, related_id)
        totals[key] = totals.get(key, 0.0) + weight
    if not totals:
        return
    values = ", ".join("(?, ?, ?, ?)" for _ in totals)
    params: list[Any] = [value for (kind, item_id, related_id), weight in totals.items() for value in (kind, item_id, related_id, weight)]
    conn.execute(
        f"INSERT INTO usage_stats (kind, item_id, related_id, pending) VALUES {values} "
        "ON CONFLICT (kind, item_id, related_id) DO UPDATE SET pending = usage_stats.pending + excluded.pending",
        params,
    )


def fold_usage_pending(conn: duckdb.DuckDBPyConnection, now: float, half_life_days: float) -> int:
    """Fold pending counters into decayed scores: the old score is decayed
    from its `as_of` to `now`, then the pending weight is added. Returns the
    number of rows folded."""
    half_life = half_life_days * 86400.0
    folded = conn.execute("SELECT COUNT(*) FROM usage_stats WHERE pending > 0").fetchone()[0]
    if folded:
        conn.execute(
            "UPDATE usage_stats SET score = score * pow(0.5, greatest(? - as_of, 0) / ?) + pending, "
            "pending = 0, as_of = ? WHERE pending > 0",
            [now, half_life, now],
        )
    return folded


def top_usage_items(
    conn: duckdb.DuckDBPyConnection, limit: int, now: float, half_life_days: float
) -> list[tuple[str, str, str, float]]:
    """The `limit` highest-scoring `(kind, item_id, related_id, score)` rows,
    with each score decayed to `now`."""
    half_life = half_life_days * 86400.0
    return conn.execute(
        "SELECT kind, item_id, related_id, score * pow(0.5, greatest(? - as_of, 0) / ?) AS decayed "
        "FROM usage_stats WHERE score > 0 ORDER BY decayed DESC, kind, item_id, related_id LIMIT ?",
        [now, half_life, limit],
    ).fetchall()


def count_usage_pending(conn: duckdb.DuckDBPyConnection) -> int:
    return conn.execute("SELECT COUNT(*) FROM usage_stats WHERE pending > 0").fetchone()[0]


#: Whether somebody asked for the file (a scan waits for it or queued it): the row of `scan_requests` for the queued file.
_ASKED_SQL = "EXISTS (SELECT 1 FROM scan_requests r WHERE r.file_path = q.file_path)"


def _unchanged_since_failure(root: Path, file_path: str, failed_mtime: Optional[float], failed_size: Optional[int]) -> bool:
    """Whether `file_path` is still as it was when a read of it failed: the same mtime and size. A file that is gone, or whose
    `stat` fails, is not (the next pass resolves it)."""
    if failed_mtime is None:
        return False
    try:
        stat = (root / file_path).stat()
    except OSError:
        return False
    return stat.st_mtime == failed_mtime and stat.st_size == failed_size


def record_rescan_failure(
    conn: duckdb.DuckDBPyConnection,
    file_path: str,
    error: str,
    mtime: Optional[float],
    size: Optional[int],
    now: float,
) -> int:
    """Note that `file_path` could not be read, with the mtime and size it had then; returns the attempt count.

    Nothing reads the file again while it stays that way: `next_rescan_batch` reads it once it is edited, and a facts version change
    clears these rows (`clear_all_rescan_backoff`). `retry_at` is the time of this failure."""
    row = conn.execute("SELECT attempts FROM rescan_backoff WHERE file_path = ?", [file_path]).fetchone()
    attempts = (row[0] if row else 0) + 1
    conn.execute(
        """
        INSERT INTO rescan_backoff (file_path, attempts, retry_at, last_error, failed_mtime, failed_size)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (file_path) DO UPDATE SET
            attempts = excluded.attempts, retry_at = excluded.retry_at, last_error = excluded.last_error,
            failed_mtime = excluded.failed_mtime, failed_size = excluded.failed_size
        """,
        [file_path, attempts, now, error, mtime, size],
    )
    return attempts


def clear_rescan_backoff(conn: duckdb.DuckDBPyConnection, file_path: str) -> None:
    conn.execute("DELETE FROM rescan_backoff WHERE file_path = ?", [file_path])


def clear_all_rescan_backoff(conn: duckdb.DuckDBPyConnection) -> None:
    """Forget every failed read, so the files are read again: used when a facts version changes (a file that failed may now parse)."""
    conn.execute("DELETE FROM rescan_backoff")


def settle_scanned_files(conn: duckdb.DuckDBPyConnection, file_paths: Iterable[str], root: Path) -> None:
    """Record that these files were just brought up to date: take them off the
    re-scan queue, clear their retry wait, and forget the snapshot of any that
    no longer exist. Both the worker's re-scan and a manual scan call this in
    the same publish that wrote the files, so a row queued before or during
    the publish cannot cause a second, redundant re-scan. `root` is the project
    folder the paths are relative to (not the process folder)."""
    paths = list(dict.fromkeys(file_paths))
    for start in range(0, len(paths), 500):
        chunk = paths[start : start + 500]
        marks = ", ".join("?" for _ in chunk)
        conn.execute(f"DELETE FROM staleness_queue WHERE file_path IN ({marks})", chunk)
        conn.execute(f"DELETE FROM rescan_backoff WHERE file_path IN ({marks})", chunk)
        gone = [path for path in chunk if not (root / path).exists()]
        if gone:
            conn.execute(f"DELETE FROM scanned_files WHERE file_path IN ({', '.join('?' for _ in gone)})", gone)


def next_rescan_batch(
    conn: duckdb.DuckDBPyConnection,
    limit: int,
    now: float,
    root: Optional[Path] = None,
) -> list[str]:
    """Up to `limit` queued files to re-scan now, in priority order.

    A file whose last read failed is skipped while it is as it was then (the same mtime and size): an edit may have fixed it, and
    nothing else can change the result. An edited one is read, and its failure is cleared. A file a scan asked for (a `scan_requests`
    row) is read whatever it was: the scan waits for it. `now` is not used: there is no timed retry. `root` is where relative paths
    are resolved for that check.
    """
    rows = conn.execute(
        f"""
        SELECT q.file_path, b.file_path IS NOT NULL, b.failed_mtime, b.failed_size, {_ASKED_SQL}
        FROM staleness_queue q LEFT JOIN rescan_backoff b ON b.file_path = q.file_path
        ORDER BY {_RESCAN_PRIORITY_SQL.replace("reason", "q.reason")}, q.flagged_at, q.file_path
        """
    ).fetchall()
    batch: list[str] = []
    for file_path, failed, failed_mtime, failed_size, asked in rows:
        if len(batch) >= limit:
            break
        if failed:
            if not asked and _unchanged_since_failure(root or Path.cwd(), file_path, failed_mtime, failed_size):
                continue
            clear_rescan_backoff(conn, file_path)
        batch.append(file_path)
    return batch


def get_rescan_backlog(conn: duckdb.DuckDBPyConnection, now: float, root: Optional[Path] = None) -> dict[str, int]:
    """Counts for `status`: how much re-scan work is outstanding, and the queued files that cannot be read as they are. Those are
    counted apart (`files_unreadable`) and not as pending: an edit brings them back. A file that changed since its failed read is
    pending, since the next pass reads it. `files_in_retry_wait` is always 0 now (no file waits on a timer). `now` is not used."""
    rows = conn.execute(
        "SELECT q.file_path, q.reason LIKE 'new\\_%' ESCAPE '\\', b.file_path IS NOT NULL, b.failed_mtime, b.failed_size, "
        f"{_ASKED_SQL} FROM staleness_queue q LEFT JOIN rescan_backoff b ON b.file_path = q.file_path"
    ).fetchall()
    base = root or Path.cwd()
    pending_rescan = pending_discovery = unreadable = 0
    for file_path, is_new, failed, failed_mtime, failed_size, asked in rows:
        if failed and not asked and _unchanged_since_failure(base, file_path, failed_mtime, failed_size):
            unreadable += 1
        elif is_new:
            pending_discovery += 1
        else:
            pending_rescan += 1
    return {"files_pending_rescan": pending_rescan, "files_pending_discovery": pending_discovery, "files_in_retry_wait": 0, "files_unreadable": unreadable}


def list_unreadable_files(conn: duckdb.DuckDBPyConnection, root: Optional[Path] = None) -> list[dict]:
    """The queued files that cannot be read as they are, for `status --details`: the file, the error of its last read, and how many
    reads failed."""
    base = root or Path.cwd()
    rows = conn.execute(
        "SELECT b.file_path, b.last_error, b.attempts, b.failed_mtime, b.failed_size, " + _ASKED_SQL + " FROM staleness_queue q "
        "JOIN rescan_backoff b ON b.file_path = q.file_path ORDER BY b.file_path"
    ).fetchall()
    return [
        {"file_path": path, "error": error, "attempts": attempts}
        for path, error, attempts, failed_mtime, failed_size, asked in rows
        if not asked and _unchanged_since_failure(base, path, failed_mtime, failed_size)
    ]


def index_completeness(conn: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """`{"pending": n, "total": m}`: files found but not scanned yet, and all files known (scanned or waiting for
    their first scan). Files the discovery walk has not reached are not counted, so a large project can have more."""
    pending = conn.execute(
        "SELECT COUNT(*) FROM staleness_queue q WHERE q.reason LIKE 'new\\_file%' ESCAPE '\\' "
        "OR (q.reason = 'search_unscanned' AND NOT EXISTS (SELECT 1 FROM scanned_files s WHERE s.file_path = q.file_path))"
    ).fetchone()[0]
    scanned = conn.execute("SELECT COUNT(*) FROM scanned_files").fetchone()[0]
    return {"pending": pending, "total": scanned + pending}


def stale_file_paths_among(conn: duckdb.DuckDBPyConnection, file_paths: list[str]) -> set[str]:
    """Which of `file_paths` are currently flagged — reads
    already-computed worker state, never a live recheck at query time. A
    single `IN` query rather than one row-existence check per result,
    since a result page can have many distinct files.

    `staleness_queue` may not exist yet for a project `drspec init`-ed
    but not rescanned since this table was added — a read-only command
    never runs DDL to create it. Treated the same as "nothing flagged"
    rather than a raw error, since that's an honest answer for a project
    this table hasn't reached yet.
    """
    if not file_paths:
        return set()
    placeholders = ", ".join("?" for _ in file_paths)
    try:
        rows = conn.execute(
            f"SELECT file_path FROM staleness_queue WHERE file_path IN ({placeholders})",
            list(file_paths),
        ).fetchall()
    except duckdb.CatalogException:
        return set()
    return {row[0] for row in rows}


# =============================================================================
# Vision Finding Model
# =============================================================================


@dataclass
class VisionFinding:
    """Represents a visual analysis finding from Vision Analyst.

    Attributes:
        id: Unique finding ID.
        function_id: Function this finding relates to.
        finding_type: Type of finding (outlier, discontinuity, etc.).
        significance: Severity level (HIGH, MEDIUM, LOW).
        description: Description of the finding.
        location: Where in the plot the finding was observed.
        invariant_implication: Suggested invariant change.
        status: Finding status (NEW, ADDRESSED, IGNORED).
        resolution_note: How the finding was addressed or why ignored.
        plot_path: Path to the plot image.
        created_at: When the finding was created.
    """

    id: Optional[int]
    function_id: str
    finding_type: str
    significance: str
    description: str
    location: Optional[str]
    invariant_implication: Optional[str]
    status: str
    resolution_note: Optional[str]
    plot_path: Optional[str]
    created_at: datetime

    @classmethod
    def from_row(cls, row: tuple) -> "VisionFinding":
        """Create VisionFinding from database row.

        Args:
            row: Database row tuple in column order.

        Returns:
            VisionFinding instance.
        """
        return cls(
            id=row[0],
            function_id=row[1],
            finding_type=row[2],
            significance=row[3],
            description=row[4],
            location=row[5],
            invariant_implication=row[6],
            status=row[7],
            resolution_note=row[8],
            plot_path=row[9],
            created_at=row[10],
        )


# =============================================================================
# Vision Finding Queries
# =============================================================================


def insert_vision_finding(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
    finding_type: str,
    significance: str,
    description: str,
    location: Optional[str] = None,
    invariant_implication: Optional[str] = None,
    plot_path: Optional[str] = None,
) -> int:
    """Insert a vision finding.

    Args:
        conn: DuckDB connection.
        function_id: Function ID the finding relates to.
        finding_type: Type of finding (outlier, discontinuity, boundary, correlation, missing_pattern).
        significance: Severity (HIGH, MEDIUM, LOW).
        description: Description of the finding.
        location: Where in the plot it was observed.
        invariant_implication: Suggested invariant change.
        plot_path: Path to the plot image.

    Returns:
        ID of the inserted finding.

    Raises:
        ValueError: If finding_type or significance is invalid.
    """
    if finding_type not in VALID_FINDING_TYPES:
        raise ValueError(f"Invalid finding_type: {finding_type}. Must be one of {VALID_FINDING_TYPES}")

    if significance not in VALID_FINDING_SIGNIFICANCE:
        raise ValueError(f"Invalid significance: {significance}. Must be one of {VALID_FINDING_SIGNIFICANCE}")

    conn.execute(
        """
        INSERT INTO vision_findings (
            function_id, finding_type, significance, description,
            location, invariant_implication, status, plot_path
        ) VALUES (?, ?, ?, ?, ?, ?, 'NEW', ?)
        """,
        [function_id, finding_type, significance, description, location, invariant_implication, plot_path],
    )

    result = conn.execute("SELECT MAX(id) FROM vision_findings WHERE function_id = ?", [function_id]).fetchone()
    return result[0] if result else 0


def get_vision_findings(
    conn: duckdb.DuckDBPyConnection,
    function_id: str,
    status: Optional[str] = None,
    significance: Optional[str] = None,
) -> list[VisionFinding]:
    """Get vision findings for a function.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to get findings for.
        status: Optional status filter (NEW, ADDRESSED, IGNORED).
        significance: Optional significance filter (HIGH, MEDIUM, LOW).

    Returns:
        List of VisionFinding objects.
    """
    query = """
        SELECT id, function_id, finding_type, significance, description,
               location, invariant_implication, status, resolution_note,
               plot_path, created_at
        FROM vision_findings
        WHERE function_id = ?
    """
    params: list[Any] = [function_id]

    if status is not None:
        query += " AND status = ?"
        params.append(status)

    if significance is not None:
        query += " AND significance = ?"
        params.append(significance)

    query += " ORDER BY created_at DESC"

    result = conn.execute(query, params).fetchall()
    return [VisionFinding.from_row(row) for row in result]


def update_vision_finding_status(
    conn: duckdb.DuckDBPyConnection,
    finding_id: int,
    status: str,
    resolution_note: Optional[str] = None,
) -> bool:
    """Update the status of a vision finding.

    Args:
        conn: DuckDB connection.
        finding_id: ID of the finding to update.
        status: New status (NEW, ADDRESSED, IGNORED).
        resolution_note: Note explaining how it was addressed or why ignored.

    Returns:
        True if finding was found and updated.

    Raises:
        ValueError: If status is invalid.
    """
    if status not in VALID_FINDING_STATUSES:
        raise ValueError(f"Invalid status: {status}. Must be one of {VALID_FINDING_STATUSES}")

    existing = conn.execute(
        "SELECT 1 FROM vision_findings WHERE id = ?", [finding_id]
    ).fetchone()
    if existing is None:
        return False

    conn.execute(
        """
        UPDATE vision_findings
        SET status = ?, resolution_note = ?
        WHERE id = ?
        """,
        [status, resolution_note, finding_id],
    )
    return True


def calculate_confidence_with_findings(
    base_confidence: int,
    findings: list[VisionFinding],
) -> int:
    """Calculate adjusted confidence based on unresolved vision findings.

    Only NEW (unresolved) findings reduce confidence.

    Args:
        base_confidence: Base confidence score (0-100).
        findings: List of VisionFinding objects.

    Returns:
        Adjusted confidence score (0-100).
    """
    penalty = 0

    for finding in findings:
        if finding.status == "NEW":
            if finding.significance == "HIGH":
                penalty += 15
            elif finding.significance == "MEDIUM":
                penalty += 8
            elif finding.significance == "LOW":
                penalty += 3

    return max(0, base_confidence - penalty)


def get_all_vision_findings(
    conn: duckdb.DuckDBPyConnection,
    status: Optional[str] = None,
    significance: Optional[str] = None,
    limit: int = 100,
) -> list[VisionFinding]:
    """Get all vision findings across all functions.

    Args:
        conn: DuckDB connection.
        status: Optional status filter (NEW, ADDRESSED, IGNORED).
        significance: Optional significance filter (HIGH, MEDIUM, LOW).
        limit: Maximum number of results (default 100).

    Returns:
        List of VisionFinding objects.

    Raises:
        ValueError: If status or significance is invalid.
    """
    query = """
        SELECT id, function_id, finding_type, significance, description,
               location, invariant_implication, status, resolution_note,
               plot_path, created_at
        FROM vision_findings
        WHERE 1=1
    """
    params: list[Any] = []

    if status is not None:
        if status.upper() not in VALID_FINDING_STATUSES:
            raise ValueError(f"Invalid status: {status}. Must be one of {VALID_FINDING_STATUSES}")
        query += " AND status = ?"
        params.append(status.upper())

    if significance is not None:
        if significance.upper() not in VALID_FINDING_SIGNIFICANCE:
            raise ValueError(f"Invalid significance: {significance}. Must be one of {VALID_FINDING_SIGNIFICANCE}")
        query += " AND significance = ?"
        params.append(significance.upper())

    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    result = conn.execute(query, params).fetchall()
    return [VisionFinding.from_row(row) for row in result]
