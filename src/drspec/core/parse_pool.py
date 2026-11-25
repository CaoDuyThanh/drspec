"""Parsing files on several cores.

Parsing is independent per file, and Python threads share one interpreter lock that the tree-sitter parser does not
give up (measured on 213 files: one thread 4.9 s, four threads 4.9 s, four processes 1.8 s with a start-up of 0.3 s),
so the files are parsed in a pool of processes. Everything that touches the database stays with the one process that
owns it: the pool only turns file paths into parse results, and the results come back in the order the paths were given,
so the same input gives the same output however many processes there are.

A process is started with `spawn`, not `fork`: the worker holds a database, sockets and threads, which a forked child
would inherit half-way through whatever they were doing.
"""

from __future__ import annotations

import multiprocessing
import os
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

from drspec.core import tracing

#: A parser process holds the grammars and a share of the interpreter: about this much memory each.
MEMORY_PER_PARSER_MB = 250
#: Where the worker keeps how many parsers the last batch used and what held the number down (for `status`).
PARSERS_KEY = "worker:parsers"
#: `status` shows it only while it is recent: a count from an hour ago says nothing about the machine now.
PARSERS_FRESH_SECONDS = 600

_SCANNER: Any = None


def _leave_with_the_parent(parent: int) -> None:
    """A parser process whose worker is gone (killed outright, so it could not ask them to stop) leaves too."""
    while True:
        time.sleep(2.0)
        if os.getppid() != parent:
            os._exit(0)


def _start_parser(directory: str, parent: int) -> None:
    """Runs once in each parser process: work from the project folder, watch for the worker going away, and load
    the grammars now rather than on the first file."""
    global _SCANNER
    os.chdir(directory)
    threading.Thread(target=_leave_with_the_parent, args=(parent,), daemon=True).start()
    from drspec.core.scanner import Scanner

    _SCANNER = Scanner()


def _parse_one(path: str) -> Tuple[Optional[Any], Optional[str], float]:
    """`(result, error, cpu_seconds)` for one file; an exception becomes the error text, not a dead process."""
    started = time.process_time()
    try:
        result = _SCANNER.scan_file(path, relative_to=Path.cwd())
        return result, None, time.process_time() - started
    except Exception as error:  # noqa: BLE001 - the caller parses this file again itself, to report it properly
        return None, f"{type(error).__name__}: {error}", time.process_time() - started


class ParsePool:
    """A set of parser processes, started on first use and kept for the next batch."""

    def __init__(self, workers: int) -> None:
        self.workers = workers
        self.cpu_seconds = 0.0
        """Total CPU the parser processes have used, so the worker's CPU limit can count it."""
        self.last_used = time.monotonic()
        self._executor: Optional[ProcessPoolExecutor] = None

    def _pool(self) -> ProcessPoolExecutor:
        if self._executor is None:
            self._executor = ProcessPoolExecutor(
                self.workers, mp_context=multiprocessing.get_context("spawn"), initializer=_start_parser, initargs=(str(Path.cwd()), os.getpid())
            )
        return self._executor

    def parse(self, paths: Sequence[str]) -> List[Tuple[Optional[Any], Optional[str]]]:
        """Parse `paths`; one `(result, error)` per path, in the same order. Raises `BrokenProcessPool` if a parser
        process died (the pool is then dropped, and the caller falls back to parsing itself)."""
        self.last_used = time.monotonic()
        began = time.perf_counter()
        try:
            outcomes = list(self._pool().map(_parse_one, paths, chunksize=max(1, len(paths) // (self.workers * 4))))
        except BrokenProcessPool:
            self.close()
            raise
        self.cpu_seconds += sum(cpu for _result, _error, cpu in outcomes)
        self.last_used = time.monotonic()
        if tracing.enabled():
            tracing.emit("rescan", (time.perf_counter() - began) * 1000.0, kind="parse_round", files=len(paths), workers=self.workers)
        return [(result, error) for result, error, _cpu in outcomes]

    def close(self) -> None:
        executor, self._executor = self._executor, None
        if executor is not None:
            # The processes are told to stop rather than left to notice: an idle one waits for work forever, and a
            # worker that exits first would leave them behind. (`cancel_futures` needs Python 3.9; 3.8 is supported.)
            for process in list(getattr(executor, "_processes", {}).values()):
                try:
                    process.terminate()
                except Exception:  # noqa: BLE001 - already gone
                    pass
            executor.shutdown(wait=False)


_pool: Optional[ParsePool] = None


def get_pool(workers: int) -> ParsePool:
    """The shared pool, with `workers` processes (replaced when the number changes)."""
    global _pool
    if _pool is not None and _pool.workers != workers:
        _pool.close()
        _pool = None
    if _pool is None:
        _pool = ParsePool(workers)
    return _pool


def cpu_seconds() -> float:
    """CPU the parser processes have used so far (zero when there is no pool)."""
    return _pool.cpu_seconds if _pool is not None else 0.0


def close_if_idle(idle_seconds: float = 60.0) -> None:
    """Give the processes back when nothing has been parsed for a while."""
    global _pool
    if _pool is not None and time.monotonic() - _pool.last_used >= idle_seconds:
        _pool.close()
        _pool = None


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def parser_plan(settings: dict, scanning: bool, busy: Optional[str], available_memory_mb: Optional[float]) -> tuple:
    """`(how many parser processes to use for a batch, why it is fewer than wanted or None)`.

    A waiting scan takes every core but one (`scan.workers` sets another number). The background drain takes as many
    as `worker.cpu_percent` of the machine allows, and one while the machine is busy. Either is held down by the
    memory that is free. The reason is `machine busy` or `low memory` when that is what held the number down."""
    cores = os.cpu_count() or 1
    why = None
    if scanning:
        configured = int(settings.get("scan.workers", 0))
        wanted = configured if configured > 0 else max(1, cores - 1)
    else:
        wanted = max(1, round(cores * max(1, int(settings.get("worker.cpu_percent", 20))) / 100.0))
        if busy is not None:
            why = busy if wanted > 1 else None  # (one is what it would have been)
            wanted = 1
    wanted = min(wanted, cores * 2)
    if available_memory_mb is not None:
        spare = available_memory_mb - float(settings.get("embedding.min_available_memory_mb", 1500))
        by_memory = max(1, int(spare // MEMORY_PER_PARSER_MB))
        if by_memory < wanted:
            wanted, why = by_memory, "low memory"
    return max(1, wanted), why


def parser_count(settings: dict, scanning: bool, busy: Optional[str], available_memory_mb: Optional[float]) -> int:
    """How many parser processes to use for a batch (see `parser_plan`)."""
    return parser_plan(settings, scanning, busy, available_memory_mb)[0]


def parsers_report(conn: Any, now: Optional[float] = None) -> Optional[dict]:
    """`{count, held_down_by, line}` for the last batch the worker parsed, while it is recent, else `None`.
    The line reads `parsers: 1 (machine busy)` when something held the number down."""
    import json

    try:
        row = conn.execute("SELECT value FROM config WHERE key = ?", [PARSERS_KEY]).fetchone()
        record = json.loads(row[0]) if row and row[0] else None
    except Exception:  # noqa: BLE001 - a status call never fails over a missing figure
        return None
    if not isinstance(record, dict) or not isinstance(record.get("count"), int):
        return None
    if (now if now is not None else time.time()) - float(record.get("at", 0)) > PARSERS_FRESH_SECONDS:
        return None
    reason = record.get("held_down_by")
    return {"count": record["count"], "held_down_by": reason, "line": f"parsers: {record['count']}" + (f" ({reason})" if reason else "")}
