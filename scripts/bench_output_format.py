"""Prototype and benchmark: shorter tool results (T-72). Nothing here is wired into the tools; the tools keep returning today's text.

The corpus is the real queries of this project's Claude Code transcripts (search in function mode and in line mode, deps_get, unused,
status), replayed in process against an index of this repository: the handlers of `drspec.mcp_server` give each query's payload and
`render_text` its text, so format A is exactly what the tools print today, whatever version printed the old transcript. No worker is
started: the handlers are given the one in-memory connection.

    python scripts/bench_output_format.py            # the table
    python scripts/bench_output_format.py --questions  # also write scripts/bench_output_format_questions.json
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))  # (the encoder is the product's)
TRANSCRIPTS = os.path.expanduser("~/.claude/projects/-media-thanh-data-Projects-drspec/*.jsonl")
TOOLS = ("search", "deps_get", "unused", "status")


def extract_queries(pattern: str = TRANSCRIPTS) -> list:
    """`[(tool, arguments)]` of every call of the four tools in the transcripts, read as streams (some files are over 100 MB)."""
    queries = []
    for path in sorted(glob.glob(pattern)):
        pending = {}
        with open(path, errors="ignore") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                content = (entry.get("message") or {}).get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use" and str(block.get("name", "")).startswith("mcp__drspec__"):
                        pending[block["id"]] = (block["name"][len("mcp__drspec__"):], block.get("input") or {})
                    elif block.get("type") == "tool_result" and block.get("tool_use_id") in pending:
                        tool, arguments = pending.pop(block["tool_use_id"])
                        if tool in TOOLS:
                            queries.append((tool, arguments))
    return queries


def build_index(root: Path = ROOT):
    """An in-memory index of the Python files of the repository (no embeddings, no worker)."""
    import duckdb

    from drspec.core.graph_sync import sync_scan_results
    from drspec.core.scanner import Scanner
    from drspec.db import init_schema
    from drspec.db.staging import publish_transaction

    connection = duckdb.connect(":memory:")
    init_schema(connection)
    scanner = Scanner()
    functions, imports, paths = [], {}, set()
    for folder in ("src", "tests", "scripts"):
        for file in sorted((root / folder).rglob("*.py")):
            relative = file.relative_to(root).as_posix()
            result = scanner.scan_file(relative, relative_to=root)
            if result is None:
                continue
            functions += result.functions
            imports.update(result.file_imports)
            paths.add(relative)
    with publish_transaction(connection) as write:
        sync_scan_results(write, functions, paths, imports)
    return connection


class _Keep:
    """The one in-memory connection, handed out for every tool call; closing it does nothing."""

    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql, params=None):
        return self._connection.execute(sql, params or [])

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._connection, name)


def replay(queries: list, connection) -> list:
    """`[{"tool", "mode", "arguments", "payload", "text"}]` for every query that succeeds on today's index."""
    from drspec import mcp_server

    mcp_server.get_worker_connection = lambda path, **kwargs: _Keep(connection)  # (the handlers reach the database through this one function)
    results = []
    seen = set()
    for tool, arguments in queries:
        arguments = {key: value for key, value in arguments.items() if key != "format"}
        key = json.dumps([tool, arguments], sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        if tool == "search" and (arguments.get("semantic") or arguments.get("count") or arguments.get("files_with_matches") or arguments.get("docs_only") or arguments.get("undocumented")):
            continue  # the modes this benchmark is about: function mode and line mode
        try:
            payload, is_error = mcp_server._TOOL_HANDLERS[tool](ROOT / "_drspec" / "contracts.db", arguments)
        except Exception:  # noqa: BLE001 - a query that no longer works on today's index is not part of the corpus
            continue
        if is_error or not isinstance(payload, dict):
            continue
        mode = "lines" if tool == "search" and ("blocks" in payload or "hits" in payload) else "functions" if tool == "search" else "-"
        results.append({"tool": tool, "mode": mode, "arguments": arguments, "payload": payload, "text": mcp_server._render_tool_result(payload, None)})
    return results


def corpus() -> list:
    os.chdir(ROOT)  # (the line search reads the live files)
    sys.path.insert(0, str(ROOT / "src"))
    return replay(extract_queries(), build_index())


# ---------------------------------------------------------------------------------------------------------------------------
# Formats B and C: B is the product's compact form (`drspec/core/compact_format.py` describes it); C is B with `~A` aliases.
# ---------------------------------------------------------------------------------------------------------------------------

# The encoder is the product's (`drspec.core.compact_format`): format B. What stays here is what only the benchmark needs: the decoder
# that shows nothing else is lost, the per-result aliases of format C and the shared dictionary of format D.
from drspec.core import compact_format as product  # noqa: E402
from drspec.core.compact_format import (  # noqa: E402
    ACCESS_EXTRA_KEYS,
    CLIP,
    CLIP_MARK,
    CONSTANTS,
    DEFAULTS,
    LONG,
    NOTE_CODE,
    NotEncodable,
    _is_blocks,
    _is_hits,
    _is_owners,
    _is_records,
    _swap,
    clip_signature,
    clipped,
    legend,
)
from drspec.core.compact_format import groups as _groups  # noqa: E402
from drspec.core.compact_format import name_of as _name_of  # noqa: E402
from drspec.core.compact_format import note_of as _note_of  # noqa: E402
from drspec.core.compact_format import split_path as _split_path  # noqa: E402

ALIAS_MIN_NAME = 40


def _value(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def canonical(payload: Any) -> Any:
    """What a decoder gives back: the payload with signatures clipped and its record lists grouped by file, as `render_text` prints them."""
    payload = clipped(payload)
    if not isinstance(payload, dict):
        return payload
    return {key: ([record for _file, group in _groups(value) for record in group] if _is_records(value) else value) for key, value in payload.items()}


class _Aliases:
    def header(self) -> str:
        return "~A " + " ".join(f"{token}={prefix}" for prefix, token in self.prefixes.items()) + (" " if self.prefixes and self.names else "") + " ".join(f"{token}={name}" for name, token in self.names.items())

    def __init__(self):
        self.prefixes: dict = {}
        self.names: dict = {}

    def dir(self, directory: str) -> str:
        for prefix, token in self.prefixes.items():
            if directory.startswith(prefix):
                return token + directory[len(prefix):]
        return directory

    def name(self, name: str) -> str:
        return self.names.get(name, name)


def _plan_aliases(payload: dict) -> _Aliases:
    """The directory prefixes and the long function names that repeat inside this one result."""
    directories, names = [], {}
    for value in payload.values():
        for item in (value if isinstance(value, list) else []):
            if not isinstance(item, dict):
                continue
            if "file_path" in item:
                directories.append(_split_path(item["file_path"])[0])
            for reference in [item.get("function_id")] if "function_id" in item else item.get("functions", []):
                if isinstance(reference, str):
                    names[_name_part(reference)] = names.get(_name_part(reference), 0) + 1
    aliases = _Aliases()
    counted: dict = {}
    for directory in set(directories):
        parts = directory.split("/")
        for count in range(1, len(parts)):
            prefix = "/".join(parts[:count]) + "/"
            counted[prefix] = counted.get(prefix, 0) + 1
    number = 0
    for prefix, uses in sorted(counted.items(), key=lambda item: -(len(item[0]) - 3) * item[1]):
        if uses >= 2 and (len(prefix) - 3) * (uses - 1) > len(prefix) + 4 and not any(prefix.startswith(done) or done.startswith(prefix) for done in aliases.prefixes):
            number += 1
            aliases.prefixes[prefix] = f"@{number}/"
    for name, uses in sorted(names.items(), key=lambda item: -len(item[0]) * item[1]):
        if len(name) > ALIAS_MIN_NAME and uses >= 2:
            number += 1
            aliases.names[name] = f"@{number}"
    return aliases


def _name_part(function_id: str) -> str:
    tail = function_id.split("::", 1)[-1]
    return tail.rsplit("#", 1)[0]


def encode(payload: dict, aliases: bool = False, clip: bool = True) -> str:
    """Format B (the product's), or C with `aliases`: the directory prefixes and long names that repeat inside the result get a
    legend line and an alias. `clip=False` keeps every signature whole."""
    plan = None
    if aliases:
        shaped = clipped(payload) if clip else payload
        found = _plan_aliases(shaped) if isinstance(shaped, dict) else None
        plan = found if found is not None and (found.prefixes or found.names) else None
    return product.encode(payload, clip=clip, plan=plan)


def decode(text: str) -> dict:
    """The payload of a B or C text (signatures as clipped)."""
    lines = text.split("\n")
    if lines[0] == "~J":
        return json.loads(lines[1])
    if lines[0] != "~B":
        raise ValueError("not a B or C result")
    prefixes, names = {}, {}
    index = 1
    if lines[index].startswith("~A "):
        for entry in lines[index][3:].split(" "):
            token, _, value = entry.partition("=")
            (prefixes if token.endswith("/") else names)[token] = value
        index += 1
    payload, _ = _decode_body(lines, index, prefixes, names, nested=False)
    if payload.get("note") == NOTE_CODE and isinstance(payload.get("index"), dict):
        payload["note"] = _note_of(payload["index"])
    return payload


def _split_cells(row: str) -> list:
    return row.split("\t")


def _decode_body(lines: list, index: int, prefixes: dict, names: dict, nested: bool) -> tuple:
    """`(dict, next index)`: the fields from `index` to the end of the text, or to the `}` that closes a section."""
    payload: dict = {}
    while index < len(lines):
        line = lines[index]
        if line == "}" and nested:
            return payload, index + 1
        if line.startswith("{ "):
            payload[line[2:]], index = _decode_body(lines, index + 1, prefixes, names, nested=True)
        elif line.startswith("# "):
            head, *constant_cells = line[2:].split("\t")
            key, *tokens = head.split(" ")
            fields, child = [], None
            for token in tokens:
                if token.endswith("]") and "[" in token:
                    name, _, keys = token[:-1].partition("[")
                    fields.append(name)
                    child = (name, keys.split(","))
                else:
                    fields.append(token)
            constants = {}
            for cell in constant_cells:
                cell_key, _, cell_value = cell.partition("=")
                constants[LONG.get(cell_key, cell_key)] = _value(cell_value)
            records: list = []
            index += 1
            directory, file_path = "", ""
            while index < len(lines) and lines[index][:1] in (">", " "):
                row = lines[index]
                if row.startswith(">"):
                    directory = _expand_directory(row[1:], prefixes)
                elif not row.startswith("  "):
                    file_path = directory + row[1:]
                elif row.startswith("   "):
                    cells = _split_cells(row[3:])
                    records[-1][child[0]].append({name: _value(cell) for name, cell in zip(child[1], cells)})
                else:
                    records.append(_decode_record(row[2:], fields, file_path, names, constants, child))
                index += 1
            payload[key] = records
        elif line.startswith("= "):
            head, *constant_cells = line[2:].split("\t")
            key, *fields = head.split(" ")
            constants = {}
            for cell in constant_cells:
                cell_key, _, cell_value = cell.partition("=")
                constants[LONG.get(cell_key, cell_key)] = _value(cell_value)
            rows: list = []
            index += 1
            while index < len(lines) and lines[index].startswith("|"):
                cells = iter(_split_cells(lines[index][1:]))
                row = {}
                for field in fields:
                    if field in constants:
                        row[field] = constants[field]
                        continue
                    cell = next(cells)
                    if cell != "~":  # (a lone `~` is a field this row does not have)
                        row[field] = _value(cell)
                rows.append(row)
                index += 1
            payload[key] = rows
        elif line.startswith("^ "):
            key = line[2:]
            hits: list = []
            index += 1
            directory, file_path, last_reference = "", "", ""
            while index < len(lines) and (lines[index][:1] in (">", " ") or lines[index][:1].isdigit()):
                row = lines[index]
                index += 1
                if row.startswith(">"):
                    directory = _expand_directory(row[1:], prefixes)
                elif row.startswith(" "):
                    file_path, last_reference = directory + row[1:], ""
                else:
                    number, reference, hit_text = row.split("\t", 2)
                    hit = {"file_path": file_path, "line": int(number), "text": hit_text, "function_id": None}
                    if reference:
                        last_reference = last_reference if reference == "=" else reference
                        hit["function_id"] = _decode_reference(last_reference, file_path, names)
                    hits.append(hit)
            payload[key] = hits
        elif line.startswith("% "):
            key = line[2:]
            blocks: list = []
            index += 1
            directory, file_path = "", ""
            while index < len(lines) and lines[index][:1] in (">", " ", "@"):
                row = lines[index]
                index += 1
                if row.startswith(">"):
                    directory = _expand_directory(row[1:], prefixes)
                elif row.startswith(" "):
                    file_path = directory + row[1:]
                else:
                    span, matched, functions = row[1:].split("\t")
                    start, end = map(int, span.split("-"))
                    marked = [int(number) for number in matched.split(",")] if matched else []
                    context = []
                    for number in range(start, end + 1):
                        context.append({"line": number, "text": lines[index][1:]})
                        index += 1
                    blocks.append({"file_path": file_path, "start_line": start, "end_line": end, "matched_lines": marked,
                                   "functions": [_decode_reference(ref, file_path, names) for ref in functions.split(",")] if functions else [], "context": context})
            payload[key] = blocks
        elif line.startswith("& "):
            key = line[2:]
            owners: list = []
            index += 1
            directory, file_path, owner, entry, function = "", "", None, None, None
            while index < len(lines) and lines[index][:1] in ("*", ">", " "):
                row = lines[index]
                index += 1
                if row.startswith("*"):
                    owner_class, count = row[1:].split("\t")
                    owner = {"owner_class": owner_class, "accesses": int(count), "files": []}
                    owners.append(owner)
                    directory = ""
                elif row.startswith(">"):
                    directory = _expand_directory(row[1:], prefixes)
                elif not row.startswith("  "):
                    file_path = directory + row[1:]
                    entry = {"file_path": file_path, "functions": []}
                    owner["files"].append(entry)
                elif not row.startswith("   "):
                    cells = row[2:].split("\t")
                    tail = _expand_name(cells[0], names)
                    name = tail.rsplit("#", 1)[0]
                    for cell in cells[1:]:
                        cell_key, _, cell_value = cell.partition("=")
                        if cell_key == "n":
                            name = _value(cell_value)
                    function = {"function_id": f"{file_path}::{tail}", "function_name": name, "accesses": []}
                    entry["functions"].append(function)
                else:
                    cells = row[3:].split("\t")
                    access = {"kind": cells[1], "line": int(cells[0]), "site": cells[2], "source": cells[3]}
                    if len(cells) > 4:
                        access.update(json.loads(cells[4]))
                    function["accesses"].append(access)
            payload[key] = owners
        else:
            key, _, value = line.partition(": ")
            payload[key] = _swap(_value(value), CONSTANTS)
            index += 1
    return payload, index


def _expand_directory(text: str, prefixes: dict) -> str:
    if text.startswith("@"):
        token, _, rest = text.partition("/")
        return prefixes[token + "/"] + rest
    return text


def _expand_name(tail: str, names: dict) -> str:
    if tail.startswith("@"):
        token, hash_mark, rest = tail.partition("#")
        return names[token] + hash_mark + rest
    return tail


def _decode_reference(reference: str, file_path: str, names: dict) -> str:
    return reference[1:] if reference.startswith("!") else f"{file_path}::{_expand_name(reference, names)}"


def _decode_record(row: str, fields: list, file_path: str, names: dict, constants: Optional[dict] = None, child: Optional[tuple] = None) -> dict:
    cells = row.split("\t")
    tail = _expand_name(cells[0], names)
    extras, absent = {}, set()
    for cell in cells[1:]:
        if cell.startswith("!") and "=" not in cell:
            absent.add(LONG.get(cell[1:], cell[1:]))
            continue
        key, _, value = cell.partition("=")
        extras[LONG.get(key, key)] = _value(value)
    record: dict = {}
    for field in fields:
        if field == "function_id":
            record[field] = f"{file_path}::{tail}"
        elif field == "file_path":
            record[field] = file_path
        elif child and field == child[0]:
            record[field] = []
        elif field in absent:
            continue
        elif field in extras:
            record[field] = extras[field]
        elif constants and field in constants:
            record[field] = constants[field]
        elif field == "function_name":
            record[field] = _name_of(tail)
        elif field in DEFAULTS:
            record[field] = DEFAULTS[field]
    return record


def function_id_of(printed: str, file_path: str, names: Optional[dict] = None) -> str:
    """The id `source_get` and `deps_get` take, from what B or C prints for a function: its `name#hash` (or the alias of the
    name, with the alias table of the result) and the file of the group it is listed under (`dir/` of the last `>` line and the ` file` line).

    What the real tools would have to accept: `file::name#hash` already works today (the `#hash` may be left out when one function of the
    file has the name), and that is what B prints once the group is known. An alias (`@2#hash`) is not an id: the agent, or the tool with
    the alias table of that result, would have to expand it. A mis-expansion breaks: a `source_get`/`deps_get` call on `@2#hash` (no such
    function), a name alias expanded with another result's table (another function, or none), the directory alias `@1/` left in a path
    given to `path_prefix`, and an id copied from a `!`-prefixed reference (a function of another file, written whole) with the `!` kept."""
    return f"{file_path}::{_expand_name(printed, names or {})}"


# ---------------------------------------------------------------------------------------------------------------------------
# Format D: today's text with ONE dictionary shared by the results that follow, built from the previous ones.
# ---------------------------------------------------------------------------------------------------------------------------

ALIAS_CHARS = "abcdefghijklmnopqrstuvwxyzABCD"  # 30 entries, each `@` and one character in the text
_CANDIDATE = re.compile(r"[A-Za-z0-9_./#:-]{8,}")


def build_dictionary(texts: list, size: int = 30) -> list:
    """The `size` strings (paths, directory prefixes, long names) that save most in `texts`, each counted against the legend line
    that costs its own length: chosen one at a time, the earlier choices already taken out of the text."""
    working = list(texts)
    chosen: list = []
    for _ in range(min(size, len(ALIAS_CHARS))):
        counts: dict = {}
        for text in working:
            for token in _CANDIDATE.findall(text):
                counts[token] = counts.get(token, 0) + 1
                if "/" in token:
                    parts = token.split("/")
                    for number in range(1, len(parts)):
                        prefix = "/".join(parts[:number]) + "/"
                        if len(prefix) >= 8:
                            counts[prefix] = counts.get(prefix, 0) + 1
        best, gain = None, 0
        for token, count in counts.items():
            value = count * (len(token) - 2) - (len(token) + 4)
            if value > gain:
                best, gain = token, value
        if best is None:
            break
        chosen.append(best)
        working = [text.replace(best, "\x00") for text in working]
    return chosen


def apply_dictionary(text: str, entries: list) -> str:
    text = text.replace("@", "@@")
    for position, entry in sorted(enumerate(entries), key=lambda item: -len(item[1])):
        text = text.replace(entry, "@" + ALIAS_CHARS[position])
    return text


def expand_dictionary(text: str, entries: list) -> str:
    table = {ALIAS_CHARS[position]: entry for position, entry in enumerate(entries)}
    return re.sub(r"@(.)", lambda found: "@" if found.group(1) == "@" else table[found.group(1)], text, flags=re.DOTALL)


def legend_of(entries: list) -> str:
    return "~D " + " ".join(f"@{ALIAS_CHARS[position]}={entry}" for position, entry in enumerate(entries)) + "\n"


def shared_dictionary(texts: list, history: int = 100, every: int = 50, size: int = 30) -> list:
    """`[(text with the dictionary applied, legend sent with it or "", entries in force)]` for each text, in order: from the
    `every`-th result on, the dictionary is rebuilt from the previous `history` results and its legend is sent with that one."""
    out, entries, legend = [], [], ""
    for position, text in enumerate(texts):
        legend = ""
        if position and position % every == 0:
            entries = build_dictionary(texts[max(0, position - history): position], size)
            legend = legend_of(entries)
        out.append((apply_dictionary(text, entries) if entries else text, legend, list(entries)))
    return out


# ---------------------------------------------------------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------------------------------------------------------

try:  # the tokenizer: tiktoken when installed, else the characters divided by 3.6 (a rough figure for English text and code)
    import tiktoken

    _encoding = tiktoken.get_encoding("cl100k_base")
    TOKENIZER = "tiktoken cl100k_base"

    def tokens(text: str) -> int:
        return len(_encoding.encode(text))
except Exception:  # noqa: BLE001
    TOKENIZER = "characters / 3.6 (tiktoken is not installed)"

    def tokens(text: str) -> int:
        return round(len(text) / 3.6)


FORMATS = ("A", "B", "B whole", "C", "D")


def render_all(results: list) -> list:
    """For each result, `{"A", "B", "B whole", "C", "D"}`: the text of each format and, for D, its legend (counted in the size)."""
    texts = [result["text"] for result in results]
    shared = shared_dictionary(texts)
    rendered = []
    for result, (dictionary_text, legend, entries) in zip(results, shared):
        payload = result["payload"]
        rendered.append({
            "A": result["text"], "B": encode(payload), "B whole": encode(payload, clip=False), "C": encode(payload, aliases=True),
            "D": legend + dictionary_text, "_entries": entries,
        })
    return rendered


def check_lossless(results: list, rendered: list) -> list:
    """The problems found (empty when every result round-trips): B and C decode to the payload with clipped signatures, and the text
    of that payload is today's text; B whole decodes to the payload itself; D expands to today's text."""
    from drspec import mcp_server

    problems = []
    for number, (result, formats) in enumerate(zip(results, rendered)):
        payload = result["payload"]
        for name in ("B", "C"):
            decoded = decode(formats[name])
            if decoded != canonical(payload):
                problems.append(f"{number}: {name} does not decode to the payload")
            elif mcp_server._render_tool_result(decoded, None) != mcp_server._render_tool_result(canonical(payload), None):
                problems.append(f"{number}: {name} does not print as the payload does")
        if decode(formats["B whole"]) != canonical_whole(payload):
            problems.append(f"{number}: B whole does not decode to the payload")
        elif mcp_server._render_tool_result(decode(formats["B whole"]), None) != result["text"]:
            problems.append(f"{number}: B whole does not print as format A")
        body = formats["D"][formats["D"].index("\n") + 1:] if formats["D"].startswith("~D ") else formats["D"]
        if (expand_dictionary(body, formats["_entries"]) if formats["_entries"] else body) != result["text"]:  # (before the first dictionary the text is A as it is)
            problems.append(f"{number}: D does not expand to format A")
    return problems


def canonical_whole(payload: Any) -> Any:
    """The payload as a decoder gives it with no clipping: record lists grouped by file, as `render_text` prints them."""
    if not isinstance(payload, dict):
        return payload
    return {key: ([record for _file, group in _groups(value) for record in group] if _is_records(value) else value) for key, value in payload.items()}


def table(results: list, rendered: list) -> str:
    rows: dict = {}
    for result, formats in zip(results, rendered):
        key = (result["tool"] + (" " + result["mode"] if result["mode"] != "-" else ""))
        row = rows.setdefault(key, {"n": 0, **{name: 0 for name in FORMATS}})
        row["n"] += 1
        for name in FORMATS:
            row[name] += len(formats[name])
    out = [f"tokenizer: {TOKENIZER}; the rows leave out the {len(legend())} characters of the legend of B, B whole and C, which the total adds once; the alias legend of C and the dictionary legend of D are inside every row", ""]
    header = f"{'tool':18}{'results':>8}" + "".join(f"{name + ' chars':>15}{'saved':>8}" for name in FORMATS)
    out += [header, "-" * len(header)]
    totals = {"n": 0, **{name: 0 for name in FORMATS}}
    for key, row in sorted(rows.items()):
        line = f"{key:18}{row['n']:>8}"
        for name in FORMATS:
            saved = "" if name == "A" else f"{100 * (row['A'] - row[name]) / row['A']:.1f}%"
            line += f"{row[name]:>15}{saved:>8}"
        out.append(line)
        for name in totals:
            totals[name] += row[name]
    for name in ("B", "B whole", "C"):
        totals[name] += len(legend())
    line = f"{'all':18}{totals['n']:>8}"
    for name in FORMATS:
        line += f"{totals[name]:>15}{'' if name == 'A' else format(100 * (totals['A'] - totals[name]) / totals['A'], '.1f') + '%':>8}"
    out += ["-" * len(header), line, ""]
    estimate = {name: sum(tokens(formats[name]) for formats in rendered) for name in FORMATS}
    out.append("estimated tokens: " + ", ".join(f"{name} {estimate[name]} ({100 * (estimate['A'] - estimate[name]) / estimate['A']:.1f}% saved)" if name != "A" else f"A {estimate['A']}" for name in FORMATS))
    return "\n".join(out)


def not_smaller(results: list, rendered: list) -> list:
    """`(position, tool, A chars, B chars, C chars)` of the results where B or C is not smaller than A."""
    return [(number, result["tool"] + " " + result["mode"], len(formats["A"]), len(formats["B"]), len(formats["C"]))
            for number, (result, formats) in enumerate(zip(results, rendered)) if len(formats["B"]) >= len(formats["A"]) or len(formats["C"]) >= len(formats["A"])]


def largest_strings(rendered: list, name: str, count: int = 10) -> list:
    """`(characters in all, times, text)` of the ten fields that hold most characters in a format (a line of A, a tab-separated cell of B and C)."""
    found: dict = {}
    for formats in rendered:
        for line in formats[name].split("\n"):
            for cell in ([line.strip()] if name in ("A", "D") else line.split("\t")):
                if cell:
                    found[cell] = found.get(cell, 0) + 1
    return sorted(((len(cell) * times, times, cell) for cell, times in found.items()), reverse=True)[:count]


def main(argv: list) -> int:
    results = corpus()
    rendered = render_all(results)
    problems = check_lossless(results, rendered)
    print(f"{len(results)} results replayed from the transcripts' queries; per tool:", dict(Counter((r["tool"], r["mode"]) for r in results)), "\n")
    print(table(results, rendered), "\n")
    worse = not_smaller(results, rendered)
    print(f"results where B or C is not smaller than A: {len(worse)} of {len(results)}", worse[:10], "\n")
    for name in ("A", "B", "C", "D"):
        print(f"ten largest strings in {name}:")
        for characters, times, cell in largest_strings(rendered, name):
            print(f"  {characters:>7} chars in {times:>4} x  {cell[:90]!r}")
        print()
    if "--questions" in argv:
        write_questions(results, rendered)
    if problems:
        print("LOSSLESS CHECK FAILED:", *problems[:20], sep="\n  ")
        return 1
    print("lossless check: every result round-trips (B, C, B whole, D)")
    return 0


QUESTIONS_FILE = ROOT / "scripts" / "bench_output_format_questions.json"


def _small(results: list, rendered: list, kind) -> list:
    """The positions of the results (shown whole in a question) of one kind, with a text of 300 to 2500 characters."""
    return [number for number, result in enumerate(results) if kind(result["payload"]) and 300 <= len(rendered[number]["A"]) <= 2500]


def _records(payload: dict, *keys: str) -> list:
    return [record for key in keys for record in payload.get(key, []) if isinstance(record, dict)]


def _short_path(path: str) -> bool:
    return not path.startswith("tests/")


def make_questions(results: list, rendered: list) -> list:
    """Twenty questions with the answer worked out from the payload (never from a text): `[(position, question, answer)]`."""
    def functions(payload):
        return _is_records(payload.get("matches"))

    def deps(payload):
        return "callers" in payload and "callees" in payload

    def hits(payload):
        return _is_hits(payload.get("hits"))

    def blocks(payload):
        return _is_blocks(payload.get("blocks"))

    def unused(payload):
        return "functions" in payload and "total_count" in payload

    out: list = []
    used: set = set()

    def take(kind, make, how_many=2):
        got = 0
        for number in _small(results, rendered, kind):
            if number in used and kind is not unused:
                continue
            made = make(results[number]["payload"])
            if made is not None:
                out.append((number, made[0], made[1]))
                used.add(number)
                got += 1
                if got == how_many:
                    return

    def in_tests(payload):
        matches = payload["matches"]
        count = sum(1 for record in matches if record["file_path"].startswith("tests/"))
        return ("How many of the matched functions are in files under tests/?", count) if 0 < count < len(matches) else None

    def most_functions(payload):
        counts = Counter(record["file_path"] for record in payload["matches"])
        top = counts.most_common(2)
        return ("Which file holds the most matched functions? Give its path.", top[0][0]) if len(top) > 1 and top[0][1] > top[1][1] else None

    def signature_of(payload):
        names = Counter(record["function_name"] for record in payload["matches"])
        for record in payload["matches"]:
            if names[record["function_name"]] == 1 and len(record["signature"]) <= CLIP and record["signature"]:
                return (f"What is the signature of the function named {record['function_name']}? Give it exactly as listed.", record["signature"])
        return None

    def stale_function(payload):
        record = payload["matches"][-1]
        return (f"Is the function {record['function_name']} (in {record['file_path']}) possibly stale? Answer yes or no.", "yes" if record["possibly_stale"] else "no")

    def same_file(payload):
        counts = Counter(record["file_path"] for record in payload["matches"])
        for record in payload["matches"]:
            if counts[record["file_path"]] >= 2:
                names = sorted(r["function_name"] for r in payload["matches"] if r["file_path"] == record["file_path"] and r is not record)
                return (f"Which other matched functions are in the same file as {record['function_name']} ({record['file_path']})? List their names, sorted.", names)
        return None

    def callers_of(payload):
        direct = sorted(record["function_name"] for record in payload["callers"] if record["depth"] == 1)
        return (f"Which functions call {payload['function_name']} directly (depth 1)? List their names, sorted.", direct) if 1 <= len(direct) <= 12 else None

    def depth_of(payload):
        entries = payload["callers"] or payload["callees"]
        names = Counter(record["function_name"] for record in entries)
        for record in entries:
            if names[record["function_name"]] == 1 and record["depth"] >= 1:
                return (f"At what depth is {record['function_name']} in the result of {payload['function_name']}? Answer with the number.", record["depth"])
        return None

    def has_contract(payload):
        entries = payload["callers"] or payload["callees"]
        record = entries[0]
        return (f"Does {record['function_name']} ({record['file_path']}) have a contract? Answer yes or no.", "yes" if record["has_contract"] else "no")

    def nth_hit(payload):
        return ("What is the line number of the 3rd hit, in the order the result lists the hits?", payload["hits"][2]["line"]) if len(payload["hits"]) >= 3 else None

    def most_hits(payload):
        counts = Counter(hit["file_path"] for hit in payload["hits"])
        top = counts.most_common(2)
        return ("Which file has the most hits? Give its path.", top[0][0]) if len(top) > 1 and top[0][1] > top[1][1] else None

    def hits_in_function(payload):
        counts = Counter(hit["function_id"] for hit in payload["hits"] if hit["function_id"])
        for function_id, count in counts.items():
            if count >= 2:
                name = function_id.split("::", 1)[1]
                return (f"Which hits are inside the function {name} (in {function_id.split('::')[0]})? List their line numbers, ascending.", sorted(hit["line"] for hit in payload["hits"] if hit["function_id"] == function_id))
        return None

    def block_matches(payload):
        return ("How many matched lines are there in all the blocks of the result?", sum(len(block["matched_lines"]) for block in payload["blocks"]))

    def first_block_function(payload):
        block = payload["blocks"][0]
        reference = block["functions"][0].split("::", 1)[1] if block["functions"] else "none"
        return (f"In which function is the first block ({block['file_path']}, lines {block['start_line']}-{block['end_line']})? Give name#hash, or none.", reference)

    def unused_count(payload):
        return ("How many functions does the result list?", len(payload["functions"]))

    def path_appears(payload):
        paths = sorted({record["file_path"] for record in payload["functions"]})
        return (f"Does the path {paths[0]} appear in the result? Answer yes or no.", "yes") if paths else None

    def path_absent(payload):
        return ("Does the path src/drspec/no_such_module.py appear in the result? Answer yes or no.", "no")

    take(functions, in_tests, 1)
    take(functions, most_functions, 1)
    take(functions, signature_of, 2)
    take(functions, stale_function, 1)
    take(functions, same_file, 1)
    take(deps, callers_of, 2)
    take(deps, depth_of, 1)
    take(deps, has_contract, 1)
    take(hits, nth_hit, 2)
    take(hits, most_hits, 1)
    take(hits, hits_in_function, 2)
    take(blocks, block_matches, 1)
    take(blocks, first_block_function, 1)
    take(unused, unused_count, 1)
    take(unused, path_appears, 1)
    take(unused, path_absent, 1)
    return out[:20]


def write_questions(results: list, rendered: list) -> None:
    questions = make_questions(results, rendered)
    shown = sorted({number for number, _question, _answer in questions})
    document = {
        "about": "Question set for the accuracy test of T-72. `result` is the position of the result in the replay; every format of it is given whole, "
                 "so a question is answered from one format at a time. `answer` is worked out from the raw data. D is today's text with the dictionary in force "
                 "(its legend first); B and C need `legend`; signatures over 80 characters are clipped in B and C, and no question asks for one.",
        "tokenizer": TOKENIZER,
        "legend": legend(),
        "results": [
            {"result": number, "tool": results[number]["tool"], "mode": results[number]["mode"], "arguments": results[number]["arguments"],
             "formats": {"A": rendered[number]["A"], "B": rendered[number]["B"], "C": rendered[number]["C"],
                         "D": legend_of(rendered[number]["_entries"]) + apply_dictionary(rendered[number]["A"], rendered[number]["_entries"]) if rendered[number]["_entries"] else rendered[number]["A"]}}
            for number in shown
        ],
        "questions": [{"n": position, "result": number, "question": question, "answer": answer} for position, (number, question, answer) in enumerate(questions, 1)],
    }
    QUESTIONS_FILE.write_text(json.dumps(document, indent=1, ensure_ascii=False) + "\n")
    print(f"{len(questions)} questions about {len(shown)} results written to {QUESTIONS_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
