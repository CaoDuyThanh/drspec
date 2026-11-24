"""Call resolution for JavaScript and TypeScript.

A call is tied to a function through what the file says about names, never through the call's
spelling alone:

1. a bare call is a function declared in the caller (nested), in its namespace, or at the top of
   its file, or a name the file imported (named, default, aliased, `require`, re-exported);
2. `new X(...)` and `<X />` name a class (its `constructor`) or a component;
3. `ns.f()` where `ns` is a namespace import, a class (static member), an object literal, a
   TypeScript namespace or a `require`d module;
4. `this.m()` and `super.m()` look in the caller's class and its parents;
5. `v.m()` where `v` has a declared type, or was created with `new X()`, looks in `X`; an
   interface or an abstract class resolves to its implementers.

An export is followed through `export { a as b } from` and `export * from` (a barrel), at most
eight files deep, with a visited set. A name that comes from a package outside the project is
counted as external and is not guessed.

One match is `EXTRACTED`; a match through a declared type is `INFERRED`; several candidates
(overloads, or implementers of an interface) are one `AMBIGUOUS` edge each.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from drspec.core.identity import qualified_name
from drspec.core.js_modules import JsConfig, resolve_module
from drspec.core.call_resolver import ResolvedCall

JS_LANGUAGES = ("javascript", "typescript", "tsx")
_RANK = {"AMBIGUOUS": 0, "INFERRED": 1, "EXTRACTED": 2}
_MAX_DEPTH = 8

# Entities a name can stand for.
Entity = Tuple[str, str, str]  # (kind, file, name): kind is fn | class | iface | module | obj | ns
IMPLICIT_CONSTRUCTOR = "implicit constructor"
"""Recorded for `new X()` of a class that declares no constructor. It is no edge, but it is not unresolved either:
the health figures count it out."""
UNRESOLVED_REASONS = ("external package", "unknown receiver", "dynamic", "no constructor", "not found")


@dataclass
class JsFileFacts:
    bindings: Dict[str, dict] = field(default_factory=dict)
    exports: Dict[str, str] = field(default_factory=dict)
    reexports: List[dict] = field(default_factory=list)
    classes: Dict[str, dict] = field(default_factory=dict)
    interfaces: Set[str] = field(default_factory=set)
    namespaces: Set[str] = field(default_factory=set)
    objects: Set[str] = field(default_factory=set)

    @staticmethod
    def from_dict(raw: dict) -> "JsFileFacts":
        facts = JsFileFacts()
        for binding in raw.get("bindings", []):
            facts.bindings[binding["local"]] = binding
        facts.exports = dict(raw.get("exports", {}))
        facts.reexports = list(raw.get("reexports", []))
        for cls in raw.get("classes", []):
            facts.classes[cls["name"]] = cls
        facts.interfaces = {cls["name"] for cls in raw.get("classes", []) if cls.get("interface")}
        facts.namespaces = set(raw.get("namespaces", []))
        facts.objects = set(raw.get("objects", []))
        return facts


@dataclass
class JsIndex:
    facts: Dict[str, JsFileFacts] = field(default_factory=dict)
    by_qualified: Dict[Tuple[str, str], List[str]] = field(default_factory=dict)
    known_files: Set[str] = field(default_factory=set)
    config: JsConfig = field(default_factory=JsConfig)
    _modules: Dict[Tuple[str, str], Optional[str]] = field(default_factory=dict)
    _implementers: Optional[Dict[Entity, List[Entity]]] = None

    def module(self, from_file: str, spec: str) -> Optional[str]:
        key = (from_file, spec)
        if key not in self._modules:
            self._modules[key] = resolve_module(from_file, spec, self.known_files, self.config)
        return self._modules[key]

    # ---- names

    def local(self, file: str, name: str, trail: Tuple = ()) -> List[Entity]:
        """What `name` means inside `file`: something declared there, or an import."""
        facts = self.facts.get(file)
        found: List[Entity] = []
        if self.by_qualified.get((file, name)):
            found.append(("fn", file, name))
        if facts is not None:
            if name in facts.classes:
                found.append(("iface" if name in facts.interfaces else "class", file, name))
            if name in facts.objects:
                found.append(("obj", file, name))
            if name in facts.namespaces:
                found.append(("ns", file, name))
            if not found and name in facts.bindings:
                found = self._through_binding(file, facts.bindings[name], trail)
        return found

    def _through_binding(self, file: str, binding: dict, trail: Tuple) -> List[Entity]:
        target = self.module(file, binding["module"])
        if target is None:
            return []
        if binding["name"] == "*":
            return [("module", target, "")]
        return self.exported(target, binding["name"], trail)

    def exported(self, file: str, name: str, trail: Tuple = (), depth: int = 0) -> List[Entity]:
        """What `file` exports as `name`, following re-exports."""
        key = (file, name)
        if key in trail or depth > _MAX_DEPTH:
            return []
        trail = trail + (key,)
        facts = self.facts.get(file)
        if facts is None:
            return []
        local = facts.exports.get(name)
        if local is not None:
            found = self.local(file, local, trail)
            if found:
                return found
        found = []
        for reexport in facts.reexports:
            target = self.module(file, reexport["module"])
            if target is None:
                continue
            if reexport["name"] == "*" and reexport.get("alias") is None:
                if name != "default":
                    found.extend(self.exported(target, name, trail, depth + 1))
            elif reexport["name"] == "*":
                if reexport["alias"] == name:
                    found.append(("module", target, ""))
            elif (reexport.get("alias") or reexport["name"]) == name:
                found.extend(self.exported(target, reexport["name"], trail, depth + 1))
        return _unique(found)

    # ---- members

    def members(self, entity: Entity, name: str, trail: Tuple = ()) -> List[str]:
        """Function ids named `name` in a class (and its parents), object, namespace or module."""
        kind, file, owner = entity
        if kind in ("class", "obj", "ns"):
            own = self.by_qualified.get((file, f"{owner}.{name}"), [])
            if own:
                return list(own)
            if kind == "class":
                return self._inherited(entity, name, trail)
            return []
        if kind == "module":
            return [i for e in self.exported(file, name) if e[0] == "fn" for i in self.by_qualified.get((e[1], e[2]), [])]
        if kind == "fn":
            return []
        return []

    def _inherited(self, entity: Entity, name: str, trail: Tuple) -> List[str]:
        kind, file, owner = entity
        if entity in trail or len(trail) > _MAX_DEPTH:
            return []
        facts = self.facts.get(file)
        parent = (facts.classes.get(owner) or {}).get("extends") if facts else None
        if not parent:
            return []
        for base in self.local(file, parent, trail + (entity,)):
            if base[0] in ("class", "iface"):
                found = self.members(base, name, trail + (entity,))
                if found:
                    return found
        return []

    def parent_class(self, entity: Entity) -> Optional[Entity]:
        kind, file, owner = entity
        facts = self.facts.get(file)
        parent = (facts.classes.get(owner) or {}).get("extends") if facts else None
        if not parent:
            return None
        for base in self.local(file, parent):
            if base[0] in ("class", "iface"):
                return base
        return None

    def implementers(self, entity: Entity) -> List[Entity]:
        """Classes that implement or extend `entity` (an interface or abstract class), in any file."""
        if self._implementers is None:
            table: Dict[Entity, List[Entity]] = {}
            for file, facts in self.facts.items():
                for name, cls in facts.classes.items():
                    if cls.get("interface"):
                        continue
                    for base in ([cls["extends"]] if cls.get("extends") else []) + list(cls.get("implements", [])):
                        for target in self.local(file, base):
                            if target[0] in ("class", "iface"):
                                table.setdefault(target, []).append(("class", file, name))
            self._implementers = table
        return self._implementers.get(entity, [])


def _unique(entities: Iterable[Entity]) -> List[Entity]:
    seen: List[Entity] = []
    for entity in entities:
        if entity not in seen:
            seen.append(entity)
    return seen


def _targets(function_ids: List[str], tier: str = "EXTRACTED") -> List[Tuple[str, str]]:
    ids = sorted(set(function_ids))
    if len(ids) == 1:
        return [(ids[0], tier)]
    return [(function_id, "AMBIGUOUS") for function_id in ids]


@dataclass
class Resolution:
    resolved: List[ResolvedCall] = field(default_factory=list)
    unresolved: Dict[str, str] = field(default_factory=dict)
    """call text -> why it was not tied to a function (one of UNRESOLVED_REASONS)."""


def resolve_js_calls(
    caller_id: str,
    caller_file: str,
    calls: List,
    declared_types: Dict[str, str],
    index: JsIndex,
    caller_class: Optional[str] = None,
) -> Resolution:
    """Resolve every call of one function. `calls` are `RawCall`-like objects with `receiver`, `name`, `text`, `kind`."""
    result = Resolution()
    caller_qualified = qualified_name(caller_id)
    scopes: List[str] = []  # enclosing function and namespace names, innermost first; a class is not a scope for a bare call
    parts = caller_qualified.split(".")
    caller_facts = index.facts.get(caller_file) or JsFileFacts()
    for count in range(len(parts) - 1, 0, -1):
        scope = ".".join(parts[:count])
        if scope not in caller_facts.classes and scope not in caller_facts.objects:
            scopes.append(scope)

    by_callee: Dict[str, List[str]] = {}
    confidence: Dict[str, str] = {}

    def record(found: List[Tuple[str, str]], text: str) -> None:
        for callee_id, tier in found:
            by_callee.setdefault(callee_id, []).append(text)
            if callee_id not in confidence or _RANK[tier] > _RANK[confidence[callee_id]]:
                confidence[callee_id] = tier

    def external(name: str) -> bool:
        binding = (index.facts.get(caller_file) or JsFileFacts()).bindings.get(name)
        return binding is not None and index.module(caller_file, binding["module"]) is None and not binding["module"].startswith((".", "/"))

    def functions_named(entities: List[Entity]) -> List[str]:
        return [i for e in entities if e[0] == "fn" for i in index.by_qualified.get((e[1], e[2]), [])]

    def construct(entities: List[Entity]) -> Tuple[List[Tuple[str, str]], str]:
        ids: List[str] = []
        for entity in entities:
            if entity[0] == "class":
                ids.extend(index.members(entity, "constructor"))
        if ids:
            return _targets(ids), ""
        return [], "no constructor" if any(e[0] == "class" for e in entities) else "not found"

    def type_entities(type_name: str) -> List[Entity]:
        name = type_name.split("<")[0].strip()
        entities = index.local(caller_file, name.split(".")[0])
        if "." in name:  # `ns.Type`: look through the namespace or module
            head = [e for e in entities if e[0] in ("module", "ns")]
            entities = [t for e in head for t in (index.exported(e[1], name.split(".")[1]) if e[0] == "module" else [])]
        return entities

    def through_type(type_name: str, member: str, exact: bool = False) -> Tuple[List[Tuple[str, str]], str]:
        """The method `member` through a type. `exact` says the object is of exactly that class (it was just
        constructed), not of something that merely satisfies the type: no subclass can be what runs."""
        entities = type_entities(type_name)
        if not entities:
            return [], "external package" if external(type_name.split(".")[0]) else "unknown receiver"
        found: List[Tuple[str, str]] = []
        for entity in entities:
            if entity[0] in ("class", "iface"):
                direct = index.members(entity, member)
                if exact and entity[0] == "class" and direct:
                    found.extend(_targets(direct))
                    continue
                implementers = index.implementers(entity)
                if direct and entity[0] == "class" and not implementers:
                    found.extend(_targets(direct, "INFERRED"))
                    continue
                ids = list(direct)
                for implementer in implementers:
                    ids.extend(index.members(implementer, member))
                if ids:
                    found.extend(_targets(ids, "INFERRED"))
        return found, "" if found else "not found"

    for call in calls:
        kind = getattr(call, "kind", "call")
        name, receiver, text = call.name, call.receiver, call.text
        found: List[Tuple[str, str]] = []
        reason = "not found"

        if receiver is None:
            # nested function, namespace sibling, top-level function, import
            local_ids: List[str] = []
            for scope in scopes:
                local_ids = index.by_qualified.get((caller_file, f"{scope}.{name}"), [])
                if local_ids:
                    break
            entities = [("fn", caller_file, f"{scope}.{name}")] if local_ids else index.local(caller_file, name)
            if kind == "new":
                found, reason = construct(entities)
            elif kind == "jsx":
                ids = functions_named(entities)
                for entity in entities:
                    if entity[0] == "class":
                        ids.extend(index.members(entity, "render"))
                found = _targets(ids) if ids else []
            else:
                ids = functions_named(entities)
                for entity in entities:
                    if entity[0] == "module":  # `const f = require('./f')` where the module exports a function
                        ids.extend(index.members(entity, "default"))
                found = _targets(ids) if ids else []
            if not found and not (kind == "new" and reason == "no constructor"):
                reason = "external package" if external(name) else "not found"
        elif receiver == "super":
            klass = ("class", caller_file, caller_class) if caller_class else None
            base = index.parent_class(klass) if klass else None
            ids = index.members(base, "constructor" if kind == "super" else name) if base else []
            found = _targets(ids) if ids else []
            reason = "not found"
        elif receiver == "this":
            klass = ("class", caller_file, caller_class) if caller_class else None
            ids = index.members(klass, name) if klass else []
            found = _targets(ids) if ids else []
            reason = "unknown receiver"
        elif receiver in declared_types:
            found, reason = through_type(declared_types[receiver], name)
        elif receiver.startswith("new "):  # `new Repo().find(id)`: the method of the class just constructed
            found, reason = through_type(receiver[4:], name, exact=True)
        else:
            entities = index.local(caller_file, receiver.split(".")[0])
            if not entities:
                reason = "external package" if external(receiver) else "unknown receiver"
            else:
                ids: List[str] = []
                constructed: List[Entity] = []
                for entity in entities:
                    if entity[0] == "module":
                        resolved_members = index.exported(entity[1], name)
                        ids.extend(functions_named(resolved_members))
                        constructed.extend(e for e in resolved_members if e[0] == "class")
                    else:
                        ids.extend(index.members(entity, name))
                        if entity[0] == "ns":
                            constructed.extend(e for e in index.local(caller_file, f"{entity[2]}.{name}") if e[0] == "class")
                if kind == "new":
                    found, reason = construct(constructed)
                else:
                    found = _targets(ids) if ids else []
                    reason = "not found"
        if found:
            record(found, text)
        elif kind == "new" and reason == "no constructor":
            result.unresolved[text] = IMPLICIT_CONSTRUCTOR  # the class has the implicit constructor: nothing to link, and not a failure
        else:
            result.unresolved[text] = reason
    result.resolved = [ResolvedCall(callee_id=callee, call_site="; ".join(sites), confidence=confidence[callee]) for callee, sites in by_callee.items()]
    return result


def build_js_index(conn, facts_of: Callable[[str], dict], known_files: Iterable[str], config: Optional[JsConfig] = None) -> JsIndex:
    """Collect every JavaScript and TypeScript function of the project and the module facts of its files."""
    index = JsIndex(known_files=set(known_files), config=config or JsConfig())
    placeholders = ", ".join("?" for _ in JS_LANGUAGES)
    rows = conn.execute(
        f"SELECT function_id, file_path FROM artifacts WHERE language IN ({placeholders}) "
        "AND function_id NOT IN (SELECT function_id FROM pending_removals)",
        list(JS_LANGUAGES),
    ).fetchall()
    for function_id, file_path in rows:
        index.by_qualified.setdefault((file_path, qualified_name(function_id)), []).append(function_id)
        index.known_files.add(file_path)
        if file_path not in index.facts:
            index.facts[file_path] = JsFileFacts.from_dict(facts_of(file_path))
    for file_path in list(index.known_files):
        if file_path not in index.facts and file_path.endswith((".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")):
            index.facts[file_path] = JsFileFacts.from_dict(facts_of(file_path))
    return index
