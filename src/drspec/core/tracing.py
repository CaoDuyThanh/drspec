"""An optional JSON-lines trace of what drspec does while it runs.

Off by default. When on, each process appends one JSON object per event to
a log outside the project (`<tempdir>/drspec-trace-<uid>/<hash of the database
path>.log`), so "where did the time go" can be answered from the file after
the fact without anything accumulating in the project. The level comes from the `trace.level` setting for the
worker and the embedding process (re-read as they run), and from the
`--trace` flag or the `DRSPEC_TRACE` variable for a single command; a
command that talks to the worker also adopts the worker's level, which the
worker attaches to its replies while tracing is on.

Tracing must never affect a command: every write is wrapped, and the first
failure stops further attempts in that process. Nothing is created while the
level is `off`.

The log holds no project data: `emit` keeps only fields on a whitelist, with
numbers, booleans or short codes as values, and counts what it dropped.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

try:
    import fcntl
except ImportError:  # not on Windows: rotation then relies on the size re-check alone
    fcntl = None  # type: ignore[assignment]

#: The name the log had inside the project, kept only so an old one can still be read and removed.
LEGACY_NAME = "trace.log"
ENV_VAR = "DRSPEC_TRACE"
DIR_ENV_VAR = "DRSPEC_TRACE_DIR"
LEVELS = ("off", "slow", "all")

DEFAULT_SLOW_MS = 500.0
DEFAULT_MAX_MB = 20.0

#: Longest SQL prefix written to a `request` event.
SQL_PREFIX_CHARS = 120

DEFAULT_KEEP_DAYS = 7.0
DEFAULT_MAX_TOTAL_MB = 200.0

_level = "off"
_slow_ms = DEFAULT_SLOW_MS
_max_bytes = int(DEFAULT_MAX_MB * 1024 * 1024)
_path: Optional[Path] = None
_db_file: Optional[Path] = None
_proc = "cli"
#: The level was fixed by a flag or the environment, so a worker's report is ignored.
_pinned = False
#: After a failed write, no write is tried until this time (monotonic seconds),
#: and each further failure doubles the wait, so a full disk neither slows every
#: event nor ends tracing in a process that lives for days.
_blocked_until = 0.0
_failures = 0
FIRST_RETRY_SECONDS = 5.0
MAX_RETRY_SECONDS = 300.0

_local = threading.local()
_process_cid: Optional[str] = None
_totals: Dict[str, float] = {}
_QUOTED = re.compile(r"'(?:[^']|'')*'")


def enabled() -> bool:
    return _level != "off"


def level() -> str:
    return _level


def new_cid() -> str:
    return os.urandom(4).hex()


def current_cid() -> str:
    """This thread's correlation id, or one for the whole process."""
    global _process_cid
    cid = getattr(_local, "cid", None)
    if cid:
        return cid
    if _process_cid is None:
        _process_cid = new_cid()
    return _process_cid


def set_cid(cid: Optional[str]) -> None:
    _local.cid = cid


def parse_level(raw: Any) -> Optional[str]:
    """`off`/`slow`/`all` from a flag or environment value, or `None` if unusable."""
    text = str(raw).strip().lower()
    if text in LEVELS:
        return text
    if text in ("1", "true", "on", "yes"):
        return "all"
    if text in ("0", "false", "no"):
        return "off"
    return None


def init_process(proc: str, honor_env: bool) -> None:
    """Name this process (`cli`, `mcp`, `worker`, `embed`) and reset tracing.

    The worker and the embedding process pass `honor_env=False`: they keep the
    environment of whichever command started them, so only their setting may
    control them.
    """
    global _proc, _level, _pinned
    _proc = proc
    _level = "off"
    _pinned = False
    _reset_failures()
    if honor_env:
        chosen = parse_level(os.environ.get(ENV_VAR, ""))
        if chosen is not None:
            _level = chosen
            _pinned = True


def set_proc(proc: str) -> None:
    """Rename this process without changing its level."""
    global _proc
    _proc = proc


def set_level(chosen: str, pinned: bool = True) -> None:
    global _level, _pinned
    _level = chosen
    _pinned = pinned
    _reset_failures()


def user_id() -> str:
    try:
        return str(os.geteuid())
    except AttributeError:  # Windows
        import getpass

        return getpass.getuser()


def default_trace_dir() -> Path:
    override = os.environ.get(DIR_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path(tempfile.gettempdir()) / f"drspec-trace-{user_id()}"


def log_name_for(db_path: Path) -> str:
    """`<first 16 hex of the SHA-1 of the resolved database path>.log`: the project is identified
    by this hash only, and no event holds its path."""
    resolved = os.fsencode(str(Path(db_path).resolve()))
    return hashlib.sha1(resolved).hexdigest()[:16] + ".log"


def log_file_for(db_path: Path) -> Path:
    """Where the log of the project whose database is `db_path` is written (nothing is created)."""
    return default_trace_dir() / log_name_for(db_path)


def legacy_log_for(db_path: Path) -> Path:
    return Path(db_path).parent / LEGACY_NAME


_dir_state: Optional[bool] = None


def ensure_trace_dir(directory: Optional[Path] = None) -> Optional[Path]:
    """Create the log folder (mode 0700) or check the one that exists: it must be a real directory
    (not a link) that belongs to this user. Otherwise `None`, with one line on standard error the
    first time in a process."""
    global _dir_state
    directory = directory or default_trace_dir()
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = os.lstat(str(directory))
        problem = None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            problem = "it is not a plain directory"
        elif hasattr(os, "geteuid") and info.st_uid != os.geteuid():
            problem = "it belongs to another user"
        elif hasattr(os, "geteuid") and info.st_mode & 0o077:
            try:
                os.chmod(str(directory), 0o700)
            except OSError:
                problem = "other users can reach it"
    except OSError as e:
        problem = f"it cannot be used ({e.strerror or e.__class__.__name__})"
    if problem:
        if _dir_state is None:
            print(f"drspec: tracing is off because the trace folder {directory} is not safe to use: {problem}.", file=sys.stderr)
        _dir_state = False
        return None
    _dir_state = True
    return directory


def _project_files(directory: Path) -> Dict[str, list]:
    """Log name -> its files (`<name>`, `<name>.1`, `<name>.lock`) that exist."""
    groups: Dict[str, list] = {}
    try:
        names = os.listdir(str(directory))
    except OSError:
        return groups
    for name in names:
        base = name[:-2] if name.endswith(".1") else name[:-5] if name.endswith(".lock") else name
        if base.endswith(".log"):
            groups.setdefault(base, []).append(directory / name)
    return groups


def list_logs(directory: Optional[Path] = None) -> list:
    """`[{"name", "size_bytes", "last_event"}]` for every project's log in the folder, newest first.
    No path is stored in a log, so none can be shown."""
    directory = directory or default_trace_dir()
    rows = []
    for name, files in _project_files(directory).items():
        stats = []
        for path in files:
            try:
                stats.append((path, os.stat(str(path))))
            except OSError:
                continue
        data = [info for path, info in stats if not path.name.endswith(".lock")]
        if not data:
            continue
        rows.append({
            "name": name[:-4],
            "size_bytes": sum(info.st_size for info in data),
            "last_event": datetime.datetime.fromtimestamp(max(info.st_mtime for info in data), datetime.timezone.utc).isoformat(timespec="seconds"),
        })
    return sorted(rows, key=lambda row: row["last_event"], reverse=True)


def prune_logs(keep_days: float, max_total_mb: float, in_use: Optional[Path] = None, directory: Optional[Path] = None) -> int:
    """Delete the logs of projects not written for `keep_days`, then the oldest until the folder is
    under `max_total_mb`. The log in use stays. Returns how many logs were removed."""
    directory = directory or default_trace_dir()
    keep_name = Path(in_use).name if in_use else None
    projects = []
    for name, files in _project_files(directory).items():
        info = []
        for path in files:
            try:
                info.append((path, os.stat(str(path))))
            except OSError:
                continue
        if info:
            projects.append((name, max(i.st_mtime for _, i in info), sum(i.st_size for _, i in info), [p for p, _ in info]))
    removed = 0

    def delete(project: tuple) -> None:
        nonlocal removed
        for path in project[3]:
            try:
                os.unlink(str(path))
            except OSError:
                pass
        removed += 1

    cutoff = time.time() - keep_days * 86400.0
    survivors = []
    for project in projects:
        if project[0] != keep_name and project[1] < cutoff:
            delete(project)
        else:
            survivors.append(project)
    total = sum(project[2] for project in survivors)
    for project in sorted(survivors, key=lambda item: item[1]):
        if total <= max_total_mb * 1024 * 1024:
            break
        if project[0] == keep_name:
            continue
        delete(project)
        total -= project[2]
    return removed


def bind(db_path: Path) -> None:
    """Write to the log of the project whose database is `db_path`."""
    global _path, _db_file
    _path = log_file_for(db_path)
    _db_file = Path(db_path)


def configure(chosen: str, slow_ms: float, max_mb: float) -> None:
    """Apply a level read from settings (the worker and the embedding process)."""
    global _level, _slow_ms, _max_bytes
    if chosen not in LEVELS:
        chosen = "off"
    if chosen != _level:
        _reset_failures()
    _level = chosen
    _slow_ms = float(slow_ms)
    _max_bytes = max(1, int(float(max_mb) * 1024 * 1024))


def configure_from_settings(settings: Dict[str, Any]) -> None:
    configure(settings.get("trace.level", "off"), settings.get("trace.slow_ms", DEFAULT_SLOW_MS), settings.get("trace.max_mb", DEFAULT_MAX_MB))


def apply_settings_now(settings: Dict[str, Any]) -> None:
    """The worker applies a changed trace setting at once instead of on its next pass."""
    if _proc == "worker":
        configure_from_settings(settings)


def worker_report() -> Optional[Dict[str, Any]]:
    """What the worker attaches to a reply while tracing is on."""
    if _level == "off":
        return None
    return {"level": _level, "slow_ms": _slow_ms, "max_mb": _max_bytes / (1024 * 1024)}


def observe_worker(report: Optional[Dict[str, Any]]) -> None:
    """Follow the worker's level, unless a flag or the environment already set it."""
    global _level, _slow_ms, _max_bytes
    if _pinned or _proc in ("worker", "embed"):
        return
    if not report:
        _level = "off"
        return
    chosen = report.get("level")
    if chosen not in LEVELS:
        return
    if chosen != _level:
        _reset_failures()
    _level = chosen
    _slow_ms = float(report.get("slow_ms", _slow_ms))
    _max_bytes = max(1, int(float(report.get("max_mb", _max_bytes / (1024 * 1024))) * 1024 * 1024))


# --- per-command totals -----------------------------------------------------


def add(name: str, value: float) -> None:
    """Add to a running total reported by the next `command` event."""
    _totals[name] = _totals.get(name, 0.0) + value


_result: Dict[str, Any] = {}


def set_result(status: str, out_bytes: Optional[int]) -> None:
    """How the command ended and how much it printed, for its `command` event."""
    _result["status"] = status
    _result["out_bytes"] = out_bytes


def take_result() -> Dict[str, Any]:
    taken = dict(_result)
    _result.clear()
    return taken


def take_totals() -> Dict[str, float]:
    taken = dict(_totals)
    _totals.clear()
    return taken


# --- notes about the request being served (worker side) ---------------------


def note(**fields: Any) -> None:
    """Attach facts to the request this thread is serving."""
    notes = getattr(_local, "notes", None)
    if notes is not None:
        notes.update(fields)


def begin_notes() -> None:
    _local.notes = {}


def end_notes() -> Dict[str, Any]:
    notes = getattr(_local, "notes", None) or {}
    _local.notes = None
    return notes


# --- writing ---------------------------------------------------------------


def code_of(text: str) -> str:
    """A short code for a phrase (`machine busy` -> `machine_busy`), for events that must not carry free text."""
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")[:39] or "unknown"


def sql_shape(sql: str) -> str:
    """The start of `sql` with quoted literals masked, so no value is recorded."""
    flat = " ".join(sql.split())
    return _QUOTED.sub("'?'", flat)[:SQL_PREFIX_CHARS]


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


def _reset_failures() -> None:
    global _failures, _blocked_until
    _failures = 0
    _blocked_until = 0.0


def _note_failure() -> None:
    global _failures, _blocked_until
    _failures += 1
    _blocked_until = time.monotonic() + min(MAX_RETRY_SECONDS, FIRST_RETRY_SECONDS * 2 ** (_failures - 1))


def _rotate(incoming: int) -> None:
    """Move the full file to `trace.log.1`. Rotations are serialised across
    processes by a lock file, and the size is checked again once the lock is
    held: another process may have rotated while this one waited."""
    lock = None
    try:
        if fcntl is not None:
            lock = os.open(str(_path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            size = os.stat(str(_path)).st_size
        except FileNotFoundError:
            return
        if size > 0 and size + incoming > _max_bytes:
            try:
                os.replace(str(_path), str(_path) + ".1")
            except FileNotFoundError:
                pass  # someone else rotated it
    finally:
        if lock is not None:
            os.close(lock)  # also releases the lock


def _write(data: bytes) -> None:
    if _path is None or _dir_state is False:
        return
    if _dir_state is None and ensure_trace_dir(_path.parent) is None:
        return
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    fd: Optional[int] = os.open(str(_path), flags, 0o600)
    try:
        size = os.fstat(fd).st_size
        if size > 0 and size + len(data) > _max_bytes:
            os.close(fd)
            fd = None  # so the `finally` below can never close it a second time
            _rotate(len(data))
            fd = os.open(str(_path), flags, 0o600)
        os.write(fd, data)
    finally:
        if fd is not None:
            os.close(fd)


# --- what may be written -----------------------------------------------------

_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
_COMMAND = re.compile(r"^[a-z][a-z0-9_\- ]{0,59}$")
_VERSION = re.compile(r"^[0-9A-Za-z.+_\-]{1,60}$")
_OPTION = re.compile(r"^--?[a-z][a-z0-9\-]{0,30}$")
_SETTING_NAME = re.compile(r"^[a-z]+\.[a-z_]+$")

#: Field name -> the check its value must pass. Numbers and booleans are accepted wherever the
#: name is listed as numeric; text only where a pattern says what it may look like. Anything else,
#: whatever the call site passed, is dropped and counted in `dropped_fields`.
_NUMERIC = frozenset(
    """out_bytes connect_ms ipc_ms ipc_calls max_rss_mb cpu_s rss_mb db_mb wal_mb load1 mem_avail_mb lock_ms run_ms rows
    functions index removal_ms documents sections links checkpoint_ms copy_ms sync_ms commit_ms fsync_ms replace_ms detect_ms discover_ms rescan_ms embed_ms
    store_ms items stored unpersisted embedded round owed wait_s failures code sleep_s queued_before drifted discovered batch
    remediated queued_after child_pid gap_ms pattern_length parse_ms files skipped
    read_during_publish deferred_write deferred_embeddings cursor_ms exec_ms fetch_ms""".split()
)
_FLAGS = frozenset("ok worker_started published".split())
_CODES = {
    "kind": _CODE, "reason": _CODE, "status": _CODE, "op": _CODE, "pause_reason": _CODE, "pattern_mode": _CODE,
    "command": _COMMAND, "sql": re.compile(r"^[^\n]{0,160}$"), "worker_build": _VERSION, "client_build": _VERSION,
    "error_class": re.compile(r"^[A-Za-z_.]{1,60}$"),
}


def _clean(name: str, value: Any) -> Any:
    """The value to write for field `name`, or `_DROP`."""
    if name in _NUMERIC:
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else _DROP
    if name in _FLAGS:
        return value if isinstance(value, bool) else _DROP
    if name in _CODES:
        return value if isinstance(value, str) and _CODES[name].match(value) else _DROP
    if name == "args":
        if isinstance(value, (list, tuple)) and all(isinstance(item, str) and _OPTION.match(item) for item in value):
            return list(value)
        return _DROP
    if name == "settings":
        if isinstance(value, dict) and all(isinstance(k, str) and _SETTING_NAME.match(k) and isinstance(v, (int, float)) for k, v in value.items()):
            return value
        return _DROP
    return _DROP


_DROP = object()


def _sanitise(fields: Dict[str, Any]) -> tuple:
    kept: Dict[str, Any] = {}
    dropped = 0
    for name, value in fields.items():
        if value is None:
            continue
        cleaned = _clean(name, value)
        if cleaned is _DROP:
            dropped += 1
        else:
            kept[name] = cleaned
    return kept, dropped


def emit(event: str, ms: float = 0.0, **fields: Any) -> None:
    """Record one event. At level `slow` only events of `slow_ms` or more are kept."""
    global _failures
    if _level == "off" or (_blocked_until and time.monotonic() < _blocked_until):
        return
    try:
        ms = round(float(ms), 3)
        if _level == "slow" and ms < _slow_ms:
            return
        record: Dict[str, Any] = {"t": _now(), "pid": os.getpid(), "proc": _proc, "event": event, "ms": ms}
        cid = fields.pop("cid", None) or getattr(_local, "cid", None)
        if cid:
            record["cid"] = cid
        kept, dropped = _sanitise(fields)
        record.update(kept)
        if dropped:
            record["dropped_fields"] = dropped
        _write((json.dumps(record, separators=(",", ":"), default=str) + "\n").encode("utf-8"))
        if _failures:
            _failures = 0
    except Exception:  # noqa: BLE001 - tracing must never affect a command
        _note_failure()


@contextlib.contextmanager
def span(event: str, **fields: Any) -> Iterator[Dict[str, Any]]:
    """Time a block and emit it as `event`; the block may add fields to the yielded dict."""
    extra: Dict[str, Any] = dict(fields)
    if _level == "off":
        yield extra
        return
    started = time.perf_counter()
    try:
        yield extra
    finally:
        emit(event, (time.perf_counter() - started) * 1000.0, **extra)


# --- memory and CPU ----------------------------------------------------------


def process_usage() -> Dict[str, float]:
    """Peak resident memory and CPU seconds (user plus system) of this process so far."""
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
    except Exception:  # noqa: BLE001 - no resource module: report nothing
        return {}
    peak = usage.ru_maxrss / (1024.0 * 1024.0 if sys.platform == "darwin" else 1024.0)  # bytes on macOS, KiB elsewhere
    return {"max_rss_mb": round(peak, 1), "cpu_s": round(usage.ru_utime + usage.ru_stime, 3)}


def _resident_mb() -> Optional[float]:
    try:
        with open("/proc/self/statm") as handle:
            pages = int(handle.read().split()[1])
        return round(pages * os.sysconf("SC_PAGE_SIZE") / (1024.0 * 1024.0), 1)
    except (OSError, ValueError, IndexError):
        return None


def _available_mb() -> Optional[float]:
    try:
        with open("/proc/meminfo") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return round(int(line.split()[1]) / 1024.0, 1)
    except (OSError, ValueError, IndexError):
        pass
    return None


def _file_mb(path: Optional[Path]) -> Optional[float]:
    if path is None:
        return None
    try:
        return round(os.stat(str(path)).st_size / (1024.0 * 1024.0), 2)
    except OSError:
        return None


def system_snapshot() -> Dict[str, Any]:
    """The machine as it is now: this process's resident memory, the database and
    its write-ahead log, the 1-minute load average and the memory still available.
    Fields that cannot be read are left out."""
    snapshot: Dict[str, Any] = {"rss_mb": _resident_mb(), "mem_avail_mb": _available_mb()}
    snapshot["db_mb"] = _file_mb(_db_file)
    snapshot["wal_mb"] = _file_mb(Path(str(_db_file) + ".wal") if _db_file is not None else None)
    try:
        snapshot["load1"] = round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        pass
    return {key: value for key, value in snapshot.items() if value is not None}


def reset_for_tests() -> None:
    global _dir_state, _level, _slow_ms, _max_bytes, _path, _db_file, _proc, _pinned, _process_cid
    _db_file = None
    _dir_state = None
    _level = "off"
    _slow_ms = DEFAULT_SLOW_MS
    _max_bytes = int(DEFAULT_MAX_MB * 1024 * 1024)
    _path = None
    _proc = "cli"
    _pinned = False
    _reset_failures()
    _process_cid = None
    _totals.clear()
    _result.clear()
    _local.cid = None
    _local.notes = None
