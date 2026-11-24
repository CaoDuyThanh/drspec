"""The keys of YAML config files (ROS parameters and the like), and the string literals in code that name them.

A YAML file is read like any other project file (`.gitignore`, `scan.ignore`, generated folders, `scan.include`), and its leaves are
stored as key paths joined with `/` (`avoidance/retry/count`), each with its line and its value. A key whose value is a list keeps the
list's scalars as one value. The code side is not read at scan time: `strings get` looks for the exact string literals in the stored
function bodies, so a project without YAML files pays nothing.

PyYAML reads the files (`yaml.compose_all` with the safe loader: no object is built from a tag). It is the only parser the project
needs for this; it is a small, pure dependency.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

CONFIG_SUFFIXES = (".yaml", ".yml")
#: A YAML file over this many kilobytes is not read (`strings.max_file_kb`).
DEFAULT_MAX_KB = 1024
#: The longest value kept for a key (the rest is cut and marked).
VALUE_CHARACTERS = 80
#: Merge keys (`<<: *base`) are not expanded: a key that is a merge is skipped, and the anchored mapping is read where it is written.
_MERGE_KEY = "<<"
#: What a yaml file's record and every note say when PyYAML cannot be imported (an install without it: the other features keep working).
YAML_MISSING = "PyYAML is not installed: yaml files are not read (install it with: pip install pyyaml)"


@dataclass
class ConfigKey:
    key: str
    line: int
    value: str


@dataclass
class ParsedConfig:
    """What a YAML file holds: its keys, and why it has none or some were not read (`problem`: the first syntax error, its line and
    message; `skipped`: why the file was not read at all, for example over the size limit)."""

    keys: List[ConfigKey] = field(default_factory=list)
    problem: Optional[Dict[str, Any]] = None
    skipped: Optional[str] = None


def is_config_path(path: str) -> bool:
    return str(path).lower().endswith(CONFIG_SUFFIXES)


def normalize_key(text: str) -> str:
    """The form a key and a literal are compared in: `~/` and a leading `/` are dropped and dots become `/` (`avoidance.timeout` is
    `avoidance/timeout`). A trailing `/` is kept: it asks for the sub-keys of a key."""
    text = text.strip()
    if text.startswith("~/"):
        text = text[2:]
    return text.lstrip("/").replace(".", "/")


def _clip(value: str) -> str:
    flat = " ".join(value.split())
    return flat if len(flat) <= VALUE_CHARACTERS else flat[: VALUE_CHARACTERS - 3] + "..."


def yaml_available() -> bool:
    """Whether PyYAML can be imported. The import is made here, not at the top of the module: a drspec installed without PyYAML must still
    init, scan and serve, and only the yaml files are then not read."""
    try:
        import yaml  # noqa: F401  (only to find out whether it is there)
    except ImportError:
        return False
    return True


def _scalars(node: Any, yaml: Any) -> List[str]:
    if isinstance(node, yaml.ScalarNode):
        return [node.value]
    if isinstance(node, yaml.SequenceNode):
        return [text for child in node.value for text in _scalars(child, yaml)]
    if isinstance(node, yaml.MappingNode):
        return [text for _key, child in node.value for text in _scalars(child, yaml)]
    return []


def _leaves(node: Any, prefix: str, keys: List[ConfigKey], yaml: Any) -> None:
    if not isinstance(node, yaml.MappingNode):
        return
    for key_node, value_node in node.value:
        if not isinstance(key_node, yaml.ScalarNode) or key_node.value == _MERGE_KEY:
            continue  # (a complex key, or a merge: not a name a parameter is read by)
        path = f"{prefix}/{key_node.value}" if prefix else key_node.value
        line = key_node.start_mark.line + 1
        if isinstance(value_node, yaml.MappingNode) and value_node.value:
            _leaves(value_node, path, keys, yaml)
        elif isinstance(value_node, yaml.SequenceNode):
            values = _scalars(value_node, yaml)
            keys.append(ConfigKey(normalize_key(path), line, _clip("[" + ", ".join(values) + "]")))
        elif isinstance(value_node, yaml.ScalarNode):
            keys.append(ConfigKey(normalize_key(path), line, _clip(value_node.value)))
        else:  # an empty mapping
            keys.append(ConfigKey(normalize_key(path), line, "{}"))


#: OpenCV's FileStorage writes `%YAML:1.0` as its first line (a colon, not the standard `%YAML 1.1`, which PyYAML reads).
_OPENCV_DIRECTIVE = re.compile(r"\A%YAML:[^\r\n]*")


def _without_opencv_directive(text: str) -> str:
    """The first line blanked when it is OpenCV's directive: blanks in its place, so every later line keeps its number."""
    match = _OPENCV_DIRECTIVE.match(text)
    return " " * len(match.group(0)) + text[match.end():] if match else text


def parse_config(text: str, size_kb: Optional[float] = None, max_kb: int = DEFAULT_MAX_KB) -> ParsedConfig:
    """The keys of one YAML file. A file that does not parse has no keys and a `problem`; one over `max_kb` is not read (`skipped`).
    An OpenCV `%YAML:1.0` first line is read as blank; a tag (`!!opencv-matrix`, `!foo`) is read as the node it marks: `compose` builds
    no object from it, so an unknown tag needs no constructor, and `!!python/...` builds nothing either."""
    if size_kb is not None and size_kb > max_kb:
        return ParsedConfig(skipped=f"over {max_kb} KB ({size_kb:.0f} KB): not read")
    try:
        import yaml
    except ImportError:  # (only the import is caught: a yaml file that does not parse is a problem of that file, below)
        return ParsedConfig(skipped=YAML_MISSING)
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)  # the C loader when PyYAML has it (same result, faster)
    parsed = ParsedConfig()
    try:
        for document in yaml.compose_all(_without_opencv_directive(text), Loader=loader):
            _leaves(document, "", parsed.keys, yaml)
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None) or getattr(error, "context_mark", None)
        parsed.keys = []
        parsed.problem = {"line": (mark.line + 1) if mark is not None else 1, "message": " ".join(str(getattr(error, "problem", None) or error).split())[:200]}
    return parsed


_STRING = r"\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`"
#: Comments and string literals, in the order they are met. A comment is skipped whole, so a name in one is not a literal. `#` starts a
#: comment only in Python: in C, C++, Rust and JavaScript a `#` line is a directive or an attribute, and its string is read.
_COMMENT_OR_STRING = re.compile(r"//[^\n]*|/\*.*?\*/|(?P<string>" + _STRING + ")", re.DOTALL)
_PYTHON_COMMENT_OR_STRING = re.compile(r"#[^\n]*|(?P<string>" + _STRING + ")", re.DOTALL)
_ESCAPED = re.compile(r"\\([\\\"'`])")


def literals_in(text: str, language: str = "") -> List[Tuple[str, int]]:
    """The string literals of a code text (comments left out), each with its offset: `(value, offset)`. The escapes that stand for a quote
    or a backslash are undone; other escapes are kept as written, so `\\n` is not a key."""
    pattern = _PYTHON_COMMENT_OR_STRING if language == "python" else _COMMENT_OR_STRING
    found: List[Tuple[str, int]] = []
    for match in pattern.finditer(text):
        raw = match.group("string")
        if raw is not None:
            found.append((_ESCAPED.sub(r"\1", raw[1:-1]), match.start()))
    return found


def literal_matches(text: str, language: str, keys: Iterable[str]) -> List[Tuple[str, int]]:
    """The literals of `text` that equal one of `keys` (normalized), each with its offset: `(key, offset)`. A literal that is only part of a
    key, or a key built at run time (`"avoidance/" + name`), is not one."""
    wanted = set(keys)
    if not wanted:
        return []
    return [(normalize_key(value), offset) for value, offset in literals_in(text, language) if normalize_key(value) in wanted]


def dump_problem(problem: Optional[Dict[str, Any]]) -> Optional[str]:
    return json.dumps(problem, separators=(",", ":")) if problem else None


_CHUNK = 500


def read_config_file(path: Any, max_kb: int = DEFAULT_MAX_KB) -> ParsedConfig:
    """The keys of the YAML file at `path`: the size is checked before it is read, and a file that is not UTF-8 is a problem, not a
    failure of the scan."""
    import os

    try:
        size_kb = os.path.getsize(str(path)) / 1024.0
        if size_kb > max_kb:
            return parse_config("", size_kb, max_kb)
        with open(str(path), "r", encoding="utf-8") as stream:
            text = stream.read()
    except UnicodeDecodeError as error:
        return ParsedConfig(problem={"line": 1, "message": f"not UTF-8 text: {error.reason}"})
    except OSError as error:
        return ParsedConfig(skipped=f"could not be read: {error.strerror or error}")
    return parse_config(text, None, max_kb)


def publish_configs(conn: Any, parsed: Dict[str, ParsedConfig], gone: Iterable[str]) -> int:
    """Replace the keys of every YAML file in `parsed` and remove those of every path in `gone`, in the current transaction (the worker
    and a scan both write here, inside the publish that also writes the file's snapshot). Returns how many files were written."""
    paths = sorted(set(parsed) | set(gone))
    for start in range(0, len(paths), _CHUNK):
        part = paths[start : start + _CHUNK]
        marks = ", ".join("?" for _ in part)
        conn.execute(f"DELETE FROM config_keys WHERE file_path IN ({marks})", part)
        conn.execute(f"DELETE FROM config_files WHERE file_path IN ({marks})", part)
    rows = [(path, key.key, key.line, key.value) for path, info in parsed.items() for key in info.keys]
    _insert(conn, "config_keys", ("file_path", "key", "line", "value"), rows)
    files = [(path, len(info.keys), dump_problem(info.problem), info.skipped) for path, info in parsed.items()]
    _insert(conn, "config_files", ("file_path", "keys", "problem", "skipped"), files)
    return len(parsed)


def _insert(conn: Any, table: str, columns: Tuple[str, ...], rows: List[tuple]) -> None:
    """Rows in one multi-row INSERT per chunk: DuckDB writes a long `executemany` one row at a time, which cost most of a publish."""
    width = len(columns)
    head = f"INSERT INTO {table} ({', '.join(columns)}) VALUES "
    for start in range(0, len(rows), _CHUNK):
        part = rows[start : start + _CHUNK]
        marks = ", ".join("(" + ", ".join("?" for _ in range(width)) + ")" for _ in part)
        conn.execute(head + marks, [value for row in part for value in row])


def config_counts(conn: Any) -> Dict[str, int]:
    """`{files, keys, files_with_problems, files_skipped, files_without_pyyaml}` of the YAML files that are indexed (zeros for a project
    without any). `files_without_pyyaml`: the files a scan met while PyYAML was not installed (they are not read until it is)."""
    files, keys, problems, skipped, missing = conn.execute(
        "SELECT count(*), coalesce(sum(keys), 0), count(problem), count(skipped), count(skipped) FILTER (WHERE skipped = ?) FROM config_files",
        [YAML_MISSING],
    ).fetchone()
    return {
        "files": int(files), "keys": int(keys), "files_with_problems": int(problems), "files_skipped": int(skipped),
        "files_without_pyyaml": int(missing),
    }


def yaml_files_in_project(root: Any) -> int:
    """How many YAML files the project has (listed with the project's own rules). Used only when PyYAML is missing, to say so once."""
    from drspec.core.project_files import list_project_files
    from drspec.core.scanner import Scanner

    listing = list_project_files(root, Scanner(), (), None, count_ignored=False)
    return sum(1 for path in listing.files if is_config_path(path))


def key_sites(conn: Any, key: str, limit: int) -> Tuple[int, List[Dict[str, Any]]]:
    """Where a YAML file sets `key` (normalized): `(total, the first `limit` sites)`, by file and line."""
    total = conn.execute("SELECT count(*) FROM config_keys WHERE key = ?", [key]).fetchone()[0]
    rows = conn.execute("SELECT file_path, line, value FROM config_keys WHERE key = ? ORDER BY file_path, line LIMIT ?", [key, limit]).fetchall()
    return int(total), [{"file_path": path, "line": line, "value": value} for path, line, value in rows]


def sub_keys(conn: Any, prefix: str, limit: int) -> Tuple[int, List[Dict[str, Any]]]:
    """The keys under `prefix` (normalized, ending with `/`), grouped by the next part of the key: `(how many groups, the first `limit`
    groups as {key, keys}, where `key` is the prefix plus that part and `keys` how many keys lie under it)."""
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = conn.execute(
        "SELECT key FROM config_keys WHERE key LIKE ? ESCAPE '\\'", [escaped + "%"]
    ).fetchall()
    groups: Dict[str, int] = {}
    for (key,) in rows:
        part = key[len(prefix):].split("/", 1)[0]
        groups[prefix + part] = groups.get(prefix + part, 0) + 1
    ordered = sorted(groups.items())
    return len(ordered), [{"key": name, "keys": count} for name, count in ordered[:limit]]


def key_exists_under(conn: Any, prefix: str) -> bool:
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return conn.execute("SELECT 1 FROM config_keys WHERE key LIKE ? ESCAPE '\\' LIMIT 1", [escaped + "%"]).fetchone() is not None


def problem_files(conn: Any) -> List[Dict[str, Any]]:
    """The YAML files that did not parse, or were not read, with the reason (`problem` as a dict, or `skipped` text)."""
    found = []
    for path, problem, skipped in conn.execute("SELECT file_path, problem, skipped FROM config_files WHERE problem IS NOT NULL OR skipped IS NOT NULL ORDER BY file_path").fetchall():
        found.append({"file_path": path, "problem": json.loads(problem) if problem else None, "skipped": skipped})
    return found
