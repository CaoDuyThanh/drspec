"""Tests for the reference resolution engine.

Hand-built `PropertyReadSite`/`ReferenceResolutionInput`s — not real
parser output, which is covered separately by `test_python_parser.py`'s
property-read extraction tests and the integration test in
`test_scan_graph_sync.py`. Mirrors `test_call_resolver.py`'s style.
"""

from drspec.core.reference_resolver import (
    PropertyReadSite,
    ReferenceResolutionInput,
    resolve_references,
)


def _read(receiver: str, name: str, text: str = "") -> PropertyReadSite:
    return PropertyReadSite(receiver=receiver, name=name, text=text or f"{receiver}.{name}")


class TestSelfThisResolution:
    """Receiver is a self/this alias — resolved against the caller's own class."""

    def test_resolves_self_property(self):
        fact = ReferenceResolutionInput(
            caller_id="a.py::Foo.show",
            property_reads=[_read("self", "value")],
            caller_parent="Foo",
            self_aliases=["self"],
            properties_by_name={"Foo": {"value": "a.py::Foo.value"}},
        )

        resolved = resolve_references(fact)

        assert len(resolved) == 1
        assert resolved[0].callee_id == "a.py::Foo.value"
        assert resolved[0].confidence == "EXTRACTED"
        assert "self.value" in resolved[0].reference_site

    def test_skips_when_name_is_not_a_known_property(self):
        """A regular method or field read via self/this is never
        confused for a property — only names in `properties_by_name`
        qualify (never guessed)."""
        fact = ReferenceResolutionInput(
            caller_id="a.py::Foo.show",
            property_reads=[_read("self", "regular_method")],
            caller_parent="Foo",
            self_aliases=["self"],
            properties_by_name={"Foo": {"value": "a.py::Foo.value"}},
        )

        assert resolve_references(fact) == []

    def test_skips_when_receiver_is_not_a_self_alias(self):
        """A method where the first param is named `order` (not `self`)
        means `self.x`-looking reads inside it don't actually refer to
        the instance, unless `self_aliases` says so."""
        fact = ReferenceResolutionInput(
            caller_id="a.py::Foo.show",
            property_reads=[_read("self", "value")],
            caller_parent="Foo",
            self_aliases=["order"],
            properties_by_name={"Foo": {"value": "a.py::Foo.value"}},
        )

        assert resolve_references(fact) == []

    def test_not_a_method_means_no_self_resolution(self):
        """A free function has no `caller_parent` — self/this resolution
        never applies regardless of `self_aliases`."""
        fact = ReferenceResolutionInput(
            caller_id="a.py::helper",
            property_reads=[_read("self", "value")],
            caller_parent=None,
            self_aliases=["self"],
            properties_by_name={"Foo": {"value": "a.py::Foo.value"}},
        )

        assert resolve_references(fact) == []


class TestDeclaredTypeResolution:
    """Receiver's class comes from a static type annotation."""

    def test_resolves_via_declared_type(self):
        fact = ReferenceResolutionInput(
            caller_id="a.py::show",
            property_reads=[_read("c", "value")],
            declared_types={"c": "Calculator"},
            properties_by_name={"Calculator": {"value": "a.py::Calculator.value"}},
        )

        resolved = resolve_references(fact)

        assert len(resolved) == 1
        assert resolved[0].callee_id == "a.py::Calculator.value"
        assert resolved[0].confidence == "EXTRACTED"

    def test_skips_when_receiver_has_no_declared_type(self):
        """No static annotation reachable for this receiver — never
        guessed, even though the name `value` happens to be a known
        property somewhere in the project."""
        fact = ReferenceResolutionInput(
            caller_id="a.py::show",
            property_reads=[_read("c", "value")],
            declared_types={},
            properties_by_name={"Calculator": {"value": "a.py::Calculator.value"}},
        )

        assert resolve_references(fact) == []

    def test_skips_when_declared_type_has_no_such_property(self):
        fact = ReferenceResolutionInput(
            caller_id="a.py::show",
            property_reads=[_read("c", "nonexistent")],
            declared_types={"c": "Calculator"},
            properties_by_name={"Calculator": {"value": "a.py::Calculator.value"}},
        )

        assert resolve_references(fact) == []


class TestSuperNeverResolves:
    def test_super_property_read_is_always_unresolved(self):
        """Inheritance-chain resolution is out of scope (mirrors
        `call_resolver`'s `SUPER_RECEIVER` handling for calls)."""
        fact = ReferenceResolutionInput(
            caller_id="a.py::Foo.show",
            property_reads=[_read("super", "value")],
            caller_parent="Foo",
            self_aliases=["self"],
            properties_by_name={"Foo": {"value": "a.py::Foo.value"}},
        )

        assert resolve_references(fact) == []


class TestMergeAcrossSites:
    def test_multiple_reads_of_same_property_merge_into_one_row(self):
        fact = ReferenceResolutionInput(
            caller_id="a.py::Foo.show",
            property_reads=[_read("self", "value", "self.value"), _read("self", "value", "self.value")],
            caller_parent="Foo",
            self_aliases=["self"],
            properties_by_name={"Foo": {"value": "a.py::Foo.value"}},
        )

        resolved = resolve_references(fact)

        assert len(resolved) == 1
        assert resolved[0].reference_site.count("self.value") == 2

    def test_reads_of_different_properties_produce_separate_rows(self):
        fact = ReferenceResolutionInput(
            caller_id="a.py::Foo.show",
            property_reads=[_read("self", "value"), _read("self", "label")],
            caller_parent="Foo",
            self_aliases=["self"],
            properties_by_name={"Foo": {"value": "a.py::Foo.value", "label": "a.py::Foo.label"}},
        )

        resolved = resolve_references(fact)

        assert {r.callee_id for r in resolved} == {"a.py::Foo.value", "a.py::Foo.label"}
