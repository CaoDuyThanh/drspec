"""Which forbidden calls each test made, to decide its tier (see tests/README.md).

Load it as a pytest plugin on any test file and read the table it prints at the end:

    python3 -m pytest -p tests.tools.tier_report tests/unit/test_version.py

For each test call it records, and prints, the forbidden calls the call made:
  process    subprocess.Popen or os.fork (a process is started)
  socket     a socket is opened
  sleep      time.sleep of 0.05 s or more
  duckdb     duckdb.connect on a file (not ':memory:')
  cli        typer's CliRunner.invoke
  outside    a path outside the repository and the test's own temporary folder was opened, listed or stat-ed
             (`tests/tools/outside_guard.py` says which calls and which folders are allowed)
together with the CPU time of the call (process plus children). The tier follows from the calls:
integration if a process, socket or sleep; component if a DuckDB file or the CLI runner; unit otherwise.

It changes no test: a test that touches the outside is failed by the guard itself (`outside_guard.py`, registered for every run
by `tests/conftest.py`); here it only shows as a flag. The patches are installed when the session starts and removed when it
ends, so a session that does not load this plugin is not affected; installing twice patches once.
"""

from __future__ import annotations

import os
import platform
import socket
import subprocess
import time
import uuid

import pytest

from tests.tools import outside_guard

# Imported before anything is patched: importing `uuid` runs `uname` through subprocess (platform.uname), which is an
# import side effect, not a call a test makes. Touching both here keeps it out of the calls of the first test.
_IMPORTED_BEFORE_PATCHING = (platform.uname(), uuid.UUID)

_calls: set = set()
_results: dict = {}
#: (owner, attribute) -> the original, for every patch installed; empty when nothing is patched.
_originals: dict = {}


def _cpu() -> float:
    times = os.times()
    return time.process_time() + times.children_user + times.children_system


def _patch(owner, attribute: str, wrapper_factory) -> None:
    key = (owner, attribute)
    if key in _originals:
        return
    original = getattr(owner, attribute)
    _originals[key] = original
    setattr(owner, attribute, wrapper_factory(original))


def _unpatch_all() -> None:
    for (owner, attribute), original in reversed(list(_originals.items())):
        setattr(owner, attribute, original)
    _originals.clear()


def _flagging(flag: str, minimum: float = 0.0):
    def factory(original):
        def wrapper(*args, **kwargs):
            if flag != "sleep" or (args and float(args[0]) >= minimum):
                _calls.add(flag)
            return original(*args, **kwargs)

        return wrapper

    return factory


def _socket_factory(original):
    def __init__(self, *args, **kwargs):
        _calls.add("socket")
        original(self, *args, **kwargs)

    return __init__


def _duckdb_factory(original):
    def connect(database=":memory:", *args, **kwargs):
        if str(database) != ":memory:":
            _calls.add("duckdb")
        return original(database, *args, **kwargs)

    return connect


def _install() -> None:
    """Patch the forbidden calls. Idempotent: a second call patches nothing more."""
    _patch(subprocess, "Popen", _flagging("process"))
    if hasattr(os, "fork"):
        _patch(os, "fork", _flagging("process"))
    _patch(time, "sleep", _flagging("sleep", minimum=0.05))
    _patch(socket.socket, "__init__", _socket_factory)
    try:
        import duckdb

        _patch(duckdb, "connect", _duckdb_factory)
    except ImportError:
        pass
    try:
        from typer.testing import CliRunner

        _patch(CliRunner, "invoke", _flagging("cli"))
    except ImportError:
        pass


def pytest_configure(config) -> None:
    _install()
    outside_guard.register(config)  # (idempotent: `tests/conftest.py` registers the same guard for every run)


def pytest_unconfigure(config) -> None:
    _unpatch_all()


def _tier(calls: list) -> str:
    if set(calls) & {"process", "socket", "sleep"}:
        return "integration"
    if set(calls) & {"duckdb", "cli"}:
        return "component"
    return "unit"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _calls.clear()
    start = _cpu()
    yield
    outside = outside_guard.found()  # (what the guard recorded since this test began: its setup and its call)
    calls = sorted(_calls | ({"outside"} if outside else set()))
    _results[item.nodeid] = {"cpu": _cpu() - start, "calls": calls, "outside": outside}


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    terminalreporter.write_sep("-", "tier report (tests/tools/tier_report.py)")
    for nodeid, record in sorted(_results.items()):
        calls = ",".join(record["calls"]) or "-"
        terminalreporter.write_line(
            f"{_tier(record['calls']):12} {record['cpu']:6.2f} s  {calls:24} {nodeid}"
        )
