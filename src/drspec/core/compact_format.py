"""The compact form of a tool result (`format: "compact"`): the same data as the text form without what the reader can rebuild.

Chosen from the measurements of `scripts/bench_output_format.py` (about 37% fewer characters over search, deps_get and unused
results; the only thing lost is the part of a signature past 80 characters). Only the encoder is here: the decoder that the tests
use to show nothing else is lost lives in the benchmark script.

One result:

    ~B                                  the marker (`legend()` says how to read the rest; the MCP server sends it once)
    key: value                          a field of the result (raw text, or JSON when raw text would read as something else)
    key: {...}                          anything that is not one of the lists below, as compact JSON
    # key field field ...               a list of function records, with the fields their records have, in order
    >src/drspec/core/                   the directory of the files below it, when it is not the last group's
     graph_sync.py                      a file (a group)
      name#hash<TAB>s=def f(x)<TAB>d=2  a function of the file: its id without the file, then the fields that are not a default
    ^ key                               a list of line-search hits: `line<TAB>function<TAB>text` under their file
    % key                               a list of line-search blocks: `@a-b<TAB>matched lines<TAB>functions`, then each line of it
    & key                               who reads and writes a member: owners, their files, their functions, their accesses
    = key field field ...               any other list of records with the same fields: `|cell<TAB>cell` rows
    { key ... }                         a field that holds lists of records: its own fields and lists, written the same way
    ~J                                  (first line) a result of a shape none of these describes: the JSON of the whole result

A list of records may name a field once for all of its records (`<TAB>reason=text` after the field list), leave a field out of a record
(`!k`: that record has no such key) and carry one list of small records under each record (`field[a,b,c]`, one row of three spaces and the
cells for each).
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from drspec.core.index_notice import NOTE as NOTE_TEMPLATE, UNSCANNED_HINT, UNUSED_CAVEAT

MARK = "~B"
JSON_MARK = "~J"
CLIP = 80
CLIP_MARK = "…"
NOTE_CODE = "~I"
#: Long texts the tools repeat word for word; a field that holds exactly one of them is written as its code.
CONSTANTS = {"~1": UNSCANNED_HINT, "~2": UNUSED_CAVEAT}
#: A field with one of these values is not written; the reader puts it back.
DEFAULTS = {"status": "PENDING", "possibly_stale": False, "has_contract": False, "only_test_callers": False, "relation": "calls", "confidence": "EXTRACTED"}
SHORT = {"signature": "s", "status": "st", "possibly_stale": "p", "has_contract": "c", "depth": "d", "relation": "r", "confidence": "cf", "function_name": "n", "only_test_callers": "o"}
LONG = {value: key for key, value in SHORT.items()}
RECORD_REQUIRED = ("function_id", "file_path")
BLOCK_KEYS = ["file_path", "start_line", "end_line", "matched_lines", "functions", "context"]
HIT_KEYS = ["file_path", "line", "text", "function_id"]
OWNER_KEYS = ["owner_class", "accesses", "files"]
FILE_KEYS = ["file_path", "functions"]
FUNCTION_KEYS = ["function_id", "function_name", "accesses"]
ACCESS_KEYS = ["kind", "line", "site", "source"]
ACCESS_EXTRA_KEYS = ("before", "after", "written_through")


def legend() -> str:
    """How to read a compact result and how to get a function id back; sent once in the MCP server's instructions."""
    keys = " ".join(f"{short}={long}" for long, short in SHORT.items())
    defaults = " ".join(f"{key}={json.dumps(value)}" for key, value in DEFAULTS.items())
    return (
        'Output format: `text` is the default; `format: "compact"` is opt-in, about a third fewer characters. A compact result starts with '
        f"`{MARK}` (or `{JSON_MARK}` and the JSON of the whole result); one without it is text (sent when that is shorter). "
        "`key: value` a field (a value starting `{` or `[` is JSON); "
        f"`note: {NOTE_CODE}` the index note; `~1` searched files not in the graph yet; `~2` `unused` is a candidate list, not proof. "
        "`# key f1 f2..` function records with those fields; `>dir/` a directory of the files below; ` file` a file; "
        "`  name#hash<TAB>k=v..` a function. THE FUNCTION ID for `source_get` or `deps_get` is the group's directory and file, `::`, "
        "then `name#hash`: `src/a/b.py::name#hash`. Unwritten fields have their default "
        f"({defaults}; function_name is the name's last part). Keys: {keys}. A signature past {CLIP} characters ends with `{CLIP_MARK}`. "
        "`^ key` hits: `line<TAB>function<TAB>text` (`=` the row above's function, empty none, `!id` another file's). "
        "`% key` blocks: `@a-b<TAB>matched lines<TAB>functions`, then lines a..b, `:` matched, `-` context. "
        "`& key` owners: `*Owner<TAB>count`, files, functions, then `line<TAB>kind<TAB>site<TAB>source`. `= key f1 f2..` other records: "
        "`|cell<TAB>cell` rows (`~` a missing field; `<TAB>f=v` gives every row that value). `{ key` .. `}` holds the lists of a field. "
        "In a record `!k` means no `k`; `f[a,b]` means rows of three spaces and cells a,b under each record. "
        "`source_get` is never compacted."
    )


class NotEncodable(Exception):
    """The result has a shape the compact form does not describe: it is written as JSON instead."""


class _Plain:
    """How a directory and a name are written. A reader of the benchmark (`scripts/bench_output_format.py`) may put aliases here."""

    def dir(self, directory: str) -> str:
        return directory

    def name(self, name: str) -> str:
        return name


def clip_signature(text: str) -> str:
    return text if len(text) <= CLIP else text[: CLIP - 1] + CLIP_MARK


def clipped(payload: Any) -> Any:
    """The payload as the compact form holds it: every signature clipped at 80 characters."""
    if isinstance(payload, dict):
        return {key: (clip_signature(value) if key == "signature" and isinstance(value, str) else clipped(value)) for key, value in payload.items()}
    if isinstance(payload, list):
        return [clipped(item) for item in payload]
    return payload


def name_of(tail: str) -> str:
    """The function name a record leaves out: the last part of the name in its id."""
    return re.split(r"[.:]+", tail.rsplit("#", 1)[0])[-1]


def encode(payload: dict, clip: bool = True, plan: Optional[Any] = None) -> str:
    """The compact form of a result; a shape it does not describe is written as `~J` and compact JSON."""
    payload = clipped(payload) if clip else payload
    try:
        return _encode(payload, plan or _Plain())
    except NotEncodable:
        return JSON_MARK + "\n" + json.dumps(payload, separators=(",", ":"))


def _write(value: Any) -> str:
    """A field value as text: raw when that reads back as the same string, JSON otherwise."""
    if isinstance(value, str):
        if "\t" in value or "\n" in value or "\r" in value or value.startswith("~"):  # (a lone `~` in a table row is a missing field)
            return json.dumps(value)
        try:
            json.loads(value)
        except ValueError:
            return value
        return json.dumps(value)
    return json.dumps(value, separators=(",", ":"))


def _swap(value: Any, table: dict) -> Any:
    if isinstance(value, dict):
        return {key: _swap(item, table) for key, item in value.items()}
    if isinstance(value, list):
        return [_swap(item, table) for item in value]
    return table.get(value, value) if isinstance(value, str) else value


def _mentions_code(value: Any) -> bool:
    """Whether a value already holds a string that would be read as a code."""
    if isinstance(value, dict):
        return any(_mentions_code(item) for item in value.values())
    if isinstance(value, list):
        return any(_mentions_code(item) for item in value)
    return isinstance(value, str) and (value == NOTE_CODE or value in CONSTANTS)


def note_of(index: dict) -> Optional[str]:
    try:
        return NOTE_TEMPLATE.format(**index)
    except (KeyError, IndexError, ValueError):
        return None


def split_path(path: str) -> tuple:
    directory, _, file = path.rpartition("/")
    return (directory + "/" if directory else ""), file


def _is_records(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(item, dict) and all(isinstance(item.get(key), str) for key in RECORD_REQUIRED) for item in value)


def _is_blocks(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(item, dict) and list(item) == BLOCK_KEYS for item in value)


def _is_hits(value: Any) -> bool:
    def hit_ok(item: Any) -> bool:
        if not isinstance(item, dict) or list(item) != HIT_KEYS:
            return False
        if not (isinstance(item["file_path"], str) and isinstance(item["line"], int) and isinstance(item["text"], str)) or "\n" in item["text"]:
            return False
        return item["function_id"] is None or isinstance(item["function_id"], str)  # (None: a line outside any function)

    return isinstance(value, list) and bool(value) and all(hit_ok(item) for item in value)


def _is_owners(value: Any) -> bool:
    def access_ok(item: Any) -> bool:
        keys = list(item) if isinstance(item, dict) else []
        if keys[:4] != ACCESS_KEYS or any(key not in ACCESS_EXTRA_KEYS for key in keys[4:]):
            return False
        return isinstance(item["line"], int) and all(isinstance(item[key], str) and "\n" not in item[key] for key in ("kind", "site", "source"))

    def function_ok(item: Any) -> bool:
        return isinstance(item, dict) and list(item) == FUNCTION_KEYS and isinstance(item["function_id"], str) and isinstance(item["function_name"], str) \
            and isinstance(item["accesses"], list) and all(access_ok(access) for access in item["accesses"])

    def file_ok(item: Any) -> bool:
        return isinstance(item, dict) and list(item) == FILE_KEYS and isinstance(item["file_path"], str) and isinstance(item["functions"], list) and all(function_ok(f) for f in item["functions"])

    def owner_ok(item: Any) -> bool:
        return isinstance(item, dict) and list(item) == OWNER_KEYS and isinstance(item["owner_class"], str) and "\n" not in item["owner_class"] and "\t" not in item["owner_class"] \
            and isinstance(item["accesses"], int) and isinstance(item["files"], list) and all(file_ok(f) for f in item["files"])

    return isinstance(value, list) and bool(value) and all(owner_ok(item) for item in value)


def groups(records: list) -> list:
    """`[(file, [records])]` in the order the text form prints them: files by first appearance, the records of a file together."""
    order: dict = {}
    for record in records:
        order.setdefault(record["file_path"], []).append(record)
    return list(order.items())


def _encode(payload: dict, plan: Any) -> str:
    if not isinstance(payload, dict):
        raise NotEncodable
    head = [MARK]
    header = getattr(plan, "header", None)
    if header:
        head.append(header())
    return "\n".join(head + _encode_body(payload, plan, top=True))


def _encode_body(payload: dict, plan: Any, top: bool = False) -> list:
    body: list = []
    for key, value in payload.items():
        if not isinstance(key, str) or ": " in key or "\n" in key or "\t" in key or key[:1] in "#%>^&~ =|{}":
            raise NotEncodable
        if _is_records(value):
            body += _encode_records(key, value, plan)
        elif _is_blocks(value):
            body += _encode_blocks(key, value, plan)
        elif _is_hits(value):
            body += _encode_hits(key, value, plan)
        elif key == "owners" and _is_owners(value):
            body += _encode_owners(key, value, plan)
        elif _is_table(value):
            body += _encode_table(key, value)
        elif isinstance(value, dict) and _holds_lists_of_records(value):
            body += [f"{{ {key}"] + _encode_body(value, plan) + ["}"]
        else:
            if top and key == "note" and isinstance(payload.get("index"), dict) and value == note_of(payload["index"]):
                body.append(f"note: {NOTE_CODE}")
                continue
            if _mentions_code(value):
                raise NotEncodable
            swapped = _swap(value, {text: code for code, text in CONSTANTS.items()})
            if isinstance(swapped, str) and swapped in CONSTANTS:
                body.append(f"{key}: {swapped}")  # (a code is written as it is, not quoted)
            else:
                body.append(f"{key}: {_write(swapped) if not isinstance(swapped, (dict, list)) else json.dumps(swapped, separators=(',', ':'))}")
    return body


def _scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _fields_of(rows: list, ordered: bool = True) -> list:
    """The keys of the rows in the order they first appear; with `ordered`, every row's own keys must keep that order (a function
    record is read back with its keys in the order the text form prints them; the rows of a table are read back in the order of the field
    list)."""
    fields: list = []
    for row in rows:
        for name in row:
            if not isinstance(name, str) or not name or any(mark in name for mark in " \t\n[],=!"):
                raise NotEncodable
            if name not in fields:
                fields.append(name)
    if ordered and any([name for name in fields if name in row] != list(row) for row in rows):
        raise NotEncodable
    return fields


def _is_table(value: Any) -> bool:
    """A list of records that are not functions or hits: dictionaries with plain key names whose keys keep one order."""
    if not (isinstance(value, list) and value and all(isinstance(row, dict) and row for row in value)):
        return False
    try:
        _fields_of(value, ordered=False)
    except NotEncodable:
        return False
    return True


def _holds_lists_of_records(value: Any) -> bool:
    """Whether a field that is a dictionary has, in it or below it, a list that the compact form groups."""
    if not isinstance(value, dict):
        return False
    for key, item in value.items():
        if isinstance(item, list) and (_is_records(item) or _is_blocks(item) or _is_hits(item) or _is_table(item) or (key == "owners" and _is_owners(item))):
            return True
        if _holds_lists_of_records(item):
            return True
    return False


def _same(a: Any, b: Any) -> bool:
    return type(a) is type(b) and a == b


def _constant_columns(rows: list, fields: list, skip: tuple) -> dict:
    """The fields that have the same value in every one of at least two rows (`reason` of every call site): written once."""
    found: dict = {}
    if len(rows) < 2:
        return found
    for field in fields:
        if field in skip or any(field not in row for row in rows):
            continue
        first = rows[0][field]
        if _scalar(first) and all(_same(row[field], first) for row in rows):
            if field in DEFAULTS and _same(first, DEFAULTS[field]):
                continue  # (a default is not written at all)
            found[field] = first
    return found


def _cells_of(constants: dict) -> str:
    return "".join(f"\t{SHORT.get(field, field)}={_write(value)}" for field, value in constants.items())


def _encode_table(key: str, rows: list) -> list:
    fields = _fields_of(rows, ordered=False)
    constants = _constant_columns(rows, fields, ())
    lines = [f"= {key} " + " ".join(fields) + _cells_of(constants)]
    for row in rows:
        lines.append("|" + "\t".join(_cell(row[field]) if field in row else "~" for field in fields if field not in constants))
    return lines


def _cell(value: Any) -> str:
    return json.dumps(value, separators=(",", ":")) if isinstance(value, (dict, list)) else _write(value)


def _group_lines(entries: list, plan: Any, emit_group) -> list:
    lines: list = []
    last_directory = None
    for file_path, group in entries:
        directory, file = split_path(file_path)
        if "\t" in file_path or "\n" in file_path or file_path.startswith("@"):
            raise NotEncodable
        if directory != last_directory:
            lines.append(">" + plan.dir(directory))
            last_directory = directory
        lines.append(" " + file)
        lines += emit_group(file_path, group)
    return lines


def _tail(function_id: str, file_path: str) -> list:
    if not function_id.startswith(file_path + "::") or function_id.startswith("@"):
        raise NotEncodable
    return function_id[len(file_path) + 2:].rsplit("#", 1)


def _child_field(records: list, fields: list) -> Optional[tuple]:
    """`(field, keys)` of the one field that is, in every record, a list of small records with the same scalar keys."""
    for field in fields:
        if field in RECORD_REQUIRED or any(field not in record or not isinstance(record[field], list) for record in records):
            continue
        rows = [row for record in records for row in record[field]]
        if not rows or not all(isinstance(row, dict) and row for row in rows):
            continue
        keys = list(rows[0])
        if all(list(row) == keys for row in rows) and all(isinstance(name, str) and name and not any(mark in name for mark in " \t\n[],=!") for name in keys) \
                and all(_scalar(value) for row in rows for value in row.values()):
            return field, keys
    return None


def _encode_records(key: str, records: list, plan: Any) -> list:
    fields = _fields_of(records)
    child = _child_field(records, fields)
    constants = _constant_columns(records, fields, (*RECORD_REQUIRED, "function_name", child[0] if child else ""))
    tokens = [f"{field}[{','.join(child[1])}]" if child and field == child[0] else field for field in fields]
    absent_ok = set(DEFAULTS) | {"function_name"}

    def emit(file_path: str, group: list) -> list:
        lines = []
        for record in group:
            name = _tail(record["function_id"], file_path)
            tail = record["function_id"][len(file_path) + 2:]
            cells = [plan.name(name[0]) + ("#" + name[1] if len(name) == 2 else "")]
            for field in fields:
                if field in RECORD_REQUIRED or field in constants or (child and field == child[0]):
                    continue
                if field not in record:
                    if field in absent_ok:
                        cells.append("!" + SHORT.get(field, field))
                    continue
                value = record[field]
                if field == "function_name" and value == name_of(tail):
                    continue
                if field in DEFAULTS and value == DEFAULTS[field] and type(value) is type(DEFAULTS[field]):
                    continue
                cells.append(f"{SHORT.get(field, field)}={_write(value)}")
            lines.append("  " + "\t".join(cells))
            if child:
                lines += ["   " + "\t".join(_cell(row[name_]) for name_ in child[1]) for row in record[child[0]]]
        return lines

    return [f"# {key} " + " ".join(tokens) + _cells_of(constants)] + _group_lines(groups(records), plan, emit)


def _reference(function_id: Optional[str], file_path: str, plan: Any) -> str:
    if function_id is None:
        return ""
    if function_id.startswith(file_path + "::"):
        tail = function_id[len(file_path) + 2:].rsplit("#", 1)
        return plan.name(tail[0]) + ("#" + tail[1] if len(tail) == 2 else "")
    return "!" + function_id


def _encode_blocks(key: str, blocks: list, plan: Any) -> list:
    lines = [f"% {key}"]
    last_file = last_directory = None
    for block in blocks:
        context = block["context"]
        numbers = [line["line"] for line in context]
        if (
            numbers != list(range(block["start_line"], block["end_line"] + 1))
            or any(list(line) != ["line", "text"] or not isinstance(line["text"], str) or "\n" in line["text"] for line in context)
            or not set(block["matched_lines"]) <= set(numbers)
            or block["matched_lines"] != sorted(set(block["matched_lines"]))
            or any(not isinstance(function_id, str) for function_id in block["functions"])
        ):
            raise NotEncodable
        file_path = block["file_path"]
        if file_path != last_file:
            directory, file = split_path(file_path)
            if directory != last_directory:
                lines.append(">" + plan.dir(directory))
                last_directory = directory
            lines.append(" " + file)
            last_file = file_path
        references = [_reference(function_id, file_path, plan) for function_id in block["functions"]]
        lines.append(f"@{block['start_line']}-{block['end_line']}\t{','.join(map(str, block['matched_lines']))}\t{','.join(references)}")
        matched = set(block["matched_lines"])
        lines += [(":" if line["line"] in matched else "-") + line["text"] for line in context]
    return lines


def _encode_hits(key: str, hits: list, plan: Any) -> list:
    lines = [f"^ {key}"]
    last_file = last_directory = None
    last_reference = ""
    for hit in hits:
        file_path = hit["file_path"]
        if file_path != last_file:
            directory, file = split_path(file_path)
            if directory != last_directory:
                lines.append(">" + plan.dir(directory))
                last_directory = directory
            lines.append(" " + file)
            last_file, last_reference = file_path, ""
        reference = _reference(hit["function_id"], file_path, plan)
        lines.append(f"{hit['line']}\t{'=' if reference and reference == last_reference else reference}\t{hit['text']}")
        if reference:
            last_reference = reference
    return lines


def _encode_owners(key: str, owners: list, plan: Any) -> list:
    """Who reads and writes a member, grouped owner > file > function as the search results are grouped by file."""
    lines = [f"& {key}"]
    for owner in owners:
        lines.append(f"*{owner['owner_class']}\t{owner['accesses']}")
        last_directory = None
        for entry in owner["files"]:
            file_path = entry["file_path"]
            if "\t" in file_path or "\n" in file_path:
                raise NotEncodable
            directory, file = split_path(file_path)
            if directory != last_directory:
                lines.append(">" + plan.dir(directory))
                last_directory = directory
            lines.append(" " + file)
            for function in entry["functions"]:
                tail = _tail(function["function_id"], file_path)
                name = function["function_id"][len(file_path) + 2:]
                derived = name.rsplit("#", 1)[0]
                cells = [plan.name(tail[0]) + ("#" + tail[1] if len(tail) == 2 else "")]
                if function["function_name"] != derived:
                    cells.append(f"n={_write(function['function_name'])}")
                lines.append("  " + "\t".join(cells))
                for access in function["accesses"]:
                    row = [str(access["line"]), access["kind"], access["site"], access["source"]]
                    extras = {name: access[name] for name in ACCESS_EXTRA_KEYS if name in access}
                    if extras:
                        row.append(json.dumps(extras, separators=(",", ":")))
                    if any("\t" in cell for cell in row[1:4]):
                        raise NotEncodable
                    lines.append("   " + "\t".join(row))
    return lines
