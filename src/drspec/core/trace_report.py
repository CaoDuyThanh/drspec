"""Reading a project's trace log back: per-event statistics, the slowest events,
one command's whole path, and clearing. The CLI and the MCP tool both call
`trace_summary`, so they report the same numbers.

The log of a project lives outside it (see `core.tracing`) and is found from the
project's database path. A log an older version left in `_drspec/` is still read
(its events are marked `legacy`) until `trace clear` removes it."""

from __future__ import annotations

import datetime
import heapq
import json
import math
import re
from array import array
from collections import deque
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from drspec.core import tracing

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_SINCE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd])\s*$")

#: The field that tells apart events of one name (`command search`, `request execute`).
_LABEL_FIELDS = ("command", "op", "kind")

#: Fields shown for a slow event beyond the common ones.
_DETAIL_FIELDS = (
    "sql", "index", "status", "connect_ms", "ipc_ms", "ipc_calls", "lock_ms", "run_ms", "rows",
    "checkpoint_ms", "copy_ms", "sync_ms", "commit_ms", "fsync_ms", "replace_ms",
    "detect_ms", "discover_ms", "rescan_ms", "embed_ms", "store_ms",
    "items", "round", "owed", "first", "reason", "code", "wait_s",
    "queued_before", "queued_after", "gap_ms", "pause_reason", "batch", "remediated", "discovered", "drifted",
    "max_rss_mb", "cpu_s", "rss_mb", "db_mb", "wal_mb", "load1", "mem_avail_mb",
)


class TraceError(ValueError):
    """An argument that cannot be used (for example an unreadable `--since`)."""


def parse_since(text: str) -> float:
    """Seconds from a value like `90s`, `15m`, `1h` or `2d`."""
    match = _SINCE.match(text or "")
    if not match:
        raise TraceError(f"--since expects a number and a unit (s, m, h, d), such as 30m or 1h; got {text!r}.")
    return float(match.group(1)) * _UNITS[match.group(2)]


def trace_paths(db_path: Path) -> List[Tuple[Path, bool]]:
    """`(path, is_legacy)` for the project's log files, older first: the old in-project file and
    its rotation, then the current log and its rotation, whichever exist."""
    found: List[Tuple[Path, bool]] = []
    for base, legacy in ((tracing.legacy_log_for(db_path), True), (tracing.log_file_for(db_path), False)):
        for path in (Path(str(base) + ".1"), base):
            if path.exists():
                found.append((path, legacy))
    return found


def _cutoff(since: Optional[str]) -> Optional[str]:
    if not since:
        return None
    moment = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=parse_since(since))
    return moment.isoformat(timespec="milliseconds")


def iter_events(db_path: Path, since: Optional[str] = None) -> Iterator[Dict[str, Any]]:
    """Every event from the trace files, one at a time, the older file first.
    Unreadable lines are skipped. Nothing is kept, so a large log costs no memory."""
    cutoff = _cutoff(since)
    for path, legacy in trace_paths(db_path):
        try:
            handle = open(path, "r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict) or "event" not in event:
                    continue
                if cutoff is not None and str(event.get("t", "")) < cutoff:
                    continue
                if legacy:
                    event["legacy"] = True
                yield event


def read_events(db_path: Path, since: Optional[str] = None) -> List[Dict[str, Any]]:
    """The events as a list in time order. Reads everything; for small logs and tests."""
    return sorted(iter_events(db_path, since), key=lambda event: str(event.get("t", "")))


def _label(event: Dict[str, Any]) -> Optional[str]:
    for field in _LABEL_FIELDS:
        value = event.get(field)
        if value:
            return str(value)
    return None


def _percentile(sorted_values: List[float], fraction: float) -> float:
    index = max(0, math.ceil(fraction * len(sorted_values)) - 1)
    return sorted_values[index]


def _median(sorted_values: List[float]) -> float:
    middle = len(sorted_values) // 2
    if len(sorted_values) % 2:
        return sorted_values[middle]
    return (sorted_values[middle - 1] + sorted_values[middle]) / 2.0


def _brief(event: Dict[str, Any]) -> Dict[str, Any]:
    brief: Dict[str, Any] = {"t": event.get("t"), "proc": event.get("proc"), "event": event.get("event")}
    label = _label(event)
    if label:
        brief["label"] = label
    brief["ms"] = event.get("ms")
    if event.get("cid"):
        brief["cid"] = event["cid"]
    for field in _DETAIL_FIELDS:
        if field in event:
            brief[field] = event[field]
    if event.get("legacy"):
        brief["legacy"] = True
    return brief


def trace_summary(db_path: Path, since: Optional[str] = None, slowest: int = 20, cid: Optional[str] = None) -> Dict[str, Any]:
    """Statistics per event name, the slowest events, or one command's path.

    The log is read as a stream: per event name only the durations are kept, in a
    compact array, and of the events themselves only the slowest few and, for
    `cid`, the ones of that command."""
    if cid:
        matching = sorted((event for event in iter_events(db_path, since) if event.get("cid") == cid), key=lambda event: str(event.get("t", "")))
        return {"cid": cid, "events": len(matching), "path": [_brief(event) for event in matching]}

    groups: Dict[str, "array[float]"] = {}
    memory: Dict[str, "array[float]"] = {}
    cpu: Dict[str, float] = {}
    heap: List[Tuple[float, int, Dict[str, Any]]] = []
    keep = max(0, slowest)
    read = 0
    for event in iter_events(db_path, since):
        read += 1
        duration = float(event.get("ms") or 0.0)
        label = _label(event)
        name = f"{event['event']} {label}" if label else str(event["event"])
        group = groups.get(name)
        if group is None:
            group = groups[name] = array("d")
        group.append(duration)
        resident = event.get("max_rss_mb", event.get("rss_mb"))
        if isinstance(resident, (int, float)):
            memory.setdefault(name, array("d")).append(float(resident))
        if isinstance(event.get("cpu_s"), (int, float)):
            cpu[name] = cpu.get(name, 0.0) + float(event["cpu_s"])
        if keep:
            entry = (duration, read, event)  # the counter keeps two equal durations from comparing their dicts
            if len(heap) < keep:
                heapq.heappush(heap, entry)
            elif duration > heap[0][0]:
                heapq.heapreplace(heap, entry)

    summary = []
    for name in sorted(groups, key=lambda key: -sum(groups[key])):
        values = sorted(groups[name])
        row = {
            "event": name,
            "count": len(values),
            "total_ms": round(sum(values), 3),
            "median_ms": round(_median(values), 3),
            "p95_ms": round(_percentile(values, 0.95), 3),
            "max_ms": round(values[-1], 3),
        }
        if name in memory:
            resident = sorted(memory[name])
            row["peak_rss_mb"] = round(resident[-1], 1)
            row["median_rss_mb"] = round(_median(resident), 1)
        if name in cpu:
            row["total_cpu_s"] = round(cpu[name], 3)
        summary.append(row)

    ranked = [event for _duration, _order, event in sorted(heap, key=lambda entry: (-entry[0], entry[1]))]
    return {
        "since": since,
        "events_read": read,
        "summary": summary,
        "slowest": [_brief(event) for event in ranked],
    }


def trace_tail(db_path: Path, count: int = 20) -> Dict[str, Any]:
    total = 0
    latest: "deque[Dict[str, Any]]" = deque(maxlen=max(0, count))
    for event in iter_events(db_path):
        total += 1
        latest.append(event)
    return {"events_total": total, "events": [_brief(event) for event in latest]}


def _empty(path: Path) -> int:
    """Empty a live log (a process may still be appending to it) and return how many bytes went."""
    try:
        size = path.stat().st_size
        with open(path, "w"):
            pass
    except OSError:
        return 0
    return size


def _remove(path: Path) -> int:
    try:
        size = path.stat().st_size
        path.unlink()
    except OSError:
        return 0
    return size


def clear_trace(db_path: Path, everything: bool = False) -> Dict[str, Any]:
    """Empty this project's log (or, with `everything`, every project's), remove a log left in
    the project by an older version, and delete logs that are old or over the folder cap."""
    removed = 0
    logs = [tracing.log_file_for(db_path)]
    if everything:
        directory = tracing.default_trace_dir()
        logs = [directory / (row["name"] + ".log") for row in tracing.list_logs(directory)]
    for current in logs:
        removed += _empty(current) + _remove(Path(str(current) + ".1"))
    result: Dict[str, Any] = {"cleared_bytes": removed}
    legacy = tracing.legacy_log_for(db_path)
    legacy_bytes = _remove(legacy) + _remove(Path(str(legacy) + ".1"))
    if legacy_bytes or legacy.exists():
        result["legacy_removed_bytes"] = legacy_bytes
    return result
