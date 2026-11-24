"""Macros a project defines to decorate declarations (`#define API`, `#define EXPORT __attribute__((visibility("default")))`,
`#define FMT(x) __attribute__((format(printf, x, x+1)))`).

The grammar cannot read `class API Name {` or `void F(const char*, ...) FMT(2);`, and the functions around them are lost.
Which names are such macros is decided by their definitions, found in the file and in the headers it includes, and not by
how a name looks: `FMT`, `API` and `EXPORT` are decorations when the project says they expand to nothing or to an
attribute, and are left alone when it says they expand to code.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

#: How deep and how many headers are followed from a file, so a huge include tree costs a bounded read.
MAX_INCLUDE_DEPTH = 3
MAX_HEADERS = 40

_DEFINE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)(\([^)]*\))?[ \t]*(.*)$")
_INCLUDE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*["<]([^">]+)[">]', re.MULTILINE)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"//.*")

#: Words a decoration may be made of besides attributes: calling conventions, inlining and `noreturn` spellings.
_DECORATION_WORDS = frozenset(
    """inline __inline __inline__ __forceinline _Noreturn noreturn __noreturn __noreturn__ __extension__ __restrict
    __restrict__ restrict __cdecl _cdecl __stdcall _stdcall __fastcall _fastcall __vectorcall __thiscall __clrcall
    __unaligned __ptr32 __ptr64 noexcept static extern __attribute__ __attribute __declspec __pragma _Pragma""".split()
)
#: Spellings with their own arguments, removed together with the parenthesised arguments.
_ARGUMENT_FORMS = re.compile(r"\b(?:__attribute__|__attribute|__declspec|__pragma|_Pragma|alignas|__alignas|noexcept)\b\s*")
_KEYWORDS = frozenset(
    """if else for while do switch case default return sizeof class struct union enum namespace template typename using
    const volatile virtual explicit friend typedef operator public private protected new delete this true false
    nullptr void int long short char bool float double unsigned signed auto register mutable constexpr""".split()
)


def _balanced(text: str, start: int) -> int:
    """The index just after the `)` that closes the `(` at `start`, or -1."""
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index + 1
    return -1


def _strip_arguments(body: str) -> Tuple[str, bool]:
    """The body without the argument forms (`__attribute__((x))`) and whether there was one."""
    found = False
    while True:
        match = _ARGUMENT_FORMS.search(body)
        if not match:
            return body, found
        found = True
        end = match.end()
        if end < len(body) and body[end] == "(":
            close = _balanced(body, end)
            end = close if close != -1 else len(body)
        body = body[: match.start()] + " " + body[end:]


def _is_decoration(body: str, function_like: bool, known: Set[str]) -> bool:
    """Whether a macro body only decorates: empty, or made of attributes, declaration specifiers of the list above, and
    other decorations. A function-like macro must contain an attribute, so a stub like `#define LOG(x)` that is a
    statement in disguise is not blanked."""
    body = _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub(" ", body)).strip()
    if not body:
        return not function_like
    rest, had_attribute = _strip_arguments(body)  # (the arguments may hold strings: `visibility("default")`)
    rest = re.sub(r"\[\[.*?\]\]", " ", rest)
    if any(char in rest for char in "\"'{};"):
        return False
    had_attribute = had_attribute or "[[" in body
    words = re.findall(r"[A-Za-z_]\w*|\S", rest)
    if any(not (word in _DECORATION_WORDS or word in known) for word in words):
        return False
    if function_like and not had_attribute:
        return False
    return True


def _read_definitions(text: str) -> Dict[str, List[Tuple[bool, str]]]:
    """name -> [(function_like, body)] for each `#define` of a file (continuation lines joined)."""
    definitions: Dict[str, List[Tuple[bool, str]]] = {}
    lines = text.split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if "define" not in line:
            continue
        while line.rstrip().endswith("\\") and index < len(lines):
            line = line.rstrip()[:-1] + " " + lines[index]
            index += 1
        match = _DEFINE.match(line)
        if match:
            definitions.setdefault(match.group(1), []).append((match.group(2) is not None, match.group(3)))
    return definitions


#: path -> ((mtime_ns, size), definitions, includes); a header is read once per process, and again when it changes.
_FILES: Dict[str, Tuple[Tuple[int, int], Dict[str, List[Tuple[bool, str]]], List[str]]] = {}


def _file_facts(path: str) -> Optional[Tuple[Dict[str, List[Tuple[bool, str]]], List[str]]]:
    try:
        stat = os.stat(path)
        key = (stat.st_mtime_ns, stat.st_size)
        cached = _FILES.get(path)
        if cached and cached[0] == key:
            return cached[1], cached[2]
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    facts = (key, _read_definitions(text), _INCLUDE.findall(text))
    if len(_FILES) > 2000:
        _FILES.clear()
    _FILES[path] = facts
    return facts[1], facts[2]


def _resolve(name: str, from_file: Optional[str]) -> Optional[str]:
    candidates = []
    if from_file:
        candidates.append(Path(from_file).parent / name)
    candidates.append(Path.cwd() / name)
    for candidate in candidates:
        if candidate.is_file():
            return os.path.normpath(str(candidate))
    return None


def _definitions_for(text: str, file_path: Optional[str]) -> Dict[str, List[Tuple[bool, str]]]:
    """Every `#define` of the file and of the headers it includes (to `MAX_INCLUDE_DEPTH`): name -> [(function_like, body)]."""
    definitions: Dict[str, List[Tuple[bool, str]]] = {}
    for name, found in _read_definitions(text).items():
        definitions.setdefault(name, []).extend(found)
    seen: Set[str] = set()
    queue: List[Tuple[str, int]] = []
    for include in _INCLUDE.findall(text):
        resolved = _resolve(include, file_path)
        if resolved:
            queue.append((resolved, 1))
    while queue and len(seen) < MAX_HEADERS:
        path, depth = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        facts = _file_facts(path)
        if not facts:
            continue
        for name, found in facts[0].items():
            definitions.setdefault(name, []).extend(found)
        if depth < MAX_INCLUDE_DEPTH:
            for include in facts[1]:
                resolved = _resolve(include, path)
                if resolved and resolved not in seen:
                    queue.append((resolved, depth + 1))
    return definitions


#: The body of an X-macro list: nothing but invocations (`X(RED) X(GREEN)`), which only the macros it names can expand.
_INVOCATION_LIST = re.compile(r"^(?:\s*[A-Za-z_]\w*\s*\([^()]*\)\s*\\?)+$")
#: The body of a macro that defines a function: a return type, a name (maybe pasted), a parameter list and a body.
_DEFINES_A_FUNCTION = re.compile(r"^\s*(?:[\w\*]+\s+)+[A-Za-z_][\w#]*(?:\s*##\s*\w+)*\s*\([^()]*\)\s*\{")


def decoration_macros(text: str, file_path: Optional[str]) -> Dict[str, bool]:
    """name -> whether it is function-like, for the macros the file and the headers it includes define that can be
    taken out of the text the grammar reads: the decorating ones (see the module's description), and X-macro lists
    (`#define COLORS X(RED) X(GREEN)`, which stand for something only the macros they name can say, and are used where
    nothing could be read: in an enum, after a value). A name defined more than once counts only when every definition
    qualifies."""
    definitions = _definitions_for(text, file_path)
    if not definitions:
        return {}
    known: Set[str] = set()
    result: Dict[str, bool] = {}
    for _round in range(3):  # a decoration may be made of another one
        changed = False
        for name, found in definitions.items():
            if name in result or name in _KEYWORDS:
                continue
            if all(_is_decoration(body, function_like, known) for function_like, body in found):
                if len({function_like for function_like, _ in found}) == 1:
                    result[name] = found[0][0]
                    known.add(name)
                    changed = True
        if not changed:
            break
    for name, found in definitions.items():
        if name not in result and name not in _KEYWORDS and all(not function_like and _INVOCATION_LIST.match(_clean(body)) for function_like, body in found):
            result[name] = False
    return result


_PARAMETER_LIST = re.compile(r"^\s*\(?\s*([A-Za-z_]\w*)\s*\)?\s*$")
_ENDS_WITH_TAG = re.compile(r"\b(enum|struct|union)\s*$")


def _parameters_of(definition_line_params: str) -> List[str]:
    return [part.strip() for part in definition_line_params.strip("()").split(",") if part.strip()]


def identity_macros(text: str, file_path: Optional[str]) -> Set[str]:
    """The function-like macros that give their one argument back, with or without parentheses (`#define LITERAL(name) name`
    and `#define LITERAL(name) (name)`): `LITERAL(Rectangle){ 1, 2 }` is the compound literal `(Rectangle){ 1, 2 }`, and
    only the name of the macro is in the way of reading it."""
    found = set()
    for name, definitions in _definitions_for(text, file_path).items():
        if name in _KEYWORDS:
            continue
        for function_like, body in definitions:
            if not function_like or not _PARAMETER_LIST.match(_clean(body)):
                break
        else:
            found.add(name)
    return found


def tag_macros(text: str, file_path: Optional[str]) -> Dict[str, str]:
    """name -> `enum`, `struct` or `union`, for function-like macros whose body ends with that word
    (`#define RGFW_ENUM(type, name) type name; enum`): `typedef RGFW_ENUM(u8, key) { ... } key;` reads as a typedef of an
    enum once the invocation is that word."""
    found: Dict[str, str] = {}
    for name, definitions in _definitions_for(text, file_path).items():
        if name in _KEYWORDS:
            continue
        words = set()
        for function_like, body in definitions:
            match = _ENDS_WITH_TAG.search(_clean(body)) if function_like else None
            if not match:
                break
            words.add(match.group(1))
        else:
            if len(words) == 1:
                found[name] = words.pop()
    return found


def blank_names(text: str, names: Set[str]) -> str:
    """The text with each use of these function-like macros stripped to its arguments: the name becomes spaces (same length)."""
    if not names:
        return text
    pattern = re.compile(r"\b(" + "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True)) + r")(?=\s*\()")
    return _apply_outside_code_markup(text, pattern, lambda match: " " * len(match.group(0)))


def replace_tags(text: str, tags: Dict[str, str]) -> str:
    """Each use `NAME(args)` of a tag macro replaced by its tag word padded with spaces to the same length."""
    if not tags:
        return text
    pattern = re.compile(r"\b(" + "|".join(re.escape(name) for name in sorted(tags, key=len, reverse=True)) + r")\s*\(")
    out, position = [], 0
    for match in pattern.finditer(text):
        if match.start() < position:
            continue
        closing = _balanced(text, match.end() - 1)
        if closing == -1:
            continue
        word = tags[match.group(1)]
        span = text[match.start() : closing + 1]
        out.append(text[position : match.start()])
        out.append(word + re.sub(r"[^\n]", " ", span[len(word):]) if len(span) >= len(word) else span)
        position = closing + 1
    out.append(text[position:])
    return "".join(out)


def _apply_outside_code_markup(text: str, pattern: "re.Pattern[str]", replace) -> str:
    """Apply `pattern` to the code of each line (not to directives, strings and comments)."""
    out: List[str] = []
    in_directive = False
    for line in text.split("\n"):
        directive = in_directive or line.lstrip().startswith("#")
        in_directive = directive and line.rstrip().endswith("\\")
        if directive or not pattern.search(line):
            out.append(line)
            continue
        characters = list(line)
        for start, end in _outside_strings(line):
            for match in pattern.finditer(line, start, end):
                characters[match.start() : match.end()] = list(replace(match))
        out.append("".join(characters))
    return "\n".join(out)


def defining_macros(text: str, file_path: Optional[str]) -> Set[str]:
    """The function-like macros of the file (and its headers) whose body defines a whole function: `DEFINE_GETTER(w, w)`
    at the top of a file stands for `int get_w(struct G *g) { ... }`, and is not read as anything without a `;`."""
    return {
        name for name, found in _definitions_for(text, file_path).items()
        if name not in _KEYWORDS and all(function_like and _DEFINES_A_FUNCTION.match(_clean(body)) for function_like, body in found)
    }


def _clean(body: str) -> str:
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub(" ", body)).strip()


def _definer_uses(text: str, names: Set[str]) -> List[Tuple[int, int]]:
    """`(start, end)` of each use of a function-defining macro where it stands as a declaration of its own: at the start
    of a line, outside any braces, and not followed by `;`, `{` or `,`. In the order they are written."""
    if not names:
        return []
    pattern = re.compile(r"(?m)^[ \t]*(" + "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True)) + r")[ \t]*\(")
    found: List[Tuple[int, int]] = []
    for match in pattern.finditer(text):
        closing = _balanced(text, match.end() - 1)
        if closing == -1:
            continue
        rest = text[closing : closing + 200].lstrip(" \t\r")
        if rest[:1] in (";", "{", ","):
            continue
        if _brace_depth(text, match.start()) != 0:
            continue
        found.append((match.start(), closing))
    return found


def blank_definers(text: str, names: Set[str]) -> str:
    """The text with each use of a function-defining macro blanked (same length) where it stands as a declaration of its
    own (see `_definer_uses`)."""
    out = text
    for start, closing in reversed(_definer_uses(text, names)):
        out = out[:start] + re.sub(r"[^\n]", " ", out[start:closing]) + out[closing:]
    return out


def definer_calls(text: str, names: Set[str]) -> List[Tuple[int, str, int]]:
    """`(first line, 'NAME(args)', last line)` for each use that `blank_definers` blanks: a function the macro defines,
    which no text of the file names, so a user can be told that it is there."""
    return [
        (text.count("\n", 0, start) + 1, " ".join(text[start:closing].split()), text.count("\n", 0, closing) + 1)
        for start, closing in _definer_uses(text, names)
    ]


def _brace_depth(text: str, position: int) -> int:
    """How many braces are open at `position`, not counting those in strings, characters and comments."""
    depth, index = 0, 0
    while index < position:
        char = text[index]
        if char in "\"'":
            quote, index = char, index + 1
            while index < position and text[index] != quote:
                index += 2 if text[index] == "\\" else 1
        elif text.startswith("//", index):
            index = text.find("\n", index)
            if index == -1:
                break
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end == -1:
                break
            index = end + 1
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        index += 1
    return depth


def _outside_strings(line: str) -> List[Tuple[int, int]]:
    """The spans of a line that are code: not inside a string or character literal, a block comment or a line comment."""
    spans: List[Tuple[int, int]] = []
    start = 0
    index = 0
    quote = ""
    while index < len(line):
        char = line[index]
        if quote:
            if char == "\\":
                index += 1
            elif char == quote:
                quote = ""
                start = index + 1
        elif char in "\"'":
            spans.append((start, index))
            quote = char
        elif line.startswith("//", index):
            spans.append((start, index))
            return spans
        elif line.startswith("/*", index):
            spans.append((start, index))
            end = line.find("*/", index + 2)
            if end == -1:
                return spans
            index = end + 1
            start = index + 1
        index += 1
    if not quote:
        spans.append((start, len(line)))
    return spans


def blank_decorations(text: str, macros: Dict[str, bool]) -> str:
    """The text with every use of the given macros replaced by spaces (same length, so no position moves). Preprocessor
    lines, strings and comments are left alone; a function-like macro is blanked with its arguments, only when it is
    called."""
    if not macros:
        return text
    names = sorted(macros, key=len, reverse=True)
    pattern = re.compile(r"\b(" + "|".join(re.escape(name) for name in names) + r")\b")
    out: List[str] = []
    in_directive = False
    for line in text.split("\n"):
        directive = in_directive or line.lstrip().startswith("#")
        in_directive = directive and line.rstrip().endswith("\\")
        if directive or not pattern.search(line):
            out.append(line)
            continue
        characters = list(line)
        for start, end in _outside_strings(line):
            for match in pattern.finditer(line, start, end):
                name = match.group(1)
                after = match.end()
                while after < len(line) and line[after] in " \t":
                    after += 1
                if macros[name]:
                    if after >= len(line) or line[after] != "(":
                        continue
                    close = _balanced(line, after)
                    if close == -1:
                        continue
                    stop = close
                else:
                    if after < len(line) and line[after] == "(":
                        continue
                    stop = match.end()
                characters[match.start():stop] = list(" " * (stop - match.start()))
        out.append("".join(characters))
    return "\n".join(out)
