"""How the per-file record (`scan:file:<path>` in `config`) is stored: packed, and read back to what it was.

A record holds, for each function of a file, what a later resolution needs (its calls and reads, its owner and flags).
It is long text, and DuckDB does not compress long text: on Django (2,943 files, 33,000 functions) the records are 45.6 MB
of text and took 81 MB of the database file. A record is now written as `z1:` and the base64 of its zlib-compressed JSON,
with the parts that repeat what is already in the record written once (the file path in each identifier, a call as a short
list, a read whose text is its own name): 7.1 MB of text and about 9 MB of file for the same records.

`unpack` accepts both forms, the packed one and the plain JSON an older database holds, and gives the same dictionary
either way, so what reads a record does not care which it is.
"""

from __future__ import annotations

import base64
import json
import zlib
from typing import Any, Dict, List

PREFIX = "z1:"
_LEVEL = 6  # (level 1 is 30% larger, 9 is no smaller and takes twice as long)


def is_packed(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def pack(snapshot: Dict[str, Any], file_path: str) -> str:
    """The stored text of the record of `file_path`."""
    slim = dict(snapshot)
    slim["functions"] = [_slim(row, file_path) for row in snapshot.get("functions", [])]
    raw = json.dumps(slim, separators=(",", ":")).encode("utf-8")
    return PREFIX + base64.b64encode(zlib.compress(raw, _LEVEL)).decode("ascii")


def unpack(value: str, file_path: str) -> Dict[str, Any]:
    """The record, whichever form it was stored in."""
    if not is_packed(value):
        return json.loads(value)
    snapshot = json.loads(zlib.decompress(base64.b64decode(value[len(PREFIX):])).decode("utf-8"))
    snapshot["functions"] = [_full(row, file_path) for row in snapshot.get("functions", [])]
    return snapshot


def _slim(row: Dict[str, Any], file_path: str) -> Dict[str, Any]:
    row = dict(row)
    function_id = row.get("function_id")
    if isinstance(function_id, str) and function_id.startswith(file_path + "::"):
        row["i"] = function_id[len(file_path) + 2 :]  # (the key of the record is the path)
        del row["function_id"]
    if row.get("calls"):
        row["calls"] = [_call_list(call) for call in row["calls"]]
    if row.get("property_reads"):
        row["property_reads"] = [_read_list(read) for read in row["property_reads"]]
    if row.get("symbol_reads"):
        row["symbol_reads"] = [_symbol(read) for read in row["symbol_reads"]]
    return row


def _full(row: Dict[str, Any], file_path: str) -> Dict[str, Any]:
    if "i" in row:
        row["function_id"] = f"{file_path}::{row.pop('i')}"
    if row.get("calls"):
        row["calls"] = [_call_dict(call) for call in row["calls"]]
    if row.get("property_reads"):
        row["property_reads"] = [_read_dict(read) for read in row["property_reads"]]
    if row.get("symbol_reads"):
        row["symbol_reads"] = [_symbol_dict(read) for read in row["symbol_reads"]]
    return row


def _call_list(call: Any) -> Any:
    if isinstance(call, dict) and set(call) == {"receiver", "name", "text", "kind"}:
        return [call["receiver"], call["name"], call["text"], call["kind"]]
    return call


def _call_dict(call: Any) -> Any:
    if isinstance(call, list):
        return {"receiver": call[0], "name": call[1], "text": call[2], "kind": call[3]}
    return call


def _read_list(read: Any) -> Any:
    if isinstance(read, dict) and set(read) == {"receiver", "name", "text"}:
        own = f"{read['receiver']}.{read['name']}"
        return [read["receiver"], read["name"], None if read["text"] == own else read["text"]]
    return read


def _read_dict(read: Any) -> Any:
    if isinstance(read, list):
        return {"receiver": read[0], "name": read[1], "text": f"{read[0]}.{read[1]}" if read[2] is None else read[2]}
    return read


def _symbol(read: Any) -> Any:
    if isinstance(read, dict) and set(read) == {"name", "text"}:
        return read["name"] if read["text"] == read["name"] else [read["name"], read["text"]]
    return read


def _symbol_dict(read: Any) -> Any:
    if isinstance(read, str):
        return {"name": read, "text": read}
    if isinstance(read, list):
        return {"name": read[0], "text": read[1]}
    return read


def packed_functions(value: str, file_path: str) -> List[Dict[str, Any]]:
    """The function rows of a record (empty when it has none)."""
    return unpack(value, file_path).get("functions", [])
