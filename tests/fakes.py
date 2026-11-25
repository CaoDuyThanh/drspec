"""Fakes shared by the tests, so that no test runs a real external tool (git above all) or starts a process.

- `git_listing`: the file listing `git ls-files` would print for a folder, given to the code that reads it.
- `FakeGit`: a git repository as data (commits with the files they hold) that answers the few `git` commands drspec
  runs (`log`, `diff`, `show`, `rev-parse`), with the same output git prints. A test installs it in the module that
  shells out; nothing is started.
- `InProcessPython`: stands in for `subprocess` in `drspec.contracts.executor` and `drspec.debugging.runtime`: the verification script they write is
  run in the test's own process (same stdin, same JSON on stdout), so the generated verifier really runs and no process starts.
- `KeptDuckDB`: stands in for `duckdb.connect` on a file: the same path gives the same in-memory database every time it is
  opened and `close()` keeps it, so a series of CLI commands sees the state the one before left, with no file, no checkpoint.
- `unreadable`: a file whose reading fails with `PermissionError` for whoever runs the test (a file mode does not stop root), and
  `not_writable`: a folder `os.access` says cannot be written to, so a test of "cannot read/write" never depends on the user.
- `FakeEmbeddingModel`: what `embedder._model` is, without weights or network: `embed([text])` gives a 384-float unit vector made from the
  words of the text (hashed bag of words, trailing "s" dropped), so two texts that share words are close and the answer never changes.
- `Counting`: a connection that counts the statements run through it and their shapes (what a test of "how many statements" wraps around one).
- `FakeHttpServer`: a `BaseHTTPRequestHandler` class driven without a socket: `get(path)` feeds it the bytes of a GET request and returns the status,
  headers and body it wrote (the attributes the handler reads from its server are given to the fake).
- `FakeTime`: a clock that moves only when something sleeps (the `fake_time` fixture of `conftest.py` makes one).
- `fake_socket_pair()` and `FakeSocket`: two connected in-memory sockets; `Served`: the clients of a `WorkerIPCServer` that is
  never started, over such pairs (the `serve` fixture of `conftest.py` makes one).
"""

from __future__ import annotations

import difflib
import queue
import subprocess
import threading
import time as _real_time
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Iterable, List, Optional


def git_listing(monkeypatch, root: Path, ignored: Iterable[str] = (), also_listed: Iterable[str] = ()) -> None:
    """Make `project_files` read the listing git would print for `root`: the files on disk that are not ignored (git
    lists tracked and untracked files alike), plus `also_listed` (a tracked file deleted from disk is still listed),
    and the ignored files on `--ignored`. drspec reads what git prints and does not match `.gitignore` patterns, so
    the listing is given directly and no `git` runs."""
    from drspec.core import project_files

    ignored = set(ignored)
    also_listed = set(also_listed)

    def listing(folder, *arguments):
        if arguments[:2] == ("rev-parse", "--is-inside-work-tree"):
            return ["true"]
        if not arguments or arguments[0] != "ls-files":
            return None
        on_disk = sorted(p.relative_to(root).as_posix() for p in Path(root).rglob("*") if p.is_file())
        specs = [a[len(":(literal)"):] for a in arguments if a.startswith(":(literal)")]
        if "--ignored" in arguments:
            names = [name for name in on_disk if name in ignored]
        else:
            names = [name for name in sorted(set(on_disk) | also_listed) if name not in ignored]
        return [n for n in names if not specs or any(n == s or n.startswith(s.rstrip("/") + "/") for s in specs)]

    monkeypatch.setattr(project_files, "_git_lines", listing)


class FakeGit:
    """A repository as data. `commit({...files...})` adds a commit holding exactly those files and returns its sha."""

    def __init__(self) -> None:
        self.commits: List[Dict] = []

    def commit(self, files: Dict[str, str], message: str = "commit", author: str = "Test", email: str = "t@example.com",
               date: str = "2026-01-01T00:00:00+00:00") -> str:
        sha = f"{len(self.commits) + 1:040x}"
        self.commits.append({"sha": sha, "files": dict(files), "message": message, "author": author, "email": email, "date": date})
        return sha

    def install(self, monkeypatch, *modules) -> "FakeGit":
        """Replace `subprocess` in each module (the ones that shell out to git) with this fake."""
        fake = SimpleNamespace(run=self.run, CalledProcessError=subprocess.CalledProcessError, PIPE=subprocess.PIPE)
        for module in modules:
            monkeypatch.setattr(module, "subprocess", fake)
        return self

    # -- refs ------------------------------------------------------------------------------------------------

    def _index(self, ref: str) -> Optional[int]:
        """The position of a commit named by `HEAD`, `HEAD~n`, a sha, or any of them followed by `^`."""
        if ref.endswith("^"):
            inner = self._index(ref[:-1])
            return inner - 1 if inner is not None and inner > 0 else None
        if ref == "HEAD":
            return len(self.commits) - 1 if self.commits else None
        if ref.startswith("HEAD~"):
            index = len(self.commits) - 1 - int(ref[5:])
            return index if index >= 0 else None
        for position, commit in enumerate(self.commits):
            if commit["sha"] == ref:
                return position
        return None

    def _fail(self, args, message: str = "fatal: bad revision"):
        raise subprocess.CalledProcessError(128, args, output="", stderr=message)

    @staticmethod
    def _done(args, stdout: str):
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    # -- the commands ----------------------------------------------------------------------------------------

    def run(self, args, **kwargs):
        command = list(args)
        if command[:1] != ["git"]:  # pattern-ok: git the fake refuses everything but git: it is the fake
            raise AssertionError(f"FakeGit only answers git, got {command[:1]}")
        if command[1] == "-C":
            command = command[3:]
        else:
            command = command[1:]
        name, rest = command[0], command[1:]
        if name == "rev-parse":
            return self._done(args, ".git\n")
        if name == "log" and "-1" in rest:
            index = self._index(rest[-1])
            if index is None:
                self._fail(args)
            c = self.commits[index]
            return self._done(args, f"{c['sha']}\n{c['author']}\n{c['email']}\n{c['date']}\n{c['message']}\n")
        if name == "log":
            start, _, end = rest[-1].partition("..")
            first, last = self._index(start), self._index(end)
            if last is None:
                self._fail(args)
            begin = (first + 1) if first is not None else 0
            shas = [self.commits[i]["sha"] for i in range(last, begin - 1, -1)]
            return self._done(args, "\n".join(shas) + ("\n" if shas else ""))
        if name == "diff":
            old_ref, _, new_ref = rest[-1].partition("..")
            old, new = self._index(old_ref), self._index(new_ref)
            if new is None:
                self._fail(args)
            return self._done(args, self._diff(self.commits[old]["files"] if old is not None else {}, self.commits[new]["files"]))
        if name == "show":
            ref, _, path = rest[-1].partition(":")
            index = self._index(ref)
            if index is None or path not in self.commits[index]["files"]:
                self._fail(args, "fatal: path does not exist")
            return self._done(args, self.commits[index]["files"][path])
        raise AssertionError(f"FakeGit does not answer: git {' '.join(command)}")

    @staticmethod
    def _diff(old_files: Dict[str, str], new_files: Dict[str, str]) -> str:
        out: List[str] = []
        for path in sorted(set(old_files) | set(new_files)):
            old, new = old_files.get(path), new_files.get(path)
            if old == new:
                continue
            out.append(f"diff --git a/{path} b/{path}")
            out.append("index 0000000..0000000 100644")
            lines = difflib.unified_diff((old or "").splitlines(), (new or "").splitlines(), f"a/{path}", f"b/{path}", n=3, lineterm="")
            out.extend(lines)
        return "\n".join(out) + ("\n" if out else "")


class FakeTime:
    """A stand-in for the `time` module as a module under test uses it: the clock moves only when something sleeps.

    Replace the module's `time` with one (`monkeypatch.setattr(module, "time", FakeTime())`): `monotonic()` and `sleep()` are
    fake, and everything else (the wall clock and the performance counter) is the real one. `slept` lists what was slept, in order."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.slept: List[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def __getattr__(self, name: str):
        return getattr(_real_time, name)


class FakeSocket:
    """One end of an in-memory duplex stream, with the methods the IPC code calls on a socket (`recv`, `sendall`, `close`,
    `settimeout`). No file, no operating-system socket: what one end sends the other receives, and closing one end is an
    end of stream (`recv` returns `b""`) for the other. Make a pair with `fake_socket_pair()`."""

    def __init__(self) -> None:
        self._inbox: queue.Queue = queue.Queue()  # chunks of bytes; None is the end of the stream
        self._rest = b""
        self._peer: Optional["FakeSocket"] = None
        self.closed = False
        self.timeout: Optional[float] = None
        self.times_out_when_empty = False  # set it: a `recv` that would have to wait while a timeout is set raises `socket.timeout` at once

    def recv(self, size: int = 65536) -> bytes:
        if not self._rest:
            if self.times_out_when_empty and self.timeout is not None and self._inbox.empty():
                import socket as _socket

                raise _socket.timeout("timed out")
            chunk = self._inbox.get()  # waits until the other end sends or closes
            if chunk is None:
                self._inbox.put(None)  # the end stays the end
                return b""
            self._rest = chunk
        data, self._rest = self._rest[:size], self._rest[size:]
        return data

    def sendall(self, data: bytes) -> None:
        if self.closed or self._peer is None or self._peer.closed:
            raise BrokenPipeError("the other end is closed")
        self._peer._inbox.put(bytes(data))

    def settimeout(self, seconds: Optional[float]) -> None:
        self.timeout = seconds

    def gettimeout(self) -> Optional[float]:
        return self.timeout

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._inbox.put(None)  # wakes a thread of this end that is waiting in `recv`
        if self._peer is not None:
            self._peer._inbox.put(None)


def fake_socket_pair() -> "tuple[FakeSocket, FakeSocket]":
    """Two connected `FakeSocket`s."""
    left, right = FakeSocket(), FakeSocket()
    left._peer, right._peer = right, left
    return left, right


class Served:
    """The clients of one `WorkerIPCServer` that is never started: each `client()` is a `WorkerConnection` over its own
    `fake_socket_pair()`, with a handler thread of the server behind the other end. No socket file, no accept loop, so nothing
    to wait for at the end. `finish()` closes every end and joins the threads (the `serve` fixture does it)."""

    def __init__(self, server) -> None:
        self.server = server
        self.threads: List[threading.Thread] = []
        self.ends: List[FakeSocket] = []

    def client(self):
        from drspec.worker_ipc import WorkerConnection

        client_end, server_end = fake_socket_pair()
        thread = threading.Thread(target=self.server._handle_client, args=(server_end,), daemon=True)
        thread.start()
        self.threads.append(thread)
        self.ends += [client_end, server_end]
        return WorkerConnection(client_end)

    def finish(self) -> None:
        for end in self.ends:
            end.close()
        for thread in self.threads:
            thread.join(5)


class InProcessPython:
    """What `subprocess` is for the code that runs a verification script (`contracts.executor`, `debugging.runtime`), without a process: `run([python, script_path], input=...)` runs the
    script file in this interpreter as `__main__`, with `input` as its stdin, and returns what it printed. A script that raises
    is a non-zero exit with the traceback on stderr, as it is in a real process. Use `install(monkeypatch)` (the
    `in_process_python` fixture of `conftest.py` does)."""

    def __init__(self) -> None:
        self.ran: List[str] = []  # the script files that were run, in order
        self.times_out = False  # set it: the next `run` raises `subprocess.TimeoutExpired`, as a real script that runs too long does

    def run(self, args, input: str = "", **kwargs):
        import contextlib
        import io
        import sys
        import traceback

        script_path = str(args[-1])
        self.ran.append(script_path)
        if self.times_out:
            raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))
        source = Path(script_path).read_text(encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        code = 0
        # what a script can change in this interpreter, put back whatever it does: a process of its own had its own copy
        saved_argv, saved_stdin, saved_modules, saved_path = sys.argv, sys.stdin, dict(sys.modules), list(sys.path)
        saved_cwd = __import__("os").getcwd()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            sys.argv = [script_path]
            sys.stdin = io.StringIO(input)
            try:
                exec(compile(source, script_path, "exec"), {"__name__": "__main__"})
            except SystemExit as stop:
                code = stop.code if isinstance(stop.code, int) else (0 if stop.code is None else 1)
            except BaseException:  # noqa: BLE001 - whatever the script raises is its exit status, not the test's failure
                traceback.print_exc(file=err)
                code = 1
            finally:
                sys.argv, sys.stdin, sys.path[:] = saved_argv, saved_stdin, saved_path
                for name in set(sys.modules) - set(saved_modules):
                    del sys.modules[name]
                sys.modules.update(saved_modules)
                __import__("os").chdir(saved_cwd)
        return subprocess.CompletedProcess(args, code, stdout=out.getvalue(), stderr=err.getvalue())

    def install(self, monkeypatch) -> "InProcessPython":
        from drspec.contracts import executor
        from drspec.debugging import runtime

        fake = SimpleNamespace(run=self.run, TimeoutExpired=subprocess.TimeoutExpired)
        for module in (executor, runtime):  # the two places that run a verification script
            monkeypatch.setattr(module, "subprocess", fake)
        return self


class _Rows:
    """A result with the one method callers use here."""

    def __init__(self, rows) -> None:
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Kept:
    """A connection to a database that `KeptDuckDB` keeps: everything is the real in-memory connection's, but `close()` leaves it open
    (the next `connect` of the same path gets the same data)."""

    def __init__(self, connection, path: str = "") -> None:
        self._connection = connection
        self._path = path

    def execute(self, sql, *args, **kwargs):
        if self._path and str(sql).strip().rstrip(";").lower() == "pragma database_list":
            # what a database FILE answers (the product finds the project folder from it): the memory database has no file, so the path of the
            # one it stands for is given
            return _Rows([(0, "main", self._path)])
        return self._connection.execute(sql, *args, **kwargs)

    def close(self) -> None:
        try:
            self._connection.rollback()  # what closing a real connection does to a transaction nobody finished
        except Exception:  # noqa: BLE001 - nothing was open
            pass

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


class KeptDuckDB:
    """What `duckdb.connect(path)` is, without a file: one in-memory database per path, kept between `connect` calls. Use it for a test whose  # pattern-ok: duckdb-file the fake's own docstring/real connect
    objective is what a series of commands does to the data, not that the data reaches a disk file (a file that survives a close and an
    open, a second process, a read-only reader beside a writer need the real thing). An empty marker file is created at the path, as the
    commands check that the database exists; `read_only` is not enforced, but a read-only open of a path never opened fails as it would.
    `PRAGMA database_list` is answered with the path of the database, as a file would (the product finds the project folder from it, to read
    `tsconfig.json`, `package.json`, `Cargo.toml`).
    `install(monkeypatch)` puts it in place (the `kept_duckdb` fixture of `conftest.py` does)."""

    def __init__(self) -> None:
        import duckdb

        self.databases: Dict[str, object] = {}
        self._real_connect = duckdb.connect  # taken now, so a recorder that wrapped it is still the one that is called

    def connect(self, database=":memory:", read_only: bool = False, *args, **kwargs):
        import duckdb

        if str(database) == ":memory:":
            return self._real_connect(":memory:", *args, **kwargs)
        path = Path(database)
        key = str(path.resolve())
        if key not in self.databases:
            if read_only:
                raise duckdb.IOException(f"Cannot open database \"{database}\" in read-only mode: database does not exist")
            self.databases[key] = self._real_connect(":memory:")
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        return _Kept(self.databases[key], key)

    def install(self, monkeypatch) -> "KeptDuckDB":
        import duckdb

        monkeypatch.setattr(duckdb, "connect", self.connect)
        return self


def unreadable(monkeypatch, *paths) -> None:  # pattern-ok: local-copy-of-product-code same name as `core.embedding_backlog.unreadable` by chance: this one makes files unreadable for a test
    """Reading any of `paths` fails with `PermissionError`, through `open`, `Path.read_text` and `Path.read_bytes`, whatever user runs the
    test (`chmod 0` does nothing to root). Everything else is read as usual."""
    import builtins
    import io

    refused = {str(Path(p).resolve()) for p in paths}
    real_open, real_text, real_bytes = builtins.open, Path.read_text, Path.read_bytes

    def check(target):
        try:
            if isinstance(target, (str, bytes, Path)) or hasattr(target, "__fspath__"):
                import os

                name = os.fsdecode(os.fspath(target))
                if str(Path(name).resolve()) in refused:
                    raise PermissionError(13, "Permission denied", name)
        except TypeError:
            pass

    def fake_open(file, *args, **kwargs):
        check(file)
        return real_open(file, *args, **kwargs)

    def fake_text(self, *args, **kwargs):
        check(self)
        return real_text(self, *args, **kwargs)

    def fake_bytes(self, *args, **kwargs):
        check(self)
        return real_bytes(self, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.setattr(io, "open", fake_open)
    monkeypatch.setattr(Path, "read_text", fake_text)
    monkeypatch.setattr(Path, "read_bytes", fake_bytes)


def not_writable(monkeypatch, *folders) -> None:
    """`os.access(folder, W_OK)` says no for these folders, whatever user runs the test (root can write anywhere)."""
    import os

    refused = {str(Path(f)) for f in folders}
    real_access = os.access

    def access(path, mode, *args, **kwargs):
        if str(path) in refused and mode & os.W_OK:
            return False
        return real_access(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "access", access)


class FakeEmbeddingModel:
    """The one thing `drspec.core.embedder` asks of its model (`embed(texts)` yielding vectors with `.tolist()`), made from the words of
    the text: no weights, no network, the same vector every time. Words that two texts share make their vectors close, so a semantic
    search over these vectors still ranks by shared words. `install(monkeypatch)` puts it where the real model would be loaded (the
    `fake_embedder` fixture of `conftest.py` does)."""

    DIM = 384

    class _Vector(list):
        def tolist(self):
            return list(self)

    def embed(self, texts):
        import hashlib
        import math
        import re

        for text in texts:
            vector = [0.0] * self.DIM
            for word in re.findall(r"[a-z0-9]+", str(text).lower()):
                word = word[:-1] if len(word) > 3 and word.endswith("s") else word
                vector[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.DIM] += 1.0
            norm = math.sqrt(sum(v * v for v in vector)) or 1.0
            yield self._Vector(v / norm for v in vector)

    def install(self, monkeypatch) -> "FakeEmbeddingModel":
        from drspec.core import embedder

        monkeypatch.setattr(embedder, "_model", self)  # `_get_model()` returns it: nothing is loaded
        return self


class Counting:
    """A connection that counts the statements run through it, and which shapes they have."""

    def __init__(self, inner):
        import collections

        self.inner = inner
        self.count = 0
        self.shapes: "collections.Counter" = collections.Counter()

    def execute(self, sql, *args, **kwargs):
        import re

        self.count += 1
        self.shapes[re.sub(r"\s+", " ", sql)[:80]] += 1
        return self.inner.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class HttpAnswer:
    """What a handler wrote for one request: `status`, `headers` (lower-case names) and `body` (bytes)."""

    def __init__(self, raw: bytes) -> None:
        head, _, self.body = raw.partition(b"\r\n\r\n")
        lines = head.decode("iso-8859-1").split("\r\n")
        self.status = int(lines[0].split()[1])
        self.headers = {name.lower(): value.strip() for name, _, value in (line.partition(":") for line in lines[1:])}

    def json(self):
        import json

        return json.loads(self.body)


class _HttpConnection:
    """The socket a `StreamRequestHandler` reads its request from and writes its answer to, in memory."""

    def __init__(self, request: bytes) -> None:
        import io

        self._request = io.BytesIO(request)
        self.sent = bytearray()

    def makefile(self, mode: str = "rb", bufsize: int = -1):
        return self._request

    def sendall(self, data) -> None:
        self.sent += bytes(data)

    def settimeout(self, seconds) -> None:
        pass

    def close(self) -> None:
        pass


class FakeHttpServer:
    """`FakeHttpServer(_GraphRequestHandler, db_path=..., initial_depth=2)`: the handler is built for each `get` as the stdlib server builds it, with
    this object as its `server` (so the handler reads `self.server.db_path` and the rest from the keyword arguments). No socket, no thread."""

    def __init__(self, handler_class, **attributes) -> None:
        self.handler_class = handler_class
        self.__dict__.update(attributes)

    def get(self, path: str) -> HttpAnswer:
        connection = _HttpConnection(f"GET {path} HTTP/1.0\r\nHost: test\r\n\r\n".encode())
        self.handler_class(connection, ("127.0.0.1", 0), self)
        return HttpAnswer(bytes(connection.sent))
