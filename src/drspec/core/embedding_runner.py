"""The low-priority process that works through the embedding backlog.

The worker starts one of these when something is owed an embedding and it
exits when nothing is, so no model stays in memory while idle. It runs at
the lowest CPU priority on a fixed number of threads, sleeps between chunks
so its average share of a core stays under a cap, and pauses while the
machine is busy or short of memory. It reaches the database only through the
worker's socket and never holds the database itself.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

from drspec.core import embedder, tracing
from drspec.core.embedding_backlog import STATE_KEY, Item, VectorNotPersisted, fetch_texts, find_backlog
from drspec.core.settings import read_all_settings
from drspec.db.queries import set_config

CHUNK_SIZE = 20
PAUSE_SECONDS = 10.0

EXIT_DONE = 0
EXIT_MODEL_UNAVAILABLE = 3
# Vectors were stored but do not read back: retrying at once would loop.
EXIT_NOT_PERSISTED = 4


def available_memory_mb() -> Optional[float]:
    """MemAvailable from the kernel, or `None` where it can't be read."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        pass
    return None


def load_per_core() -> Optional[float]:
    try:
        return os.getloadavg()[0] / (os.cpu_count() or 1)
    except (OSError, AttributeError):
        return None


def pause_reason(settings: dict[str, Any]) -> Optional[str]:
    """Why embedding should wait right now, or `None`."""
    load = load_per_core()
    if load is not None and load > settings["embedding.max_load_per_core"]:
        return "machine busy"
    memory = available_memory_mb()
    if memory is not None and memory < settings["embedding.min_available_memory_mb"]:
        return "low memory"
    return None


def write_state(conn: Any, state: str, detail: str = "") -> None:
    try:
        set_config(conn, STATE_KEY, json.dumps({"state": state, "detail": detail, "at": time.time()}))
    except Exception:  # noqa: BLE001 - reporting must not stop the work
        pass


def _give_up_not_persisted(conn: Any, detail: str, items: list[Item], round_number: int) -> int:
    """Report that vectors do not persist and stop; the supervisor backs off."""
    write_state(conn, "error", "vector did not persist")
    print(f"vector did not persist: {detail}", flush=True)
    tracing.emit("embed", kind="not_persisted", round=round_number, items=len(items))
    return EXIT_NOT_PERSISTED


def run_embedding_process(
    conn: Any,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Embed the backlog through `conn` (a connection to the worker) until
    nothing is owed. Returns an exit code."""
    began = time.perf_counter()
    code = EXIT_DONE
    try:
        code = _embed_backlog(conn, sleep, clock)
        return code
    finally:
        if tracing.enabled():
            tracing.emit("embed", (time.perf_counter() - began) * 1000.0, kind="exit", code=code, **tracing.process_usage())


def _embed_backlog(conn: Any, sleep: Callable[[float], None], clock: Callable[[], float]) -> int:
    try:
        os.nice(19)
    except (OSError, AttributeError):
        pass

    settings = read_all_settings(conn)
    tracing.configure_from_settings(settings)
    if tracing.enabled():
        tracing.emit("embed", kind="start", **tracing.system_snapshot())
    if not settings["embedding.enabled"]:
        return EXIT_DONE
    backlog = find_backlog(conn, settings["embedding.top_n"], settings["embedding.half_life_days"], time.time())
    if not backlog:
        write_state(conn, "idle")
        return EXIT_DONE

    write_state(conn, "loading" if embedder.model_is_cached() else "downloading")
    try:
        embedder.load_model(settings["embedding.threads"])
    except Exception as e:  # noqa: BLE001 - any failure to get the model is retried later
        write_state(conn, "model_unavailable", str(e))
        return EXIT_MODEL_UNAVAILABLE
    write_state(conn, "running")

    round_number = 0
    while True:
        round_number += 1
        stored_this_round = 0
        queue: list[Item] = list(backlog)
        round_items = list(backlog)
        paused = False
        while queue:
            settings = read_all_settings(conn)
            tracing.configure_from_settings(settings)
            if not settings["embedding.enabled"]:
                write_state(conn, "disabled")
                return EXIT_DONE
            reason = pause_reason(settings)
            if reason is not None:
                write_state(conn, "paused", reason)
                paused = True
                tracing.emit("embed", PAUSE_SECONDS * 1000.0, kind="pause", reason=tracing.code_of(reason), round=round_number, owed=len(queue))
                sleep(PAUSE_SECONDS)
                continue
            if paused:
                write_state(conn, "running")
                paused = False

            chunk, queue = queue[:CHUNK_SIZE], queue[CHUNK_SIZE:]
            tracing.set_cid(tracing.new_cid())
            started = clock()
            results = [(item, embedder.embed_text(text)) for item, text in fetch_texts(conn, chunk)]
            # The extra reading is for the trace only; with tracing off the clock is read twice per chunk.
            embedded_at = clock() if tracing.enabled() else started
            try:
                stored_now = conn.store_embeddings(results) if results else 0
            except VectorNotPersisted as e:
                names = ", ".join(item.table + ":" + "|".join(item.keys) for item in e.items[:5])
                return _give_up_not_persisted(conn, f"{len(e.items)} stored item(s) did not read back (first: {names})", e.items, round_number)
            stored_this_round += stored_now
            ended = clock()
            worked = ended - started
            share = settings["embedding.cpu_percent"] / 100.0
            pause_for = max(0.0, worked * (1.0 / share - 1.0))
            if tracing.enabled():
                tracing.emit(
                    "embed",
                    worked * 1000.0,
                    kind="chunk",
                    round=round_number,
                    items=len(chunk),
                    embedded=len(results),
                    stored=stored_now,
                    embed_ms=round((embedded_at - started) * 1000.0, 3),
                    store_ms=round((ended - embedded_at) * 1000.0, 3),
                    sleep_s=round(pause_for, 3),
                    owed=len(queue),
                    **tracing.system_snapshot(),
                )
            sleep(pause_for)

        settings = read_all_settings(conn)
        owed_before = set(round_items)
        backlog = find_backlog(conn, settings["embedding.top_n"], settings["embedding.half_life_days"], time.time())
        if not backlog or stored_this_round == 0:
            write_state(conn, "idle")
            return EXIT_DONE
        still_owed = owed_before & set(backlog)
        if len(still_owed) == len(owed_before):
            # Vectors were stored yet nothing this round embedded is any less owed.
            return _give_up_not_persisted(conn, "a full round stored vectors but none of those items left the backlog", list(still_owed), round_number)
        write_state(conn, "running")
