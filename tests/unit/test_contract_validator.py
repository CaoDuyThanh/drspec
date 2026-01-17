"""Tests for the contract validation engine."""

import json


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


class TestValidateContract:
    """Tests for validate_contract function."""

    def _make_valid_contract_json(self) -> str:
        """Create valid contract JSON for testing."""
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
        """Test validation of valid contract."""
        result = validate_contract(self._make_valid_contract_json())

        assert result.success is True
        assert result.contract is not None
        assert result.error is None
        assert result.contract.function_signature == "def add(a: int, b: int) -> int"

    def test_valid_contract_with_examples(self):
        """Test validation of contract with IO examples."""
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
        assert len(result.contract.io_examples) == 1

    def test_invalid_json(self):
        """Test validation fails for invalid JSON."""
        result = validate_contract("not valid json {")

        assert result.success is False
        assert result.contract is None
        assert result.error is not None
        assert result.error.code == "INVALID_JSON"
        assert "position" in result.error.details

    def test_invalid_json_position(self):
        """Test invalid JSON error includes position info."""
        result = validate_contract('{"key": value}')  # missing quotes around value

        assert result.success is False
        assert result.error.code == "INVALID_JSON"
        assert "position" in result.error.details
        assert "line" in result.error.details
        assert "column" in result.error.details

    def test_missing_required_field(self):
        """Test validation fails for missing required field."""
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

        assert result.success is False
        assert result.error.code == "INVALID_SCHEMA"
        assert "errors" in result.error.details

        # Check that function_signature is mentioned in errors
        errors = result.error.details["errors"]
        assert any("function_signature" in str(e.get("loc", [])) for e in errors)

    def test_empty_invariants(self):
        """Test validation fails for empty invariants list."""
        contract_json = json.dumps({
            "function_signature": "def foo()",
            "intent_summary": "Does something useful here",
            "invariants": [],
        })

        result = validate_contract(contract_json)

        assert result.success is False
        assert result.error.code == "INVALID_SCHEMA"

    def test_invalid_criticality(self):
        """Test validation fails for invalid criticality value."""
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

        assert result.success is False
        assert result.error.code == "INVALID_SCHEMA"

        # Check error mentions criticality
        errors = result.error.details["errors"]
        assert any("criticality" in str(e.get("loc", [])) for e in errors)

    def test_invalid_on_fail(self):
        """Test validation fails for invalid on_fail value."""
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

        assert result.success is False
        assert result.error.code == "INVALID_SCHEMA"

    def test_intent_summary_too_short(self):
        """Test validation fails for too short intent summary."""
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

        assert result.success is False
        assert result.error.code == "INVALID_SCHEMA"

    def test_invariant_logic_too_short(self):
        """Test validation fails for too short invariant logic."""
        contract_json = json.dumps({
            "function_signature": "def foo()",
            "intent_summary": "Does something useful here",
            "invariants": [
                {
                    "name": "test",
                    "logic": "abc",  # Too short
                    "criticality": "HIGH",
                    "on_fail": "error",
                }
            ],
        })

        result = validate_contract(contract_json)

        assert result.success is False
        assert result.error.code == "INVALID_SCHEMA"

    def test_multiple_invariants(self):
        """Test validation with multiple invariants."""
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
        assert len(result.contract.invariants) == 3


class TestValidateContractDict:
    """Tests for validate_contract_dict function."""

    def test_valid_dict(self):
        """Test validation of valid dictionary."""
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

        assert result.success is True
        assert result.contract is not None

    def test_invalid_dict(self):
        """Test validation fails for invalid dictionary."""
        data = {
            "function_signature": "def foo()",
            # Missing intent_summary and invariants
        }

        result = validate_contract_dict(data)

        assert result.success is False
        assert result.error.code == "INVALID_SCHEMA"


class TestValidationResult:
    """Tests for ValidationResult class."""

    def test_success_result_to_dict(self):
        """Test successful result converts to dict."""
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

        assert data["success"] is True
        assert data["contract"] is not None
        assert data["error"] is None

    def test_error_result_to_dict(self):
        """Test error result converts to dict."""
        error = ValidationError(
            code="INVALID_SCHEMA",
            message="Validation failed",
            details={"errors": [{"loc": ["name"], "msg": "Required"}]},
        )
        result = ValidationResult(success=False, error=error)

        data = result.to_dict()

        assert data["success"] is False
        assert data["contract"] is None
        assert data["error"] is not None
        assert data["error"]["code"] == "INVALID_SCHEMA"


class TestFormatValidationErrors:
    """Tests for format_validation_errors function."""

    def test_format_success(self):
        """Test formatting successful result."""
        result = ValidationResult(success=True)

        output = format_validation_errors(result)

        assert output == "Validation successful"

    def test_format_schema_error(self):
        """Test formatting schema validation error."""
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

        assert "INVALID_SCHEMA" in output
        assert "invariants" in output
        assert "Field required" in output

    def test_format_json_error(self):
        """Test formatting JSON parse error."""
        error = ValidationError(
            code="INVALID_JSON",
            message="Invalid JSON at position 10: Expecting value",
            details={"position": 10, "line": 1, "column": 11},
        )
        result = ValidationResult(success=False, error=error)

        output = format_validation_errors(result)

        assert "INVALID_JSON" in output
        assert "line 1" in output
        assert "column 11" in output


class TestArchitectureExample:
    """Test the example from architecture documentation."""

    def test_architecture_example_contract(self):
        """Test the reconcile_transactions example from docs."""
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
        assert "reconcile_transactions" in result.contract.function_signature
        assert len(result.contract.invariants) == 2
        assert result.contract.invariants[0].name == "no_duplicate_transaction_ids"
        assert result.contract.invariants[0].criticality == Criticality.HIGH
        assert len(result.contract.io_examples) == 1
