"""Output formatting for DrSpec CLI.

Provides consistent response formatting for all CLI commands. The default
output is Graphify-style labeled plain text — the same label vocabulary
and the same record/edge rendering rules apply to every command's
response, regardless of that response's own shape. `--json` remains an
explicit opt-in for scripts/automation that need strict, stable machine
parsing.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
from datetime import datetime
from enum import Enum
from typing import Any, Optional

import typer

from drspec.core import tracing


# =============================================================================
# Error Code Catalog
# =============================================================================


class ErrorCode(str, Enum):
    """Standard error codes for DrSpec CLI responses.

    All error codes use SCREAMING_SNAKE_CASE convention.
    """

    # Initialization errors
    DB_NOT_INITIALIZED = "DB_NOT_INITIALIZED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    INIT_FAILED = "INIT_FAILED"
    PATH_NOT_FOUND = "PATH_NOT_FOUND"

    # Command errors
    SCAN_ERROR = "SCAN_ERROR"
    STATUS_ERROR = "STATUS_ERROR"
    DATABASE_BUSY = "DATABASE_BUSY"

    # Contract errors
    CONTRACT_NOT_FOUND = "CONTRACT_NOT_FOUND"
    INVALID_SCHEMA = "INVALID_SCHEMA"
    INVALID_FUNCTION_ID = "INVALID_FUNCTION_ID"

    # Function/artifact errors
    FUNCTION_NOT_FOUND = "FUNCTION_NOT_FOUND"

    # Parsing errors
    PARSE_ERROR = "PARSE_ERROR"
    UNSUPPORTED_LANGUAGE = "UNSUPPORTED_LANGUAGE"
    INVALID_JSON = "INVALID_JSON"

    # Verification errors
    VERIFICATION_FAILED = "VERIFICATION_FAILED"

    # General errors
    INTERNAL_ERROR = "INTERNAL_ERROR"
    INVALID_INPUT = "INVALID_INPUT"
    EXECUTION_ERROR = "EXECUTION_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"


# =============================================================================
# Response Builders
# =============================================================================


def success_response(data: dict[str, Any]) -> dict[str, Any]:
    """Create a success response wrapper.

    Args:
        data: Response data payload (must use snake_case keys).

    Returns:
        Standard success response wrapper.

    Example:
        >>> success_response({"function_id": "foo.py::bar", "status": "VERIFIED"})
        {
            "success": True,
            "data": {"function_id": "foo.py::bar", "status": "VERIFIED"},
            "error": None
        }
    """
    return {
        "success": True,
        "data": data,
        "error": None,
    }


def error_response(
    code: str | ErrorCode,
    message: str,
    details: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Create an error response wrapper.

    Args:
        code: Error code (SCREAMING_SNAKE_CASE).
        message: Human-readable error message.
        details: Optional additional error details.

    Returns:
        Standard error response wrapper.

    Example:
        >>> error_response("FUNCTION_NOT_FOUND", "No function with ID foo.py::bar")
        {
            "success": False,
            "data": None,
            "error": {
                "code": "FUNCTION_NOT_FOUND",
                "message": "No function with ID foo.py::bar",
                "details": {}
            }
        }
    """
    # Convert ErrorCode enum to string if needed
    code_str = code.value if isinstance(code, ErrorCode) else code
    error: dict[str, Any] = {"code": code_str, "message": message, "details": details or {}}
    if code_str in WORKER_HINT_CODES:
        error["hint"] = WORKER_STOP_HINT  # a worker still holds the database or its lock: this is what clears it

    return {"success": False, "data": None, "error": error}


#: The errors that a worker still holding the database (or its lock) causes, and the fix every one of them ends with.
WORKER_HINT_CODES = frozenset({"DATABASE_BUSY", "MIGRATION_FAILED"})
WORKER_STOP_HINT = "Stop the old worker with `drspec worker stop`, then run the command again."


# =============================================================================
# JSON Serialization
# =============================================================================


class DrSpecEncoder(json.JSONEncoder):
    """Custom JSON encoder for DrSpec types.

    Handles:
    - datetime objects (ISO format)
    - Pydantic models (.model_dump())
    - Enum values
    - Path objects
    """

    def default(self, obj: Any) -> Any:
        """Encode non-standard types."""
        # Handle datetime
        if isinstance(obj, datetime):
            return obj.isoformat()

        # Handle Pydantic models
        if hasattr(obj, "model_dump"):
            return obj.model_dump()

        # Handle Enum
        if isinstance(obj, Enum):
            return obj.value

        # Handle pathlib.Path
        if hasattr(obj, "__fspath__"):
            return str(obj)

        return super().default(obj)


def output_json(response: dict[str, Any]) -> None:
    """Output compact JSON to stdout — the `--json` opt-in's own format.

    Args:
        response: Response dictionary to output.
    """
    typer.echo(json.dumps(response, cls=DrSpecEncoder, separators=(",", ":")))


# =============================================================================
# Label Vocabulary
# =============================================================================

# Shared across every command so the same label always means the same
# underlying field everywhere — `ID` is always `function_id`, never
# `function_name` in one command and `function_id` in another. Not
# exhaustive: any key missing here falls back to `key.replace("_", " ").title()`.
_LABELS: dict[str, str] = {
    "function_id": "ID",
    "file_path": "File",
    "function_name": "Name",
    "signature": "Signature",
    "status": "Status",
    "confidence": "Confidence",
    "language": "Language",
    "possibly_stale": "Possibly Stale",
    "priority": "Priority",
    "reason": "Reason",
    "code_hash": "Hash",
    "source_hash": "Hash",
    "has_contract": "Has Contract",
    "depth": "Depth",
    "direction": "Direction",
    "pattern": "Pattern",
    "significance": "Significance",
    "commit_sha": "Commit",
}

# Picking a list-of-records block's own header value — first of these
# present in a record wins, and is never also repeated as an indented
# field under itself.
_RECORD_HEADER_FIELDS = ("function_id", "section_id", "finding_id", "id", "commit_sha", "key")

# `deps get`'s own edge-shaped lists (`core/dependency_info.py`) — rendered
# with Graphify-style arrow notation instead of generic record blocks.
# `callees` point away from the centered function, `callers` point into it.
_EDGE_LIST_ARROWS = {"callees": "outgoing", "callers": "incoming"}


_FIELD_NAME = re.compile(r"[A-Za-z0-9_]+")


def _label(key: str) -> str:
    if key in _LABELS:
        return _LABELS[key]
    # Some dict keys are data, not field names (function IDs, file paths);
    # title-casing those would corrupt an ID a caller copies back into a tool.
    if not _FIELD_NAME.fullmatch(key):
        return key
    return key.replace("_", " ").title()


def _format_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if value is None:
        return "-"
    return str(value)


def _render_fields(data: dict[str, Any], indent: int, skip: frozenset[str] = frozenset()) -> None:
    for key, value in data.items():
        if key in skip:
            continue
        if key == "unresolved_callers" and isinstance(value, dict) and "callers" in data:
            continue  # printed under Callers
        _render_field(key, value, indent)
        if key == "callers" and isinstance(data.get("unresolved_callers"), dict):
            _render_unresolved_callers(data["unresolved_callers"], indent + 1)


def _render_unresolved_callers(found: dict[str, Any], indent: int) -> None:
    """Under an empty `Callers:`: the call sites that name the function but could not be resolved to it."""
    pad = "  " * indent
    typer.echo(f"{pad}({found.get('message', 'call sites name this function but could not be resolved')})")
    for site in found.get("sites", []):
        where = f"{site['file_path']}:{site['line']}" if site.get("line") else site["file_path"]
        function = site["function_id"]
        function = function[len(site["file_path"]) + 2 :] if function.startswith(site["file_path"] + "::") else function
        reason = f"  [{site['reason']}]" if site.get("reason") else ""
        typer.echo(f"{pad}  {where}  in {function}: {site.get('call', '')}{reason}")
    if found.get("sites_omitted"):
        typer.echo(f"{pad}  ... and {found['sites_omitted']} more")


def _render_field(key: str, value: Any, indent: int) -> None:
    pad = "  " * indent
    label = _label(key)

    if key in _EDGE_LIST_ARROWS and isinstance(value, list):
        _render_edge_list(label, _EDGE_LIST_ARROWS[key], value, indent)
        return

    if key == "hits" and isinstance(value, list) and value:
        _render_hit_list(label, value, indent)
        return

    if key == "blocks" and isinstance(value, list) and value:
        _render_block_list(label, value, indent)
        return

    if key == "leaves" and isinstance(value, list) and value:
        _render_leaf_list(label, value, indent)
        return

    if key == "yaml_sites" and isinstance(value, list) and value and len({site.get("file_path") for site in value}) < len(value):
        _render_site_groups(label, value, indent)  # a yaml file that sets the key more than once: its path once, above its lines
        return

    if isinstance(value, dict):
        if not value:
            typer.echo(f"{pad}{label}: (none)")
            return
        typer.echo(f"{pad}{label}:")
        _render_fields(value, indent + 1)
        return

    if isinstance(value, list):
        if not value:
            typer.echo(f"{pad}{label}: (none)")
            return
        if _is_function_records(value) and not _is_ranked(value):
            _render_grouped_records(key, label, value, indent)
            return
        if all(isinstance(v, dict) for v in value):
            typer.echo(f"{pad}{label}:")
            for i, record in enumerate(value):
                if i:
                    typer.echo()
                _render_record(record, indent + 1)
            return
        typer.echo(f"{pad}{label}:")
        for item in value:
            typer.echo(f"{pad}  - {_format_scalar(item)}")
        return

    typer.echo(f"{pad}{label}: {_format_scalar(value)}")


def _render_record(record: dict[str, Any], indent: int) -> None:
    """One block per record — a header line naming it, then the rest of
    its fields indented underneath, mirroring Graphify's own
    `Node: want_bytes()` style."""
    header_key = next((k for k in _RECORD_HEADER_FIELDS if record.get(k) is not None), None)
    pad = "  " * indent
    if header_key is None:
        _render_fields(record, indent)
        return
    typer.echo(f"{pad}{_label(header_key)}: {record[header_key]}")
    _render_fields(record, indent + 1, skip=frozenset({header_key}))


def _identifier_key(record: dict[str, Any]) -> str:
    return "function_id" if isinstance(record.get("function_id"), str) else "section_id"


def _is_function_records(value: list) -> bool:
    """Functions, or document sections, each with the file it is in (a combined search lists both)."""
    return all(
        isinstance(v, dict)
        and (isinstance(v.get("function_id"), str) or isinstance(v.get("section_id"), str))
        and isinstance(v.get("file_path"), str)
        and v["file_path"]
        for v in value
    )


def _is_ranked(records: list) -> bool:
    """A list of functions whose order is a ranking (semantic search carries a score) stays in that order,
    each function with its file, rather than being regrouped by file. A section's score only says how well
    it matched; sections are grouped by document like functions are by file."""
    return any("score" in record for record in records if "function_id" in record)


def _id_value(record: dict[str, Any]) -> str:
    """The part of a function's identifier after its file: `Class.method#hash`. The file is
    printed once per group, and `File::<this value>` is the identifier to pass to other commands."""
    identifier, file_path = record[_identifier_key(record)], record["file_path"]
    prefix = file_path + "::"
    return identifier[len(prefix):] if identifier.startswith(prefix) else identifier


def _group_by_file(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        groups.setdefault(record["file_path"], []).append(record)
    return groups


def _group_title(file_path: str, group: list[dict[str, Any]], noun: str) -> str:
    """`File: path   (python, 3 matches)`; `noun` is the singular (`match`, `function`)."""
    languages = {r.get("language") for r in group if r.get("language")}
    language = f"{next(iter(languages))}, " if len(languages) == 1 else ""
    count = len(group)
    return f"File: {file_path}   ({language}{count} {noun if count == 1 else noun + 'es' if noun.endswith('ch') else noun + 's'})"


_rule_stated = False
"""Whether the current output has already said how to rebuild an identifier (once is enough)."""


def _state_id_rule(pad: str) -> None:
    global _rule_stated
    if not _rule_stated:
        typer.echo(f"{pad}Function ID: <File>::<ID>")
        _rule_stated = True


_GROUPED_SKIP = frozenset({"function_id", "section_id", "file_path", "function_name", "language"})


def _render_grouped_records(key: str, label: str, records: list[dict[str, Any]], indent: int) -> None:
    """Functions listed under the file they are in: the file, its language and the number of
    functions once per group, then each function by the part of its identifier that follows
    the file. Every other field stays on every function (nothing is implied by absence)."""
    pad = "  " * indent
    _state_id_rule(pad)
    typer.echo(f"{pad}{label}:")
    noun = "match" if key == "matches" else "function"
    for number, (file_path, group) in enumerate(_group_by_file(records).items()):
        if number:
            typer.echo()
        typer.echo(f"{pad}  {_group_title(file_path, group, noun)}")
        for record in group:
            typer.echo(f"{pad}    ID: {_id_value(record)}")
            _render_fields(record, indent + 3, skip=_GROUPED_SKIP)


def _render_edge_list(label: str, direction: str, edges: list[dict[str, Any]], indent: int) -> None:
    """Graphify-style arrow notation: `.verify_signature() --calls [EXTRACTED]--> want_bytes()`.
    `get_dependency_info` doesn't carry a per-edge confidence tier, so the
    bracket from that example is omitted rather than invented."""
    pad = "  " * indent
    if edges and _is_function_records(edges):
        _state_id_rule(pad)
        typer.echo(f"{pad}{label}:")
        for number, (file_path, group) in enumerate(_group_by_file(edges).items()):
            if number:
                typer.echo()
            typer.echo(f"{pad}  {_group_title(file_path, group, 'function')}")
            for edge in group:
                relation = edge.get("relation", "calls")
                arrow = f"--{relation}--> " if direction == "outgoing" else f"<--{relation}-- "
                typer.echo(f"{pad}    {arrow}{_id_value(edge)}")
                _render_fields(edge, indent + 3, skip=_GROUPED_SKIP | {"relation"})
        return
    typer.echo(f"{pad}{label}:")
    for i, edge in enumerate(edges):
        if i:
            typer.echo()
        relation = edge.get("relation", "calls")
        target = edge.get("function_id", "?")
        if direction == "outgoing":
            typer.echo(f"{pad}  --{relation}--> {target}")
        else:
            typer.echo(f"{pad}  <--{relation}-- {target}")
        _render_fields(edge, indent + 2, skip=frozenset({"relation", "function_id"}))


def _in_function(function_ids: list[str]) -> str:
    return f"  [in {', '.join(function_ids)}]" if function_ids else ""


def _clipped_note(entry: dict[str, Any]) -> str:
    """Text mode's counterpart of the `line_length` key: a clipped line says
    how long it really was."""
    length = entry.get("line_length")
    return f"  [clipped, line is {length} chars]" if length else ""


def _render_hit_list(label: str, hits: list[dict[str, Any]], indent: int) -> None:
    """`live_search` line hits, in grep's own `file:line:text` shape."""
    pad = "  " * indent
    typer.echo(f"{pad}{label}:")
    for hit in hits:
        function_id = hit.get("function_id")
        suffix = _in_function([function_id] if function_id else [])
        typer.echo(f"{pad}  {hit['file_path']}:{hit['line']}:{hit['text']}{_clipped_note(hit)}{suffix}")


def _render_site_groups(label: str, sites: list[dict[str, Any]], indent: int) -> None:
    """Sites of a key in yaml, grouped by file (only when a file repeats: a single site keeps its own block)."""
    pad = "  " * indent
    typer.echo(f"{pad}{label}:")
    groups: dict[str, list[dict[str, Any]]] = {}
    for site in sites:
        groups.setdefault(site["file_path"], []).append(site)
    for path, group in groups.items():
        typer.echo(f"{pad}  {path}" + (f" ({len(group)})" if len(group) > 1 else ""))
        for site in group:
            typer.echo(f"{pad}    {site['line']}  value {site['value']}")


def _render_leaf_list(label: str, leaves: list[dict[str, Any]], indent: int) -> None:
    """The config leaves of a prefix (`strings get <prefix>/`), one line each, grouped by their yaml file: the file is printed once above its
    leaves (with the count when there is more than one), and a reader file the line above already named is not named again."""
    pad = "  " * indent
    typer.echo(f"{pad}{label}:")
    groups: dict[str, list[dict[str, Any]]] = {}
    for leaf in leaves:
        groups.setdefault(leaf["file_path"], []).append(leaf)  # (groups in the order of their first leaf; leaves keep their order)
    for path, group in groups.items():
        typer.echo(f"{pad}  {path}" + (f" ({len(group)})" if len(group) > 1 else ""))
        previous_reader: str | None = None  # the reader file of the line above, within this group
        for leaf in group:
            reader_file = leaf.get("read_file")
            if not reader_file:
                reader = "read: none"
                previous_reader = None
            elif reader_file == previous_reader:
                reader = f"read: {leaf['read_function']}"
            else:
                reader = f"read: {reader_file} {leaf['read_function']}"
                previous_reader = reader_file
            typer.echo(f"{pad}    {leaf['line']}  {leaf['key']} = {leaf['value']}  {reader}")


def _render_block_list(label: str, blocks: list[dict[str, Any]], indent: int) -> None:
    """`live_search` context blocks, like grep -C: `:` marks a matching
    line, `-` a context line."""
    pad = "  " * indent
    typer.echo(f"{pad}{label}:")
    for i, block in enumerate(blocks):
        if i:
            typer.echo(f"{pad}  --")
        typer.echo(
            f"{pad}  {block['file_path']} (lines {block['start_line']}-{block['end_line']})"
            f"{_in_function(block.get('functions') or [])}"
        )
        matched = set(block["matched_lines"])
        width = len(str(block["end_line"]))
        for entry in block["context"]:
            separator = ":" if entry["line"] in matched else "-"
            typer.echo(f"{pad}    {entry['line']:>{width}}{separator} {entry['text']}{_clipped_note(entry)}")


# =============================================================================
# Human-Readable Output (the default)
# =============================================================================


def output_pretty(response: dict[str, Any]) -> None:
    """Output the default, Graphify-style labeled plain text.

    Args:
        response: Response dictionary to output.
    """
    global _rule_stated
    _rule_stated = False
    if response.get("success"):
        _output_success_pretty(response.get("data", {}))
    else:
        _output_error_pretty(response.get("error", {}))


def _output_success_pretty(data: dict[str, Any]) -> None:
    """Format success data for human reading."""
    if "message" in data:
        typer.echo(typer.style("✓ ", fg=typer.colors.GREEN, bold=True) + str(data["message"]))
        typer.echo()

    _render_fields(data, indent=0, skip=frozenset({"message"}))


def _output_error_pretty(error: dict[str, Any]) -> None:
    """Format error for human reading."""
    code = error.get("code", "UNKNOWN_ERROR")
    message = error.get("message", "An unknown error occurred")
    details = error.get("details", {})

    typer.echo(typer.style("✗ Error: ", fg=typer.colors.RED, bold=True) + message)
    typer.echo(typer.style(f"  Code: {code}", fg=typer.colors.YELLOW))
    if error.get("hint"):
        typer.echo(f"  Hint: {error['hint']}")

    if details:
        typer.echo("  Details:")
        _render_fields(details, indent=2)


# =============================================================================
# Output Helper
# =============================================================================


def render_text(response: dict[str, Any]) -> str:
    """Render `response` as the default Graphify-style labeled plain
    text and return it as a string instead of printing it.

    Exists for `mcp_server.py`: it needs this rendering as a string to
    put into a tool result's `content[0].text`, not written to the
    real process stdout `output_pretty` targets. Reuses `output_pretty`
    itself — capturing its `typer.echo` calls via `redirect_stdout` —
    rather than a second copy of `_render_fields`/`_render_record`/
    `_render_edge_list`.
    """
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        output_pretty(response)
    return buffer.getvalue().rstrip("\n")


def output(response: dict[str, Any], json_output: bool = False) -> None:
    """Output response in appropriate format.

    This is the main output function that should be used by commands.
    It respects the global `--json`/`--no-json` flag.

    Args:
        response: Response dictionary to output.
        json_output: If True, output compact JSON. Otherwise, output the
            default Graphify-style labeled plain text.
    """
    if tracing.enabled():
        _note_result(response, json_output)
    if json_output:
        output_json(response)
    else:
        output_pretty(response)


def _note_result(response: dict[str, Any], json_output: bool) -> None:
    """Tell the trace how the command ended and how large its output is."""
    try:
        error = response.get("error") or {}
        status = "ok" if response.get("success") else str(error.get("code", "error"))
        text = json.dumps(response) if json_output else render_text(response)
        tracing.set_result(status, len(text.encode("utf-8")))
    except Exception:  # noqa: BLE001 - tracing must never affect a command
        pass
