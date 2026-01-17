"""Tests for runtime verification module (Story 5-2).

These tests verify the debugger agent's runtime verification API:
- serialize_for_verification: Complex data serialization
- verify_at_runtime: Runtime verification execution
- RuntimeVerificationResult and InvariantResult models
"""

from __future__ import annotations

import time
from datetime import datetime
from decimal import Decimal

import pytest

# Import from debugging module to avoid contracts package (Pydantic 3.8 issue)
from drspec.debugging import (
    InvariantResult,
    RuntimeVerificationResult,
    serialize_for_verification,
    deserialize_from_verification,
    verify_at_runtime,
)


# =============================================================================
# InvariantResult Tests
# =============================================================================


class TestInvariantResult:
    """Tests for InvariantResult dataclass."""

    def test_create_passed_invariant(self):
        """Should create a passed invariant result."""
        result = InvariantResult(
            name="positive_output",
            passed=True,
            criticality="HIGH",
        )

        assert result.name == "positive_output"
        assert result.passed is True
        assert result.criticality == "HIGH"
        assert result.message is None

    def test_create_failed_invariant(self):
        """Should create a failed invariant result with details."""
        result = InvariantResult(
            name="non_empty",
            passed=False,
            criticality="MEDIUM",
            message="Output was empty",
            expected="Non-empty list",
            actual=[],
        )

        assert result.passed is False
        assert result.message == "Output was empty"
        assert result.expected == "Non-empty list"
        assert result.actual == []

    def test_to_dict(self):
        """Should convert to dictionary."""
        result = InvariantResult(
            name="test",
            passed=True,
            criticality="LOW",
        )

        d = result.to_dict()

        assert isinstance(d, dict)
        assert d["name"] == "test"
        assert d["passed"] is True
        assert d["criticality"] == "LOW"


# =============================================================================
# RuntimeVerificationResult Tests
# =============================================================================


class TestRuntimeVerificationResult:
    """Tests for RuntimeVerificationResult dataclass."""

    def test_create_success_result(self):
        """Should create a successful verification result."""
        result = RuntimeVerificationResult(
            function_id="src/math.py::add",
            passed=True,
            invariants=[
                InvariantResult(name="inv1", passed=True, criticality="HIGH"),
                InvariantResult(name="inv2", passed=True, criticality="MEDIUM"),
            ],
            execution_time_ms=45.2,
        )

        assert result.passed is True
        assert len(result.invariants) == 2
        assert result.error is None

    def test_create_failure_result(self):
        """Should create a failed verification result."""
        result = RuntimeVerificationResult(
            function_id="src/math.py::divide",
            passed=False,
            invariants=[
                InvariantResult(name="no_division_by_zero", passed=False, criticality="HIGH"),
            ],
            execution_time_ms=12.5,
        )

        assert result.passed is False
        assert len(result.failed_invariants) == 1

    def test_failed_invariants_property(self):
        """Should return only failed invariants."""
        result = RuntimeVerificationResult(
            function_id="test::func",
            passed=False,
            invariants=[
                InvariantResult(name="inv1", passed=True, criticality="HIGH"),
                InvariantResult(name="inv2", passed=False, criticality="HIGH"),
                InvariantResult(name="inv3", passed=False, criticality="LOW"),
            ],
        )

        failed = result.failed_invariants

        assert len(failed) == 2
        assert failed[0].name == "inv2"
        assert failed[1].name == "inv3"

    def test_critical_failures_property(self):
        """Should return only HIGH criticality failures."""
        result = RuntimeVerificationResult(
            function_id="test::func",
            passed=False,
            invariants=[
                InvariantResult(name="inv1", passed=False, criticality="HIGH"),
                InvariantResult(name="inv2", passed=False, criticality="LOW"),
                InvariantResult(name="inv3", passed=False, criticality="HIGH"),
            ],
        )

        critical = result.critical_failures

        assert len(critical) == 2
        assert all(inv.criticality == "HIGH" for inv in critical)

    def test_to_dict(self):
        """Should convert to dictionary with nested invariants."""
        result = RuntimeVerificationResult(
            function_id="test::func",
            passed=True,
            invariants=[
                InvariantResult(name="inv1", passed=True, criticality="HIGH"),
            ],
            execution_time_ms=100.0,
        )

        d = result.to_dict()

        assert d["function_id"] == "test::func"
        assert d["passed"] is True
        assert len(d["invariants"]) == 1
        assert d["execution_time_ms"] == 100.0


# =============================================================================
# Serialization Tests
# =============================================================================


class TestSerializeForVerification:
    """Tests for serialize_for_verification function."""

    def test_primitive_types(self):
        """Should pass through primitives unchanged."""
        assert serialize_for_verification(42) == 42
        assert serialize_for_verification(3.14) == 3.14
        assert serialize_for_verification("hello") == "hello"
        assert serialize_for_verification(True) is True
        assert serialize_for_verification(None) is None

    def test_list_serialization(self):
        """Should serialize lists recursively."""
        data = [1, "two", 3.0]
        result = serialize_for_verification(data)

        assert result == [1, "two", 3.0]

    def test_dict_serialization(self):
        """Should serialize dicts recursively."""
        data = {"a": 1, "b": "two", "c": [1, 2, 3]}
        result = serialize_for_verification(data)

        assert result == {"a": 1, "b": "two", "c": [1, 2, 3]}

    def test_nested_structure(self):
        """Should handle deeply nested structures."""
        data = {
            "level1": {
                "level2": {
                    "level3": [1, 2, 3],
                },
            },
        }
        result = serialize_for_verification(data)

        assert result["level1"]["level2"]["level3"] == [1, 2, 3]

    def test_datetime_serialization(self):
        """Should serialize datetime with type marker."""
        dt = datetime(2024, 6, 15, 10, 30, 0)
        result = serialize_for_verification(dt)

        assert result["__type__"] == "datetime"
        assert result["value"] == "2024-06-15T10:30:00"

    def test_decimal_serialization(self):
        """Should serialize Decimal with type marker."""
        d = Decimal("123.456")
        result = serialize_for_verification(d)

        assert result["__type__"] == "decimal"
        assert result["value"] == "123.456"

    def test_bytes_serialization(self):
        """Should serialize bytes with type marker."""
        data = b"hello"
        result = serialize_for_verification(data)

        assert result["__type__"] == "bytes"
        assert result["value"] == "hello"

    def test_set_serialization(self):
        """Should serialize set with type marker."""
        data = {1, 2, 3}
        result = serialize_for_verification(data)

        assert result["__type__"] == "set"
        assert sorted(result["value"]) == [1, 2, 3]

    def test_tuple_serialization(self):
        """Should serialize tuple as list."""
        data = (1, 2, 3)
        result = serialize_for_verification(data)

        assert result == [1, 2, 3]

    def test_object_serialization(self):
        """Should serialize object with __dict__."""

        class TestObj:
            def __init__(self):
                self.x = 10
                self.y = "test"

        obj = TestObj()
        result = serialize_for_verification(obj)

        assert result["__type__"] == "TestObj"
        assert result["x"] == 10
        assert result["y"] == "test"


class TestDeserializeFromVerification:
    """Tests for deserialize_from_verification function."""

    def test_primitive_types(self):
        """Should pass through primitives unchanged."""
        assert deserialize_from_verification(42) == 42
        assert deserialize_from_verification("hello") == "hello"
        assert deserialize_from_verification(None) is None

    def test_datetime_deserialization(self):
        """Should deserialize datetime from type marker."""
        data = {"__type__": "datetime", "value": "2024-06-15T10:30:00"}
        result = deserialize_from_verification(data)

        assert isinstance(result, datetime)
        assert result.year == 2024
        assert result.month == 6

    def test_decimal_deserialization(self):
        """Should deserialize Decimal from type marker."""
        data = {"__type__": "decimal", "value": "123.456"}
        result = deserialize_from_verification(data)

        assert isinstance(result, Decimal)
        assert result == Decimal("123.456")

    def test_set_deserialization(self):
        """Should deserialize set from type marker."""
        data = {"__type__": "set", "value": [1, 2, 3]}
        result = deserialize_from_verification(data)

        assert isinstance(result, set)
        assert result == {1, 2, 3}

    def test_roundtrip(self):
        """Should roundtrip complex data."""
        original = {
            "timestamp": datetime(2024, 1, 15),
            "amount": Decimal("99.99"),
            "items": [1, 2, 3],
        }

        serialized = serialize_for_verification(original)
        deserialized = deserialize_from_verification(serialized)

        assert deserialized["timestamp"] == original["timestamp"]
        assert deserialized["amount"] == original["amount"]
        assert deserialized["items"] == original["items"]


# =============================================================================
# verify_at_runtime Tests
# =============================================================================


class TestVerifyAtRuntime:
    """Tests for verify_at_runtime function."""

    def test_simple_passing_verification(self):
        """Should verify passing contract."""
        script = '''
def _check_invariant_1(input_data, output_data):
    """Check positive output."""
    return output_data > 0

def verify(input_data, output_data):
    if not _check_invariant_1(input_data, output_data):
        return (False, "Invariant 'positive' violated")
    return (True, "All 1 invariant(s) passed")
'''
        result = verify_at_runtime(
            function_id="test::positive",
            script=script,
            input_data={"x": 5},
            output_data=10,
        )

        assert result.passed is True
        assert len(result.invariants) >= 1
        assert result.error is None

    def test_simple_failing_verification(self):
        """Should detect failing contract."""
        script = '''
def _check_invariant_1(input_data, output_data):
    """Check positive output."""
    return output_data > 0

def verify(input_data, output_data):
    if not _check_invariant_1(input_data, output_data):
        return (False, "Invariant 'positive' violated")
    return (True, "All 1 invariant(s) passed")
'''
        result = verify_at_runtime(
            function_id="test::positive",
            script=script,
            input_data={"x": 5},
            output_data=-5,  # Negative output should fail
        )

        assert result.passed is False

    def test_with_invariant_info(self):
        """Should use invariant info for detailed reporting."""
        script = '''
def _check_invariant_1(input_data, output_data):
    return output_data > 0

def _check_invariant_2(input_data, output_data):
    return output_data < 100

def verify(input_data, output_data):
    if not _check_invariant_1(input_data, output_data):
        return (False, "positive violated")
    if not _check_invariant_2(input_data, output_data):
        return (False, "bounded violated")
    return (True, "All 2 invariant(s) passed")
'''
        invariant_info = [
            {"name": "positive_output", "logic": "output > 0", "criticality": "HIGH"},
            {"name": "bounded_output", "logic": "output < 100", "criticality": "MEDIUM"},
        ]

        result = verify_at_runtime(
            function_id="test::bounded",
            script=script,
            input_data={"x": 5},
            output_data=50,
            invariant_info=invariant_info,
        )

        assert result.passed is True
        assert len(result.invariants) == 2
        # Check invariant names are preserved
        names = [inv.name for inv in result.invariants]
        assert "positive_output" in names
        assert "bounded_output" in names

    def test_handles_complex_data(self):
        """Should handle complex data types."""
        script = '''
def _check_invariant_1(input_data, output_data):
    # Check output is non-empty list
    return isinstance(output_data, list) and len(output_data) > 0

def verify(input_data, output_data):
    if not _check_invariant_1(input_data, output_data):
        return (False, "non_empty violated")
    return (True, "All 1 invariant(s) passed")
'''
        result = verify_at_runtime(
            function_id="test::list_func",
            script=script,
            input_data={
                "items": [1, 2, 3],
                "timestamp": datetime.now(),
            },
            output_data=[4, 5, 6],
        )

        assert result.passed is True

    def test_timeout_handling(self):
        """Should handle script timeout."""
        script = '''
import time
def _check_invariant_1(input_data, output_data):
    time.sleep(10)  # Sleep longer than timeout
    return True

def verify(input_data, output_data):
    return (True, "passed")
'''
        result = verify_at_runtime(
            function_id="test::slow",
            script=script,
            input_data={},
            output_data=None,
            timeout=0.5,  # Short timeout
        )

        assert result.passed is False
        assert "timed out" in result.error.lower()

    def test_syntax_error_handling(self):
        """Should handle script syntax errors."""
        script = '''
def _check_invariant_1(input_data, output_data):
    return True  # Missing colon on next line
def verify(input_data output_data)  # Syntax error
    return (True, "passed")
'''
        result = verify_at_runtime(
            function_id="test::bad",
            script=script,
            input_data={},
            output_data=None,
        )

        assert result.passed is False
        assert result.error is not None

    def test_execution_time_recorded(self):
        """Should record execution time."""
        script = '''
def _check_invariant_1(input_data, output_data):
    return True

def verify(input_data, output_data):
    return (True, "passed")
'''
        result = verify_at_runtime(
            function_id="test::timed",
            script=script,
            input_data={},
            output_data=None,
        )

        assert result.execution_time_ms > 0
        assert result.execution_time_ms < 1000  # Should be under 1 second (NFR3)

    def test_multiple_invariants(self):
        """Should check multiple invariants independently."""
        script = '''
def _check_invariant_1(input_data, output_data):
    return output_data > 0

def _check_invariant_2(input_data, output_data):
    return output_data < 100

def _check_invariant_3(input_data, output_data):
    return isinstance(output_data, int)

def verify(input_data, output_data):
    results = [
        _check_invariant_1(input_data, output_data),
        _check_invariant_2(input_data, output_data),
        _check_invariant_3(input_data, output_data),
    ]
    if all(results):
        return (True, "All 3 invariant(s) passed")
    return (False, "Some invariant failed")
'''
        invariant_info = [
            {"name": "positive", "logic": "output > 0", "criticality": "HIGH"},
            {"name": "bounded", "logic": "output < 100", "criticality": "MEDIUM"},
            {"name": "is_int", "logic": "output is int", "criticality": "LOW"},
        ]

        result = verify_at_runtime(
            function_id="test::multi",
            script=script,
            input_data={},
            output_data=50,
            invariant_info=invariant_info,
        )

        assert result.passed is True
        assert len(result.invariants) == 3


# =============================================================================
# Performance Tests
# =============================================================================


class TestPerformance:
    """Tests for performance requirements (NFR3: < 1 second)."""

    def test_verification_under_1_second(self):
        """Should complete verification in under 1 second."""
        script = '''
def _check_invariant_1(input_data, output_data):
    return True

def _check_invariant_2(input_data, output_data):
    return True

def _check_invariant_3(input_data, output_data):
    return True

def verify(input_data, output_data):
    return (True, "All 3 invariant(s) passed")
'''
        start = time.perf_counter()

        result = verify_at_runtime(
            function_id="test::perf",
            script=script,
            input_data={"x": 1, "y": 2, "z": 3},
            output_data={"result": 6},
        )

        elapsed = time.perf_counter() - start

        assert result.passed is True
        assert elapsed < 1.0, f"Verification took {elapsed:.2f}s, expected < 1s"
