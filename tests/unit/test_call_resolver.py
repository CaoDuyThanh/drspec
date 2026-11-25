"""Tests for the call resolution engine.

Uses hand-built `CallSite`/`ResolutionInput` fixtures rather than real parser
output, which is covered by the per-language parser tests and the
dogfooding/integration tests in `test_scan_graph_sync.py`. `resolve_calls()`
consumes pre-extracted `CallSite`s, so there's no raw-text ambiguity to guard
against at this layer.
"""

import pytest

from drspec.core.call_resolver import CallSite, ResolutionInput, resolve_calls

pytestmark = pytest.mark.unit  # hand-built inputs in, resolved calls out


def _bare(name: str, text: str = "") -> CallSite:
    return CallSite(receiver=None, name=name, text=text or f"{name}()")


def _attr(receiver: str, name: str, text: str = "") -> CallSite:
    return CallSite(receiver=receiver, name=name, text=text or f"{receiver}.{name}()")


def _calls(resolved) -> list:
    """What was resolved, in order: (callee, confidence, the call-site text(s))."""
    return [(r.callee_id, r.confidence, r.call_site) for r in resolved]


class TestSameFileResolution:
    """Same-file bare calls, including the ambiguity guard."""

    def test_resolves_single_same_file_candidate(self):
        fact = ResolutionInput(
            caller_id="a.py::foo",
            call_sites=[_bare("bar")],
            same_file_functions={"bar": ["a.py::bar"], "foo": ["a.py::foo"]},
        )

        resolved = resolve_calls(fact)

        assert _calls(resolved) == [("a.py::bar", "EXTRACTED", "bar()")]

    def test_skips_ambiguous_same_name(self):
        """Two same-named candidates in the file means unresolved (no
        edge), not a guess."""
        fact = ResolutionInput(
            caller_id="a.py::foo",
            call_sites=[_bare("bar")],
            same_file_functions={"bar": ["a.py::Bar.bar", "a.py::bar"]},
        )

        assert resolve_calls(fact) == []

    def test_skips_unknown_name(self):
        """A call to something not in the same-file function map (an
        import, a builtin, a method call, ...) resolves to nothing —
        out of scope for same-file resolution."""
        fact = ResolutionInput(
            caller_id="a.py::foo",
            call_sites=[_bare("len")],
            same_file_functions={"foo": ["a.py::foo"]},
        )

        assert resolve_calls(fact) == []

    def test_merges_multiple_call_sites_to_same_callee(self):
        """Two calls to the same callee merge into one ResolvedCall, with
        both call-site texts preserved."""
        fact = ResolutionInput(
            caller_id="a.py::foo",
            call_sites=[_bare("bar", "bar(1)"), _bare("bar", "bar(2)")],
            same_file_functions={"bar": ["a.py::bar"]},
        )

        resolved = resolve_calls(fact)

        assert _calls(resolved) == [("a.py::bar", "EXTRACTED", "bar(1); bar(2)")]  # one row, the texts joined in the order of the calls

    def test_does_not_resolve_cross_file_name(self):
        """A name not present in this file's own function map at all
        doesn't resolve — same-file resolution's whole scope."""
        fact = ResolutionInput(
            caller_id="a.py::foo",
            call_sites=[_bare("helper")],
            same_file_functions={"foo": ["a.py::foo"]},
        )

        assert resolve_calls(fact) == []

    def test_genuine_self_recursion_resolves(self):
        """A real recursive call resolves normally — since call sites
        come from real `call` AST nodes (never a function's own
        signature, which is a `function_definition` node, not a `call`
        one), there's no self-signature exclusion needed at all here."""
        fact = ResolutionInput(
            caller_id="a.py::foo",
            call_sites=[_bare("foo", "foo(n - 1)")],
            same_file_functions={"foo": ["a.py::foo"]},
        )

        resolved = resolve_calls(fact)

        assert _calls(resolved) == [("a.py::foo", "EXTRACTED", "foo(n - 1)")]


class TestStep1ImportedAndModuleQualified:

    def test_bare_imported_name_resolves_extracted(self):
        fact = ResolutionInput(
            caller_id="a.py::foo",
            call_sites=[_bare("charge_card", "charge_card(10)")],
            imported_names={"charge_card": ["billing.py::charge_card"]},
        )

        resolved = resolve_calls(fact)

        assert _calls(resolved) == [("billing.py::charge_card", "EXTRACTED", "charge_card(10)")]

    def test_module_qualified_call_resolves_extracted(self):
        fact = ResolutionInput(
            caller_id="a.py::foo",
            call_sites=[_attr("mymodule", "helper")],
            imported_modules={"mymodule": {"helper": ["mymodule.py::helper"]}},
        )

        resolved = resolve_calls(fact)

        assert _calls(resolved) == [("mymodule.py::helper", "EXTRACTED", "mymodule.helper()")]

    def test_module_qualified_unknown_attribute_is_not_a_guess(self):
        """`mymodule` is a known import, but `mymodule.missing()` isn't
        one of its tracked functions — no edge, and it must NOT fall
        through to Step 2b or the fallback."""
        fact = ResolutionInput(
            caller_id="a.py::foo",
            call_sites=[_attr("mymodule", "missing")],
            imported_modules={"mymodule": {"helper": ["mymodule.py::helper"]}},
            deep_scan=True,
            project_function_index={"missing": ["other.py::missing"]},
        )

        assert resolve_calls(fact) == []

    def test_class_membership_check_resolves_cpp_implicit_this(self):
        """Bare call matching a method on the same class resolves via
        implicit `this->` (C++ only, when `bare_calls_imply_self` is set),
        checked before same-file matching."""
        fact = ResolutionInput(
            caller_id="a.cpp::Widget::run",
            call_sites=[_bare("helper")],
            caller_parent="Widget",
            bare_calls_imply_self=True,
            classes_by_name={"Widget": {"helper": ["a.cpp::Widget::helper"]}},
        )

        resolved = resolve_calls(fact)

        assert _calls(resolved) == [("a.cpp::Widget::helper", "EXTRACTED", "helper()")]


    def test_bare_call_does_not_imply_self_for_python_or_js(self):
        """Regression: Python/JS require an explicit `self.`/`this.` for a
        method call — a bare call inside a method must not implicitly
        resolve to a same-named sibling method, and instead resolves like
        any other bare call (here, a top-level same-file function)."""
        fact = ResolutionInput(
            caller_id="a.js::Calculator.add",
            call_sites=[_bare("add", "add(this.value, x)")],
            caller_parent="Calculator",
            bare_calls_imply_self=False,
            classes_by_name={"Calculator": {"add": ["a.js::Calculator.add"]}},
            same_file_functions={"add": ["a.js::add"]},
        )

        resolved = resolve_calls(fact)

        assert _calls(resolved) == [("a.js::add", "EXTRACTED", "add(this.value, x)")]

    def test_declared_parameter_shadows_module_alias(self):
        """A locally declared parameter shadows the module import binding."""
        fact = ResolutionInput(
            caller_id="a.py::foo",
            call_sites=[_attr("thing", "run")],
            imported_modules={"thing": {"run": ["module_target.py::run"]}},
            declared_types={"thing": "SomeType"},
            classes_by_name={"SomeType": {"run": ["type_target.py::run"]}},
        )

        resolved = resolve_calls(fact)

        assert _calls(resolved) == [("type_target.py::run", "EXTRACTED", "thing.run()")]


class TestStep2aSelfThis:
    """self/this calls; `super` is tested separately below since it's
    never conflated with self/this."""

    def test_python_self_call_resolves_via_actual_first_param_name(self):
        """Not a hardcoded "self" string match — whatever the method's
        own first parameter is actually named."""
        fact = ResolutionInput(
            caller_id="a.py::Order.process",
            call_sites=[_attr("order", "validate")],
            caller_parent="Order",
            self_aliases=["order"],
            classes_by_name={"Order": {"validate": ["a.py::Order.validate"]}},
        )

        resolved = resolve_calls(fact)

        assert _calls(resolved) == [("a.py::Order.validate", "EXTRACTED", "order.validate()")]


    def test_js_this_call_resolves(self):
        fact = ResolutionInput(
            caller_id="a.js::Widget.run",
            call_sites=[_attr("this", "helper")],
            caller_parent="Widget",
            self_aliases=["this"],
            classes_by_name={"Widget": {"helper": ["a.js::Widget.helper"]}},
        )

        resolved = resolve_calls(fact)

        assert _calls(resolved) == [("a.js::Widget.helper", "EXTRACTED", "this.helper()")]

    def test_cpp_explicit_this_arrow_call_resolves(self):
        fact = ResolutionInput(
            caller_id="a.cpp::Widget::run",
            call_sites=[_attr("this", "helper")],
            caller_parent="Widget",
            self_aliases=["this"],
            classes_by_name={"Widget": {"helper": ["a.cpp::Widget::helper"]}},
        )

        resolved = resolve_calls(fact)

        assert _calls(resolved) == [("a.cpp::Widget::helper", "EXTRACTED", "this.helper()")]

    def test_self_method_not_found_on_class_is_not_a_guess(self):
        """Known scope limit: inherited methods aren't resolved — no
        fallback guess either, even under --deep-scan."""
        fact = ResolutionInput(
            caller_id="a.py::Order.process",
            call_sites=[_attr("self", "inherited_method")],
            caller_parent="Order",
            self_aliases=["self"],
            classes_by_name={"Order": {}},
            deep_scan=True,
            project_function_index={"inherited_method": ["base.py::Base.inherited_method"]},
        )

        assert resolve_calls(fact) == []

    def test_self_checked_before_declared_type(self):
        """Step-ordering: self/this is checked before the general
        declared-type case, even if the receiver also happens to have a
        declared-type entry."""
        fact = ResolutionInput(
            caller_id="a.py::Order.process",
            call_sites=[_attr("order", "validate")],
            caller_parent="Order",
            self_aliases=["order"],
            declared_types={"order": "WrongType"},
            classes_by_name={
                "Order": {"validate": ["a.py::Order.validate"]},
                "WrongType": {"validate": ["wrong.py::validate"]},
            },
        )

        resolved = resolve_calls(fact)

        assert _calls(resolved) == [("a.py::Order.validate", "EXTRACTED", "order.validate()")]


class TestSuperNeverResolves:
    """`super`-receiver call sites (Python's `super()`, JS's `super`, C++'s
    explicit base-class call) always stay unresolved and are never conflated
    with self/this; `CallSite(receiver="super", ...)` identifies them
    directly, with nothing to misparse."""

    def test_super_call_never_resolves_to_own_class(self):
        fact = ResolutionInput(
            caller_id="a.py::Child.__init__",
            call_sites=[CallSite(receiver="super", name="__init__", text='super().__init__("x")')],
            caller_parent="Child",
            self_aliases=["self"],
            classes_by_name={"Child": {"__init__": ["a.py::Child.__init__"]}},
        )

        assert resolve_calls(fact) == []


    def test_super_call_does_not_fall_back_to_a_same_file_guess(self):
        fact = ResolutionInput(
            caller_id="a.py::Child.default",
            call_sites=[CallSite(receiver="super", name="default", text="super().default(obj)")],
            caller_parent="Child",
            self_aliases=["self"],
            classes_by_name={"Child": {"default": ["a.py::Child.default"]}},
            same_file_functions={"default": ["a.py::unrelated_default"]},
        )

        assert resolve_calls(fact) == []

    def test_other_calls_in_the_same_body_still_resolve(self):
        """The super-exclusion is scoped to that one call site, not the
        whole caller."""
        fact = ResolutionInput(
            caller_id="a.py::Child.__init__",
            call_sites=[
                CallSite(receiver="super", name="__init__", text="super().__init__()"),
                _bare("helper"),
            ],
            caller_parent="Child",
            self_aliases=["self"],
            classes_by_name={"Child": {"__init__": ["a.py::Child.__init__"]}},
            same_file_functions={"helper": ["a.py::helper"]},
        )

        resolved = resolve_calls(fact)

        assert _calls(resolved) == [("a.py::helper", "EXTRACTED", "helper()")]


class TestStep2bDeclaredType:

    def test_declared_type_resolved_via_import(self):
        """The fact-builder is responsible for resolving the type name
        to a class regardless of origin; `resolve_calls()` just looks it
        up in `classes_by_name`."""
        fact = ResolutionInput(
            caller_id="a.py::handle",
            call_sites=[_attr("order", "process")],
            declared_types={"order": "Order"},
            classes_by_name={"Order": {"process": ["models.py::Order.process"]}},
        )

        resolved = resolve_calls(fact)

        assert _calls(resolved) == [("models.py::Order.process", "EXTRACTED", "order.process()")]

    def test_declared_type_resolved_via_same_file_class(self):
        fact = ResolutionInput(
            caller_id="a.py::handle",
            call_sites=[_attr("order", "process")],
            declared_types={"order": "Order"},
            classes_by_name={"Order": {"process": ["a.py::Order.process"]}},
        )

        resolved = resolve_calls(fact)

        assert _calls(resolved) == [("a.py::Order.process", "EXTRACTED", "order.process()")]


    def test_unannotated_receiver_never_guesses_even_with_deep_scan(self):
        """"Never guess a receiver type" — an attribute call with no
        declared type anywhere reachable gets no edge, even under
        --deep-scan."""
        fact = ResolutionInput(
            caller_id="a.py::handle",
            call_sites=[_attr("order", "process")],
            deep_scan=True,
            project_function_index={"process": ["models.py::Order.process"]},
        )

        assert resolve_calls(fact) == []

    def test_declared_type_method_not_found_is_not_a_guess(self):
        fact = ResolutionInput(
            caller_id="a.py::handle",
            call_sites=[_attr("order", "missing")],
            declared_types={"order": "Order"},
            classes_by_name={"Order": {}},
            deep_scan=True,
            project_function_index={"missing": ["other.py::missing"]},
        )

        assert resolve_calls(fact) == []


class TestStep3Fallback:
    """Opt-in project-wide fallback for bare calls only."""

    def test_disabled_by_default(self):
        fact = ResolutionInput(
            caller_id="a.py::foo",
            call_sites=[_bare("process")],
            project_function_index={"process": ["b.py::process"]},
        )

        assert resolve_calls(fact) == []

    def test_single_match_is_inferred(self):
        fact = ResolutionInput(
            caller_id="a.py::foo",
            call_sites=[_bare("process")],
            deep_scan=True,
            project_function_index={"process": ["b.py::process"]},
        )

        resolved = resolve_calls(fact)

        assert _calls(resolved) == [("b.py::process", "INFERRED", "process()")]

    def test_multiple_matches_are_ambiguous_one_row_each(self):
        fact = ResolutionInput(
            caller_id="a.py::foo",
            call_sites=[_bare("process")],
            deep_scan=True,
            project_function_index={"process": ["b.py::process", "c.py::process"]},
        )

        resolved = resolve_calls(fact)

        assert _calls(resolved) == [("b.py::process", "AMBIGUOUS", "process()"), ("c.py::process", "AMBIGUOUS", "process()")]

    def test_zero_matches_stays_unresolved(self):
        fact = ResolutionInput(
            caller_id="a.py::foo",
            call_sites=[_bare("external_lib_call")],
            deep_scan=True,
            project_function_index={},
        )

        assert resolve_calls(fact) == []

    def test_ambiguous_same_file_candidates_fall_through_to_fallback(self):
        """A same-file name collision doesn't resolve as EXTRACTED, but under
        --deep-scan the project-wide fallback covers the same candidates,
        resolving them as AMBIGUOUS rather than silently guessing."""
        fact = ResolutionInput(
            caller_id="a.py::foo",
            call_sites=[_bare("process")],
            same_file_functions={"process": ["a.py::bar.process", "a.py::process"]},
            deep_scan=True,
            project_function_index={"process": ["a.py::bar.process", "a.py::process"]},
        )

        resolved = resolve_calls(fact)

        assert _calls(resolved) == [("a.py::bar.process", "AMBIGUOUS", "process()"), ("a.py::process", "AMBIGUOUS", "process()")]


class TestConfidenceMergeAcrossSteps:
    """Call sites merged into one row take the highest confidence among
    them — verified here across call sites resolved by different steps,
    not just different occurrences of the same shape."""

    def test_extracted_call_site_wins_over_inferred_to_same_callee(self):
        """Two different call sites targeting the same callee: one
        resolves for certain (same-file), the other only via the opt-in
        fallback. The merged row must be EXTRACTED, not INFERRED."""
        fact = ResolutionInput(
            caller_id="a.py::foo",
            call_sites=[_bare("bar", "bar()"), _bare("some_alias_for_bar", "some_alias_for_bar()")],
            same_file_functions={"bar": ["a.py::bar"]},
            deep_scan=True,
            project_function_index={
                "bar": ["a.py::bar"],
                "some_alias_for_bar": ["a.py::bar"],
            },
        )

        resolved = resolve_calls(fact)

        assert _calls(resolved) == [("a.py::bar", "EXTRACTED", "bar(); some_alias_for_bar()")]
