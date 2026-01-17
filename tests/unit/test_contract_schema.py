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


class TestCriticalityEnum:
    """Tests for Criticality enum."""

    def test_criticality_values(self):
        """Test criticality has expected values."""
        assert Criticality.HIGH == "HIGH"
        assert Criticality.MEDIUM == "MEDIUM"
        assert Criticality.LOW == "LOW"

    def test_criticality_is_string_enum(self):
        """Test criticality can be used as string."""
        # As a str subclass, comparing to string works
        assert Criticality.HIGH == "HIGH"
        assert Criticality.HIGH.value == "HIGH"


class TestOnFailEnum:
    """Tests for OnFail enum."""

    def test_on_fail_values(self):
        """Test on_fail has expected values."""
        assert OnFail.ERROR == "error"
        assert OnFail.WARN == "warn"

    def test_on_fail_is_string_enum(self):
        """Test on_fail can be used as string."""
        assert OnFail.ERROR.value == "error"
        assert OnFail.WARN.value == "warn"


class TestInvariant:
    """Tests for Invariant model."""

    def test_valid_invariant(self):
        """Test creating a valid invariant."""
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
        """Test creating invariant with string values for enums."""
        inv = Invariant(
            name="test",
            logic="Test logic here",
            criticality="MEDIUM",
            on_fail="warn",
        )
        assert inv.criticality == Criticality.MEDIUM
        assert inv.on_fail == OnFail.WARN

    def test_invariant_name_required(self):
        """Test invariant name is required."""
        with pytest.raises(ValidationError) as exc_info:
            Invariant(
                logic="Some logic",
                criticality=Criticality.HIGH,
                on_fail=OnFail.ERROR,
            )
        assert "name" in str(exc_info.value)

    def test_invariant_name_not_empty(self):
        """Test invariant name cannot be empty."""
        with pytest.raises(ValidationError) as exc_info:
            Invariant(
                name="",
                logic="Some logic",
                criticality=Criticality.HIGH,
                on_fail=OnFail.ERROR,
            )
        assert "name" in str(exc_info.value).lower()

    def test_invariant_name_whitespace_stripped(self):
        """Test invariant name has whitespace stripped."""
        inv = Invariant(
            name="  test_name  ",
            logic="Some logic",
            criticality=Criticality.HIGH,
            on_fail=OnFail.ERROR,
        )
        assert inv.name == "test_name"

    def test_invariant_logic_required(self):
        """Test invariant logic is required."""
        with pytest.raises(ValidationError) as exc_info:
            Invariant(
                name="test",
                criticality=Criticality.HIGH,
                on_fail=OnFail.ERROR,
            )
        assert "logic" in str(exc_info.value)

    def test_invariant_logic_minimum_length(self):
        """Test invariant logic must be at least 5 characters."""
        with pytest.raises(ValidationError) as exc_info:
            Invariant(
                name="test",
                logic="abc",
                criticality=Criticality.HIGH,
                on_fail=OnFail.ERROR,
            )
        assert "5 characters" in str(exc_info.value)

    def test_invariant_invalid_criticality(self):
        """Test invalid criticality is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            Invariant(
                name="test",
                logic="Valid logic here",
                criticality="INVALID",
                on_fail=OnFail.ERROR,
            )
        assert "criticality" in str(exc_info.value).lower()

    def test_invariant_invalid_on_fail(self):
        """Test invalid on_fail is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            Invariant(
                name="test",
                logic="Valid logic here",
                criticality=Criticality.HIGH,
                on_fail="invalid",
            )
        assert "on_fail" in str(exc_info.value).lower()


class TestIOExample:
    """Tests for IOExample model."""

    def test_valid_io_example(self):
        """Test creating a valid IO example."""
        example = IOExample(
            input={"a": 1, "b": 2},
            output=3,
        )
        assert example.input == {"a": 1, "b": 2}
        assert example.output == 3
        assert example.description is None

    def test_io_example_with_description(self):
        """Test IO example with description."""
        example = IOExample(
            input={"x": [1, 2, 3]},
            output=[3, 2, 1],
            description="Reverses a list",
        )
        assert example.description == "Reverses a list"

    def test_io_example_complex_types(self):
        """Test IO example with complex nested types."""
        example = IOExample(
            input={"items": [{"id": 1}, {"id": 2}]},
            output={"result": {"total": 2, "ids": [1, 2]}},
        )
        assert example.input["items"][0]["id"] == 1
        assert example.output["result"]["total"] == 2


class TestContract:
    """Tests for Contract model."""

    def _make_invariant(self, name: str = "test_inv") -> Invariant:
        """Create a valid invariant for testing."""
        return Invariant(
            name=name,
            logic="Test invariant logic",
            criticality=Criticality.HIGH,
            on_fail=OnFail.ERROR,
        )

    def test_valid_contract(self):
        """Test creating a valid contract."""
        contract = Contract(
            function_signature="def add(a: int, b: int) -> int",
            intent_summary="Adds two integers and returns the sum",
            invariants=[self._make_invariant()],
        )
        assert contract.function_signature == "def add(a: int, b: int) -> int"
        assert contract.intent_summary == "Adds two integers and returns the sum"
        assert len(contract.invariants) == 1
        assert contract.io_examples == []

    def test_contract_with_examples(self):
        """Test contract with IO examples."""
        contract = Contract(
            function_signature="def add(a: int, b: int) -> int",
            intent_summary="Adds two integers and returns the sum",
            invariants=[self._make_invariant()],
            io_examples=[
                IOExample(input={"a": 1, "b": 2}, output=3),
                IOExample(input={"a": 0, "b": 0}, output=0),
            ],
        )
        assert len(contract.io_examples) == 2

    def test_contract_function_signature_required(self):
        """Test function signature is required."""
        with pytest.raises(ValidationError) as exc_info:
            Contract(
                intent_summary="Does something",
                invariants=[self._make_invariant()],
            )
        assert "function_signature" in str(exc_info.value)

    def test_contract_function_signature_not_empty(self):
        """Test function signature cannot be empty."""
        with pytest.raises(ValidationError) as exc_info:
            Contract(
                function_signature="",
                intent_summary="Does something useful here",
                invariants=[self._make_invariant()],
            )
        assert "signature" in str(exc_info.value).lower()

    def test_contract_intent_summary_required(self):
        """Test intent summary is required."""
        with pytest.raises(ValidationError) as exc_info:
            Contract(
                function_signature="def foo()",
                invariants=[self._make_invariant()],
            )
        assert "intent_summary" in str(exc_info.value)

    def test_contract_intent_summary_minimum_length(self):
        """Test intent summary must be at least 10 characters."""
        with pytest.raises(ValidationError) as exc_info:
            Contract(
                function_signature="def foo()",
                intent_summary="Short",
                invariants=[self._make_invariant()],
            )
        assert "10 characters" in str(exc_info.value)

    def test_contract_invariants_required(self):
        """Test invariants list is required."""
        with pytest.raises(ValidationError) as exc_info:
            Contract(
                function_signature="def foo()",
                intent_summary="Does something useful here",
            )
        assert "invariants" in str(exc_info.value)

    def test_contract_invariants_not_empty(self):
        """Test contract must have at least one invariant."""
        with pytest.raises(ValidationError) as exc_info:
            Contract(
                function_signature="def foo()",
                intent_summary="Does something useful here",
                invariants=[],
            )
        # Could be "at least 1" from min_length or "at least one" from validator
        assert "invariant" in str(exc_info.value).lower()

    def test_contract_multiple_invariants(self):
        """Test contract with multiple invariants."""
        contract = Contract(
            function_signature="def process(data: list) -> dict",
            intent_summary="Processes data and returns results",
            invariants=[
                self._make_invariant("inv1"),
                self._make_invariant("inv2"),
                self._make_invariant("inv3"),
            ],
        )
        assert len(contract.invariants) == 3


class TestContractSerialization:
    """Tests for contract serialization."""

    def _make_contract(self) -> Contract:
        """Create a valid contract for testing."""
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
        """Test contract serializes to JSON."""
        contract = self._make_contract()
        json_str = contract.to_json()

        # Should be valid JSON
        data = json.loads(json_str)
        assert data["function_signature"] == "def add(a: int, b: int) -> int"
        assert data["intent_summary"] == "Adds two integers and returns the sum"
        assert len(data["invariants"]) == 1
        assert data["invariants"][0]["name"] == "non_negative_inputs"
        assert data["invariants"][0]["criticality"] == "MEDIUM"
        assert data["invariants"][0]["on_fail"] == "warn"

    def test_to_json_pretty(self):
        """Test contract serializes to pretty JSON."""
        contract = self._make_contract()
        json_str = contract.to_json(indent=2)

        # Should have newlines and indentation
        assert "\n" in json_str
        assert "  " in json_str

    def test_to_dict(self):
        """Test contract converts to dictionary."""
        contract = self._make_contract()
        data = contract.to_dict()

        assert isinstance(data, dict)
        assert data["function_signature"] == "def add(a: int, b: int) -> int"
        assert "invariants" in data
        assert "io_examples" in data

    def test_from_json(self):
        """Test contract deserializes from JSON."""
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
        assert len(contract.invariants) == 1
        assert contract.invariants[0].criticality == Criticality.HIGH

    def test_from_json_invalid(self):
        """Test from_json raises ValueError for invalid JSON."""
        with pytest.raises(ValueError) as exc_info:
            Contract.from_json("not valid json")
        assert "Invalid JSON" in str(exc_info.value)

    def test_from_json_missing_fields(self):
        """Test from_json raises error for missing required fields."""
        with pytest.raises(ValidationError):
            Contract.from_json('{"function_signature": "def foo()"}')

    def test_from_dict(self):
        """Test contract creates from dictionary."""
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
        assert len(contract.invariants) == 1

    def test_roundtrip_json(self):
        """Test contract survives JSON roundtrip."""
        original = self._make_contract()
        json_str = original.to_json()
        restored = Contract.from_json(json_str)

        assert restored.function_signature == original.function_signature
        assert restored.intent_summary == original.intent_summary
        assert len(restored.invariants) == len(original.invariants)
        assert restored.invariants[0].name == original.invariants[0].name

    def test_snake_case_in_json(self):
        """Test JSON output uses snake_case keys."""
        contract = self._make_contract()
        json_str = contract.to_json()
        data = json.loads(json_str)

        # All keys should be snake_case
        assert "function_signature" in data
        assert "intent_summary" in data
        assert "io_examples" in data
        assert "on_fail" in data["invariants"][0]

        # No camelCase
        assert "functionSignature" not in data
        assert "intentSummary" not in data


class TestContractFromArchitectureExample:
    """Test the example contract from architecture docs."""

    def test_architecture_example(self):
        """Test the exact example from architecture.md."""
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

        assert "reconcile_transactions" in contract.function_signature
        assert len(contract.invariants) == 2
        assert contract.invariants[0].name == "no_duplicate_transaction_ids"
        assert contract.invariants[0].criticality == Criticality.HIGH
        assert contract.invariants[1].name == "balance_preserved"
        assert len(contract.io_examples) == 1
