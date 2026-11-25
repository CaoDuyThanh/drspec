"""Tests for the contract schema module."""

import json

import pytest
from pydantic import ValidationError

from drspec.contracts import (
    Contract,
    Criticality,
    Invariant,
    IOExample,
    OnFail,
)

pytestmark = pytest.mark.unit  # pydantic models, no I/O


def _errors(exc_info) -> list:
    """What pydantic says went wrong: `(location, message)` for each error."""
    return [(error["loc"], error["msg"]) for error in exc_info.value.errors()]


class TestCriticalityEnum:
    def test_criticality_values(self):
        assert Criticality.HIGH == "HIGH"
        assert Criticality.MEDIUM == "MEDIUM"
        assert Criticality.LOW == "LOW"

    def test_criticality_is_string_enum(self):
        # a str subclass, so comparing to a plain string works
        assert Criticality.HIGH == "HIGH"
        assert Criticality.HIGH.value == "HIGH"


class TestOnFailEnum:
    def test_on_fail_values(self):
        assert OnFail.ERROR == "error"
        assert OnFail.WARN == "warn"

    def test_on_fail_is_string_enum(self):
        assert OnFail.ERROR.value == "error"
        assert OnFail.WARN.value == "warn"


class TestInvariant:
    def test_valid_invariant(self):
        inv = Invariant(
            name="non_negative_output",
            logic="Output value is always >= 0",
            criticality=Criticality.HIGH,
            on_fail=OnFail.ERROR,
        )
        assert inv.name == "non_negative_output"
        assert inv.logic == "Output value is always >= 0"
        assert inv.criticality == Criticality.HIGH
        assert inv.on_fail == OnFail.ERROR

    def test_invariant_with_string_enums(self):
        inv = Invariant(
            name="test",
            logic="Test logic here",
            criticality="MEDIUM",
            on_fail="warn",
        )
        assert inv.criticality == Criticality.MEDIUM
        assert inv.on_fail == OnFail.WARN

    def test_invariant_name_required(self):
        with pytest.raises(ValidationError) as exc_info:
            Invariant(
                logic="Some logic",
                criticality=Criticality.HIGH,
                on_fail=OnFail.ERROR,
            )
        assert _errors(exc_info) == [(("name",), "Field required")]

    def test_invariant_name_not_empty(self):
        with pytest.raises(ValidationError) as exc_info:
            Invariant(
                name="",
                logic="Some logic",
                criticality=Criticality.HIGH,
                on_fail=OnFail.ERROR,
            )
        assert _errors(exc_info) == [(("name",), "String should have at least 1 character")]


    def test_invariant_name_whitespace_stripped(self):
        inv = Invariant(
            name="  test_name  ",
            logic="Some logic",
            criticality=Criticality.HIGH,
            on_fail=OnFail.ERROR,
        )
        assert inv.name == "test_name"

    def test_invariant_logic_required(self):
        with pytest.raises(ValidationError) as exc_info:
            Invariant(
                name="test",
                criticality=Criticality.HIGH,
                on_fail=OnFail.ERROR,
            )
        assert _errors(exc_info) == [(("logic",), "Field required")]


    def test_invariant_logic_minimum_length(self):
        with pytest.raises(ValidationError) as exc_info:
            Invariant(
                name="test",
                logic="abc",
                criticality=Criticality.HIGH,
                on_fail=OnFail.ERROR,
            )
        assert _errors(exc_info) == [(("logic",), "Value error, Invariant logic must be at least 5 characters")]

    def test_invariant_invalid_criticality(self):
        with pytest.raises(ValidationError) as exc_info:
            Invariant(
                name="test",
                logic="Valid logic here",
                criticality="INVALID",
                on_fail=OnFail.ERROR,
            )
        assert _errors(exc_info) == [(("criticality",), "Input should be 'HIGH', 'MEDIUM' or 'LOW'")]

    def test_invariant_invalid_on_fail(self):
        with pytest.raises(ValidationError) as exc_info:
            Invariant(
                name="test",
                logic="Valid logic here",
                criticality=Criticality.HIGH,
                on_fail="invalid",
            )
        assert _errors(exc_info) == [(("on_fail",), "Input should be 'error' or 'warn'")]


class TestIOExample:
    def test_valid_io_example(self):
        example = IOExample(
            input={"a": 1, "b": 2},
            output=3,
        )
        assert example.input == {"a": 1, "b": 2}
        assert example.output == 3
        assert example.description is None

    def test_io_example_with_description(self):
        example = IOExample(
            input={"x": [1, 2, 3]},
            output=[3, 2, 1],
            description="Reverses a list",
        )
        assert example.description == "Reverses a list"

    def test_io_example_complex_types(self):
        example = IOExample(
            input={"items": [{"id": 1}, {"id": 2}]},
            output={"result": {"total": 2, "ids": [1, 2]}},
        )
        assert example.input == {"items": [{"id": 1}, {"id": 2}]}
        assert example.output == {"result": {"total": 2, "ids": [1, 2]}}


class TestContract:
    def _make_invariant(self, name: str = "test_inv") -> Invariant:
        return Invariant(
            name=name,
            logic="Test invariant logic",
            criticality=Criticality.HIGH,
            on_fail=OnFail.ERROR,
        )

    def test_valid_contract(self):
        contract = Contract(
            function_signature="def add(a: int, b: int) -> int",
            intent_summary="Adds two integers and returns the sum",
            invariants=[self._make_invariant()],
        )
        assert contract.function_signature == "def add(a: int, b: int) -> int"
        assert contract.intent_summary == "Adds two integers and returns the sum"
        assert [i.name for i in contract.invariants] == ["test_inv"]
        assert contract.io_examples == []

    def test_contract_with_examples(self):
        contract = Contract(
            function_signature="def add(a: int, b: int) -> int",
            intent_summary="Adds two integers and returns the sum",
            invariants=[self._make_invariant()],
            io_examples=[
                IOExample(input={"a": 1, "b": 2}, output=3),
                IOExample(input={"a": 0, "b": 0}, output=0),
            ],
        )
        assert [(e.input, e.output) for e in contract.io_examples] == [({"a": 1, "b": 2}, 3), ({"a": 0, "b": 0}, 0)]

    def test_contract_function_signature_required(self):
        with pytest.raises(ValidationError) as exc_info:
            Contract(
                intent_summary="Does something",
                invariants=[self._make_invariant()],
            )
        assert _errors(exc_info) == [(("function_signature",), "Field required")]

    def test_contract_function_signature_not_empty(self):
        with pytest.raises(ValidationError) as exc_info:
            Contract(
                function_signature="",
                intent_summary="Does something useful here",
                invariants=[self._make_invariant()],
            )
        assert _errors(exc_info) == [(("function_signature",), "String should have at least 1 character")]


    def test_contract_intent_summary_required(self):
        with pytest.raises(ValidationError) as exc_info:
            Contract(
                function_signature="def foo()",
                invariants=[self._make_invariant()],
            )
        assert _errors(exc_info) == [(("intent_summary",), "Field required")]

    def test_contract_intent_summary_minimum_length(self):
        with pytest.raises(ValidationError) as exc_info:
            Contract(
                function_signature="def foo()",
                intent_summary="Short",
                invariants=[self._make_invariant()],
            )
        assert _errors(exc_info) == [(("intent_summary",), "Value error, Intent summary must be at least 10 characters")]


    def test_contract_invariants_required(self):
        with pytest.raises(ValidationError) as exc_info:
            Contract(
                function_signature="def foo()",
                intent_summary="Does something useful here",
            )
        assert _errors(exc_info) == [(("invariants",), "Field required")]

    def test_contract_invariants_not_empty(self):
        with pytest.raises(ValidationError) as exc_info:
            Contract(
                function_signature="def foo()",
                intent_summary="Does something useful here",
                invariants=[],
            )
        assert _errors(exc_info) == [(("invariants",), "List should have at least 1 item after validation, not 0")]

    def test_contract_multiple_invariants(self):
        contract = Contract(
            function_signature="def process(data: list) -> dict",
            intent_summary="Processes data and returns results",
            invariants=[
                self._make_invariant("inv1"),
                self._make_invariant("inv2"),
                self._make_invariant("inv3"),
            ],
        )
        assert [i.name for i in contract.invariants] == ["inv1", "inv2", "inv3"]  # in the order given


class TestContractSerialization:
    def _make_contract(self) -> Contract:
        return Contract(
            function_signature="def add(a: int, b: int) -> int",
            intent_summary="Adds two integers and returns the sum",
            invariants=[
                Invariant(
                    name="non_negative_inputs",
                    logic="Both inputs must be non-negative",
                    criticality=Criticality.MEDIUM,
                    on_fail=OnFail.WARN,
                ),
            ],
            io_examples=[
                IOExample(input={"a": 1, "b": 2}, output=3),
            ],
        )

    def test_to_json(self):
        contract = self._make_contract()
        json_str = contract.to_json()

        assert json.loads(json_str) == {
            "function_signature": "def add(a: int, b: int) -> int",
            "intent_summary": "Adds two integers and returns the sum",
            "invariants": [{"name": "non_negative_inputs", "logic": "Both inputs must be non-negative", "criticality": "MEDIUM", "on_fail": "warn"}],
            "io_examples": [{"input": {"a": 1, "b": 2}, "output": 3, "description": None}],
        }

    def test_to_json_pretty(self):
        contract = self._make_contract()
        json_str = contract.to_json(indent=2)

        assert json_str == json.dumps(json.loads(json_str), indent=2)  # two spaces, one key per line
        assert json_str.startswith('{\n  "function_signature": ')
        assert "\n" not in contract.to_json()  # compact by default

    def test_to_dict(self):
        contract = self._make_contract()
        data = contract.to_dict()

        assert data == {
            "function_signature": "def add(a: int, b: int) -> int",
            "intent_summary": "Adds two integers and returns the sum",
            "invariants": [{"name": "non_negative_inputs", "logic": "Both inputs must be non-negative", "criticality": "MEDIUM", "on_fail": "warn"}],
            "io_examples": [{"input": {"a": 1, "b": 2}, "output": 3, "description": None}],
        }  # (the enums are str enums: they compare equal to their values)

    def test_from_json(self):
        json_str = '''
        {
            "function_signature": "def foo() -> str",
            "intent_summary": "Returns a greeting message",
            "invariants": [
                {
                    "name": "returns_string",
                    "logic": "Always returns a non-empty string",
                    "criticality": "HIGH",
                    "on_fail": "error"
                }
            ]
        }
        '''
        contract = Contract.from_json(json_str)

        assert contract.function_signature == "def foo() -> str"
        assert contract.intent_summary == "Returns a greeting message"
        assert contract.invariants == [Invariant(name="returns_string", logic="Always returns a non-empty string", criticality=Criticality.HIGH, on_fail=OnFail.ERROR)]

    def test_from_json_invalid(self):
        with pytest.raises(ValueError) as exc_info:
            Contract.from_json("not valid json")
        assert str(exc_info.value) == "Invalid JSON: Expecting value: line 1 column 1 (char 0)"

    def test_from_json_missing_fields(self):
        with pytest.raises(ValidationError) as exc_info:
            Contract.from_json('{"function_signature": "def foo()"}')
        assert _errors(exc_info) == [(("intent_summary",), "Field required"), (("invariants",), "Field required")]

    def test_from_dict(self):
        data = {
            "function_signature": "def bar(x: int) -> int",
            "intent_summary": "Doubles the input value",
            "invariants": [
                {
                    "name": "doubles_value",
                    "logic": "Output is exactly 2 times the input",
                    "criticality": "HIGH",
                    "on_fail": "error",
                }
            ],
        }
        contract = Contract.from_dict(data)

        assert contract.function_signature == "def bar(x: int) -> int"
        assert [(i.name, i.criticality, i.on_fail) for i in contract.invariants] == [("doubles_value", Criticality.HIGH, OnFail.ERROR)]

    def test_roundtrip_json(self):
        original = self._make_contract()
        json_str = original.to_json()
        restored = Contract.from_json(json_str)

        assert restored == original  # every field: signature, summary, invariants, io examples
        assert Contract.from_dict(original.to_dict()) == original

    def test_snake_case_in_json(self):
        contract = self._make_contract()
        json_str = contract.to_json()
        data = json.loads(json_str)

        assert set(data) == {"function_signature", "intent_summary", "invariants", "io_examples"}  # no camelCase aliases
        assert set(data["invariants"][0]) == {"name", "logic", "criticality", "on_fail"}


class TestContractFromArchitectureExample:
    """A realistic, non-trivial contract (not a minimal synthetic fixture)
    exercising the schema end to end."""

    def test_architecture_example(self):
        contract_json = '''
        {
            "function_signature": "def reconcile_transactions(pending: List[Transaction], posted: List[Transaction]) -> ReconciliationResult",
            "intent_summary": "Matches pending transactions with posted transactions, identifying discrepancies and duplicate entries",
            "invariants": [
                {
                    "name": "no_duplicate_transaction_ids",
                    "logic": "No transaction ID appears twice in the output merged list",
                    "criticality": "HIGH",
                    "on_fail": "error"
                },
                {
                    "name": "balance_preserved",
                    "logic": "Sum of all transaction amounts in output equals sum of all input amounts",
                    "criticality": "HIGH",
                    "on_fail": "error"
                }
            ],
            "io_examples": [
                {
                    "input": {"pending": [{"id": 1, "amount": 100}], "posted": []},
                    "output": {"merged": [{"id": 1, "amount": 100}], "discrepancies": []}
                }
            ]
        }
        '''
        contract = Contract.from_json(contract_json)

        assert contract.function_signature.startswith("def reconcile_transactions(pending: List[Transaction]")
        assert [(i.name, i.criticality, i.on_fail) for i in contract.invariants] == [
            ("no_duplicate_transaction_ids", Criticality.HIGH, OnFail.ERROR),
            ("balance_preserved", Criticality.HIGH, OnFail.ERROR),
        ]
        assert [(e.input, e.output) for e in contract.io_examples] == [
            ({"pending": [{"id": 1, "amount": 100}], "posted": []}, {"merged": [{"id": 1, "amount": 100}], "discrepancies": []})
        ]
