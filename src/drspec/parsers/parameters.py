"""The parameters of a function, as the tokens its identifier is built from.

One token per parameter, in order: the declared type (whitespace normalised) or,
for a parameter with no declared type, its name. Not included: default values,
the return type, decorators, and the implicit `self`/`cls` (Python) or `this`
(C++, TypeScript). `*args` and `**kwargs` stay as written. A C++ method's `const`,
`volatile` and reference qualifiers follow the list, since they distinguish
overloads.

Reading them from the syntax tree, rather than from the signature text, is what
keeps a multi-line parameter list or a single-line function intact.
"""

from __future__ import annotations

import re
from typing import Any, List, Optional

_PUNCTUATION = re.compile(r"\s*([\[\]<>(),*&:|.])\s*")
_SELF_NAMES = ("self", "cls")


def normalise_type(text: str) -> str:
    """Collapse whitespace and drop the spaces around punctuation, so `const char *` and
    `const char*` are the same type."""
    return _PUNCTUATION.sub(r"\1", " ".join(text.split()))


def _text(node: Any) -> str:
    return node.text.decode("utf8")


def _child(node: Any, type_name: str) -> Optional[Any]:
    for child in node.children:
        if child.type == type_name:
            return child
    return None


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------


def python_parameter_tokens(function_node: Any) -> List[str]:
    parameters = function_node.child_by_field_name("parameters") or _child(function_node, "parameters")
    if parameters is None:
        return []
    tokens: List[str] = []
    implicit = next((c for c in parameters.children if c.type not in ("(", ",", "comment")), None)
    if implicit is not None and not (implicit.type == "identifier" and _text(implicit) in _SELF_NAMES):
        implicit = None  # only a bare, untyped `self`/`cls` in first place is implicit
    for child in parameters.children:
        if child is implicit:
            continue
        kind = child.type
        if kind == "identifier":
            tokens.append(_text(child))
        elif kind in ("typed_parameter", "typed_default_parameter"):
            type_node = child.child_by_field_name("type") or _child(child, "type")
            name_node = child.child_by_field_name("name") if kind == "typed_default_parameter" else None
            if kind == "typed_parameter":
                inner = next((c for c in child.children if c.type in ("identifier", "list_splat_pattern", "dictionary_splat_pattern")), None)
                if inner is not None and inner.type != "identifier":
                    tokens.append(_text(inner))  # `*args: int` stays `*args`
                    continue
                name_node = inner
            if type_node is not None:
                tokens.append(normalise_type(_text(type_node)))
            elif name_node is not None:
                tokens.append(_text(name_node))
        elif kind == "default_parameter":
            name_node = child.child_by_field_name("name") or _child(child, "identifier")
            if name_node is not None:
                tokens.append(_text(name_node))
        elif kind in ("list_splat_pattern", "dictionary_splat_pattern"):
            tokens.append(_text(child))
        elif kind == "keyword_separator":
            tokens.append("*")
        elif kind == "positional_separator":
            tokens.append("/")
    return tokens


# ---------------------------------------------------------------------------
# JavaScript / TypeScript
# ---------------------------------------------------------------------------


def _annotation(node: Any) -> Optional[str]:
    annotation = node.child_by_field_name("type") or _child(node, "type_annotation")
    if annotation is None:
        return None
    text = _text(annotation).strip()
    if text.startswith(":"):
        text = text[1:]
    return normalise_type(text)


def javascript_parameter_tokens(function_node: Any) -> List[str]:
    parameters = function_node.child_by_field_name("parameters")
    if parameters is None:
        single = function_node.child_by_field_name("parameter")  # `x => ...`
        return [_text(single)] if single is not None else []
    tokens: List[str] = []
    for child in parameters.children:
        kind = child.type
        if kind in ("(", ")", ",", "comment"):
            continue
        if kind == "identifier":
            if _text(child) == "this":
                continue
            tokens.append(_text(child))
        elif kind in ("required_parameter", "optional_parameter"):
            pattern = child.child_by_field_name("pattern") or child.children[0]
            if _text(pattern) == "this":
                continue
            tokens.append(_annotation(child) or normalise_type(_text(pattern)))
        elif kind == "assignment_pattern":
            left = child.child_by_field_name("left") or child.children[0]
            tokens.append(normalise_type(_text(left)))
        elif kind in ("rest_pattern", "rest_parameter"):
            tokens.append(normalise_type(_text(child).split(":")[0]))
        else:
            tokens.append(normalise_type(_text(child)))  # destructuring patterns
    return tokens


# ---------------------------------------------------------------------------
# C++
# ---------------------------------------------------------------------------


def innermost_function_declarator(declarator: Any) -> Any:
    """`int (*pick(void))(int)` is a function `pick` returning a pointer to a function: the declarator that names it is
    inside the parentheses, and its parameters are the function's own (the outer ones are the returned pointer's)."""
    current = declarator
    while True:
        inner = current.child_by_field_name("declarator")
        if inner is None or inner.type != "parenthesized_declarator":
            return current
        found = _function_declarator_inside(inner)
        if found is None:
            return current
        current = found


def _function_declarator_inside(node: Any) -> Optional[Any]:
    for child in node.children:
        if child.type == "function_declarator":
            return child
        if child.type in ("pointer_declarator", "parenthesized_declarator", "reference_declarator"):
            found = _function_declarator_inside(child)
            if found is not None:
                return found
    return None


def _find_function_declarator(node: Any) -> Optional[Any]:
    if node.type == "function_declarator":
        return innermost_function_declarator(node)
    for child in node.children:
        if child.type in ("compound_statement", "field_initializer_list", "try_statement"):
            continue
        found = _find_function_declarator(child)
        if found is not None:
            return found
    return None


def _declarator_name_range(declarator: Any) -> Optional[tuple]:
    """Byte range of the identifier a parameter declares, if it names one."""
    identifier = None
    stack = [declarator]
    while stack:
        current = stack.pop()
        if current.type == "identifier":
            identifier = current
            break
        stack.extend(reversed(current.children))
    return (identifier.start_byte, identifier.end_byte) if identifier is not None else None


def _cpp_parameter_type(parameter: Any) -> str:
    start = parameter.start_byte
    text = parameter.text
    end = parameter.end_byte
    cut: List[tuple] = []
    default = parameter.child_by_field_name("default_value")
    if default is not None:
        equals = next((c for c in parameter.children if c.type == "="), None)
        cut.append((equals.start_byte if equals is not None else default.start_byte, end))
    declarator = parameter.child_by_field_name("declarator")
    if declarator is not None:
        name_range = _declarator_name_range(declarator)
        if name_range is not None:
            cut.append(name_range)
    kept = bytearray()
    position = start
    for low, high in sorted(cut):
        kept += text[position - start : low - start]
        position = max(position, high)
    kept += text[position - start : end - start]
    return normalise_type(kept.decode("utf8"))


def cpp_parameter_tokens(function_node: Any) -> List[str]:
    declarator = _find_function_declarator(function_node)
    if declarator is None:
        return []
    parameters = declarator.child_by_field_name("parameters") or _child(declarator, "parameter_list")
    tokens: List[str] = []
    if parameters is not None:
        for child in parameters.children:
            if child.type in ("parameter_declaration", "optional_parameter_declaration", "variadic_parameter_declaration"):
                tokens.append(_cpp_parameter_type(child))
        if tokens == ["void"]:
            tokens = []
    for child in declarator.children:
        if child.type == "type_qualifier":
            tokens.append(_text(child))
        elif child.type == "ref_qualifier":
            tokens.append(_text(child))
    return tokens


# ---------------------------------------------------------------------------
# Rust
# ---------------------------------------------------------------------------


def rust_parameter_tokens(function_node: Any) -> List[str]:
    """Rust: the declared type of each parameter (`&mut Formatter`, `Vec<u8>`); `self`, `&self` and
    `&mut self` are the receiver, not a parameter, like Python's `self`. Lifetimes and generics are
    kept as written."""
    parameters = function_node.child_by_field_name("parameters") or _child(function_node, "parameters")
    if parameters is None:
        return []
    tokens: List[str] = []
    for child in parameters.children:
        if child.type == "parameter":
            type_node = child.child_by_field_name("type")
            tokens.append(normalise_type(_text(type_node)) if type_node is not None else _text(child))
        elif child.type == "variadic_parameter":
            tokens.append("...")
    return tokens
