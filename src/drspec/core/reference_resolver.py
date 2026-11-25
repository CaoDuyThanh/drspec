"""Resolves non-call dependencies — currently Python `@property` reads
(attribute access with no trailing `()`), which the call resolver never
sees since it only walks `call` AST nodes.

Smaller than the call resolver: a property read is always instance-bound,
so only self/this and declared-type lookups apply, and nothing is ever
guessed from a bare name — every resolved reference is `EXTRACTED`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Sentinel for `super().attr`; inheritance-chain resolution is out of
# scope, so this is never resolved.
SUPER_RECEIVER = "super"


@dataclass
class PropertyReadSite:
    """One non-call attribute-access expression, extracted from an AST
    node — never from text scanning.

    Mirrors `drspec.parsers.models.RawPropertyRead` field-for-field so
    this module never needs to import from `drspec.parsers`.
    """

    receiver: str
    name: str
    text: str


@dataclass
class ResolvedReference:
    """One resolved `(callee_id, reference_site)`, merged across every
    read site in this caller that targets the same property. Confidence
    is always `EXTRACTED` — see this module's docstring for why."""

    callee_id: str
    reference_site: str
    confidence: str = "EXTRACTED"


@dataclass
class ReferenceResolutionInput:
    """The fact set this resolver needs for one caller — the `references`
    counterpart of `call_resolver.ResolutionInput`, with only the fields
    a property read can actually use."""

    caller_id: str
    property_reads: list[PropertyReadSite] = field(default_factory=list)
    caller_parent: Optional[str] = None
    """Enclosing class name, if this function is a method."""
    self_aliases: list[str] = field(default_factory=list)
    """Receiver identifiers that mean "this instance" for this caller —
    same meaning as `call_resolver.ResolutionInput.self_aliases`."""
    properties_by_name: dict[str, dict[str, str]] = field(default_factory=dict)
    """scoped class identity -> {property_name: function_id}, from
    `project_facts.build_properties_by_name` — backs the self/this lookup."""
    declared_types: dict[str, str] = field(default_factory=dict)
    """Receiver variable name -> class name, from a static annotation —
    same meaning as `call_resolver.ResolutionInput.declared_types`, backs
    the declared-type lookup."""


def resolve_references(fact: ReferenceResolutionInput) -> list[ResolvedReference]:
    """Resolve every property read in `fact.property_reads` against
    `fact`. Multiple read sites to the same property merge into one
    `ResolvedReference`.

    Returns:
        One `ResolvedReference` per distinct property this caller reads.
    """
    sites_by_callee: dict[str, list[str]] = {}

    for read in fact.property_reads:
        callee_id = _resolve_one(fact, read)
        if callee_id is not None:
            sites_by_callee.setdefault(callee_id, []).append(read.text)

    return [
        ResolvedReference(callee_id=callee_id, reference_site="; ".join(sites))
        for callee_id, sites in sites_by_callee.items()
    ]


def _resolve_one(fact: ReferenceResolutionInput, read: PropertyReadSite) -> Optional[str]:
    if read.receiver == SUPER_RECEIVER:
        return None  # inheritance-chain resolution out of scope

    if fact.caller_parent and read.receiver in fact.self_aliases:
        properties = fact.properties_by_name.get(fact.caller_parent, {})
        return properties.get(read.name)

    declared_type = fact.declared_types.get(read.receiver)
    if declared_type:
        properties = fact.properties_by_name.get(declared_type, {})
        return properties.get(read.name)

    return None  # receiver's class isn't statically known — never guessed
