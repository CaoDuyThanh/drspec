"""The worker's request/response IPC interface.

A held-open read-only DuckDB connection was assumed not to block a
writer; an isolated, zero-drspec-code repro confirmed that false for
this project's actual DuckDB version (two separate processes: one
holds a read-only connection open, the other's plain `duckdb.connect()`
fails immediately with a lock-conflict `IOException`, every run).
DuckDB allows at most one connection of any kind to touch the file for
a writer to succeed at all.

The fix: the staleness worker becomes the *only* process that ever
opens the database file, for both reads and writes, so DuckDB's
documented single-process concurrency model (threads + MVCC) applies
cleanly. Every other command is a thin client of this module instead
of calling `get_connection()` directly.

Transport is a Unix domain socket (not HTTP, unlike `drspec serve`) —
purely local, no port to manage, stdlib-only. Framing is newline-
delimited JSON, the same pattern `mcp_server.py` already uses for its
own stdio protocol.

Concurrency model: one shared `threading.Lock` around the worker's
single real connection. A bare `execute` holds the lock only for that
one call; an explicit `begin()` acquires it and doesn't release it
until that same client's `commit()`/`rollback()` — safe because each
client is served by its own handler thread processing its own requests
in order, so no other client's request can interleave inside the
transaction. A client that disconnects mid-transaction is caught in
the handler's own `finally:` (rollback + release) so one dropped
connection can't wedge the whole worker.

Exception fidelity: a `duckdb.Error` is serialized by class name and
reconstructed as the same exception class on the client side (falling
back to `RuntimeError` otherwise) — several existing `except
duckdb.Error:` call sites depend on catching the real type, not a
generic one.

Publishing: `remediate` runs the whole write in one transaction
on this same connection (`db.staging.publish_transaction`), under `self._lock`,
so no copy of the database file is made and nothing needs reconnecting.

Test mode (`DRSPEC_DISABLE_AUTO_WORKER`): `get_worker_connection`
returns a `_DirectConnection` — a plain, single-use `duckdb.connect()`
per call — instead of spawning a real worker and talking to it over a
socket, so ~1400 same-process unit tests don't each pay for a real OS
process.
"""

from __future__ import annotations

import base64
import datetime
import decimal
import hashlib
import json
import logging
import os
import re
import socket
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

import duckdb

from drspec.core import tracing
from drspec.core.build_id import build_id

logger = logging.getLogger(__name__)

SOCKET_FILENAME = "worker.sock"

# How long a client waits, in total, for the worker's socket to come up
# after (re-)spawning it — a cold start (schema init, first connect)
# should clear well inside this; a client surfaces a clear error rather
# than hanging forever if it doesn't, and never silently falls back to
# a direct connection, which would quietly reintroduce the exact race
# this module exists to close.
CONNECT_TIMEOUT_SECONDS = 15.0
_POLL_INTERVAL_SECONDS = 0.1

# While waiting, how often to re-attempt a spawn: the previous worker may
# still be shutting down when the first attempt runs (its PID file is the
# last thing it releases), which makes that attempt a no-op.
_RESPAWN_INTERVAL_SECONDS = 2.0

# One request/response round trip — generous for a real scan of a
# non-trivial project, short enough that a genuinely hung worker is
# reported, not waited on forever.
REQUEST_TIMEOUT_SECONDS = 120.0


#: A Unix socket path has to fit in about 104 bytes (macOS) to 108 (Linux), counting the
#: terminator; a project folder nested deeper than that cannot hold the socket.
_MAX_SOCKET_PATH_BYTES = 100


def socket_path(db_path: Path) -> Path:
    """Where the worker for `db_path` listens: beside the database, or, when that path
    would be too long for a socket, in the temporary directory under a name derived
    from the database's own path (every process derives the same one)."""
    beside = Path(db_path).parent / SOCKET_FILENAME
    if len(os.fsencode(str(beside))) <= _MAX_SOCKET_PATH_BYTES:
        return beside
    digest = hashlib.sha1(os.fsencode(str(Path(db_path).resolve()))).hexdigest()[:16]
    shorter = Path(tempfile.gettempdir()) / f"drspec-{digest}.sock"
    if len(os.fsencode(str(shorter))) > _MAX_SOCKET_PATH_BYTES:
        raise WorkerIPCError(
            f"The path {beside} is too long for the worker's socket (limit about {_MAX_SOCKET_PATH_BYTES} characters), "
            f"and so is the temporary directory {shorter.parent}. Move the project to a shorter path, or set TMPDIR to a short one."
        )
    return shorter


# ---------------------------------------------------------------------------
# JSON encode/decode for raw DuckDB row values — the lists and tuples DuckDB returns
# for list-typed columns (e.g. `embedding FLOAT[]`) and the temporal,
# decimal and binary types aren't JSON-native.
# ---------------------------------------------------------------------------

_TAGGED_DECODERS = {
    "__datetime__": datetime.datetime.fromisoformat,
    "__date__": datetime.date.fromisoformat,
    "__time__": datetime.time.fromisoformat,
    "__timedelta__": lambda parts: datetime.timedelta(*parts),
    "__decimal__": decimal.Decimal,
    "__bytes__": base64.b64decode,
}


def _encode_value(value: Any) -> Any:
    # `datetime.datetime` subclasses `datetime.date`, so it must be tested first.
    if isinstance(value, datetime.datetime):
        return {"__datetime__": value.isoformat()}
    if isinstance(value, datetime.date):
        return {"__date__": value.isoformat()}
    if isinstance(value, datetime.time):
        return {"__time__": value.isoformat()}
    if isinstance(value, datetime.timedelta):
        return {"__timedelta__": [value.days, value.seconds, value.microseconds]}
    if isinstance(value, decimal.Decimal):
        return {"__decimal__": str(value)}
    if isinstance(value, (bytes, bytearray)):
        return {"__bytes__": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, (tuple, list)):
        return [_encode_value(v) for v in value]
    return value


def _decode_value(value: Any) -> Any:
    if isinstance(value, dict) and len(value) == 1:
        (tag, payload), = value.items()
        decoder = _TAGGED_DECODERS.get(tag)
        if decoder is not None:
            return decoder(payload)
    if isinstance(value, list):
        return [_decode_value(v) for v in value]
    return value


def encode_rows(rows: list[tuple]) -> list[list[Any]]:
    return [[_encode_value(v) for v in row] for row in rows]


def decode_rows(rows: list[list[Any]]) -> list[tuple]:
    return [tuple(_decode_value(v) for v in row) for row in rows]


def _reconstruct_exception(error_type: str, message: str) -> Exception:
    cls = getattr(duckdb, error_type, None)
    if isinstance(cls, type) and issubclass(cls, BaseException):
        return cls(message)
    return RuntimeError(message)


def _read_line(conn_sock: socket.socket, buffer: bytearray) -> Optional[bytes]:
    """Read one newline-delimited message from `conn_sock`, using
    `buffer` to hold anything already read past the delimiter.
    `None` on a clean EOF with nothing left buffered."""
    while b"\n" not in buffer:
        chunk = conn_sock.recv(65536)
        if not chunk:
            return None if not buffer else bytes(buffer)
        buffer.extend(chunk)
    line, _, rest = bytes(buffer).partition(b"\n")
    buffer.clear()
    buffer.extend(rest)
    return line


def _send_message(conn_sock: socket.socket, message: dict) -> None:
    conn_sock.sendall((json.dumps(message) + "\n").encode("utf-8"))


# ---------------------------------------------------------------------------
# Client side
# ---------------------------------------------------------------------------


class WorkerIPCError(RuntimeError):
    """The worker couldn't be reached at all (not a query error — a
    query error reconstructs as the real `duckdb`/other exception
    instead)."""


class _WorkerCursor:
    """Just enough of DuckDB's own cursor-ish `execute()` return value
    for every existing call site's `conn.execute(...).fetchall()` /
    `.fetchone()` to keep working unchanged."""

    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows
        self._index = 0

    def fetchall(self) -> list[tuple]:
        return self._rows

    def fetchone(self) -> Optional[tuple]:
        if self._index < len(self._rows):
            row = self._rows[self._index]
            self._index += 1
            return row
        return None


class WorkerConnection:
    """Drop-in stand-in for a `duckdb.DuckDBPyConnection`, for exactly
    the subset of the API this project's own command code uses
    (`execute`, `begin`, `commit`, `rollback`, `close`) — routes every
    call through the worker's single real connection instead of
    opening one directly."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buffer = bytearray()
        self._closed = False
        self._unanswered = 0
        """Requests sent whose reply was not read: a Ctrl-C (or any exception that is not a lost connection) that came
        while a request was waiting for its reply leaves the reply on the socket, and it would be read as the answer to
        the next request. Those replies are read and dropped before the next request is sent."""

    def _drop_unanswered(self) -> None:
        while self._unanswered:
            try:
                line = _read_line(self._sock, self._buffer)
            except OSError as e:
                raise WorkerIPCError(f"Lost contact with the drspec worker: {e}") from e
            if line is None:
                raise WorkerIPCError("The drspec worker closed the connection unexpectedly.")
            self._unanswered -= 1

    def _request(self, message: dict) -> dict:
        if self._closed:
            raise WorkerIPCError("This connection is already closed.")
        self._drop_unanswered()
        # An older worker ignores the correlation id.
        message = {**message, "cid": tracing.current_cid()}
        started = time.perf_counter()
        try:
            self._unanswered += 1  # (counted before it is sent: an interrupt may come at any point after)
            _send_message(self._sock, message)
            line = _read_line(self._sock, self._buffer)
            if line is not None:
                self._unanswered -= 1
        except OSError as e:
            self._unanswered = 0  # the connection itself is gone: there is nothing to read
            raise WorkerIPCError(f"Lost contact with the drspec worker: {e}") from e
        if line is None:
            self._unanswered = 0
            raise WorkerIPCError("The drspec worker closed the connection unexpectedly.")
        tracing.add("ipc_ms", (time.perf_counter() - started) * 1000.0)
        tracing.add("ipc_calls", 1)
        response = json.loads(line)
        tracing.observe_worker(response.get("trace"))
        if not response.get("ok", False):
            raise _reconstruct_exception(response.get("error_type", ""), response.get("error", "worker error"))
        return response

    def execute(self, sql: str, params: Optional[list] = None) -> _WorkerCursor:
        # Params need the same datetime/array encoding row *values*
        # already get: `_fk_references_detached` (`db/queries.py`)
        # fetches a row, then feeds it straight back as `execute(...)`'s
        # own params to restore it, so a bound parameter can itself be a
        # previously-fetched `datetime` — plain `json.dumps` can't
        # serialize that otherwise.
        response = self._request({"op": "execute", "sql": sql, "params": encode_rows([params or []])[0]})
        return _WorkerCursor(decode_rows(response.get("rows", [])))

    def begin(self) -> None:
        self._request({"op": "begin"})

    def commit(self) -> None:
        self._request({"op": "commit"})

    def rollback(self) -> None:
        self._request({"op": "rollback"})

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._request({"op": "close"})
        except WorkerIPCError:
            pass
        finally:
            self._closed = True
            try:
                self._sock.close()
            except OSError:
                pass

    @contextmanager
    def _while_the_worker_works(self) -> Iterator[None]:
        """A request that takes as long as the work it asks for (a batch of a large project is published for minutes, a scan of
        100,000 files is listed for as long) is waited for without a read timeout: the worker is alive and answers when it is done.
        With the usual timeout the request failed after `REQUEST_TIMEOUT_SECONDS`, the answer that came later was read as the answer of
        the next request, and every later request of this connection got another's rows."""
        previous = self._sock.gettimeout()
        self._sock.settimeout(None)
        try:
            yield
        finally:
            try:
                self._sock.settimeout(previous)
            except OSError:
                pass

    def scan_start(self, **spec: Any) -> dict:
        """Ask the worker to find what a scan has to read and queue it (see `core.scan_service.start_scan`)."""
        with self._while_the_worker_works():
            return self._request({"op": "scan_start", "spec": spec})["result"]

    def scan_progress(self, scan_id: str) -> dict:
        return self._request({"op": "scan_progress", "scan_id": scan_id})["result"]

    def scan_cancel(self, scan_id: str) -> dict:
        return self._request({"op": "scan_cancel", "scan_id": scan_id})["result"]

    def scan_detach(self, scan_id: str) -> dict:
        return self._request({"op": "scan_detach", "scan_id": scan_id})["result"]

    def mark_internal(self) -> None:
        """Tell the worker this connection is its own (loopback or embedding
        child), so its requests don't wake it as client activity."""
        self._request({"op": "hello", "internal": True})

    def request_restart(self) -> bool:
        """Ask the worker to replace itself when it is idle. True when it is busy now (a batch or a waited-for scan: it finishes
        that first and this connection stays valid); False when it will exit at once. A worker too old to know the request answers
        with an error (the caller then stops it as before)."""
        return bool(self._request({"op": "restart_when_idle"}).get("busy"))

    def worker_build(self) -> str:
        """The build identifier the worker reports; empty for a worker too old
        to report one."""
        return str(self._request({"op": "hello"}).get("build") or "")

    def store_embeddings(self, results: list[tuple[Any, list[float]]]) -> int:
        """Hand computed vectors to the worker, which writes them in one
        step under its lock (an artifact write briefly detaches the rows that
        reference it, which no other request may observe half-done)."""
        from drspec.core.embedding_backlog import Item, VectorNotPersisted

        wire = [{"item": item.as_wire(), "vector": vector} for item, vector in results]
        response = self._request({"op": "store_embeddings", "results": wire})
        if response.get("unpersisted"):
            raise VectorNotPersisted([Item.from_wire(raw) for raw in response["unpersisted"]])
        return response["stored"]

    def remediate(self, file_paths: list[str], pass_seconds: Optional[float] = None) -> int:
        """The worker's own staleness-remediation publish, routed
        through this same loopback connection (module docstring) so it
        shares the exact lock/reconnect discipline every external
        client gets, rather than touching the real connection by a
        second, separately-reasoned-about path."""
        message: dict = {"op": "remediate", "file_paths": file_paths}
        if pass_seconds is not None:
            message["pass_seconds"] = pass_seconds
        with self._while_the_worker_works():
            response = self._request(message)
        return response["remediated"]


def _try_connect(path: Path, timeout: float) -> Optional[socket.socket]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(path))
    except OSError:
        sock.close()
        return None
    sock.settimeout(REQUEST_TIMEOUT_SECONDS)
    return sock


_DISABLE_ENV_VAR = "DRSPEC_DISABLE_AUTO_WORKER"


class _DirectConnection:
    """Test-mode-only stand-in (see module docstring's "Test mode"):
    the same `.execute`/`.begin`/`.commit`/`.rollback`/`.close`/
    `.sync_scan`/`.remediate` surface as `WorkerConnection`, but talks
    to one single-use real `duckdb` connection directly, in-process —
    no socket, no worker: each call site opens one of these, does its one
    thing, and closes it.
    """

    def __init__(self, db_path: Path, read_only: bool = False) -> None:
        self._db_path = db_path
        # Unlike the real worker (one connection serving every command,
        # so `read_only` has nothing to attach to), this *is* a regular
        # per-command connection — keeping `read_only` real here matters:
        # a regression test exercises "a held-open reader doesn't block
        # another reader" against a real CLI invocation, which runs
        # through this path in test mode.
        self._conn = duckdb.connect(str(db_path), read_only=read_only)
        self.read_only = read_only

    def execute(self, sql: str, params: Optional[list] = None):
        return self._conn.execute(sql, params or [])

    def begin(self) -> None:
        self._conn.begin()

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def scan_start(self, **spec: Any) -> dict:
        """No worker loop in this mode: the scan's files are read right away, in the batches the worker would use."""
        from drspec.core import scan_service

        from drspec.db.connection import init_schema

        init_schema(self._conn)  # a schema addition reaches an existing project here, as the worker's start does it
        result = scan_service.start_scan(self._conn, self._db_path, spec)
        if spec.get("wait", True):
            scan_service.drain(self._conn, self._db_path, result["scan_id"])
        return result

    def scan_progress(self, scan_id: str) -> dict:
        from drspec.core import scan_service

        return scan_service.progress(self._conn, scan_id)

    def scan_cancel(self, scan_id: str) -> dict:
        from drspec.core import scan_service

        return scan_service.cancel(self._conn, scan_id)

    def scan_detach(self, scan_id: str) -> dict:
        from drspec.core import scan_service

        return scan_service.detach(self._conn, scan_id)

    def mark_internal(self) -> None:
        pass

    def store_embeddings(self, results: list[tuple[Any, list[float]]]) -> int:
        from drspec.core.embedding_backlog import store_embeddings

        return store_embeddings(self._conn, results)

    def remediate(self, file_paths: list[str], pass_seconds: Optional[float] = None) -> int:
        from drspec.staleness_worker import _remediate_queued_files

        return _remediate_queued_files(self._conn, self._db_path, file_paths, pass_seconds)


def connect_loopback(db_path: Path, timeout: float = 5.0) -> WorkerConnection:
    """For the worker's own staleness-detection loop only (module
    docstring): connects to the socket the worker itself just bound,
    as an ordinary client, so that loop shares the exact same
    lock/reconnect discipline as every external client rather than
    touching `self._conn` through some second, untested path. Must be
    called only after `WorkerIPCServer.start()` has returned."""
    sock = _try_connect(socket_path(db_path), timeout=timeout)
    if sock is None:
        raise WorkerIPCError(f"Could not connect to the worker's own just-started socket for {db_path}.")
    connection = WorkerConnection(sock)
    connection.mark_internal()
    return connection


def peek_worker_build(db_path: Path) -> Optional[str]:
    """The build identifier of the worker serving `db_path`, without starting one
    or asking it to do anything else: `None` when none answers, an empty string
    when it is too old to report one."""
    sock = _try_connect(socket_path(db_path), timeout=2.0)
    if sock is None:
        return None
    connection = WorkerConnection(sock)
    try:
        return connection.worker_build()
    except WorkerIPCError:
        return None
    finally:
        connection.close()


def worker_is_outdated(db_path: Path) -> bool:
    """True when a worker is serving `db_path` with code other than this
    process's, which is what it looks like after the package was reinstalled."""
    build = peek_worker_build(db_path)
    return build is not None and build != build_id()


# Databases whose outdated worker this process has already asked to restart. Two
# installs of different code sharing one project would otherwise keep replacing
# each other's worker on every command.
_restart_attempted: set[str] = set()


def _restart_if_outdated(db_path: Path, connection: WorkerConnection) -> Optional[WorkerConnection]:
    """When the worker behind `connection` runs different code, stop it and return
    `None` so the caller starts the current one. Otherwise the connection is
    returned as it is. Asked once per process and database."""
    try:
        build = connection.worker_build()
    except WorkerIPCError:
        return connection
    key = str(Path(db_path).resolve())
    if build == build_id() or key in _restart_attempted:
        return connection
    _restart_attempted.add(key)
    tracing.emit("worker restart", 0.0, worker_build=build or "unknown", client_build=build_id())
    from drspec.staleness_worker import stop_worker, wait_for_worker_exit

    try:
        busy = connection.request_restart()
    except Exception:  # noqa: BLE001 - a worker too old to know the request (or one that cannot answer): stopped as before
        busy = None
    if busy:
        # A batch is being published, or a scan is waited for: the worker finishes it, and replaces itself at its next idle
        # moment. This command is answered by it meanwhile (a scan that was running does not end because of the restart).
        tracing.emit("worker restart deferred", 0.0, worker_build=build or "unknown", client_build=build_id())
        return connection
    connection.close()
    if busy is False:  # idle: it exits at once
        exited = wait_for_worker_exit(db_path)
        return None if exited else _reopen(db_path)
    try:
        _pid, exited = stop_worker(db_path)
    except OSError:
        return None if peek_worker_build(db_path) is None else _reopen(db_path)
    return None if exited else _reopen(db_path)


def _reopen(db_path: Path) -> Optional[WorkerConnection]:
    sock = _try_connect(socket_path(db_path), timeout=2.0)
    return WorkerConnection(sock) if sock is not None else None


def get_worker_connection(db_path: Path, read_only: bool = False, check_build: bool = True) -> WorkerConnection:
    """The client-side entry point every routed command uses in place
    of `get_connection()`. `read_only` is accepted for call-site
    compatibility but doesn't change anything here — the worker is the
    sole owner of the one real connection either way, so there's no
    separate read-only mode to request; it's the audit of which
    commands may call this at all that actually enforces "never
    writes," not this parameter.

    Launches the worker (reusing `spawn_worker_if_not_running`) if it's
    not already up, then waits for its socket — never silently falls
    back to a direct connection. Returns a `_DirectConnection` instead,
    with no worker involved at all, when `DRSPEC_DISABLE_AUTO_WORKER`
    is set (see module docstring's "Test mode").

    A worker running code other than this process's (it was started before the
    package was reinstalled) is stopped and replaced, unless `check_build` is
    false (`worker status` reports the difference instead of acting on it).
    """
    if os.environ.get(_DISABLE_ENV_VAR):
        return _DirectConnection(db_path, read_only=read_only)
    del read_only  # not meaningful once the worker owns the connection

    tracing.bind(db_path)
    began = time.perf_counter()
    path = socket_path(db_path)
    sock = _try_connect(path, timeout=2.0)
    if sock is not None:
        connection: Optional[WorkerConnection] = WorkerConnection(sock)
        if check_build:
            connection = _restart_if_outdated(db_path, connection)
        if connection is not None:
            tracing.add("connect_ms", (time.perf_counter() - began) * 1000.0)
            return connection

    from drspec.staleness_worker import spawn_worker_if_not_running

    tracing.add("worker_starts", 1)
    spawn_worker_if_not_running(db_path)
    last_spawn = time.monotonic()

    deadline = last_spawn + CONNECT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        sock = _try_connect(path, timeout=2.0)
        if sock is not None:
            tracing.add("connect_ms", (time.perf_counter() - began) * 1000.0)
            return WorkerConnection(sock)
        time.sleep(_POLL_INTERVAL_SECONDS)
        if time.monotonic() - last_spawn >= _RESPAWN_INTERVAL_SECONDS:
            spawn_worker_if_not_running(db_path)
            last_spawn = time.monotonic()

    from drspec.staleness_worker import read_startup_error

    reason = read_startup_error(db_path)
    raise WorkerIPCError(
        f"Could not reach the drspec worker for {db_path} after waking it "
        f"(waited {CONNECT_TIMEOUT_SECONDS:.0f}s). "
        + (f"It refused to start: {reason}" if reason else "Try 'drspec worker status', or 'drspec worker run' to see why it isn't starting.")
    )


# ---------------------------------------------------------------------------
# Server side
# ---------------------------------------------------------------------------


_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
_WRITING_WORD = re.compile(r"\b(insert|update|delete|create|drop|alter|copy|nextval|pragma|call|attach|install|load|checkpoint|set)\b")


#: What a client writes while a publish runs and nobody needs to see before it ends: how much a result was used
#: (`usage_stats`), a file noticed as changed (`staleness_queue`: the detection finds it again if it is lost), a setting (`config`).
#: It does not hold the client up: it is kept in memory and written, in the order it came, before the publish gives up the lock.
_DEFERRABLE_WRITE = re.compile(r"^\s*insert\s+into\s+(usage_stats|staleness_queue|config)\b", re.IGNORECASE)

#: A report that reads the file and nothing else (the size `status` shows), answered like a read.
_READ_ONLY_PRAGMA = re.compile(r"^\s*pragma\s+database_size\s*;?\s*$", re.IGNORECASE)


def is_deferrable_write(sql: str) -> bool:
    """An insert that can wait for the publish in progress: one of the tables above, and no `RETURNING` (its answer is the rows)."""
    return _DEFERRABLE_WRITE.match(sql) is not None and "returning" not in sql.lower()


def is_plain_read(sql: str) -> bool:
    """A statement that only reads: a SELECT (or a WITH that leads to one) with nothing in it that writes or
    advances a sequence, or the size report of the file. Such a statement can be answered from the last committed
    state while a publish runs."""
    if _READ_ONLY_PRAGMA.match(sql):
        return True
    lowered = sql.lstrip().lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        return False
    return _WRITING_WORD.search(_STRING_LITERAL.sub("''", lowered)) is None  # (a label such as 'call' in the text is not a call)


class _Session:
    """Per-client state, confined to that client's own handler thread —
    only `in_transaction` needs to be known by this thread across
    multiple requests."""

    __slots__ = ("in_transaction", "internal")

    def __init__(self) -> None:
        self.in_transaction = False
        # The worker's own loopback and its embedding child say so on
        # connect; their requests are not client activity and must not wake it.
        self.internal = False


class _ReadGate:
    """Many reads at once, and one swap of the connection that waits for them and keeps new ones out for its moment."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._readers = 0
        self._swapping = False

    @contextmanager
    def read(self) -> Iterator[None]:
        with self._condition:
            while self._swapping:
                self._condition.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._condition:
                self._readers -= 1
                self._condition.notify_all()

    @contextmanager
    def swap(self) -> Iterator[None]:
        with self._condition:
            self._swapping = True
            while self._readers:
                self._condition.wait()
        try:
            yield
        finally:
            with self._condition:
                self._swapping = False
                self._condition.notify_all()


class WorkerIPCServer:
    """Owns the worker's one real connection and a lock serializing
    every use of it. Each accepted client gets its own handler thread,
    looping on that one client's own newline-delimited requests until
    it disconnects or sends `close`.
    """

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        db_path: Path,
        remediate_fn,
    ) -> None:
        self._conn = conn
        self._lock = threading.Lock()
        self._db_path = db_path
        self._remediate_fn = remediate_fn
        self._path = socket_path(db_path)
        self._listener: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        #: Set by any request from an external client, so a dormant worker
        #: runs a pass within seconds instead of waiting out its sleep.
        self.wake = threading.Event()
        #: Set whenever a scan or re-scan publishes, which changes what may
        #: need embedding.
        self.published = threading.Event()
        #: Set while a publish is building the next state. A plain read is then answered from the last
        #: committed state on a cursor of its own, without waiting for the publish to finish.
        self._publishing = threading.Event()
        #: Set by a client whose build differs from this worker's (`restart_when_idle`): the worker replaces itself when no batch is
        #: being published and no scan is waited for, not before.
        self.restart_requested = threading.Event()
        self._deferred: list = []
        self._deferred_calls: list = []  # what a client asked for while a batch was being read, done when it ends
        self._deferred_lock = threading.Lock()
        self._read_gate = _ReadGate()

    @contextmanager
    def _timed_lock(self) -> Iterator[None]:
        """Hold the connection lock, noting how long the request waited for it."""
        waited = time.perf_counter()
        self._lock.acquire()
        tracing.note(lock_ms=(time.perf_counter() - waited) * 1000.0)
        try:
            yield
        finally:
            self._lock.release()

    def start(self) -> None:
        if self._path.exists():
            try:
                self._path.unlink()
            except OSError:
                pass
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self._path))
        listener.listen(16)
        listener.settimeout(0.5)  # so the accept loop can notice `stop()`
        self._listener = listener
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._listener is not None:
            try:
                self._listener.shutdown(socket.SHUT_RDWR)  # wakes the accept loop now, not at its next 0.5 s timeout
            except OSError:
                pass
            try:
                self._listener.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        with self._lock:
            try:
                self._conn.close()
            except duckdb.Error:
                pass

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                client_sock, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_client, args=(client_sock,), daemon=True).start()

    def _handle_client(self, client_sock: socket.socket) -> None:
        session = _Session()
        buffer = bytearray()
        try:
            while True:
                line = _read_line(client_sock, buffer)
                if line is None:
                    break
                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    _send_message(client_sock, {"ok": False, "error": "invalid JSON request", "error_type": ""})
                    continue

                response = self._handle_request(session, request)
                try:
                    _send_message(client_sock, response)
                except (TypeError, ValueError) as e:
                    # `json.dumps` fails before anything is written, so an
                    # unencodable value still gets a clean answer naming
                    # its type instead of ending this client's session.
                    _send_message(
                        client_sock,
                        {"ok": False, "error": f"The worker could not serialize its response: {e}", "error_type": type(e).__name__},
                    )

                if request.get("op") == "close":
                    break
        except OSError:
            pass
        finally:
            if session.in_transaction:
                try:
                    self._conn.rollback()
                except duckdb.Error:
                    pass
                session.in_transaction = False
                self._lock.release()
            try:
                client_sock.close()
            except OSError:
                pass

    def _handle_request(self, session: _Session, request: dict) -> dict:
        if not tracing.enabled():
            response = self._dispatch(session, request)
            report = tracing.worker_report()  # this request may have just turned tracing on
            if report is not None:
                response["trace"] = report
            return response
        # The client's correlation id lets one command's whole path be put back together.
        tracing.set_cid(request.get("cid"))
        tracing.begin_notes()
        started = time.perf_counter()
        response: dict = {}
        try:
            response = self._dispatch(session, request)
            return response
        finally:
            total = (time.perf_counter() - started) * 1000.0
            notes = tracing.end_notes()
            lock_ms = float(notes.pop("lock_ms", 0.0))
            op = request.get("op")
            if op != "hello":
                tracing.emit(
                    "request",
                    total,
                    op=op,
                    sql=tracing.sql_shape(request["sql"]) if op == "execute" and isinstance(request.get("sql"), str) else None,
                    lock_ms=round(lock_ms, 3),
                    run_ms=round(max(0.0, total - lock_ms), 3),
                    ok=bool(response.get("ok", False)),
                    **notes,
                )
            report = tracing.worker_report()
            if report is not None and response:
                response["trace"] = report
            tracing.set_cid(None)

    def _dispatch(self, session: _Session, request: dict) -> dict:
        op = request.get("op")
        if op == "hello":
            session.internal = bool(request.get("internal"))
            return {"ok": True, "build": build_id()}
        if not session.internal:
            self.wake.set()
        try:
            if op == "execute":
                return self._op_execute(session, request)
            if op == "begin":
                return self._op_begin(session)
            if op == "commit":
                return self._op_commit(session)
            if op == "rollback":
                return self._op_rollback(session)
            if op == "close":
                return {"ok": True}
            if op == "scan_start":
                return self._op_scan("start", request)
            if op in ("scan_progress", "scan_cancel", "scan_detach"):
                return self._op_scan(op[len("scan_"):], request)
            if op == "restart_when_idle":
                return self._op_restart_when_idle()
            if op == "remediate":
                return self._op_remediate(request)
            if op == "store_embeddings":
                return self._op_store_embeddings(request)
            return {"ok": False, "error": f"Unknown operation: {op!r}", "error_type": ""}
        except duckdb.Error as e:
            return {"ok": False, "error": str(e), "error_type": type(e).__name__}
        except Exception as e:  # noqa: BLE001 - must always answer the client, never crash the worker
            return {"ok": False, "error": str(e), "error_type": type(e).__name__}

    def _op_execute(self, session: _Session, request: dict) -> dict:
        sql = request["sql"]
        params = list(decode_rows([request.get("params") or []])[0])
        if session.in_transaction:
            rows = self._conn.execute(sql, params).fetchall()
            self._apply_trace_setting_change(sql, params)
        elif is_plain_read(sql) and (self._publishing.is_set() or self._lock.locked()):
            # (the lock is held by a publish, a scan being started, or another client's open transaction: the answer is the last
            # committed state, and does not wait for any of them)
            rows = self._read_committed(sql, params)
            tracing.note(read_during_publish=1)
        elif self._publishing.is_set() and is_deferrable_write(sql) and self._defer_write(sql, params):
            rows = []
            tracing.note(deferred_write=1)
        else:
            with self._timed_lock():
                rows = self._conn.execute(sql, params).fetchall()
                self._apply_trace_setting_change(sql, params)
        tracing.note(rows=len(rows))
        return {"ok": True, "rows": encode_rows(rows)}

    def _defer_write(self, sql: str, params: list) -> bool:
        """Keep a write for the end of the publish in progress. False when the publish ended meanwhile (it is then written now)."""
        with self._deferred_lock:  # (the publish clears its flag and takes what is held under this same lock)
            if not self._publishing.is_set():
                return False
            self._deferred.append((sql, params))
            return True

    def _read_committed(self, sql: str, params: list) -> list:
        """Run a read on a cursor of its own: it sees what was committed before the publish in progress, and
        neither waits for the publish nor lets it see a half-written state."""
        with self._read_gate.read():
            started = time.perf_counter()
            cursor = self._conn.cursor()
            opened = time.perf_counter()
            try:
                result = cursor.execute(sql, params)
                executed = time.perf_counter()
                rows = result.fetchall()
                # Where the time of a read that did not wait for the lock went (a slow one was seen only under load, while a scan
                # was being started): the cursor, the statement or the rows. `lock_ms` of such a request is 0 by design.
                tracing.note(cursor_ms=round((opened - started) * 1000.0, 1), exec_ms=round((executed - opened) * 1000.0, 1), fetch_ms=round((time.perf_counter() - executed) * 1000.0, 1))
                return rows
            finally:
                cursor.close()

    def compact_if_needed(self, free_share: float, min_bytes: int) -> str:
        """Give back the free blocks of the database file while the worker runs: `compact` (nothing to do), `compacted`
        or `failed` (the file and the connection are as they were).

        The write lock is held for the whole rebuild, so a write waits for it, while plain reads are answered from the
        last committed state on cursors of their own (as during a publish). The new file is made from a checkpointed
        copy; the swap (close, rename, reopen) is the only moment a read waits, and it is short."""
        import os
        import shutil
        import tempfile

        from drspec.db.compaction import build_replacement, needs_compaction
        from drspec.db.staging import STAGING_PREFIX

        with self._timed_lock(), self._publishing_state():
            # What was written lives in the write-ahead log until a checkpoint (DuckDB makes one by itself only when the
            # log passes about 16 MB), and the file does not show its free blocks before that, so the question is asked
            # after one. It is cheap when little is waiting.
            try:
                self._conn.execute("CHECKPOINT")
            except Exception:  # noqa: BLE001 - the file is as it was
                logger.warning("Could not checkpoint the database to see its free space", exc_info=True)
                return "failed"
            if not needs_compaction(self._conn, free_share, min_bytes):
                return "compact"
            work = Path(tempfile.mkdtemp(prefix=f"{STAGING_PREFIX}{os.getpid()}-", dir=str(self._db_path.parent)))
            try:
                try:
                    replacement = build_replacement(self._db_path, work)
                except Exception:  # noqa: BLE001 - whatever went wrong, the file is as it was
                    logger.warning("Could not compact the database; it is left as it is", exc_info=True)
                    return "failed"
                with self._read_gate.swap():
                    self._conn.close()
                    try:
                        os.replace(replacement, self._db_path)
                        wal = Path(str(self._db_path) + ".wal")
                        if wal.exists():
                            wal.unlink()
                    finally:
                        self._conn = duckdb.connect(str(self._db_path))
                return "compacted"
            finally:
                shutil.rmtree(work, ignore_errors=True)

    @contextmanager
    def _publishing_state(self) -> Iterator[None]:
        """While a publish builds the next state: plain reads are answered from the last committed one, and
        usage bookkeeping is held back. Called with the connection lock held; what was held back is written
        before the lock is given up."""
        self._publishing.set()
        try:
            yield
        finally:
            from drspec.core import scan_service

            scan_service.end_batch()  # (a batch that failed before it began to publish leaves its progress behind otherwise)
            with self._deferred_lock:  # (cleared under the lock, so a call is either queued before this or runs at once)
                self._publishing.clear()
                held, self._deferred = self._deferred, []
                calls, self._deferred_calls = self._deferred_calls, []
            for sql, params in held:
                try:
                    self._conn.execute(sql, params)
                    self._apply_trace_setting_change(sql, params)
                except duckdb.Error:
                    pass  # a usage count lost is not worth failing the publish (a file noticed is found again by the next pass)
            for call in calls:
                try:
                    call()
                except Exception:  # noqa: BLE001 - a cancel that fails leaves its files queued, which the next scan finds
                    logger.warning("A request made while a batch was being read could not be carried out", exc_info=True)

    def _apply_trace_setting_change(self, sql: str, params: list) -> None:
        """When a `trace.*` setting was just written (or reset), apply it now. Called
        with the connection lock held."""
        if sql.lstrip()[:6].upper() not in ("INSERT", "UPDATE", "DELETE"):
            return
        if not any(isinstance(value, str) and value.startswith("setting:trace.") for value in params):
            return
        try:
            from drspec.core.settings import read_all_settings

            tracing.apply_settings_now(read_all_settings(self._conn))
        except Exception:  # noqa: BLE001 - a settings problem must not fail the write
            pass

    def _op_begin(self, session: _Session) -> dict:
        waited = time.perf_counter()
        self._lock.acquire()
        tracing.note(lock_ms=(time.perf_counter() - waited) * 1000.0)
        try:
            self._conn.begin()
        except duckdb.Error:
            self._lock.release()
            raise
        session.in_transaction = True
        return {"ok": True}

    def _op_commit(self, session: _Session) -> dict:
        # A bare `conn.commit()` with no open explicit transaction is a
        # safe no-op on a real `duckdb` connection (every statement
        # already autocommitted) — several existing helpers
        # (`_fk_references_detached`) call it unconditionally this way,
        # working whether called standalone or nested. `WorkerConnection`
        # must match that, not error, or every write path using one of
        # those helpers breaks the moment it's routed through the worker.
        if not session.in_transaction:
            return {"ok": True}
        try:
            self._conn.commit()
        finally:
            session.in_transaction = False
            self._lock.release()
        return {"ok": True}

    def _op_rollback(self, session: _Session) -> dict:
        if not session.in_transaction:
            # Matches real `duckdb`'s own behavior exactly — unlike
            # `commit()`, a bare `rollback()` with nothing open
            # genuinely raises, not a no-op.
            return {
                "ok": False,
                "error": "TransactionContext Error: cannot rollback - no transaction is active",
                "error_type": "TransactionException",
            }
        try:
            self._conn.rollback()
        finally:
            session.in_transaction = False
            self._lock.release()
        return {"ok": True}

    def _op_scan(self, what: str, request: dict) -> dict:
        """Start, watch, cancel or let go of a scan (`core.scan_service`). Queuing a scan's files does not publish
        anything; the worker's loop reads them."""
        from drspec.core import scan_service

        if what == "progress" and self._publishing.is_set():  # a read: the answer need not wait for the publish
            cursor = self._conn.cursor()
            try:
                return {"ok": True, "result": scan_service.progress(cursor, request["scan_id"])}
            finally:
                cursor.close()
        if what in ("cancel", "detach"):
            # A batch holds the lock for as long as it is read and published (tens of seconds for a first scan): the
            # answer does not wait for it. The cancel is known to the batch at once (it stops reading that scan's
            # files), and the queue is cleaned up when the batch ends.
            scan_id = request["scan_id"]
            with self._deferred_lock:
                if self._publishing.is_set():
                    cursor = self._conn.cursor()
                    try:
                        left = scan_service.remaining(cursor, scan_id)
                        run = scan_service.load_run(cursor, scan_id) or {}
                    finally:
                        cursor.close()
                    if what == "cancel":
                        in_time = scan_service.mark_cancelling(scan_id)
                        self._deferred_calls.append(lambda: scan_service.cancel(self._conn, scan_id))
                        # In time (the batch is still reading), the files left are the ones cancelled. Too late (it is
                        # publishing), the batch is published whole: its files will be done, and the client waits to
                        # see how many. `done` lets it say which were done by that batch.
                        return {
                            "ok": True,
                            "result": {"scan_id": scan_id, "cancelled": left, "done": run.get("done", 0), "deferred": True, "publishing": not in_time},
                        }
                    self._deferred_calls.append(lambda: scan_service.detach(self._conn, scan_id))
                    return {"ok": True, "result": {"scan_id": scan_id, "remaining": left, "deferred": True}}
        if what == "start":
            # Listing the project, finding what changed and queuing it holds the lock for seconds on a large project (7 s for
            # django): it is held like a publish, so a read, the progress, a cancel and the small writes do not wait for it.
            with self._timed_lock(), self._publishing_state():
                result = scan_service.start_scan(self._conn, self._db_path, request["spec"])
            return {"ok": True, "result": result}
        with self._timed_lock():
            if what == "progress":
                result = scan_service.progress(self._conn, request["scan_id"])
            elif what == "cancel":
                result = scan_service.cancel(self._conn, request["scan_id"])
            else:
                result = scan_service.detach(self._conn, request["scan_id"])
        return {"ok": True, "result": result}

    def _op_restart_when_idle(self) -> dict:
        """A client of another build asks this worker to replace itself. It does so at its next idle moment: a batch that is being
        published is finished and published first, and so is a scan somebody waits for (all its batches). The answer says
        whether it is busy now (then the client goes on with this worker) or will exit at once."""
        from drspec.core import scan_service

        self.restart_requested.set()
        self.wake.set()
        busy = self._publishing.is_set() or self._lock.locked()
        if not busy:
            cursor = self._conn.cursor()
            try:
                busy = scan_service.scanning_now(cursor)
            except Exception:  # noqa: BLE001 - a failed question is "not busy": the loop looks again before it exits
                busy = False
            finally:
                cursor.close()
        return {"ok": True, "busy": bool(busy)}

    def _op_remediate(self, request: dict) -> dict:
        with self._timed_lock(), self._publishing_state():
            extra = (request["pass_seconds"],) if request.get("pass_seconds") is not None else ()
            remediated = self._remediate_fn(self._conn, self._db_path, request["file_paths"], *extra)
        if remediated:
            self.published.set()
        return {"ok": True, "remediated": remediated}

    def _op_store_embeddings(self, request: dict) -> dict:
        from drspec.core.embedding_backlog import Item, _store, unreadable

        results = [(Item.from_wire(entry["item"]), entry["vector"]) for entry in request["results"]]

        def store_now() -> tuple:
            written = _store(self._conn, results)
            return written, unreadable(self._conn, written)

        if self._publishing.is_set():
            with self._deferred_lock:
                if self._publishing.is_set():
                    # stored when the publish ends, before it gives up the lock: what it replaced meanwhile is found unreadable then
                    # and left to the next round; the client is told all was taken (the vectors are its to forget now)
                    self._deferred_calls.append(store_now)
                    tracing.note(items=len(results), deferred_embeddings=len(results))
                    return {"ok": True, "stored": len(results), "unpersisted": []}
        with self._timed_lock():
            written, lost = store_now()
        tracing.note(items=len(results), stored=len(written) - len(lost), unpersisted=len(lost))
        return {"ok": True, "stored": len(written) - len(lost), "unpersisted": [item.as_wire() for item in lost]}
