"""Taking a function identifier apart.

An identifier is `<file>::<qualified name>`, optionally followed by `#` and eight
hexadecimal characters that distinguish same-named definitions of one file (see
`function_id` in the scanner). Code that needs only the readable parts goes through
here instead of splitting the string itself.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Optional, Sequence, Tuple

_HASH_SUFFIX = re.compile(r"^(?P<head>.*)#(?P<hash>[0-9a-f]{8})$", re.DOTALL)


def split_function_id(function_id: str) -> Tuple[str, str, Optional[str]]:
    """`(file, qualified_name, parameter_hash)`; the hash is `None` for an identifier without one.
    An identifier with no `::` has an empty file."""
    head = function_id
    parameter_hash: Optional[str] = None
    match = _HASH_SUFFIX.match(function_id)
    if match:
        head, parameter_hash = match.group("head"), match.group("hash")
    file_path, separator, name = head.partition("::")
    if not separator:
        return "", file_path, parameter_hash
    return file_path, name, parameter_hash


def qualified_name(function_id: str) -> str:
    """The qualified name of a function: `Class.method`, `helper`, `Outer::Inner::f`."""
    return split_function_id(function_id)[1]


#: Part of the text the hash is taken from; a deliberate change to how parameters are
#: normalised bumps it, and every identifier is then migrated once.
HASH_VERSION = "v1"
HASH_LENGTH = 8


def parameter_hash(parameter_tokens: Sequence[str]) -> str:
    """Eight hexadecimal characters of a stable hash of the parameter tokens, in order.
    A function with no parameters has the hash of the empty list."""
    canonical = json.dumps([HASH_VERSION, *parameter_tokens], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:HASH_LENGTH]


def function_identity(file_path: str, qualified_name: str, parameter_tokens: Sequence[str]) -> str:
    """`<file>::<qualified name>#<hash of the parameters>`, the identifier of a function.

    The one place identifiers are built; the scanner calls it for every language."""
    return f"{file_path}::{qualified_name}#{parameter_hash(parameter_tokens)}"


def display_name(function_id: str, function_name: Optional[str] = None) -> str:
    """The short, readable name of a function: its stored name when there is one, else the
    last segment of the qualified name, without the file and the hash."""
    if function_name:
        return function_name
    name = qualified_name(function_id)
    for separator in ("::", "."):
        name = name.rsplit(separator, 1)[-1]
    return name
