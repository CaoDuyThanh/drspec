"""Tests for verification script executor."""

from __future__ import annotations

import pytest

from drspec.contracts import (
    Contract,
    Criticality,
    Invariant,
    OnFail,
    VerificationResult,
    execute_verification,
    generate_verification_script,
    validate_script,
    DEFAULT_TIMEOUT,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def simple_script() -> str:
    """A simple verification script that always passes."""
    return '''
def verify(input_data, output_data):
    """Simple verification that always passes."""
    return (True, "All checks passed")
'''


@pytest.fixture
def failing_script() -> str:
    """A verification script that always fails."""
    return '''
def verify(input_data, output_data):
    """Verification that always fails."""
    return (False, "Check failed: value is incorrect")
'''


@pytest.fixture
def conditional_script() -> str:
    """A verification script that checks if output is positive."""
    return '''
def verify(input_data, output_data):
    """Check if output is positive."""
    if output_data > 0:
        return (True, "Output is positive")
    return (False, "Output must be positive")
'''


@pytest.fixture
def error_script() -> str:
    """A verification script that raises an error."""
    return '''
def verify(input_data, output_data):
    """Script that raises an error."""
    raise ValueError("Intentional error")
'''


@pytest.fixture
def slow_script() -> str:
    """A verification script that takes too long."""
    return '''
import time
def verify(input_data, output_data):
    """Script that sleeps too long."""
    time.sleep(10)
    return (True, "Done")
'''


@pytest.fixture
def non_empty_contract() -> Contract:
    """A contract checking for non-empty output."""
    return Contract(
        function_signature="def get_items() -> list",
        intent_summary="Gets a list of items",
        invariants=[
            Invariant(
                name="non_empty",
                logic="Output is not empty",
                criticality=Criticality.HIGH,
                on_fail=OnFail.ERROR,
            )
        ],
    )


# =============================================================================
# Basic Execution Tests
# =============================================================================


class TestBasicExecution:
    """Tests for basic script execution."""

    def test_execute_passing_script(self, simple_script: str) -> None:
        """Should return passed=True for passing script."""
        result = execute_verification(simple_script, {}, None)

        assert isinstance(result, VerificationResult)
        assert result.passed is True
        assert result.error is None
        assert result.execution_time > 0

    def test_execute_failing_script(self, failing_script: str) -> None:
        """Should return passed=False for failing script."""
        result = execute_verification(failing_script, {}, None)

        assert result.passed is False
        assert "failed" in result.message.lower() or "incorrect" in result.message.lower()
        assert result.error is None  # Script ran successfully, just returned False

    def test_execute_conditional_script_pass(self, conditional_script: str) -> None:
        """Should pass when condition is met."""
        result = execute_verification(conditional_script, {}, 5)

        assert result.passed is True
        assert "positive" in result.message.lower()

    def test_execute_conditional_script_fail(self, conditional_script: str) -> None:
        """Should fail when condition is not met."""
        result = execute_verification(conditional_script, {}, -1)

        assert result.passed is False
        assert "positive" in result.message.lower()


# =============================================================================
# Error Handling Tests
# =============================================================================


class TestErrorHandling:
    """Tests for error handling during execution."""

    def test_handles_script_exception(self, error_script: str) -> None:
        """Should handle exceptions in script gracefully."""
        result = execute_verification(error_script, {}, None)

        assert result.passed is False
        assert "error" in result.message.lower()

    def test_handles_timeout(self, slow_script: str) -> None:
        """Should timeout for slow scripts."""
        result = execute_verification(slow_script, {}, None, timeout=0.1)

        assert result.passed is False
        assert result.error == "TIMEOUT"
        assert "timed out" in result.message.lower()

    def test_handles_syntax_error(self) -> None:
        """Should handle scripts with syntax errors."""
        bad_script = "def verify(input, output):\n    return True  # missing paren"
        # This script is actually valid, let's use a real syntax error
        bad_script = "def verify(input, output)\n    return True"  # missing colon

        result = execute_verification(bad_script, {}, None)

        assert result.passed is False
        assert result.error == "EXECUTION_ERROR"

    def test_handles_missing_verify_function(self) -> None:
        """Should handle scripts without verify function."""
        no_verify_script = '''
def some_other_function():
    return True
'''
        result = execute_verification(no_verify_script, {}, None)

        assert result.passed is False
        # Should fail when trying to call verify()

    def test_handles_invalid_return_type(self) -> None:
        """Should handle scripts that return wrong type."""
        bad_return_script = '''
def verify(input_data, output_data):
    return "just a string"  # Should return tuple
'''
        result = execute_verification(bad_return_script, {}, None)

        # The wrapper should handle this gracefully
        assert result.passed is False


# =============================================================================
# Timeout Tests
# =============================================================================


class TestTimeout:
    """Tests for timeout behavior."""

    def test_default_timeout(self) -> None:
        """Default timeout should be 1 second."""
        assert DEFAULT_TIMEOUT == 1.0

    def test_custom_timeout(self, simple_script: str) -> None:
        """Should respect custom timeout."""
        result = execute_verification(simple_script, {}, None, timeout=5.0)

        assert result.passed is True
        assert result.execution_time < 5.0

    def test_execution_time_recorded(self, simple_script: str) -> None:
        """Should record execution time."""
        result = execute_verification(simple_script, {}, None)

        assert result.execution_time > 0
        assert result.execution_time < 1.0  # Should be fast


# =============================================================================
# Integration with Generator
# =============================================================================


class TestGeneratorIntegration:
    """Tests for integration with script generator."""

    def test_execute_generated_script(self, non_empty_contract: Contract) -> None:
        """Should execute scripts from generator."""
        script = generate_verification_script(non_empty_contract, "test.py::func")

        # Test with non-empty output (should pass)
        result = execute_verification(script, {}, [1, 2, 3])
        assert result.passed is True

        # Test with empty output (should fail)
        result = execute_verification(script, {}, [])
        assert result.passed is False

    def test_execute_complex_contract(self) -> None:
        """Should execute scripts with multiple invariants."""
        contract = Contract(
            function_signature="def process(items: list) -> list",
            intent_summary="Processes items and returns filtered results",
            invariants=[
                Invariant(
                    name="non_empty",
                    logic="Output is not empty",
                    criticality=Criticality.HIGH,
                    on_fail=OnFail.ERROR,
                ),
                Invariant(
                    name="all_positive",
                    logic="All values in output are positive",
                    criticality=Criticality.MEDIUM,
                    on_fail=OnFail.WARN,
                ),
            ],
        )
        script = generate_verification_script(contract, "test.py::process")

        # All positive numbers should pass
        result = execute_verification(script, {}, [1, 2, 3])
        assert result.passed is True

        # Empty should fail first invariant
        result = execute_verification(script, {}, [])
        assert result.passed is False
        assert "non_empty" in result.message


# =============================================================================
# Script Validation Tests
# =============================================================================


class TestScriptValidation:
    """Tests for script validation."""

    def test_valid_script(self, simple_script: str) -> None:
        """Should accept valid script."""
        is_valid, error = validate_script(simple_script)

        assert is_valid is True
        assert error is None

    def test_invalid_syntax(self) -> None:
        """Should reject script with syntax error."""
        bad_script = "def verify(:\n    return True"

        is_valid, error = validate_script(bad_script)

        assert is_valid is False
        assert error is not None
        assert "syntax" in error.lower() or "error" in error.lower()

    def test_indentation_error(self) -> None:
        """Should reject script with indentation error."""
        bad_script = '''
def verify(input_data, output_data):
return True
'''
        is_valid, error = validate_script(bad_script)

        assert is_valid is False
        assert error is not None


# =============================================================================
# Result Serialization Tests
# =============================================================================


class TestResultSerialization:
    """Tests for VerificationResult serialization."""

    def test_to_dict(self, simple_script: str) -> None:
        """Should serialize result to dictionary."""
        result = execute_verification(simple_script, {}, None)
        result_dict = result.to_dict()

        assert isinstance(result_dict, dict)
        assert "passed" in result_dict
        assert "message" in result_dict
        assert "execution_time" in result_dict
        assert "error" in result_dict

    def test_to_dict_with_error(self, slow_script: str) -> None:
        """Should include error in serialized result."""
        result = execute_verification(slow_script, {}, None, timeout=0.1)
        result_dict = result.to_dict()

        assert result_dict["error"] == "TIMEOUT"
        assert result_dict["passed"] is False


# =============================================================================
# Data Passing Tests
# =============================================================================


class TestDataPassing:
    """Tests for passing data to verification scripts."""

    def test_passes_input_data(self) -> None:
        """Should pass input data to script."""
        script = '''
def verify(input_data, output_data):
    if input_data.get("x") == 5:
        return (True, "Input x is 5")
    return (False, f"Expected x=5, got {input_data.get('x')}")
'''
        result = execute_verification(script, {"x": 5}, None)
        assert result.passed is True

        result = execute_verification(script, {"x": 10}, None)
        assert result.passed is False

    def test_passes_output_data(self) -> None:
        """Should pass output data to script."""
        script = '''
def verify(input_data, output_data):
    if output_data == 42:
        return (True, "Output is 42")
    return (False, f"Expected 42, got {output_data}")
'''
        result = execute_verification(script, {}, 42)
        assert result.passed is True

        result = execute_verification(script, {}, 0)
        assert result.passed is False

    def test_handles_complex_data(self) -> None:
        """Should handle complex nested data structures."""
        script = '''
def verify(input_data, output_data):
    items = input_data.get("items", [])
    result = output_data.get("result", {})
    if len(items) == len(result.get("processed", [])):
        return (True, "Lengths match")
    return (False, "Length mismatch")
'''
        input_data = {"items": [1, 2, 3]}
        output_data = {"result": {"processed": [10, 20, 30]}}

        result = execute_verification(script, input_data, output_data)
        assert result.passed is True

    def test_handles_none_values(self) -> None:
        """Should handle None input/output."""
        script = '''
def verify(input_data, output_data):
    if output_data is None:
        return (False, "Output is None")
    return (True, "Output is not None")
'''
        result = execute_verification(script, {}, None)
        assert result.passed is False

        result = execute_verification(script, {}, "something")
        assert result.passed is True
