"""Tests for the contract validation engine."""

import json

import pytest

from drspec.contracts import (
    Contract,
    Criticality,
    Invariant,
    OnFail,
    ValidationError,
    ValidationResult,
    format_validation_errors,
    validate_contract,
    validate_contract_dict,
)

pytestmark = pytest.mark.unit  # JSON text in, validation result out


def _failures(result) -> list:
    """Where and why the validation failed, as `(location, message)` pairs."""
    assert result.success is False and result.contract is None
    assert result.error.code == "INVALID_SCHEMA" and result.error.message == "Contract validation failed"
    return [(tuple(e["loc"]), e["msg"]) for e in result.error.details["errors"]]


class TestValidateContract:
    def _make_valid_contract_json(self) -> str:
        return json.dumps({
            "function_signature": "def add(a: int, b: int) -> int",
            "intent_summary": "Adds two integers and returns the sum",
            "invariants": [
                {
                    "name": "returns_integer",
                    "logic": "Output is always an integer",
                    "criticality": "HIGH",
                    "on_fail": "error",
                }
            ],
        })

    def test_valid_contract(self):
        result = validate_contract(self._make_valid_contract_json())

        assert result.success is True and result.error is None
        assert result.contract.function_signature == "def add(a: int, b: int) -> int"
        assert result.contract.intent_summary == "Adds two integers and returns the sum"
        assert result.contract.invariants == [Invariant(name="returns_integer", logic="Output is always an integer", criticality=Criticality.HIGH, on_fail=OnFail.ERROR)]
        assert result.contract.io_examples == []

    def test_valid_contract_with_examples(self):
        contract_json = json.dumps({
            "function_signature": "def add(a: int, b: int) -> int",
            "intent_summary": "Adds two integers and returns the sum",
            "invariants": [
                {
                    "name": "returns_integer",
                    "logic": "Output is always an integer",
                    "criticality": "HIGH",
                    "on_fail": "error",
                }
            ],
            "io_examples": [
                {"input": {"a": 1, "b": 2}, "output": 3},
            ],
        })

        result = validate_contract(contract_json)

        assert result.success is True
        assert [(e.input, e.output) for e in result.contract.io_examples] == [({"a": 1, "b": 2}, 3)]

    def test_invalid_json(self):
        result = validate_contract("not valid json {")

        assert result.success is False
        assert result.contract is None
        assert result.error.code == "INVALID_JSON"
        assert result.error.message == "Invalid JSON at position 0: Expecting value"
        assert result.error.details == {"position": 0, "line": 1, "column": 1}

    def test_invalid_json_position(self):
        result = validate_contract('{"key": value}')  # missing quotes around value

        assert result.success is False
        assert result.error.code == "INVALID_JSON"
        assert result.error.details == {"position": 8, "line": 1, "column": 9}  # `value` starts at the 9th character; positions are 0-based, columns 1-based

    def test_missing_required_field(self):
        contract_json = json.dumps({
            "intent_summary": "Does something",
            "invariants": [
                {
                    "name": "test",
                    "logic": "Test logic here",
                    "criticality": "HIGH",
                    "on_fail": "error",
                }
            ],
        })

        result = validate_contract(contract_json)

        assert _failures(result) == [(("function_signature",), "Field required")]

    def test_empty_invariants(self):
        contract_json = json.dumps({
            "function_signature": "def foo()",
            "intent_summary": "Does something useful here",
            "invariants": [],
        })

        result = validate_contract(contract_json)

        assert _failures(result) == [(("invariants",), "List should have at least 1 item after validation, not 0")]

    def test_invalid_criticality(self):
        contract_json = json.dumps({
            "function_signature": "def foo()",
            "intent_summary": "Does something useful here",
            "invariants": [
                {
                    "name": "test",
                    "logic": "Test logic here",
                    "criticality": "INVALID",
                    "on_fail": "error",
                }
            ],
        })

        result = validate_contract(contract_json)

        assert _failures(result) == [(("invariants", 0, "criticality"), "Input should be 'HIGH', 'MEDIUM' or 'LOW'")]

    def test_invalid_on_fail(self):
        contract_json = json.dumps({
            "function_signature": "def foo()",
            "intent_summary": "Does something useful here",
            "invariants": [
                {
                    "name": "test",
                    "logic": "Test logic here",
                    "criticality": "HIGH",
                    "on_fail": "invalid",
                }
            ],
        })

        result = validate_contract(contract_json)

        assert _failures(result) == [(("invariants", 0, "on_fail"), "Input should be 'error' or 'warn'")]

    def test_intent_summary_too_short(self):
        contract_json = json.dumps({
            "function_signature": "def foo()",
            "intent_summary": "Short",
            "invariants": [
                {
                    "name": "test",
                    "logic": "Test logic here",
                    "criticality": "HIGH",
                    "on_fail": "error",
                }
            ],
        })

        result = validate_contract(contract_json)

        assert _failures(result) == [(("intent_summary",), "Value error, Intent summary must be at least 10 characters")]

    def test_invariant_logic_too_short(self):
        contract_json = json.dumps({
            "function_signature": "def foo()",
            "intent_summary": "Does something useful here",
            "invariants": [
                {
                    "name": "test",
                    "logic": "abc",  # too short
                    "criticality": "HIGH",
                    "on_fail": "error",
                }
            ],
        })

        result = validate_contract(contract_json)

        assert _failures(result) == [(("invariants", 0, "logic"), "Value error, Invariant logic must be at least 5 characters")]

    def test_multiple_invariants(self):
        contract_json = json.dumps({
            "function_signature": "def process(data: list) -> dict",
            "intent_summary": "Processes data and returns results",
            "invariants": [
                {
                    "name": "input_not_empty",
                    "logic": "Input list must not be empty",
                    "criticality": "HIGH",
                    "on_fail": "error",
                },
                {
                    "name": "output_has_status",
                    "logic": "Output dict contains 'status' key",
                    "criticality": "MEDIUM",
                    "on_fail": "warn",
                },
                {
                    "name": "no_data_loss",
                    "logic": "All input items are reflected in output",
                    "criticality": "HIGH",
                    "on_fail": "error",
                },
            ],
        })

        result = validate_contract(contract_json)

        assert result.success is True
        assert [(i.name, i.criticality, i.on_fail) for i in result.contract.invariants] == [
            ("input_not_empty", Criticality.HIGH, OnFail.ERROR),
            ("output_has_status", Criticality.MEDIUM, OnFail.WARN),
            ("no_data_loss", Criticality.HIGH, OnFail.ERROR),
        ]  # in the order written


class TestValidateContractDict:
    def test_valid_dict(self):
        data = {
            "function_signature": "def foo() -> str",
            "intent_summary": "Returns a greeting message",
            "invariants": [
                {
                    "name": "returns_string",
                    "logic": "Always returns a non-empty string",
                    "criticality": "HIGH",
                    "on_fail": "error",
                }
            ],
        }

        result = validate_contract_dict(data)

        assert result.success is True and result.error is None
        assert result.contract.function_signature == "def foo() -> str"
        assert [i.name for i in result.contract.invariants] == ["returns_string"]

    def test_invalid_dict(self):
        data = {
            "function_signature": "def foo()",
            # missing intent_summary and invariants
        }

        result = validate_contract_dict(data)

        assert _failures(result) == [(("intent_summary",), "Field required"), (("invariants",), "Field required")]  # every missing field, not only the first


class TestValidationResult:
    def test_success_result_to_dict(self):
        contract = Contract(
            function_signature="def foo() -> int",
            intent_summary="Returns a number greater than zero",
            invariants=[
                Invariant(
                    name="positive",
                    logic="Output is always positive",
                    criticality=Criticality.HIGH,
                    on_fail=OnFail.ERROR,
                )
            ],
        )
        result = ValidationResult(success=True, contract=contract)

        data = result.to_dict()

        assert data == {
            "success": True,
            "contract": {
                "function_signature": "def foo() -> int",
                "intent_summary": "Returns a number greater than zero",
                "invariants": [{"name": "positive", "logic": "Output is always positive", "criticality": Criticality.HIGH, "on_fail": OnFail.ERROR}],
                "io_examples": [],
            },
            "error": None,
        }

    def test_error_result_to_dict(self):
        error = ValidationError(
            code="INVALID_SCHEMA",
            message="Validation failed",
            details={"errors": [{"loc": ["name"], "msg": "Required"}]},
        )
        result = ValidationResult(success=False, error=error)

        data = result.to_dict()

        assert data == {
            "success": False,
            "contract": None,
            "error": {"code": "INVALID_SCHEMA", "message": "Validation failed", "details": {"errors": [{"loc": ["name"], "msg": "Required"}]}},
        }


class TestFormatValidationErrors:
    def test_format_success(self):
        result = ValidationResult(success=True)

        output = format_validation_errors(result)

        assert output == "Validation successful"

    def test_format_schema_error(self):
        error = ValidationError(
            code="INVALID_SCHEMA",
            message="Contract validation failed",
            details={
                "errors": [
                    {"loc": ["invariants", 0, "name"], "msg": "Field required"},
                ]
            },
        )
        result = ValidationResult(success=False, error=error)

        output = format_validation_errors(result)

        assert output == "Error: Contract validation failed (INVALID_SCHEMA)\nDetails:\n  - invariants -> 0 -> name: Field required"

    def test_format_json_error(self):
        error = ValidationError(
            code="INVALID_JSON",
            message="Invalid JSON at position 10: Expecting value",
            details={"position": 10, "line": 1, "column": 11},
        )
        result = ValidationResult(success=False, error=error)

        output = format_validation_errors(result)

        assert output == "Error: Invalid JSON at position 10: Expecting value (INVALID_JSON)\n  At line 1, column 11 (position 10)"


class TestArchitectureExample:
    """A realistic, non-trivial contract (not a minimal synthetic fixture)
    exercising validation end to end."""

    def test_architecture_example_contract(self):
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

        result = validate_contract(contract_json)

        assert result.success is True
        assert result.contract.function_signature.startswith("def reconcile_transactions(pending: List[Transaction]")
        assert [(i.name, i.criticality) for i in result.contract.invariants] == [
            ("no_duplicate_transaction_ids", Criticality.HIGH),
            ("balance_preserved", Criticality.HIGH),
        ]
        assert [e.input for e in result.contract.io_examples] == [{"pending": [{"id": 1, "amount": 100}], "posted": []}]
