"""User-adjustable settings, stored in the `config` table.

The background worker lives for the whole session, so settings are read
from the database on each pass rather than from its environment, which
it inherits once at spawn. A change applies on the worker's next pass.
Keys are stored under a `setting:` prefix so they cannot collide with the
internal state that shares the table (`scan:file:`, `embedding:`, ...).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Optional

import duckdb

from drspec.db.queries import get_config, set_config

KEY_PREFIX = "setting:"


@dataclass(frozen=True)
class Setting:
    key: str
    default: Any
    kind: type
    description: str
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    choices: Optional[tuple] = None


SETTINGS: dict[str, Setting] = {
    s.key: s
    for s in [
        Setting("worker.poll_interval", 60.0, float, "Seconds between background passes.", 1, 86400),
        Setting("worker.rescan_budget", 200, int, "Most files re-scanned in one batch (a background batch also ends after worker.pass_seconds; a scan that is being waited for uses worker.scan_budget instead).", 1, 1000),
        Setting("worker.scan_budget", 5000, int, "Most files published in one batch of a scan that is being waited for (`drspec scan`, `init --scan`). A publish costs more the more the index holds, so one large batch is several times faster than many small ones; the batch is held in memory until it is published. Measured peak of the worker and its parser processes together (resident memory summed over the process tree, sampled every 0.5 s): 1.2 GB for one batch of 4,649 files (31,000 functions); 6.0 GB for a first scan of 18,884 files and 176,850 functions at the default (3.7 GB of it the worker, which grows with what is indexed, and the rest its 12 parser processes). A smaller batch is not a memory lever: 2,000 files per batch peaked at 6.3 GB and took 62% longer.", 1, 100000),
        Setting("worker.pass_seconds", 1.0, float, "A background re-scan batch stops after this many seconds of parsing (at least one file), so a waiting request gets the worker. A scan that is being waited for is cut by worker.scan_budget alone.", 0.01, 600),
        Setting("worker.cpu_percent", 20, int, "Most of the whole machine's CPU (process CPU time over wall time and cores) the background re-scan may average.", 1, 100),
        Setting("worker.compact_idle_seconds", 600, int, "After this many seconds with nothing to do, the worker gives back the free space of the database file when more than 30% of it is free (the file is at least 32 MB). 0 turns it off.", 0, 86400),
        Setting("worker.yield_ms", 50, int, "Pause between two re-scan batches, so a waiting request is served first.", 0, 10000),
        Setting("worker.recent_days", 14.0, float, "A new file modified within this many days is indexed before older new files.", 0, 36500),
        Setting("worker.discovery_budget", 2000, int, "Files examined per pass when looking for new files.", 10, 1000000),
        Setting("worker.wake_min_gap", 5.0, float, "Minimum seconds between passes when a request wakes the worker.", 0, 3600),
        Setting("scan.workers", 0, int, "Parser processes a waiting scan uses; 0 means all cores but one. (The background drain uses as many as worker.cpu_percent of the machine allows.)", 0, 256),
        Setting("embedding.enabled", True, bool, "Embed in the background. Off also prevents the one-time model download."),
        Setting("embedding.top_n", 2000, int, "How many of the most-used nodes, edges and references get embedded.", 1, 10000000),
        Setting("embedding.half_life_days", 7.0, float, "Days for a usage score to halve.", 0.01, 3650),
        Setting("embedding.cpu_percent", 25, int, "Average share of one core the embedding process may use.", 1, 100),
        Setting("embedding.threads", 1, int, "Threads the embedding process may use (at most the number of cores).", 1, os.cpu_count() or 1),
        Setting("embedding.max_load_per_core", 1.0, float, "Pause embedding while the 1-minute load average per core exceeds this.", 0.05, 100),
        Setting("embedding.min_available_memory_mb", 1500, int, "Pause embedding while available memory is below this.", 0, 10000000),
        Setting("resolve.infer_by_unique_name", True, bool, "A C++ call on a receiver whose type is not known (`x.name()`, `p->name()`) gets an INFERRED edge to the one method called `name` when exactly one method of that name is defined in the project; two or more get none. Turn off to keep only the edges the declared types give (applied at the next publish; `drspec scan --full` applies it to every file)."),
        Setting("docs.enabled", False, bool, "Index Markdown documents as searchable sections linked to the code they name. Off by default; turn it on with: drspec config set docs.enabled true, then scan."),
        Setting("docs.extensions", "md,markdown", str, "Comma-separated extensions of the document files that are indexed."),
        Setting("docs.max_file_kb", 1024, int, "Document files larger than this are skipped.", 1, 1000000),
        Setting("strings.enabled", True, bool, "Read the YAML config files of the project: their keys, and the code that reads them by name (drspec strings get). On by default; turn it off with: drspec config set strings.enabled false, then scan."),
        Setting("strings.max_file_kb", 1024, int, "YAML config files larger than this are not read.", 1, 1000000),
        Setting("docs.keywords_per_section", 12, int, "Keywords kept per document section.", 1, 100),
        Setting("docs.max_links_per_section", 50, int, "Most code links kept for one document section (the best).", 1, 10000),
        Setting("docs.link_min_score", 0.3, float, "A link below this score is not stored.", 0, 1),
        Setting("docs.common_name_limit", 8, int, "A bare name carried by more functions than this creates no link on its own.", 1, 100000),
        Setting("docs.auto_ack_on_edit", True, bool, "Editing a section clears its links that were marked as drifted, and records the edit."),
        Setting("scan.ignore", "", str, "Comma-separated folders (relative to the project) never indexed, although git tracks them, for example docs/archive. Generated build folders (CMake build trees, catkin devel and install) are left out by default without this setting; see scan.include."),
        Setting("scan.include", "", str, "Comma-separated folders indexed although .gitignore ignores them, for example generated code people read. Also the way to read a generated build folder (CMake build tree, catkin devel or install) that is left out by default."),
        Setting("usage.search_weight", 1.0, float, "Usage score added when a search returns an item.", 0, 1000),
        Setting("usage.lookup_weight", 5.0, float, "Usage score added when source_get/deps_get returns an item.", 0, 1000),
        Setting("trace.level", "off", str, "Write a trace of what drspec does to a log outside the project (see: drspec trace path): off, slow (only events at or above trace.slow_ms) or all.", choices=("off", "slow", "all")),
        Setting("trace.slow_ms", 500.0, float, "At trace.level slow, the shortest event that is written.", 0, 3600000),
        Setting("trace.max_mb", 20.0, float, "Size at which a project's trace log is renamed to <name>.log.1 (one older file is kept).", 0.01, 100000),
        Setting("trace.keep_days", 7.0, float, "Trace logs (of every project) not written for this many days are deleted when the worker starts.", 0.01, 3650),
        Setting("trace.max_total_mb", 200.0, float, "When all trace logs together exceed this, the oldest are deleted first (the one in use stays).", 1, 1000000),
    ]
}


class SettingError(ValueError):
    """An unknown key or a value that does not fit the setting."""


def _setting(key: str) -> Setting:
    try:
        return SETTINGS[key]
    except KeyError:
        raise SettingError(f"Unknown setting: {key!r}. Known: {', '.join(sorted(SETTINGS))}.") from None


def parse_value(setting: Setting, raw: Any) -> Any:
    """`raw` (a CLI string or an MCP JSON value) as the setting's type, or `SettingError`."""
    try:
        if setting.kind is bool:
            if isinstance(raw, bool):
                value: Any = raw
            elif str(raw).strip().lower() in ("true", "on", "yes", "1"):
                value = True
            elif str(raw).strip().lower() in ("false", "off", "no", "0"):
                value = False
            else:
                raise ValueError
        elif setting.kind is str:
            value = str(raw).strip().lower()
        elif setting.kind is int:
            if isinstance(raw, bool) or (isinstance(raw, float) and not raw.is_integer()):
                raise ValueError
            value = int(raw)
        else:
            if isinstance(raw, bool):
                raise ValueError
            value = float(raw)
    except (TypeError, ValueError):
        raise SettingError(f"{setting.key} expects {setting.kind.__name__}, got {raw!r}.") from None

    if setting.choices is not None and value not in setting.choices:
        raise SettingError(f"{setting.key} must be one of {', '.join(setting.choices)}, got {raw!r}.")
    if setting.kind not in (bool, str):
        if setting.minimum is not None and value < setting.minimum:
            raise SettingError(f"{setting.key} must be at least {setting.minimum:g}, got {value:g}.")
        if setting.maximum is not None and value > setting.maximum:
            raise SettingError(f"{setting.key} must be at most {setting.maximum:g}, got {value:g}.")
    return value


def get_setting(conn: duckdb.DuckDBPyConnection, key: str) -> Any:
    """The stored value, or the default if unset or no longer valid."""
    setting = _setting(key)
    stored = get_config(conn, KEY_PREFIX + key)
    if stored is None:
        return setting.default
    try:
        return parse_value(setting, json.loads(stored))
    except (SettingError, ValueError):
        return setting.default


def set_setting(conn: duckdb.DuckDBPyConnection, key: str, raw: Any) -> Any:
    """Validate and store; returns the stored value."""
    setting = _setting(key)
    value = parse_value(setting, raw)
    set_config(conn, KEY_PREFIX + key, json.dumps(value))
    return value


def reset_setting(conn: duckdb.DuckDBPyConnection, key: str) -> Any:
    setting = _setting(key)
    conn.execute("DELETE FROM config WHERE key = ?", [KEY_PREFIX + key])
    return setting.default


def list_settings(conn: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    rows = []
    for key, setting in SETTINGS.items():
        value = get_setting(conn, key)
        rows.append(
            {
                "key": key,
                "value": value,
                "default": setting.default,
                "is_default": value == setting.default,
                "description": setting.description,
            }
        )
    return rows


def read_all_settings(conn: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """Every setting as `{key: value}` from a single query."""
    stored = dict(conn.execute("SELECT key, value FROM config WHERE starts_with(key, ?)", [KEY_PREFIX]).fetchall())
    values: dict[str, Any] = {}
    for key, setting in SETTINGS.items():
        raw = stored.get(KEY_PREFIX + key)
        try:
            values[key] = setting.default if raw is None else parse_value(setting, json.loads(raw))
        except (SettingError, ValueError):
            values[key] = setting.default
    return values
