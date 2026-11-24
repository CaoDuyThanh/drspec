"""The written types of C++ code, reduced to what the call resolver needs.

A type as written (`const std::vector<game::Body>&`) is reduced to the name of the class it refers to with its template
arguments (`std::vector<game::Body>`): qualifiers, references and pointers are dropped, an array is `Body[]`. No type is
worked out: only what is written is kept, and a type that is not a plain, possibly qualified and templated, name
(`auto`, a function pointer, a built-in) is `None`.

Receivers of calls are kept as short expressions (`getSteering()`, `bodies[]`, `h.get()`, `this->ctx`), with the
arguments and indices left out; `split_chain` takes one apart.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

BUILTIN_TYPES = frozenset(
    "int long short char bool float double void unsigned signed size_t ptrdiff_t wchar_t "
    "int8_t int16_t int32_t int64_t uint8_t uint16_t uint32_t uint64_t intptr_t uintptr_t".split()
)

#: The standard containers whose element type is the first template argument.
CONTAINERS = frozenset(
    "vector array deque list set unordered_set multiset span initializer_list forward_list valarray".split()
)

#: The smart pointers whose receivers are the class they point to (`std::shared_ptr<Body>` is a `Body` to the resolver).
POINTER_WRAPPERS = frozenset({"shared_ptr", "unique_ptr", "std::shared_ptr", "std::unique_ptr"})
#: A receiver that is a call of `std::make_shared<T>()` or `std::make_unique<T>()`, as `_chain` writes it: the type is `T`.
MAKE_POINTER = re.compile(r"(?:::)?(?:std::)?make_(?:shared|unique)<(.+)>\(\)")

_NOISE = re.compile(r"\b(?:const|volatile|struct|class|union|enum|typename|static|extern|mutable|register|constexpr|inline|virtual)\b")
_NAME = r"(?:::)?[A-Za-z_]\w*"
_PLAIN = re.compile(rf"{_NAME}(?:::[A-Za-z_]\w*)*")


def split_arguments(text: str) -> List[str]:
    """The top-level, comma-separated parts of a template argument list."""
    parts: List[str] = []
    depth = 0
    current = []
    for char in text:
        if char in "<([":
            depth += 1
        elif char in ">)]":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    if current or parts:
        parts.append("".join(current))
    return parts


def _balanced(text: str) -> bool:
    depth = 0
    for char in text:
        if char == "<":
            depth += 1
        elif char == ">":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def normalize_type(text: str, array: bool = False) -> Optional[str]:
    """A type as written, reduced as described above; `None` when it is not a plain name or is a built-in type."""
    text = _NOISE.sub(" ", text).replace("&", " ").replace("*", " ")
    if text.split() and all(word in BUILTIN_TYPES for word in text.split()):
        return None  # `unsigned int`, `long long`: words of the language, not a class
    text = "".join(text.split())
    if not text or text == "auto" or "decltype" in text or "(" in text or not _balanced(text):
        return None
    opened = text.find("<")
    if opened < 0:
        head, arguments, tail = text, None, ""
    else:
        closing = _matching(text, opened)
        if closing is None:
            return None
        head, arguments, tail = text[:opened], text[opened + 1 : closing], text[closing + 1 :]
    if tail:
        return None  # a name after the template arguments (`std::vector<int>::iterator`): not a type this reads
    if not _PLAIN.fullmatch(head):
        return None
    if arguments is None and not tail and head in BUILTIN_TYPES:
        return None
    result = head + tail
    if arguments is not None:
        kept = []
        for argument in split_arguments(arguments):
            normalized = normalize_type(argument)
            kept.append(normalized if normalized is not None else (argument if re.fullmatch(r"\d+", argument) else argument.strip() or "?"))
        result = head + tail + "<" + ",".join(kept) + ">"
    return result + "[]" if array else result


def _matching(text: str, opened: int) -> Optional[int]:
    depth = 0
    for position in range(opened, len(text)):
        if text[position] == "<":
            depth += 1
        elif text[position] == ">":
            depth -= 1
            if depth == 0:
                return position
    return None


def parse_type(text: str) -> Tuple[str, List[str], bool]:
    """`(name, template arguments, is an array)` of a normalized type: `std::vector<Body>` is
    `("std::vector", ["Body"], False)` and `Body[]` is `("Body", [], True)`."""
    array = text.endswith("[]")
    if array:
        text = text[:-2]
    if text.endswith(">"):
        opened = _opening(text)
        if opened is not None:
            return text[:opened], split_arguments(text[opened + 1 : -1]), array
    return text, [], array


def _opening(text: str) -> Optional[int]:
    """The position of the `<` that the final `>` closes."""
    depth = 0
    for position in range(len(text) - 1, -1, -1):
        if text[position] == ">":
            depth += 1
        elif text[position] == "<":
            depth -= 1
            if depth == 0:
                return position
    return None


def pointee(text: str) -> str:
    """The type a smart pointer written in `text` points to, peeled off as many times as it is wrapped (`std::shared_ptr<Body>` is
    `Body`); `text` itself when it is not one. A pointer that names no type (`std::shared_ptr` alone) is returned as it is."""
    while True:
        name, args, array = parse_type(text)
        if array or len(args) != 1 or name not in POINTER_WRAPPERS:
            return text
        text = args[0]


def last_component(name: str) -> str:
    return name.rsplit("::", 1)[-1]


#: One step of a receiver expression: the name and what follows it, each `()` (a call) or `[]` (an index).
Step = Tuple[str, List[str]]


def split_chain(expression: str) -> Optional[List[Step]]:
    """A receiver expression (`h.get()`, `this->ctx`, `bodies[]`, `getSteering()`) as its steps: `[("h", []),
    ("get", ["()"])]`. `None` when the text is not made only of names, `.`, `->`, `()` and `[]`."""
    steps: List[Step] = []
    position = 0
    length = len(expression)
    while position < length:
        match = re.compile(rf"{_NAME}(?:::[A-Za-z_]\w*)*").match(expression, position)
        if match is None:
            return None
        name, position, ops = match.group(0), match.end(), []
        while position < length and expression[position] in "([":
            closer = ")" if expression[position] == "(" else "]"
            if not expression.startswith(expression[position] + closer, position):
                return None
            ops.append(expression[position] + closer)
            position += 2
        steps.append((name, ops))
        if position >= length:
            break
        if expression.startswith("->", position):
            position += 2
        elif expression[position] == ".":
            position += 1
        else:
            return None
    return steps or None
