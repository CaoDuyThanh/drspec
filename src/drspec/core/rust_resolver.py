"""Call resolution for Rust.

A call is tied to a function through the module tree, never through its spelling alone:

1. a bare call `f()` is a function declared in the caller (nested), in its module, or a name the module
   brought in with `use` (an alias, a group, a glob such as `use super::*`, a `pub use` re-export);
2. a path call `a::b::f()`, `crate::x::y()`, `super::f()`, `Self::helper()` and `Type::new()` follows the
   path through modules and types; an associated function is looked for in the type's inherent `impl`
   blocks, then its trait implementations, then the default methods of the traits it implements;
3. `self.m()` looks at the type of the enclosing `impl` (or at the trait, inside a trait's default method);
4. `v.m()` uses the type `v` was declared with, or built with (`Type::new(..)`, `Type { .. }`), seen through
   references and `Box`/`Rc`/`Arc`/`Option`; a generic parameter or `impl Trait` is its trait, and a call
   through a trait is one `AMBIGUOUS` edge to each implementer (and to the default method);
5. a user macro `name!` is the `macro_rules!` of that name in the same crate.

Module files come from `mod name;` declarations (`name.rs`, `name/mod.rs`, `#[path]`), crates from
`Cargo.toml`. A name from the standard library or another crate stays unresolved and is counted as external.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from drspec.core.call_resolver import ResolvedCall
from drspec.core.identity import qualified_name
from drspec.core.js_resolver import Resolution, _targets, _unique
from drspec.core.rust_modules import Crate, child_file, discover_crates

_RANK = {"AMBIGUOUS": 0, "INFERRED": 1, "EXTRACTED": 2}
_MAX_PASSES = 64  # most rounds of working out lookups that reach each other (it stops sooner, when nothing changes)
_EXTERNAL_ROOTS = frozenset({"std", "core", "alloc", "proc_macro", "test"})
_PRELUDE = frozenset({
    "Some", "None", "Ok", "Err", "Box", "Vec", "String", "Option", "Result", "Default", "Clone", "From", "Into", "Iterator",
    "ToString", "ToOwned", "Drop", "Fn", "FnMut", "FnOnce", "drop", "i8", "i16", "i32", "i64", "u8", "u16", "u32", "u64",
    "usize", "isize", "f32", "f64", "bool", "char", "str",
})

Scope = Tuple[str, str]  # (file, inline module path)
Entity = Tuple  # ("fn", file, qualified) | ("type", file, mod, name, kind) | ("mod", file, mod) | ("ext", name)


@dataclass
class RustIndex:
    facts: Dict[str, dict] = field(default_factory=dict)
    by_qualified: Dict[Tuple[str, str], List[str]] = field(default_factory=dict)
    known_files: Set[str] = field(default_factory=set)
    crates: List[Crate] = field(default_factory=list)
    crate_of_file: Dict[str, str] = field(default_factory=dict)
    parent_scope: Dict[Scope, Scope] = field(default_factory=dict)
    children: Dict[Scope, Dict[str, Scope]] = field(default_factory=dict)
    crate_root: Dict[str, Scope] = field(default_factory=dict)
    _scopes_built: bool = False
    _impls: Optional[Dict[Entity, List[dict]]] = None
    _trait_impls: Optional[Dict[Entity, List[Entity]]] = None
    _macros: Dict[Tuple[str, str], List[str]] = field(default_factory=dict)
    # Everything below is derived from the facts once per index (one index lives for one publish) and looked up after.
    _final: Dict[Tuple, List[Entity]] = field(default_factory=dict)  # lookups worked out, see `lookup`
    _paths: Dict[Tuple, List[Entity]] = field(default_factory=dict)  # paths worked out
    _env: Optional[Dict[Tuple, List[Entity]]] = None  # the lookups being worked out together and what is known of each
    _order: List[Tuple] = field(default_factory=list)  # ... in the order they were met
    _inline: Dict[str, Set[str]] = field(default_factory=dict)
    _types: Dict[Scope, Dict[str, List[Entity]]] = field(default_factory=dict)
    _uses: Dict[Scope, Tuple[Dict[str, List[dict]], List[dict]]] = field(default_factory=dict)
    _associated: Dict[Tuple, list] = field(default_factory=dict)
    _self_entities: Optional[Dict[Tuple[str, str], Entity]] = None
    _implementers: Optional[Dict[Entity, List[Entity]]] = None
    _crate_files: Optional[Dict[str, List[str]]] = None

    # ---- the module tree

    def build(self) -> None:
        if self._scopes_built:
            return
        self._scopes_built = True
        reached: Set[str] = set()
        for crate in self.crates:
            for index, root in enumerate(crate.roots):
                self._walk_file(root, "", crate.name if index == 0 or len(crate.roots) == 1 else f"{crate.name}:{posixpath.basename(root)}", None, True, reached)
            self.crate_root.setdefault(crate.name, (crate.roots[0], ""))
        for file in sorted(self.facts):
            if file not in reached:
                self._walk_file(file, "", f"({file})", None, True, reached)

    def _walk_file(self, file: str, inline: str, crate: str, parent: Optional[Scope], is_root: bool, reached: Set[str]) -> None:
        if file in reached and not inline:
            return
        scope = (file, inline)
        if not inline:
            reached.add(file)
            self.crate_of_file[file] = crate
            if parent is not None:
                self.parent_scope[scope] = parent
        if is_root and not inline:
            self.crate_root.setdefault(crate, scope)
        facts = self.facts.get(file, {})
        for mod in facts.get("mods", []):
            if mod["mod"] != inline:
                continue
            if mod["inline"]:
                child = (file, f"{inline}::{mod['name']}" if inline else mod["name"])
                self.parent_scope[child] = scope
                self.children.setdefault(scope, {})[mod["name"]] = child
                self._walk_file(file, child[1], crate, scope, False, reached)
            else:
                path = [part for part in inline.split("::") if part]
                target = child_file(file, path, mod["name"], mod.get("path"), is_root and not inline, self.known_files)
                if target is not None and target not in reached:
                    child = (target, "")
                    self.children.setdefault(scope, {})[mod["name"]] = child
                    self._walk_file(target, "", crate, scope, False, reached)
                elif target is not None:
                    self.children.setdefault(scope, {})[mod["name"]] = (target, "")

    # ---- names

    def functions_in(self, scope: Scope, name: str) -> List[str]:
        file, mod = scope
        qualified = f"{mod}::{name}" if mod else name
        if mod and mod not in self._inline_mods(file):
            return []
        return list(self.by_qualified.get((file, qualified), []))

    def _inline_mods(self, file: str) -> Set[str]:
        mods = self._inline.get(file)
        if mods is None:
            facts = self.facts.get(file, {})
            mods = self._inline[file] = {f"{m['mod']}::{m['name']}" if m["mod"] else m["name"] for m in facts.get("mods", []) if m["inline"]}
        return mods

    def types_in(self, scope: Scope, name: str) -> List[Entity]:
        table = self._types.get(scope)
        if table is None:
            file, mod = scope
            table = self._types[scope] = {}
            for t in self.facts.get(file, {}).get("types", []):
                if t["mod"] == mod:
                    table.setdefault(t["name"], []).append(("type", file, mod, t["name"], t["kind"]))
        return list(table.get(name, ()))

    def _uses_in(self, scope: Scope) -> Tuple[Dict[str, List[dict]], List[dict]]:
        """The `use` declarations of a module by the name they bring in, and its glob imports, in source order."""
        entry = self._uses.get(scope)
        if entry is None:
            by_alias: Dict[str, List[dict]] = {}
            globs: List[dict] = []
            for use in self.facts.get(scope[0], {}).get("uses", []):
                if use["mod"] != scope[1]:
                    continue
                by_alias.setdefault(use["alias"], []).append(use)
                if use["alias"] == "*":
                    globs.append(use)
            entry = self._uses[scope] = (by_alias, globs)
        return entry

    # A lookup follows `use` declarations that can lead back to where it started (`use super::*` in two modules that
    # glob each other, as a crate's prelude does). The lookups that reach each other are therefore worked out together:
    # each starts with nothing known, is worked out again from what the others have so far, and this repeats until none
    # changes. The result is what every route through the cycle contributes, however the cycle is entered, so it is
    # worked out once for a lookup and kept. (Walking every route as a path, cutting where it met itself, took time
    # that grew with the square of the number of modules, and gave a lookup in a cycle a different answer from each
    # entry.)

    def lookup(self, scope: Scope, name: str, outside: bool = False) -> List[Entity]:
        """What `name` means in `scope`. `outside` is a lookup through a path (`a::name`), which sees only what
        the module makes public by `pub use`, besides its own items."""
        key = (scope, name, outside)
        done = self._final.get(key)
        if done is not None:
            return done
        if self._env is not None:  # asked while others are being worked out: what is known of it so far
            known = self._env.get(key)
            if known is None:
                known = self._env[key] = []
                self._order.append(key)
            return known
        return self._solve(key)

    def _solve(self, root: Tuple) -> List[Entity]:
        self._env = {root: []}
        self._order = [root]
        try:
            for _ in range(_MAX_PASSES):
                met = len(self._order)
                changed = False
                for key in reversed(list(self._order)):  # what a lookup uses was met after it: work that out first
                    value = self._evaluate(key)
                    if value != self._env[key]:
                        self._env[key] = value
                        changed = True
                if not changed and len(self._order) == met:
                    break
            self._final.update(self._env)
        finally:
            self._env = None
            self._order = []
        return self._final[root]

    def _evaluate(self, key: Tuple) -> List[Entity]:
        scope, name, outside = key
        found: List[Entity] = []
        if self.functions_in(scope, name):
            found.append(("fn", scope[0], f"{scope[1]}::{name}" if scope[1] else name))
        found.extend(self.types_in(scope, name))
        child = self.children.get(scope, {}).get(name)
        if child is not None:
            found.append(("mod", child[0], child[1]))
        if not found:
            by_alias, globs = self._uses_in(scope)
            for use in by_alias.get(name, ()):
                if outside and not use.get("pub"):
                    continue
                found.extend(self.resolve_path(scope, use["path"].split("::"), use=True))
            if not found:
                for use in globs:
                    if outside and not use.get("pub"):
                        continue
                    for target in self.resolve_path(scope, use["path"].split("::"), use=True):
                        if target[0] == "mod":
                            found.extend(self.lookup((target[1], target[2]), name, True))
        return _unique(found)

    def resolve_path(self, scope: Scope, parts: List[str], use: bool = False) -> List[Entity]:
        """What a path means in `scope`: modules, then the type or function at its end."""
        if not parts:
            return []
        if self._env is not None:  # inside a lookup being worked out: from what is known so far, not kept
            return self._resolve_path(scope, parts, use)
        key = (scope, tuple(parts), use)
        done = self._paths.get(key)
        if done is None:
            done = self._paths[key] = self._resolve_path(scope, parts, use)
        return done

    def _resolve_path(self, scope: Scope, parts: List[str], use: bool) -> List[Entity]:
        first, rest = parts[0], parts[1:]
        current: List[Entity]
        if first == "crate":
            root = self.crate_root.get(self.crate_of_file.get(scope[0], ""))
            current = [("mod", root[0], root[1])] if root else [("mod", scope[0], "")]
        elif first == "self":
            current = [("mod", scope[0], scope[1])]
        elif first == "super":
            parent = self.parent_scope.get(scope)
            current = [("mod", parent[0], parent[1])] if parent else []
        else:
            current = self.lookup(scope, first, False)
            if not current:
                crate = self.crate_root.get(first)
                if crate is not None and first != self.crate_of_file.get(scope[0]):
                    current = [("mod", crate[0], crate[1])]
                elif first in _EXTERNAL_ROOTS or first in _PRELUDE:
                    return [("ext", "::".join(parts))]
        for part in rest:
            if part == "super" and current and current[0][0] == "mod":
                parent = self.parent_scope.get((current[0][1], current[0][2]))
                current = [("mod", parent[0], parent[1])] if parent else []
                continue
            following: List[Entity] = []
            for entity in current:
                if entity[0] == "mod":
                    following.extend(self.lookup((entity[1], entity[2]), part, True))
                elif entity[0] == "type":
                    following.extend(("fn", f[0], f[1]) for f in self.associated(entity, part, ids=False))
            current = _unique(following)
            if not current:
                return []
        return current

    # ---- types and impls

    def impls_by_type(self) -> Dict[Entity, List[dict]]:
        if self._impls is None:
            self.build()
            table: Dict[Entity, List[dict]] = {}
            for file, facts in self.facts.items():
                for impl in facts.get("impls", []):
                    scope = (file, impl["mod"])
                    types = self.resolve_path(scope, (impl.get("type_path") or impl["type"]).split("::"))
                    target = next((t for t in types if t[0] == "type"), None) or ("ext", impl["type"])
                    trait = None
                    if impl.get("trait"):
                        trait = next((t for t in self.lookup(scope, impl["trait"]) if t[0] == "type"), None) or ("ext", impl["trait"])
                    table.setdefault(target, []).append({**impl, "file": file, "trait_entity": trait})
            self._impls = table
        return self._impls

    def implementers(self, trait: Entity) -> List[Entity]:
        if self._implementers is None:
            table: Dict[Entity, List[Entity]] = {}
            for entity, impls in self.impls_by_type().items():
                for implemented in dict.fromkeys(impl["trait_entity"] for impl in impls if impl["trait_entity"] is not None):
                    table.setdefault(implemented, []).append(entity)
            self._implementers = table
        return list(self._implementers.get(trait, ()))

    def self_entity_of(self, file: str, qualified: str) -> Optional[Entity]:
        """The type whose `impl` block in `file` holds the method `qualified` (the last one, when several do)."""
        if self._self_entities is None:
            table: Dict[Tuple[str, str], Entity] = {}
            for entity, impls in self.impls_by_type().items():
                for record in impls:
                    for method in record["methods"]:
                        table[(record["file"], method)] = entity
            self._self_entities = table
        return self._self_entities.get((file, qualified))

    def files_of_crate(self, crate: Optional[str]) -> List[str]:
        if self._crate_files is None:
            table: Dict[str, List[str]] = {}
            for file, name in self.crate_of_file.items():
                table.setdefault(name, []).append(file)
            self._crate_files = table
        return self._crate_files.get(crate, []) if crate is not None else []

    def trait_defaults(self, trait: Entity, name: str) -> List[str]:
        if trait[0] != "type" or trait[4] != "trait":
            return []
        return list(self.by_qualified.get((trait[1], f"{trait[2]}::{trait[3]}::{name}" if trait[2] else f"{trait[3]}::{name}"), []))

    def associated(self, entity: Entity, name: str, ids: bool = True):
        """Functions called `name` that belong to the type `entity`: its own `impl` blocks first, then the traits it
        implements. With `ids=False`, `(file, qualified)` pairs."""
        memo_key = (entity, name, ids)
        memo = self._associated.get(memo_key)
        if memo is not None:
            return memo
        found: List[Tuple[str, str]] = []
        impls = self.impls_by_type().get(entity, [])
        for pass_traits in (False, True):
            for impl in impls:
                if (impl["trait_entity"] is not None) != pass_traits:
                    continue
                for method in impl["methods"]:
                    if method.rsplit("::", 1)[-1] == name:
                        found.append((impl["file"], method))
                if pass_traits and impl["trait_entity"] is not None:
                    if self.trait_defaults(impl["trait_entity"], name):
                        owner = impl["trait_entity"]
                        found.append((owner[1], f"{owner[2]}::{owner[3]}::{name}" if owner[2] else f"{owner[3]}::{name}"))
            if found:
                break
        if entity[0] == "type" and entity[4] == "trait" and not found:
            owner = entity
            qualified = f"{owner[2]}::{owner[3]}::{name}" if owner[2] else f"{owner[3]}::{name}"
            if self.by_qualified.get((owner[1], qualified)):
                found.append((owner[1], qualified))
        found = list(dict.fromkeys(found))
        if ids:
            found = [function_id for file, qualified in found for function_id in self.by_qualified.get((file, qualified), [])]
        self._associated[memo_key] = found
        return found


def _scope_of(index: RustIndex, file: str, caller_qualified: str) -> Scope:
    """The module a function sits in: the longest prefix of its qualified name that is an inline module."""
    parts = caller_qualified.split("::")
    mods = index._inline_mods(file)
    for count in range(len(parts) - 1, 0, -1):
        prefix = "::".join(parts[:count])
        if prefix in mods:
            return (file, prefix)
    return (file, "")


def resolve_rust_calls(
    caller_id: str, caller_file: str, calls: List, declared_types: Dict[str, str], index: RustIndex,
) -> Resolution:
    """Resolve every call of one Rust function."""
    index.build()
    result = Resolution()
    caller_qualified = qualified_name(caller_id)
    scope = _scope_of(index, caller_file, caller_qualified)
    facts = index.facts.get(caller_file, {})

    self_entity: Optional[Entity] = index.self_entity_of(caller_file, caller_qualified)
    if self_entity is None:
        for trait in facts.get("traits", []):
            qualified_prefix = f"{trait['mod']}::{trait['name']}" if trait["mod"] else trait["name"]
            if caller_qualified.startswith(qualified_prefix + "::"):
                self_entity = next(iter(index.types_in((caller_file, trait["mod"]), trait["name"])), None)

    by_callee: Dict[str, List[str]] = {}
    confidence: Dict[str, str] = {}

    def record(found: List[Tuple[str, str]], text: str) -> None:
        for callee_id, tier in found:
            by_callee.setdefault(callee_id, []).append(text)
            if callee_id not in confidence or _RANK[tier] > _RANK[confidence[callee_id]]:
                confidence[callee_id] = tier

    def functions(entities: List[Entity]) -> List[str]:
        return [i for e in entities if e[0] == "fn" for i in index.by_qualified.get((e[1], e[2]), [])]

    def external(parts: List[str]) -> bool:
        return parts[0] in _EXTERNAL_ROOTS or parts[0] in _PRELUDE or any(e[0] == "ext" for e in index.resolve_path(scope, parts))

    def through_type(entity: Entity, name: str, tier: str) -> List[Tuple[str, str]]:
        if entity[0] == "type" and entity[4] == "trait":
            ids = list(index.associated(entity, name))
            for implementer in index.implementers(entity):
                ids.extend(index.associated(implementer, name))
            return _targets(ids, tier) if ids else []
        ids = index.associated(entity, name)
        return _targets(ids, tier) if ids else []

    def type_entity(type_name: str) -> Optional[Entity]:
        entities = index.resolve_path(scope, type_name.split("::"))
        return next((e for e in entities if e[0] == "type"), None) or next((e for e in entities if e[0] == "ext"), None)

    for call in calls:
        kind = getattr(call, "kind", "call")
        name, receiver, text = call.name, call.receiver, call.text
        found: List[Tuple[str, str]] = []
        reason = "not found"

        if kind == "macro":
            candidates = []
            for file in index.files_of_crate(index.crate_of_file.get(caller_file)):
                candidates.extend(index.by_qualified.get((file, name), []))
            found = _targets(candidates) if candidates else []
            reason = "macro"
        elif receiver is None:
            local = [i for ancestor in _ancestors(caller_qualified) for i in index.by_qualified.get((caller_file, f"{ancestor}::{name}"), [])]
            entities = [("fn", caller_file, f"{_ancestors(caller_qualified)[0]}::{name}")] if local else index.lookup(scope, name)
            ids = local or functions(entities)
            found = _targets(ids) if ids else []
            if not found:
                reason = "external package" if (name in _PRELUDE or any(e[0] == "ext" for e in entities) or not entities and name[:1].isupper()) else "not found"
        elif kind == "path":
            parts = receiver.split("::")
            if parts == ["Self"] and self_entity is not None:
                found = through_type(self_entity, name, "EXTRACTED")
            else:
                targets = index.resolve_path(scope, parts)
                ids: List[str] = []
                for entity in targets:
                    if entity[0] == "mod":
                        ids.extend(functions(index.lookup((entity[1], entity[2]), name, True)))
                    elif entity[0] == "type":
                        found.extend(through_type(entity, name, "EXTRACTED"))
                if ids:
                    found.extend(_targets(ids))
                if not found:
                    reason = "external package" if external(parts) else "not found"
        elif receiver == "self":
            found = through_type(self_entity, name, "EXTRACTED") if self_entity is not None else []
            reason = "unknown receiver"
        else:
            type_name = declared_types.get(receiver)
            entity = type_entity(type_name) if type_name else None
            if entity is None:
                reason = "unknown receiver"
            elif entity[0] == "ext":
                reason = "external package"
            else:
                found = through_type(entity, name, "INFERRED")

        if found:
            record(found, text)
        else:
            result.unresolved[text] = reason
    result.resolved = [ResolvedCall(callee_id=callee, call_site="; ".join(sites), confidence=confidence[callee]) for callee, sites in by_callee.items()]
    return result


def _ancestors(qualified: str) -> List[str]:
    """The caller itself and the names it is nested in, innermost first, for a call to a nested function."""
    parts = qualified.split("::")
    return ["::".join(parts[:count]) for count in range(len(parts), 0, -1)]


def build_rust_index(conn, facts_of: Callable[[str], dict], known_files: Iterable[str], root=None) -> RustIndex:
    """Collect every Rust function of the project and the module facts of its files."""
    index = RustIndex(known_files=set(known_files))
    for function_id, file_path in conn.execute(
        "SELECT function_id, file_path FROM artifacts WHERE language = 'rust' AND function_id NOT IN (SELECT function_id FROM pending_removals)"
    ).fetchall():
        index.by_qualified.setdefault((file_path, qualified_name(function_id)), []).append(function_id)
        index.known_files.add(file_path)
    for file_path in list(index.known_files):
        if file_path.endswith(".rs"):
            index.facts[file_path] = facts_of(file_path)
    index.crates = discover_crates(root, index.known_files)
    return index
