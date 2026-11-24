"""What still needs embedding, and recording the result.

An item (a function, a call edge, or a reference edge) is up to date when it
has a vector and its `embedding:` bookkeeping row names the current model
and the current source hash. Two groups are owed an embedding:

* repairs: items that were embedded before but are not up to date now,
  because a re-scan cleared the vector, the source changed, or the model
  changed. An embedding that once existed is never silently dropped.
* hot items: the `embedding.top_n` highest-scoring items by decayed usage
  that have no up-to-date vector yet.

An item that falls out of the top N keeps its vector.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Optional

import duckdb

from drspec.core.embedder import MODEL_VERSION
from drspec.db.queries import check_embedding_length, get_config, set_artifact_embedding, set_config, top_usage_items

META_PREFIX = "embedding:"
STATE_KEY = "worker:embedding_state"

# usage kind -> (table, key columns, text column, source-hash column)
SPECS: dict[str, tuple[str, tuple[str, ...], str, str]] = {
    "node": ("artifacts", ("function_id",), "body", "code_hash"),
    "edge": ("dependencies", ("caller_id", "callee_id"), "call_site", "edge_hash"),
    "reference": ("code_references", ("caller_id", "callee_id"), "reference_site", "edge_hash"),
}
_KIND_BY_TABLE = {spec[0]: kind for kind, spec in SPECS.items()}


@dataclass(frozen=True)
class Item:
    table: str
    keys: tuple[str, ...]
    source_hash: str

    def as_wire(self) -> dict[str, Any]:
        return {"table": self.table, "keys": list(self.keys), "source_hash": self.source_hash}

    @staticmethod
    def from_wire(raw: dict[str, Any]) -> "Item":
        return Item(raw["table"], tuple(raw["keys"]), raw["source_hash"])


def meta_key(table: str, keys: Iterable[str]) -> str:
    return META_PREFIX + json.dumps([table, *keys])


def meta_value(source_hash: Any) -> str:
    return json.dumps([MODEL_VERSION, source_hash])


def _rows(conn: duckdb.DuckDBPyConnection, kind: str) -> dict[tuple[str, ...], tuple[Any, bool]]:
    table, key_columns, _text, hash_column = SPECS[kind]
    columns = ", ".join(key_columns)
    if table == "artifacts":  # the function vectors live in their own table
        sql = (
            "SELECT a.function_id, a.code_hash, e.function_id IS NOT NULL FROM artifacts a "
            "LEFT JOIN artifact_embeddings e ON e.function_id = a.function_id"
        )
    else:
        sql = f"SELECT {columns}, {hash_column}, embedding IS NOT NULL FROM {table}"
    return {
        tuple(row[: len(key_columns)]): (row[len(key_columns)], bool(row[len(key_columns) + 1]))
        for row in conn.execute(sql).fetchall()
    }


def find_backlog(conn: Any, top_n: int, half_life_days: float, now: float) -> list[Item]:
    """Everything owed an embedding, repairs first, then hot items by score."""
    metas = dict(conn.execute("SELECT key, value FROM config WHERE starts_with(key, ?)", [META_PREFIX]).fetchall())
    rows = {kind: _rows(conn, kind) for kind in SPECS}

    def owed(kind: str, keys: tuple[str, ...]) -> Any:
        row = rows[kind].get(keys)
        if row is None or row[0] is None:
            return None
        source_hash, has_vector = row
        table = SPECS[kind][0]
        stored = metas.get(meta_key(table, keys))
        if has_vector and stored == meta_value(source_hash):
            return None
        return Item(table, keys, source_hash), stored is not None or has_vector

    repairs: list[Item] = []
    for kind in SPECS:
        for keys in sorted(rows[kind]):
            found = owed(kind, keys)
            if found is not None and found[1]:
                repairs.append(found[0])

    seen = set(repairs)
    hot: list[Item] = []
    for kind, item_id, related_id, _score in top_usage_items(conn, top_n, now, half_life_days):
        if kind not in SPECS:
            continue
        keys = (item_id,) if kind == "node" else (item_id, related_id)
        found = owed(kind, keys)
        if found is not None and found[0] not in seen:
            seen.add(found[0])
            hot.append(found[0])
    return repairs + hot


def embedding_counts(conn: Any) -> dict[str, int]:
    """`embedded` and `total` over all items."""
    embedded = total = 0
    for table, _keys, _text, _hash in SPECS.values():
        if table == "artifacts":
            row = (conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0], conn.execute("SELECT COUNT(*) FROM artifact_embeddings").fetchone()[0])
        else:
            row = conn.execute(f"SELECT COUNT(*), COUNT(embedding) FROM {table}").fetchone()
        total += row[0]
        embedded += row[1]
    return {"embedded": embedded, "total": total}


def fetch_texts(conn: Any, items: list[Item]) -> list[tuple[Item, str]]:
    """Each item's current text, for those whose source hash has not moved
    since the backlog was computed."""
    out: list[tuple[Item, str]] = []
    for item in items:
        _table, key_columns, text_column, hash_column = SPECS[_KIND_BY_TABLE[item.table]]
        where = " AND ".join(f"{column} = ?" for column in key_columns)
        row = conn.execute(f"SELECT {text_column}, {hash_column} FROM {item.table} WHERE {where}", list(item.keys)).fetchone()
        if row is not None and row[0] is not None and row[1] == item.source_hash:
            out.append((item, row[0]))
    return out


class VectorNotPersisted(RuntimeError):
    """A stored vector did not read back, so the database cannot be trusted to
    keep embeddings. `items` are the ones that did not."""

    def __init__(self, items: list["Item"]) -> None:
        super().__init__(f"{len(items)} stored vector(s) did not persist")
        self.items = items


def _store(conn: duckdb.DuckDBPyConnection, results: list[tuple[Item, list[float]]]) -> list[Item]:
    """Write each vector whose item still has the same source hash; returns those written."""
    for _item, vector in results:
        check_embedding_length(vector)
    written: list[Item] = []
    for item, vector in results:
        _table, key_columns, _text, hash_column = SPECS[_KIND_BY_TABLE[item.table]]
        where = " AND ".join(f"{column} = ?" for column in key_columns)
        current = conn.execute(f"SELECT 1 FROM {item.table} WHERE {where} AND {hash_column} = ?", [*item.keys, item.source_hash]).fetchone()
        if current is None:
            continue
        if item.table == "artifacts":
            set_artifact_embedding(conn, item.keys[0], vector)
        else:
            conn.execute(f"UPDATE {item.table} SET embedding = ? WHERE {where}", [vector, *item.keys])
        set_config(conn, meta_key(item.table, item.keys), meta_value(item.source_hash))
        written.append(item)
    return written


def unreadable(conn: Any, items: list[Item]) -> list[Item]:
    """The items whose vector does not show up when the table is read the way
    `find_backlog` reads it (a full scan of every row), which is a different
    access path from the keyed write that stored it."""
    wanted: dict[str, list[Item]] = {}
    for item in items:
        wanted.setdefault(_KIND_BY_TABLE[item.table], []).append(item)
    missing: list[Item] = []
    for kind, group in wanted.items():
        rows = _rows(conn, kind)
        missing.extend(item for item in group if not rows.get(item.keys, (None, False))[1])
    return missing


def store_embeddings(conn: duckdb.DuckDBPyConnection, results: list[tuple[Item, list[float]]]) -> int:
    """Write vectors for items whose source hash still matches, record the model
    version used, and read them back. Returns how many were stored; raises
    `VectorNotPersisted` if any of them does not read back."""
    written = _store(conn, results)
    lost = unreadable(conn, written)
    if lost:
        raise VectorNotPersisted(lost)
    return len(written)


def semantic_coverage(conn: Any, language: Optional[str] = None, path_prefix: Optional[str] = None, status: Optional[str] = None) -> dict[str, Any]:
    """What a semantic search looked at, for its result: `searched` (the functions in its scope that have an embedding), `functions_in_scope`
    and, when some have none, `coverage_note`: a function without an embedding cannot be found by its meaning, and how to raise it."""
    from drspec.core.settings import get_setting
    from drspec.db.queries import count_semantic_scope

    searched, total = count_semantic_scope(conn, language, path_prefix, status)
    found: dict[str, Any] = {"searched": searched, "functions_in_scope": total}
    if searched < total:
        top_n = get_setting(conn, "embedding.top_n")
        found["coverage_note"] = (
            f"Searched {searched} of {total} functions: a function without an embedding cannot be found by its meaning. To raise it: "
            f"`drspec scan <paths> --embed` embeds the files it reads, and `drspec config set embedding.top_n <N>` (now {top_n}) lets the "
            "background worker embed more of the functions in use."
        )
    return found


def embedding_status(conn: Any, settings: dict[str, Any], now: float) -> dict[str, Any]:
    """Progress and state for `status`: counts, how much is owed, and what
    the embedding process last reported."""
    counts = embedding_counts(conn)
    enabled = bool(settings["embedding.enabled"])
    owed = len(find_backlog(conn, settings["embedding.top_n"], settings["embedding.half_life_days"], now)) if enabled else 0
    raw = get_config(conn, STATE_KEY)
    try:
        reported = json.loads(raw) if raw else {}
    except ValueError:
        reported = {}
    state = reported.get("state", "idle")
    if not enabled:
        message = "embedding off"
    elif state == "model_unavailable":
        message = "embedding waiting: model download failed"
    elif state == "error":
        message = f"embedding failed: {reported.get('detail') or 'unknown error'}"
    elif state == "downloading":
        message = "embedding: downloading the model"
    elif state == "paused":
        message = f"embedding paused: {reported.get('detail') or 'machine busy'}"
    elif owed:
        message = f"embedding: {owed} waiting ({counts['embedded']} of {counts['total']} embedded, top_n = {settings['embedding.top_n']})"
    elif counts["embedded"] < counts["total"]:
        # nothing is owed because only the functions in use are candidates (`embedding.top_n`): the rest is not "up to date"
        message = f"{counts['embedded']} of {counts['total']} embedded (top_n = {settings['embedding.top_n']}): the others are not found by meaning"
    else:
        message = "embedding up to date"
    return {"enabled": enabled, **counts, "top_n": settings["embedding.top_n"], "backlog": owed, "state": state, "message": message}
