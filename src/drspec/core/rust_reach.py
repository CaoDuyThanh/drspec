"""What a change in Rust code can reach.

A Rust call is resolved through the module facts of other files (`use`, `pub use`, `mod`, `impl`, `trait`), so a change in one
file can change a call in a file that does not name the changed function: a re-export under another name, a trait
implemented in a new file, a module brought into the tree. The rule that decides which files to look at again:

1. A change is described by a set of TOKENS (names). It holds the name of every function added, removed or moved, and the
   names touched by a difference of the module facts of a changed file: each segment and alias of a `use` that was added or
   removed, each `mod`, type, trait and impl (its type, its trait and the names of its methods) that was added, removed or
   altered, and the stem of a file that appeared or went away (the name a `mod` declaration gives it).
2. The tokens grow through what other files make of them, until nothing is added: a `pub use a::f as g` makes `g` a way to
   reach `f`, so `g` is a token whenever `f` is; a trait whose name is a token brings the names of its default methods.
   Only `pub use` is followed (a private `use` is visible in its own file, and a changed file is looked at whole).
3. A call can resolve differently only if it names something that changed: its function name, a segment of its receiver
   path, a type of the function's parameters or of the impl it sits in. A function none of whose calls (nor the type it is a
   method of) holds a token keeps its stored edges: they are what a fresh resolution gives.
4. What this cannot decide is answered with every Rust function re-resolved (`None` for the tokens): a `mod` declaration
   that was added, removed or changed (it moves a file, and everything below it, in the module tree), a `pub use` of a glob
   (it exposes every name of its target), a crate root that appeared or went away, and a file whose earlier facts are not
   known.
"""

from __future__ import annotations

import json
import posixpath
import re
from typing import Iterable, Optional

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*!?")
_ROOT_NAMES = ("lib.rs", "main.rs", "build.rs")


def identifiers(text: Optional[str]) -> list:
    """Every identifier in a path, a type or a receiver expression (`a::b::C<D>` -> a, b, C, D)."""
    return _IDENTIFIER.findall(text) if text else []


def _leaf(qualified: str) -> str:
    return qualified.rsplit("::", 1)[-1]


def file_names(path: str) -> list:
    """The name a `mod` declaration gives the file: its stem, or the folder of a `mod.rs`."""
    stem = posixpath.splitext(posixpath.basename(path))[0]
    folder = posixpath.basename(posixpath.dirname(path))
    return [folder] if stem == "mod" and folder else [stem]


def is_crate_root(path: str) -> bool:
    """A file a crate may start from (`lib.rs`, `main.rs`, `build.rs`, a binary under `src/bin`): one that appears or goes
    away changes which files form a crate."""
    return posixpath.basename(path) in _ROOT_NAMES or "/src/bin/" in "/" + path


def is_rust_facts(facts: Optional[dict]) -> bool:
    """Whether stored module facts are a Rust file's (the parser always writes all five lists, even for a file with no
    function, such as a `lib.rs` that only declares modules and re-exports)."""
    return bool(facts) and all(kind in facts for kind in ("uses", "mods", "types", "impls", "traits"))


def mentions(functions, facts: dict) -> dict:
    """What the functions of a Rust file and its module facts name, for the choice of the files to look at again."""
    names: set = set()
    for func in functions:
        for call in func.calls:
            names.add(call.name)
            if call.receiver:
                names.add(call.receiver)
                names.update(identifiers(call.receiver))
        for read in func.property_reads:
            names.add(read.name)
            if read.receiver is not None:
                names.add(read.receiver)
        for annotation in func.declared_param_types.values():
            names.update(identifiers(annotation))
        names.update(identifiers(func.parent))
    reexports: list = []
    for use in facts.get("uses", []):
        names.update(identifiers(use["path"]))
        if use["alias"] != "*":
            names.add(use["alias"])
        if use.get("pub") and use["alias"] != "*":
            last = use["path"].rsplit("::", 1)[-1]
            if last != use["alias"]:
                reexports.append([last, use["alias"]])
    for mod in facts.get("mods", []):
        names.add(mod["name"])
    for impl in facts.get("impls", []):
        names.update(identifiers(impl.get("type_path") or impl["type"]))
        if impl.get("trait"):
            names.add(impl["trait"])
    result: dict = {"r": 1, "n": sorted(names), "s": []}
    if reexports:
        result["u"] = reexports
    traits = {trait["name"]: sorted({_leaf(method) for method in trait.get("methods", [])}) for trait in facts.get("traits", [])}
    if traits:
        result["t"] = traits
    return result


def _entries(facts: dict, kind: str) -> dict:
    return {json.dumps(entry, sort_keys=True): entry for entry in facts.get(kind, [])}


def _methods_by_owner(facts: dict, kind: str, owner) -> dict:
    """`{owner key: (the entry, the leaf names of its methods)}`; several blocks of one owner are one."""
    table: dict = {}
    for entry in facts.get(kind, []):
        key = json.dumps(owner(entry), sort_keys=True)
        held = table.setdefault(key, (entry, set()))
        held[1].update(_leaf(method) for method in entry.get("methods", []))
    return table


def _owner_tokens(old: dict, new: dict, kind: str, owner, own_names) -> set:
    """What a difference of the `impl` blocks or the traits of a file touches: for an owner on both sides only the methods
    that came or went (a method added to `impl World` names `World` to nobody); for an owner on one side, its own names and
    all its methods."""
    tokens: set = set()
    before, after = _methods_by_owner(old, kind, owner), _methods_by_owner(new, kind, owner)
    for key in set(before) | set(after):
        if key in before and key in after:
            tokens.update(before[key][1] ^ after[key][1])
        else:
            entry, methods = before.get(key) or after[key]
            tokens.update(methods)
            tokens.update(own_names(entry))
    return tokens


def fact_tokens(old: dict, new: dict) -> Optional[set]:
    """The names touched by the difference between the old and the new module facts of one file, or `None` when the
    difference is one the rule cannot bound (see the module's rule 4)."""
    tokens: set = set()
    before, after = _entries(old, "mods"), _entries(new, "mods")
    if set(before) != set(after):
        return None  # a module moves a file and what is below it in the tree
    before, after = _entries(old, "uses"), _entries(new, "uses")
    for key in set(before) ^ set(after):
        entry = before.get(key) or after[key]
        if entry["alias"] == "*" and entry.get("pub"):
            return None  # a re-exported glob exposes every name of its target
        tokens.update(identifiers(entry["path"]))
        if entry["alias"] != "*":
            tokens.add(entry["alias"])
    before, after = _entries(old, "types"), _entries(new, "types")
    for key in set(before) ^ set(after):
        tokens.add((before.get(key) or after[key])["name"])
    tokens |= _owner_tokens(
        old, new, "impls", lambda e: (e["mod"], e["type"], e.get("type_path"), e.get("trait")),
        lambda e: [*identifiers(e.get("type_path") or e["type"]), *([e["trait"]] if e.get("trait") else [])],
    )
    tokens |= _owner_tokens(old, new, "traits", lambda e: (e["mod"], e["name"]), lambda e: [e["name"]])
    return tokens


def reach(
    changed_facts: dict,
    appeared: Iterable[str],
    gone: Iterable[str],
    stored_mentions: dict,
    names: set,
) -> Optional[set]:
    """The tokens of a change (see the module's rule), or `None` for every Rust function.

    `changed_facts` is `{file: (old facts or None when not known, new facts)}` for the Rust files whose facts differ;
    `appeared` and `gone` are the Rust files that were added or removed; `stored_mentions` is every file's stored mentions
    (text); `names` the names of the functions that changed."""
    tokens = set(names)
    for path, (old, new) in changed_facts.items():
        if old is None:
            return None
        found = fact_tokens(old, new)
        if found is None:
            return None
        tokens |= found
    for path in (*appeared, *gone):
        if is_crate_root(path):
            return None
        tokens.update(file_names(path))
    reexports: list = []
    traits: dict = {}
    for text in stored_mentions.values():
        if '"r"' not in text:
            continue
        try:
            entry = json.loads(text)
        except ValueError:
            continue
        reexports.extend(entry.get("u", ()))
        for trait, methods in entry.get("t", {}).items():
            traits.setdefault(trait, set()).update(methods)
    grown = True
    while grown:
        grown = False
        for last, alias in reexports:
            if last in tokens and alias not in tokens:
                tokens.add(alias)
                grown = True
        for trait, methods in traits.items():
            if trait in tokens and not methods <= tokens:
                tokens |= methods
                grown = True
    return tokens


def function_may_resolve_differently(func, tokens: Optional[set]) -> bool:
    """Whether a Rust function, restored from its stored facts, can resolve a call differently after a change with
    `tokens` (`None`: it cannot be told)."""
    if tokens is None:
        return True
    for call in func.calls:
        if call.name in tokens or (call.receiver and (call.receiver in tokens or any(part in tokens for part in identifiers(call.receiver)))):
            return True
    if any(read.name in tokens or read.receiver in tokens for read in func.property_reads):
        return True
    if any(part in tokens for annotation in func.declared_param_types.values() for part in identifiers(annotation)):
        return True
    return any(part in tokens for part in identifiers(func.parent))
