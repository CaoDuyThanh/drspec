"""The guard that no test touches anything outside the repository and its own temporary folder, and that none writes into the repository.

The rule (see tests/README.md): a test READS only the repository, its `tmp_path` and the folders of the Python that runs it, and WRITES
only in its own temporary folder (this session's `basetemp`). The repository is read-only for tests: a test that writes, makes a folder,
renames, removes or `chdir`s a file there, or leaves `_drspec` or `.gitignore` in it, fails. No sibling clone of a project, no home folder,
no `/tmp` except through `tmp_path`, no absolute path of the developer's machine, no socket file outside. A test then gives the same
result on any machine and leaves nothing behind.

This is a runtime check of every test, not a search of the text. While a test runs (its setup, its call and its teardown) the
functions that take a path are wrapped; a path that is not allowed for what is done to it is recorded with the call that named it, and
the test FAILS, naming the first path. The access itself is refused (`OutsideAccess`, a `PermissionError`), so nothing is read from the
real machine and nothing is left outside; code that catches an `OSError` sees what it would see on a machine without the folder.
A test that needs a real thing outside says so with `@pytest.mark.outside(reason=...)`: it is not failed and not refused, and the
end of the run lists it with its reason (there should be none).

Wrapped (R: read, W: write; a call that opens a file is W when its mode or flags write):
  open: `builtins.open`, `io.open`, `os.open`, `Path.open`
  look and read (R): `os.listdir`, `os.scandir`, `os.stat`, `os.lstat`, `os.access`, `os.readlink`, `os.chdir` (so `os.walk` and
    `os.path.exists/isfile/isdir/islink`, which reach `os.stat` and `os.lstat`), `Path.read_text`, `read_bytes`, `iterdir`, `glob`, `rglob`,
    `exists`, `is_file`, `is_dir`, `stat`, `lstat`, `readlink`, `samefile`
  change (W): `os.mkdir`, `os.makedirs` (so `tempfile.mkdtemp`), `os.remove`, `os.unlink`, `os.rmdir`, `os.rename`, `os.replace`, `os.link`,
    `os.symlink`, `os.chmod`, `os.utime`, `os.truncate`, `os.chown`, `Path.write_text`, `write_bytes`, `mkdir`, `touch`, `unlink`, `rmdir`,
    `rename`, `replace`, `symlink_to`, `hardlink_to`, `chmod`
  `shutil` (source R, destination W): `copy`, `copy2`, `copyfile`, `copytree`; `move` and `rmtree` (W)
  databases: `duckdb.connect` and `sqlite3.connect` on a file (W, unless `read_only=True`); `:memory:` is not a path. The text of an
    `ATTACH` inside SQL is out of scope: the guard sees the path given to `connect`, not the statements that follow.
  sockets: `socket.socket.bind` on a path (W: it makes a socket file) and `connect` (R)
  processes: the `cwd` of `subprocess.Popen` (R)

A path is judged as written (made absolute and normalised, links not followed); the guard never touches the file system itself. The
folders ABOVE an allowed root may be looked at (`stat`, not listed or opened): `Path.resolve()` and `os.path.realpath` look at every
parent of a path. The session isolates `HOME`, the temporary folder and the XDG folders inside pytest's own temporary folder
(`tests/conftest.py`), so what the code derives from them lands inside the allowed area.

Two things are answered, not refused, and both are COUNTED and printed at the end of every run, so that nothing is hidden: a project
marker looked for above the area (`.git`, `pyproject.toml`, `_drspec`, ... from `init`'s walk up) is answered "absent"; and what a
third-party library reads while it is being imported (`requests`, `matplotlib`, `onnxruntime`) is left unrecorded.
"""

from __future__ import annotations

import builtins
import errno
import io
import os
import shutil
import site
import socket
import subprocess
import sys
import sysconfig
import threading
from collections import Counter
from pathlib import Path
from typing import Any, Callable

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]

#: Devices a program may always open, and the process's own view of itself.
ALWAYS_ALLOWED = ("/dev/null", "/dev/zero", "/dev/urandom", "/dev/random", "/dev/tty")


def is_in_repository(path) -> bool:
    """Whether `path` is the repository or something inside it (for a test-session setting that must not look in the developer's checkout)."""
    text = os.path.realpath(os.fspath(path))
    root = os.path.realpath(REPOSITORY)
    return text == root or text.startswith(root + os.sep)


def readable_prefixes() -> tuple:
    """The folders a test may READ other than its own temporary folder: the repository (committed files) and the folders of the running
    Python (the code and data it was installed with, what it imports from). Its own temporary folder, and nothing else, may also be
    WRITTEN; that is only this session's `basetemp`, added by `allow()` (not every session of the user)."""
    roots = {str(REPOSITORY), os.path.realpath(REPOSITORY)}
    roots |= {sys.prefix, sys.base_prefix, sys.exec_prefix, os.path.realpath(sys.prefix), os.path.realpath(sys.base_prefix)}
    try:
        roots |= set(site.getsitepackages()) | {site.getusersitepackages()}
    except AttributeError:  # (an old virtual environment has no `getsitepackages`)
        pass
    roots |= {value for value in sysconfig.get_paths().values() if value}
    roots |= {entry for entry in sys.path if entry and os.path.isabs(entry)}  # (what the interpreter imports from, `PYTHONPATH` included)
    for name in ("sitecustomize", "usercustomize"):  # (a distribution's `sitecustomize.py` is often a link into /etc/pythonX.Y)
        module = sys.modules.get(name)
        if getattr(module, "__file__", None):
            roots.add(os.path.dirname(os.path.realpath(module.__file__)))
    roots |= {os.path.realpath(root) for root in roots}
    return tuple(sorted(root.rstrip(os.sep) for root in roots if root))


#: What the code under test looks for while it walks UP from a project folder: the markers of a project root (`detect_project_root`),
#: the folder of a drspec project (`project_folder_of`) and the files a pytest session looks for above its folder. Above the test's area
#: they are answered "absent", whatever the machine has there (`/tmp/pyproject.toml`, `~/Projects/.git`), so that the walk gives the same
#: result everywhere; the answer is not a violation, and the end of the run counts them.
ABOVE_MARKERS = frozenset(
    {".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "Makefile", "_drspec"}
    | {"pytest.ini", ".pytest.ini", "tox.ini", "setup.cfg", "setup.py", "conftest.py"}  # (what a pytest session, even a nested one, looks for above its folder)
    | {".matplotlib-repo"}  # (matplotlib asks, the first time it is USED, not imported, whether its folder is a development checkout)
)

_originals: dict = {}
_temporary: list = []  # the folders of this session that may be read and written: `allow()`
_readable: list = []  # `readable_prefixes()`, computed when the wrappers are installed
_state = threading.local()
_recording = False
_blocking = True  # the test being run has no `outside` marker: an outside access is refused
_lock = threading.Lock()
#: Everything recorded since `begin()`, in order: `(call, path)`.
_found: list = []
#: What was answered or left unrecorded, for the whole run (printed at its end).
_absent: Counter = Counter()  # project marker name -> probes answered "absent"
_unrecorded: Counter = Counter()  # library -> accesses made while it was being imported that would have been outside


def _text(path: Any) -> str | None:
    """`path` as an absolute, normalised string, or `None` when it names no path (a file descriptor, an object)."""
    if isinstance(path, int):
        return None
    try:
        text = os.fspath(path)
    except TypeError:
        return None
    if isinstance(text, bytes):
        text = os.fsdecode(text)
    if not text.startswith("/"):
        text = os.path.join(os.getcwd(), text)
    return os.path.normpath(text)


def allow(root: Any) -> None:
    """Add a folder that is the session's own (pytest's `basetemp`, `--basetemp`, the isolated area): it may be read and written."""
    text = _text(root)
    if text and text not in _temporary:
        _temporary.append(text.rstrip(os.sep))
        real = os.path.realpath(text)
        if real != text:
            _temporary.append(real.rstrip(os.sep))


def _under(text: str, roots) -> bool:
    return any(text == root or text.startswith(root + os.sep) for root in roots)


def is_outside(path: Any, looking: bool = False, writing: bool = False) -> bool:
    """Whether `path` (as written) may not be touched in this way. Reading: outside the repository, the Python's folders and the
    session's temporary folder. Writing: outside the session's temporary folder (the repository is read-only). Looking (a `stat`): the
    folders above a readable root may be looked at too."""
    text = _text(path)
    if text is None:
        return False
    if text in ALWAYS_ALLOWED:
        return False
    if not writing and (text == "/proc/self" or text.startswith(("/proc/self/", f"/proc/{os.getpid()}/"))):
        return False
    if _under(text, _temporary):
        return False
    roots = _readable or list(readable_prefixes())
    if not writing and _under(text, roots):
        return False
    if looking and not writing:
        for root in [*roots, *_temporary]:
            if root.startswith(text.rstrip(os.sep) + os.sep) or text == os.sep:
                return False
    return True


def _is_marker_probe(text: str) -> str | None:
    """The marker name when `text` names a project marker directly inside a folder above an allowed root (and is not inside one itself)."""
    roots = [*(_readable or list(readable_prefixes())), *_temporary]
    if _under(text, roots):
        return None
    for root in roots:
        folder = os.path.dirname(root)
        while True:
            prefix = folder.rstrip(os.sep) + os.sep
            if text.startswith(prefix):
                name = text[len(prefix):].split(os.sep, 1)[0]
                if name in ABOVE_MARKERS:
                    return name
            if folder in (os.sep, ""):
                break
            folder = os.path.dirname(folder)
    return None


def begin(blocking: bool = True) -> None:
    """Start remembering the outside paths of one test (its setup, call and teardown)."""
    global _recording, _blocking
    with _lock:
        _found.clear()
        _recording = True
        _blocking = blocking


def end() -> list:
    """Stop, and return what was recorded: `[(call, path), ...]` in order."""
    global _recording
    with _lock:
        _recording = False
        return list(_found)


def found() -> list:
    with _lock:
        return list(_found)


class _Absent(FileNotFoundError):
    """A project marker above the test's area: it is not there, whatever the machine has at that place."""


class OutsideAccess(PermissionError):
    """Raised for an access outside the repository and the test's own temporary folder, or a write into the repository."""

    def __init__(self, call: str, path: str) -> None:
        super().__init__(13, f"a test touched a path it may not ({call}): {path}", path)


def _record(call: str, *entries: tuple) -> None:
    """Record every path among `entries` (`(path, mode)`, mode "read", "look" or "write") that may not be touched that way; refuse the call
    when the test is not allowed to touch the outside."""
    if not _recording or getattr(_state, "inside", False):
        return
    importing = getattr(_state, "importing", 0)
    _state.inside = True
    first = None
    try:
        for path, mode in entries:
            writing, looking = mode == "write", mode == "look"
            if not is_outside(path, looking=looking, writing=writing):
                continue
            if importing:  # (a library being imported: not an act of the test; counted, so that it is seen)
                _unrecorded[getattr(_state, "library", "?")] += 1
                continue
            if looking and _blocking:
                name = _is_marker_probe(_text(path))
                if name:
                    _absent[name] += 1
                    raise _Absent(errno.ENOENT, "absent (a project marker above the test's area is not looked at)", _text(path))
            with _lock:
                _found.append((call, _text(path)))
            first = first or (call, _text(path))
            if os.environ.get("DRSPEC_GUARD_STACK"):  # (to find who asked: `DRSPEC_GUARD_STACK=1 pytest ...` prints the stack of each)
                import traceback

                sys.stderr.write(f"[outside guard] {call} {_text(path)}\n" + "".join(traceback.format_stack(limit=9)[:-2]))
    finally:
        _state.inside = False
    if first is not None and _blocking:
        raise OutsideAccess(*first)


# What each wrapped call does to its arguments: a function from `(args, kwargs)` to the `(path, mode)` entries to check.
def _read(*positions: int) -> Callable:
    return lambda args, kwargs: [(args[i], "read") for i in positions if i < len(args)]


def _look(*positions: int) -> Callable:
    return lambda args, kwargs: [(args[i], "look") for i in positions if i < len(args)]


def _write(*positions: int) -> Callable:
    return lambda args, kwargs: [(args[i], "write") for i in positions if i < len(args)]


def _copy(args, kwargs) -> list:  # (the source is read, the destination is written)
    return [(args[0], "read")] * (len(args) > 0) + [(args[1], "write")] * (len(args) > 1)


def _opening(mode_position: int) -> Callable:
    def entries(args, kwargs) -> list:
        mode = args[mode_position] if len(args) > mode_position else kwargs.get("mode", "r")
        writes = isinstance(mode, str) and any(letter in mode for letter in "wax+")
        return [(args[0], "write" if writes else "read")] if args else []

    return entries


def _os_opening(args, kwargs) -> list:
    flags = args[1] if len(args) > 1 else kwargs.get("flags", 0)
    writes = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
    return [(args[0], "write" if writes else "read")] if args else []


def _anchored(found: list, kwargs: dict) -> list:
    """A relative path given with `dir_fd` is relative to that open folder, not to the current directory (`shutil.rmtree` removes the
    entries of a folder that way): it is made absolute from the folder the descriptor names (Linux and macOS ask the system; a descriptor
    that cannot be resolved is left out and counted, never guessed against the current directory)."""
    anchors = [kwargs.get("dir_fd"), None]
    if "src_dir_fd" in kwargs or "dst_dir_fd" in kwargs:
        anchors = [kwargs.get("src_dir_fd"), kwargs.get("dst_dir_fd")]
    result = []
    for position, (path, mode) in enumerate(found):
        descriptor = anchors[position] if position < len(anchors) else None
        if descriptor is None or isinstance(path, int) or os.path.isabs(os.fspath(path)):
            result.append((path, mode))
            continue
        try:
            folder = os.readlink(f"/proc/self/fd/{descriptor}")  # pattern-ok: outside-path the process's own descriptor table (Linux)
        except OSError:
            _unrecorded["a relative path from a descriptor that cannot be resolved"] += 1
            continue
        result.append((os.path.join(folder, os.fspath(path)), mode))
    return result


def _watching(original: Callable, call: str, entries: Callable) -> Callable:
    """A wrapper that checks the paths `entries` names before calling `original`."""

    def watched(*args, **kwargs):
        if _recording:
            try:
                _record(call, *_anchored(entries(args, kwargs), kwargs))
            except _Absent:
                if call in ("Path.exists", "Path.is_file", "Path.is_dir"):  # (these answer yes or no; the others raise, as a missing file does)
                    return False
                raise
        return original(*args, **kwargs)

    watched.__name__ = getattr(original, "__name__", call)
    watched.__wrapped__ = original  # type: ignore[attr-defined]
    return watched


def _os_capability_sets() -> list:
    """The sets of `os` functions that say what the platform supports (`shutil.copystat` asks `os.stat in os.supports_follow_symlinks`)."""
    return [getattr(os, name) for name in ("supports_dir_fd", "supports_fd", "supports_follow_symlinks", "supports_effective_ids") if hasattr(os, name)]


def _patch(owner: Any, attribute: str, call: str, entries: Callable) -> None:
    key = (owner, attribute)
    if key in _originals or not hasattr(owner, attribute):
        return
    original = getattr(owner, attribute)
    _originals[key] = original
    watched = _watching(original, call, entries)
    setattr(owner, attribute, watched)
    if owner is os:  # the wrapper must be as capable as the function it stands for
        for capabilities in _os_capability_sets():
            if original in capabilities:
                capabilities.add(watched)


def _database_watch(original: Callable, call: str) -> Callable:
    def connect(database=":memory:", *args, **kwargs):
        if _recording and str(database) not in (":memory:", ""):
            _record(call, (database, "read" if kwargs.get("read_only") else "write"))
        return original(database, *args, **kwargs)

    return connect


def _socket_watch(original: Callable, call: str, mode: str) -> Callable:
    def watched(self, address, *args, **kwargs):
        if _recording and isinstance(address, (str, bytes)):  # (a Unix socket file; a network address is a tuple)
            _record(call, (address, mode))
        return original(self, address, *args, **kwargs)

    return watched


def _popen_watch(original: Callable) -> Callable:
    def __init__(self, *args, **kwargs):
        if _recording and kwargs.get("cwd"):
            _record("subprocess.Popen(cwd=)", (kwargs["cwd"], "read"))
        return original(self, *args, **kwargs)

    return __init__


OWN_PACKAGES = ("drspec", "tests")


def _import_watch(original: Callable) -> Callable:
    """`__import__`, with the accesses of a THIRD-PARTY import (the standard library and the installed libraries) left unrecorded (and
    counted, by library): a library that looks at the machine when it is first imported (`requests` finds the system's CA bundle,
    `matplotlib` asks whether it is a development checkout, `onnxruntime` reads `/etc/os-release`) is not an act of the test, and which
    test imports it first is chance. The imports of `drspec` and of the tests are watched like everything else."""

    def watched(name, globals=None, locals=None, fromlist=(), level=0):
        if _recording or getattr(_state, "importing", 0):
            top = name.partition(".")[0] if level == 0 else (globals or {}).get("__name__", "").partition(".")[0]
            if top and top not in OWN_PACKAGES:
                outermost = getattr(_state, "importing", 0) == 0
                _state.importing = getattr(_state, "importing", 0) + 1
                if outermost:
                    _state.library = top
                try:
                    return original(name, globals, locals, fromlist, level)
                finally:
                    _state.importing -= 1
        return original(name, globals, locals, fromlist, level)

    return watched


def install() -> None:
    """Wrap the functions. Idempotent."""
    global _readable
    if (builtins, "__import__") not in _originals:
        _originals[(builtins, "__import__")] = builtins.__import__
        builtins.__import__ = _import_watch(builtins.__import__)
    if not _readable:
        _readable = list(readable_prefixes())
    _patch(builtins, "open", "open", _opening(1))
    _patch(io, "open", "open", _opening(1))
    _patch(os, "open", "os.open", _os_opening)
    for attribute in ("listdir", "scandir", "access"):
        _patch(os, attribute, f"os.{attribute}", _read(0))
    _patch(os, "readlink", "os.readlink", _look(0))  # (`os.path.realpath` reads the link of every parent of a path)
    _patch(os, "chdir", "os.chdir", _read(0))
    for attribute in ("stat", "lstat"):
        _patch(os, attribute, f"os.{attribute}", _look(0))
    for attribute in ("mkdir", "makedirs", "remove", "unlink", "rmdir", "chmod", "utime", "truncate", "chown"):
        _patch(os, attribute, f"os.{attribute}", _write(0))
    for attribute in ("rename", "replace", "link"):
        _patch(os, attribute, f"os.{attribute}", _write(0, 1))
    _patch(os, "symlink", "os.symlink", _write(1))  # (the first argument is only the text the link holds)
    for attribute in ("copy", "copy2", "copyfile", "copytree"):
        _patch(shutil, attribute, f"shutil.{attribute}", _copy)
    _patch(shutil, "move", "shutil.move", _write(0, 1))
    _patch(shutil, "rmtree", "shutil.rmtree", _write(0))
    for attribute in ("exists", "is_file", "is_dir", "stat", "lstat", "samefile"):
        _patch(Path, attribute, f"Path.{attribute}", _look(0))
    for attribute in ("read_text", "read_bytes", "iterdir", "glob", "rglob"):
        _patch(Path, attribute, f"Path.{attribute}", _read(0))
    _patch(Path, "readlink", "Path.readlink", _look(0))
    _patch(Path, "open", "Path.open", _opening(1))
    for attribute in ("write_text", "write_bytes", "mkdir", "touch", "unlink", "rmdir", "chmod", "symlink_to", "hardlink_to"):
        _patch(Path, attribute, f"Path.{attribute}", _write(0))
    for attribute in ("rename", "replace"):
        _patch(Path, attribute, f"Path.{attribute}", _write(0, 1))
    key = (subprocess.Popen, "__init__")
    if key not in _originals:
        _originals[key] = subprocess.Popen.__init__
        subprocess.Popen.__init__ = _popen_watch(subprocess.Popen.__init__)  # type: ignore[method-assign]
    for owner_name, attribute in (("duckdb", "connect"), ("sqlite3", "connect")):
        try:
            owner = __import__(owner_name)
        except ImportError:
            continue
        key = (owner, attribute)
        if key not in _originals:
            _originals[key] = getattr(owner, attribute)
            setattr(owner, attribute, _database_watch(_originals[key], f"{owner_name}.connect"))
    for attribute, mode in (("bind", "write"), ("connect", "read")):
        key = (socket.socket, attribute)
        if key not in _originals:
            _originals[key] = getattr(socket.socket, attribute)
            setattr(socket.socket, attribute, _socket_watch(_originals[key], f"socket.{attribute}", mode))


def uninstall() -> None:
    for (owner, attribute), original in list(_originals.items()):
        if owner is os:
            for capabilities in _os_capability_sets():
                capabilities.discard(getattr(owner, attribute))
        setattr(owner, attribute, original)
    _originals.clear()


# ---- the pytest plugin: every test is checked, and fails with the first outside path -------------------------------------------

PLUGIN_NAME = "drspec-outside-guard"
_audit: dict = {}  # node id -> [(call, path), ...] for every test that touched an outside path
_exempt: dict = {}  # node id -> reason, for a test marked `outside`
_at_call: dict = {}  # node id -> how many were recorded when its call ended


class Guard:
    """The plugin object: register it once per session (`register`)."""

    def __init__(self, config) -> None:
        self.config = config

    def pytest_sessionstart(self, session):
        factory = getattr(session.config, "_tmp_path_factory", None)
        if factory is not None:  # THIS session's folder only (not every `pytest-of-<user>` session): made now, before any test is watched
            allow(factory.getbasetemp())

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item, nextitem):
        begin(blocking=item.get_closest_marker("outside") is None)
        try:
            yield
        finally:
            records = end()
            if records:
                _audit[item.nodeid] = records

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        report = outcome.get_result()
        if report.when not in ("call", "teardown"):
            return
        records = found()
        marker = item.get_closest_marker("outside")
        if marker is not None and records:
            _exempt[item.nodeid] = marker.kwargs.get("reason", "")
            return
        before = _at_call.get(item.nodeid, 0) if report.when == "teardown" else 0
        if report.when == "call":
            _at_call[item.nodeid] = len(records)
        if len(records) > before and report.passed:
            report.outcome = "failed"
            report.longrepr = _message(item.nodeid, records)

    def pytest_terminal_summary(self, terminalreporter):
        terminalreporter.write_sep("-", "outside guard: what it answered or left unrecorded (nothing is hidden)")
        probes = ", ".join(f"{name} {count}" for name, count in sorted(_absent.items())) or "-"
        terminalreporter.write_line(f"  answered 'absent': {sum(_absent.values())} probes of project markers above the area ({probes})")
        libraries = ", ".join(f"{name} {count}" for name, count in _unrecorded.most_common()) or "-"
        terminalreporter.write_line(f"  unrecorded accesses inside third-party imports: {sum(_unrecorded.values())}, libraries: {libraries}")
        terminalreporter.write_line(f"  tests exempted by @pytest.mark.outside: {len(_exempt)}")
        if _exempt:
            terminalreporter.write_sep("-", f"tests allowed to touch the outside (@pytest.mark.outside): {len(_exempt)}")
            for nodeid, reason in sorted(_exempt.items()):
                terminalreporter.write_line(f"  {nodeid}  reason: {reason}")
        flagged = {nodeid: records for nodeid, records in _audit.items() if nodeid not in _exempt}
        if flagged:
            terminalreporter.write_sep("-", f"touched a path outside the repository and tmp_path: {len(flagged)} test(s)")
            for nodeid, records in sorted(flagged.items()):
                terminalreporter.write_line(f"  {nodeid}")
                for call, path in dict.fromkeys(records):
                    terminalreporter.write_line(f"      {call} {path}")


def _message(nodeid: str, records: list) -> str:
    unique = list(dict.fromkeys(records))
    lines = [f"{nodeid} touched a path outside the repository and its own temporary folder, or wrote into the repository:"]
    lines += [f"    {call}  {path}" for call, path in unique[:10]]
    if len(unique) > 10:
        lines.append(f"    ... and {len(unique) - 10} more")
    lines.append("A test reads the repository and tmp_path and writes only tmp_path (tests/README.md, `outside_guard.py`). Use tmp_path, a committed")
    lines.append("excerpt under tests/fixtures, or a fake; `@pytest.mark.outside(reason=...)` only if the real thing outside is the objective.")
    return "\n".join(lines)


def register(config) -> None:
    """Install the wrappers and the plugin for this session (idempotent: `conftest.py` and `tier_report` both call it)."""
    config.addinivalue_line("markers", "outside(reason): the test touches the real outside on purpose, and says why (listed at the end of the run)")
    install()
    basetemp = getattr(config.option, "basetemp", None)
    if basetemp:  # a folder the person chose for the temporary files of the tests is the tests' own
        allow(os.path.abspath(str(basetemp)))
    if not config.pluginmanager.has_plugin(PLUGIN_NAME):
        config.pluginmanager.register(Guard(config), PLUGIN_NAME)


def unregister(config) -> None:
    plugin = config.pluginmanager.get_plugin(PLUGIN_NAME)
    if plugin is not None:
        config.pluginmanager.unregister(plugin)
    uninstall()
