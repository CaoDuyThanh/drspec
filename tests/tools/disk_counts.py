"""How much disk work a test makes, as COUNTS (report only: nothing here fails a test).

The wall time of a test that writes a DuckDB file is mostly the disk's flushes, and it depends on what else the machine is doing; the
CPU time hides it. The number of flushes the test asks for does not depend on the machine, so it is the figure to cut:

  commits      a write statement of a file database outside `BEGIN` .. `COMMIT`, and every `COMMIT` (each is a write of the log)
  checkpoints  an explicit `CHECKPOINT`
  closes       a close of a file database (DuckDB checkpoints on the last close of a file)
  fsyncs       `os.fsync` calls
  connects     file databases opened (`:memory:` is free and not counted)

`install()` wraps `duckdb.connect` and `os.fsync`; `uninstall()` restores them. `tests/tools/tier_summary.py` installs it when
`DRSPEC_TIER_REPORT=1` and prints the counts next to the CPU time. It reads the statements a test makes through the connection object
(`execute`, `executemany`); work done by a cursor or a C-level path is not seen, which is why these are counts of requests, not of disk calls.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from typing import Any

FIELDS = ("commits", "checkpoints", "closes", "fsyncs", "connects")
_WRITE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|COPY|CALL)\b", re.IGNORECASE)

counts: Counter = Counter()
_originals: dict = {}


def snapshot() -> dict:
    return {name: counts[name] for name in FIELDS}


def reset() -> None:
    counts.clear()


def delta(before: dict) -> dict:
    return {name: counts[name] - before.get(name, 0) for name in FIELDS}


class CountedConnection:
    """A DuckDB connection that tells `counts` what is asked of it; everything else is the real connection."""

    def __init__(self, inner: Any, on_disk: bool) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_on_disk", on_disk)
        object.__setattr__(self, "_in_transaction", False)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _statement(self, sql: Any) -> None:
        if not self._on_disk or not isinstance(sql, str):
            return
        word = sql.strip().split(None, 1)[0].upper() if sql.strip() else ""
        if word == "CHECKPOINT":
            counts["checkpoints"] += 1
        elif word == "BEGIN":
            object.__setattr__(self, "_in_transaction", True)
        elif word in ("COMMIT", "END"):
            object.__setattr__(self, "_in_transaction", False)
            counts["commits"] += 1
        elif word == "ROLLBACK":
            object.__setattr__(self, "_in_transaction", False)
        elif _WRITE.match(sql) and not self._in_transaction:
            counts["commits"] += 1

    def execute(self, sql: Any, *args: Any, **kwargs: Any) -> Any:
        self._statement(sql)
        return self._inner.execute(sql, *args, **kwargs)

    def executemany(self, sql: Any, *args: Any, **kwargs: Any) -> Any:
        self._statement(sql)
        return self._inner.executemany(sql, *args, **kwargs)

    def commit(self) -> Any:
        if self._on_disk:
            counts["commits"] += 1
        return self._inner.commit()

    def close(self) -> Any:
        if self._on_disk:
            counts["closes"] += 1
        return self._inner.close()


def _counting_connect(original):
    def connect(database=":memory:", *args, **kwargs):
        on_disk = str(database) != ":memory:"
        if on_disk:
            counts["connects"] += 1
        return CountedConnection(original(database, *args, **kwargs), on_disk)

    return connect


def _counting_fsync(original):
    def fsync(descriptor):
        counts["fsyncs"] += 1
        return original(descriptor)

    return fsync


def install() -> None:
    """Wrap `duckdb.connect` and `os.fsync` (idempotent)."""
    if _originals:
        return
    import duckdb

    for owner, attribute, factory in ((duckdb, "connect", _counting_connect), (os, "fsync", _counting_fsync)):
        original = getattr(owner, attribute)
        wrapper = factory(original)
        setattr(owner, attribute, wrapper)
        _originals[(owner, attribute)] = (original, wrapper)


def uninstall() -> None:
    """Put the originals back, except where another tool wrapped ours after it (its own restore will do that)."""
    for (owner, attribute), (original, wrapper) in list(_originals.items()):
        if getattr(owner, attribute) is wrapper:
            setattr(owner, attribute, original)
    _originals.clear()
