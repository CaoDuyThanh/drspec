"""The end-of-run tier report, printed only when `DRSPEC_TIER_REPORT=1` is set (see tests/README.md).

It reports and does not fail anything: per tier, the number of tests and their total CPU time; the 15 slowest tests with
their tier; and the tests over their tier's budget. A test with no tier is counted as 'no tier'. The tier of a test is
read from its marker (`unit`, `component`, `integration`), else the file's `pytestmark`, else `tests/tiers.json`.

CPU time is the process CPU time plus the children's CPU time (`os.times()`), around the test call only.
"""

from __future__ import annotations

import functools
import json
import os
import time
from pathlib import Path

import pytest

from tests.tools import disk_counts

TIERS = ("unit", "component", "integration")
BUDGET_SECONDS = {"unit": 0.05, "component": 0.25, "integration": 5.0}
ENV_VAR = "DRSPEC_TIER_REPORT"

_cpu_by_nodeid: dict[str, float] = {}
_tier_by_nodeid: dict[str, str] = {}
_budget_by_nodeid: dict[str, tuple] = {}  # node id -> (seconds, reason) for a test with a budget marker
LIST_OVER_SECONDS = 0.1  # every test above this CPU time is listed, whatever its tier
_disk_by_nodeid: dict[str, dict] = {}  # node id -> disk work of its setup, call and teardown (`disk_counts.py`)
_load: dict[str, tuple] = {}  # "start" / "end" -> the machine's load averages


def _enabled() -> bool:
    return os.environ.get(ENV_VAR) == "1"


def _cpu() -> float:
    times = os.times()
    return time.process_time() + times.children_user + times.children_system


_TIERS_FILE = Path(__file__).resolve().parents[1] / "tiers.json"


@functools.lru_cache(maxsize=1)
def _tier_table() -> dict:
    if not _TIERS_FILE.exists():
        return {}
    return json.loads(_TIERS_FILE.read_text(encoding="utf-8"))


def _tier_of(item) -> str:
    """The marker on the test, else the file's `pytestmark`, else `tests/tiers.json` (the order of the README)."""
    for name in TIERS:
        if item.get_closest_marker(name) is not None:
            return name
    entry = _tier_table().get(item.nodeid)
    return entry["tier"] if entry else "no tier"


def pytest_sessionstart(session):
    if _enabled():
        disk_counts.install()
        _load["start"] = os.getloadavg()


def pytest_sessionfinish(session, exitstatus):
    if _enabled():
        _load["end"] = os.getloadavg()
        disk_counts.uninstall()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    if not _enabled():
        yield
        return
    before = disk_counts.snapshot()
    yield
    _disk_by_nodeid[item.nodeid] = disk_counts.delta(before)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    if not _enabled():
        yield
        return
    start = _cpu()
    yield
    _cpu_by_nodeid[item.nodeid] = _cpu() - start
    _tier_by_nodeid[item.nodeid] = _tier_of(item)
    marker = item.get_closest_marker("budget")
    if marker is not None:
        _budget_by_nodeid[item.nodeid] = (float(marker.kwargs.get("seconds", 0)), marker.kwargs.get("reason", ""))


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if not _enabled() or not _cpu_by_nodeid:
        return
    counts = {name: 0 for name in TIERS + ("no tier",)}
    cpu = {name: 0.0 for name in TIERS + ("no tier",)}
    over: list = []
    for nodeid, used in _cpu_by_nodeid.items():
        tier = _tier_by_nodeid[nodeid]
        counts[tier] += 1
        cpu[tier] += used
        if nodeid in _budget_by_nodeid:
            seconds, reason = _budget_by_nodeid[nodeid]
            limit = seconds if seconds else BUDGET_SECONDS.get(tier, float("inf"))
        else:
            limit = BUDGET_SECONDS.get(tier, float("inf"))
        if used > limit:
            over.append((used, tier, nodeid, limit))
    terminalreporter.write_sep("-", "test tiers (DRSPEC_TIER_REPORT=1; reports only)")
    for name in TIERS + ("no tier",):
        terminalreporter.write_line(f"{name:12} {counts[name]:5} tests  {cpu[name]:8.1f} s CPU")
    terminalreporter.write_line("15 slowest by CPU:")
    for nodeid, used in sorted(_cpu_by_nodeid.items(), key=lambda pair: -pair[1])[:15]:
        terminalreporter.write_line(f"  {used:7.2f} s  {_tier_by_nodeid[nodeid]:12} {nodeid}")
    above = sorted(
        ((used, _tier_by_nodeid[nodeid], nodeid) for nodeid, used in _cpu_by_nodeid.items() if used > LIST_OVER_SECONDS),
        reverse=True,
    )
    terminalreporter.write_line(f"tests over {LIST_OVER_SECONDS * 1000:g} ms CPU (all tiers): {len(above)}")
    for used, tier, nodeid in above:
        terminalreporter.write_line(f"  {used:7.3f} s  {tier:12} {nodeid}")
    totals = {name: sum(record[name] for record in _disk_by_nodeid.values()) for name in disk_counts.FIELDS}
    terminalreporter.write_line(
        "disk work (counts of requests: the same on a busy machine and a quiet one): "
        + ", ".join(f"{name} {totals[name]}" for name in disk_counts.FIELDS)
    )
    busiest = sorted(
        ((sum(record.values()), nodeid, record) for nodeid, record in _disk_by_nodeid.items() if any(record.values())), reverse=True
    )[:10]
    for _, nodeid, record in busiest:
        terminalreporter.write_line("  " + " ".join(f"{name}={record[name]}" for name in disk_counts.FIELDS) + f"  {nodeid}")
    if _load:
        terminalreporter.write_line("load average (1, 5, 15 min) at start / end: " + " / ".join(
            ", ".join(f"{value:.2f}" for value in _load[when]) for when in ("start", "end") if when in _load))
    terminalreporter.write_line(f"over their tier budget: {len(over)}")
    for used, tier, nodeid, limit in sorted(over, reverse=True):
        reason = _budget_by_nodeid.get(nodeid, (0, ""))[1]
        suffix = f"  [budget marker: {reason}]" if reason else ""
        terminalreporter.write_line(f"  {used:7.3f} s > {limit:g} s ({tier})  {nodeid}{suffix}")
    marked = sorted(_budget_by_nodeid.items())
    if marked:
        terminalreporter.write_line(f"tests with a budget marker: {len(marked)}")
        for nodeid, (seconds, reason) in marked:
            terminalreporter.write_line(f"  {seconds:g} s  {nodeid}  reason: {reason}")
