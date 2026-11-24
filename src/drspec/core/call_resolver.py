"""Call resolution engine.

Resolves `CallSite`s extracted directly from real `call`/`call_expression`
AST nodes, never from scanning `caller_body` text — so a docstring,
comment, or function signature can never be mistaken for a call.

Resolution order:
1. Bare imported name, module-qualified call, same-file bare call (with a
   class-membership check for C++'s implicit `this`, and an ambiguity
   guard for same-file name collisions).
2. `self`/`this` calls against the enclosing class, then other attribute
   calls via a declared-type annotation. `super()`/`super` is always
   unresolved (see `SUPER_RECEIVER`).
3. Opt-in (`deep_scan=True`) project-wide name-match fallback, for bare
   calls only. In this resolver an unresolved attribute call never falls
   through here: guessing a method with no idea what class it's on is a
   worse guess than a bare free-function guess by name alone.

C++ has its own resolver (`cpp_resolver`), and there an unresolved member call
falls back, when `resolve.infer_by_unique_name` is on (the default), only to a
name with exactly one method definition in the project, as INFERRED, and never
to several (nor to a free function): a wrong edge is worse than none for impact
analysis. It is tried only for a receiver whose type could not be worked out from
what is declared, so it never hides a call that an exact rule can resolve, and
the `--deep-scan` behaviour above is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from drspec.core.identity import qualified_name

_CONFIDENCE_RANK = {"AMBIGUOUS": 0, "INFERRED": 1, "EXTRACTED": 2}

# Sentinel for a super()/super call — always unresolved; inheritance-chain resolution is out of scope.
SUPER_RECEIVER = "super"


@dataclass
class CallSite:
    """One real call expression, extracted from a `call`/`call_expression`
    AST node — never from text scanning.

    Mirrors `drspec.parsers.models.RawCall` field-for-field, kept as a
    separate type so this module (in `core`) never needs to import from
    `parsers`; `graph_sync.py` converts between the two.
    """

    receiver: Optional[str]
    name: str
    text: str


@dataclass
class ResolvedCall:
    """One resolved `(callee_id, call_site, confidence)` result, merged
    across every call site in this caller that targets the same callee —
    one row per `(caller_id, callee_id)` pair, taking the highest
    confidence among the merged call sites."""

    callee_id: str
    call_site: str
    confidence: str  # "EXTRACTED" | "INFERRED" | "AMBIGUOUS"


@dataclass
class ResolutionInput:
    """The fact set the resolution tree needs for one caller.

    Every field is a real extracted fact — imports, call sites, and
    declared types all come from AST nodes, never text-scanned or guessed.
    """

    caller_id: str
    call_sites: list[CallSite] = field(default_factory=list)
    caller_parent: Optional[str] = None
    """Enclosing class name, if this function is a method."""
    bare_calls_imply_self: bool = False
    """Whether a bare call (no receiver) should be checked against the
    enclosing class first — true only for C++, where `this->` is optional
    so `bar()` can silently mean `this->bar()`. Python/JS always require
    an explicit `self.`/`this.`, so a bare call there never implicitly
    targets a same-named sibling method."""
    self_aliases: list[str] = field(default_factory=list)
    """Receiver identifiers that mean "this instance" for this caller —
    e.g. `["order"]` for a Python method whose first parameter is named
    `order` (not necessarily `self`), or `["this"]` for JS/C++. Never
    includes `"super"` — see `SUPER_RECEIVER`."""
    same_file_functions: dict[str, list[str]] = field(default_factory=dict)
    """function_name -> [function_id, ...] for every function in the
    caller's own file, from this scan's fresh parse."""
    classes_by_name: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    """scoped class identity -> {method_name: [function_id, ...]}, for every class
    this resolver can reach (same-file or imported). More than one id for a name
    means overloads."""
    imported_names: dict[str, list[str]] = field(default_factory=dict)
    """Bare name -> [function_id, ...], for names bound by `from X import Y`."""
    imported_modules: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    """Module alias -> {attribute_name: [function_id, ...]}, for names bound by
    `import X [as alias]`."""
    declared_types: dict[str, str] = field(default_factory=dict)
    """Receiver variable name -> class name, from a static annotation
    reachable from this caller's own signature."""
    deep_scan: bool = False
    """Whether this invocation asked for the opt-in project-wide fallback."""
    project_function_index: dict[str, list[str]] = field(default_factory=dict)
    """function_name -> [function_id, ...], project-wide — only consulted
    when `deep_scan` is True."""


def resolve_calls(fact: ResolutionInput) -> list[ResolvedCall]:
    """Resolve every call site in `fact.call_sites` against `fact`.
    Multiple call sites to the same callee merge into one `ResolvedCall`,
    taking the highest-confidence tier among them.

    Returns:
        One `ResolvedCall` per distinct callee this caller resolves to.
    """
    call_sites_by_callee: dict[str, list[str]] = {}
    confidence_by_callee: dict[str, str] = {}

    for call in fact.call_sites:
        if call.receiver is None:
            resolved = _resolve_bare(fact, call.name)
        elif call.receiver == SUPER_RECEIVER:
            resolved = None
        else:
            resolved = _resolve_attribute(fact, call.receiver, call.name)

        if not resolved:
            continue

        for callee_id, confidence in resolved:
            call_sites_by_callee.setdefault(callee_id, []).append(call.text)
            current = confidence_by_callee.get(callee_id)
            if current is None or _CONFIDENCE_RANK[confidence] > _CONFIDENCE_RANK[current]:
                confidence_by_callee[callee_id] = confidence

    return [
        ResolvedCall(callee_id=callee_id, call_site="; ".join(sites), confidence=confidence_by_callee[callee_id])
        for callee_id, sites in call_sites_by_callee.items()
    ]


def _resolve_bare(fact: ResolutionInput, name: str) -> "list[tuple[str, str]] | None":
    """Resolve a bare imported name, then same-file (with the C++-only
    class-membership check first), then the opt-in fallback.
    """
    if name in fact.imported_names:
        return _targets(fact.imported_names[name])

    if fact.bare_calls_imply_self and fact.caller_parent:
        own_class_methods = fact.classes_by_name.get(fact.caller_parent, {})
        if name in own_class_methods:
            return _targets(own_class_methods[name])

    candidates = fact.same_file_functions.get(name)
    if candidates and (len(candidates) == 1 or len({qualified_name(candidate) for candidate in candidates}) == 1):
        return _targets(candidates)

    # Zero matches, or same-file functions of different qualified names (which one is
    # meant is unknown): unresolved here. deep_scan's project-wide search covers the
    # ambiguous case too, so no separate ambiguity guard is needed.
    if fact.deep_scan:
        return _resolve_fallback(fact, name)

    return None


def _targets(function_ids: list[str]) -> "list[tuple[str, str]]":
    """One function: it. Several definitions of one name (overloads): the call may reach
    any of them, so every one is an AMBIGUOUS target rather than none or an arbitrary one."""
    if len(function_ids) == 1:
        return [(function_ids[0], "EXTRACTED")]
    return [(function_id, "AMBIGUOUS") for function_id in function_ids]


def _resolve_attribute(fact: ResolutionInput, base: str, name: str) -> "list[tuple[str, str]] | None":
    """Resolve module-qualified calls, self/this calls, and declared-type
    attribute calls.

    Unresolved attribute calls never fall through to the project-wide
    fallback — see the module docstring for why.
    """
    if fact.caller_parent and base in fact.self_aliases:
        class_methods = fact.classes_by_name.get(fact.caller_parent, {})
        return _targets(class_methods[name]) if name in class_methods else None

    declared_type = fact.declared_types.get(base)
    if declared_type:
        class_methods = fact.classes_by_name.get(declared_type, {})
        return _targets(class_methods[name]) if name in class_methods else None

    if base in fact.imported_modules:
        targets = fact.imported_modules[base].get(name)
        return _targets(targets) if targets else None

    return None


def _resolve_fallback(fact: ResolutionInput, name: str) -> "list[tuple[str, str]] | None":
    """Exactly one project-wide match -> INFERRED; more than one -> one
    AMBIGUOUS entry per candidate; zero -> unresolved."""
    candidates = fact.project_function_index.get(name, [])
    if len(candidates) == 1:
        return [(candidates[0], "INFERRED")]
    if len(candidates) > 1:
        return [(callee_id, "AMBIGUOUS") for callee_id in candidates]
    return None
