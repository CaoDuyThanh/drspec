"""Tests for verification script generator."""

from __future__ import annotations

import pytest

from drspec.contracts import (
    Contract,
    Criticality,
    Invariant,
    IOExample,
    OnFail,
    generate_verification_script,
    compute_script_hash,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def simple_contract() -> Contract:
    """A simple contract with one invariant."""
    return Contract(
        function_signature="def double(x: int) -> int",
        intent_summary="Doubles the input value",
        invariants=[
            Invariant(
                name="output_is_double",
                logic="Output equals input multiplied by 2",
                criticality=Criticality.HIGH,
                on_fail=OnFail.ERROR,
            )
        ],
    )


@pytest.fixture
def multi_invariant_contract() -> Contract:
    """A contract with multiple invariants."""
    return Contract(
        function_signature="def process_items(items: list) -> list",
        intent_summary="Processes a list of items and returns filtered results",
        invariants=[
            Invariant(
                name="non_empty_output",
                logic="Output is not empty when input is not empty",
                criticality=Criticality.HIGH,
                on_fail=OnFail.ERROR,
            ),
            Invariant(
                name="no_duplicates",
                logic="No duplicate IDs in output",
                criticality=Criticality.MEDIUM,
                on_fail=OnFail.WARN,
            ),
            Invariant(
                name="positive_values",
                logic="All values in output are positive",
                criticality=Criticality.LOW,
                on_fail=OnFail.WARN,
            ),
        ],
    )


@pytest.fixture
def contract_with_examples() -> Contract:
    """A contract with IO examples."""
    return Contract(
        function_signature="def add(a: int, b: int) -> int",
        intent_summary="Adds two numbers and returns the sum",
        invariants=[
            Invariant(
                name="returns_integer",
                logic="Output is an integer",
                criticality=Criticality.HIGH,
                on_fail=OnFail.ERROR,
            )
        ],
        io_examples=[
            IOExample(input={"a": 1, "b": 2}, output=3),
            IOExample(input={"a": 0, "b": 0}, output=0),
        ],
    )


# =============================================================================
# Basic Script Generation Tests
# =============================================================================


class TestBasicGeneration:
    """Tests for basic script generation."""

    def test_generates_valid_python(self, simple_contract: Contract) -> None:
        """Generated script should be valid Python."""
        script = generate_verification_script(simple_contract, "test.py::double")

        # Should be able to compile without errors
        compile(script, "<string>", "exec")

    def test_contains_verify_function(self, simple_contract: Contract) -> None:
        """Script should contain a verify function."""
        script = generate_verification_script(simple_contract, "test.py::double")

        assert "def verify(" in script
        assert "input_data" in script
        assert "output_data" in script

    def test_contains_function_id_in_docstring(self, simple_contract: Contract) -> None:
        """Script should document the function ID."""
        script = generate_verification_script(simple_contract, "test.py::double")

        assert "test.py::double" in script

    def test_contains_intent_summary(self, simple_contract: Contract) -> None:
        """Script should include the contract intent."""
        script = generate_verification_script(simple_contract, "test.py::double")

        assert "Doubles the input value" in script

    def test_contains_check_functions(self, simple_contract: Contract) -> None:
        """Script should have check functions for each invariant."""
        script = generate_verification_script(simple_contract, "test.py::double")

        assert "_check_invariant_1" in script

    def test_multiple_invariants_generate_multiple_checks(
        self, multi_invariant_contract: Contract
    ) -> None:
        """Each invariant should get its own check function."""
        script = generate_verification_script(
            multi_invariant_contract, "test.py::process_items"
        )

        assert "_check_invariant_1" in script
        assert "_check_invariant_2" in script
        assert "_check_invariant_3" in script


# =============================================================================
# Script Execution Tests
# =============================================================================


class TestScriptExecution:
    """Tests for actually executing generated scripts."""

    def test_verify_returns_tuple(self, simple_contract: Contract) -> None:
        """verify() should return (bool, str) tuple."""
        script = generate_verification_script(simple_contract, "test.py::double")

        # Execute the script to get the verify function
        namespace: dict = {}
        exec(script, namespace)
        verify = namespace["verify"]

        result = verify({"x": 5}, 10)
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], str)

    def test_verify_passes_for_valid_data(self) -> None:
        """verify() should pass for valid input/output with translatable invariant."""
        # Use an invariant that can be translated
        contract = Contract(
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
        script = generate_verification_script(contract, "test.py::get_items")

        namespace: dict = {}
        exec(script, namespace)
        verify = namespace["verify"]

        # Non-empty list should pass
        passed, message = verify({}, [1, 2, 3])
        assert passed is True
        assert "passed" in message.lower()

    def test_verify_handles_exception(self, simple_contract: Contract) -> None:
        """verify() should catch exceptions and return failure."""
        script = generate_verification_script(simple_contract, "test.py::double")

        namespace: dict = {}
        exec(script, namespace)
        verify = namespace["verify"]

        # Pass None which might cause issues in some checks
        passed, message = verify(None, None)
        # Should handle gracefully (pass or fail, but not crash)
        assert isinstance(passed, bool)
        assert isinstance(message, str)


# =============================================================================
# Invariant Translation Tests
# =============================================================================


class TestInvariantTranslation:
    """Tests for invariant logic translation."""

    def test_translates_non_empty_output(self) -> None:
        """Should translate 'not empty' invariants."""
        contract = Contract(
            function_signature="def get_items() -> list",
            intent_summary="Gets all items",
            invariants=[
                Invariant(
                    name="non_empty",
                    logic="Output is not empty",
                    criticality=Criticality.HIGH,
                    on_fail=OnFail.ERROR,
                )
            ],
        )
        script = generate_verification_script(contract, "test.py::get_items")

        # Should contain an actual check, not just "return True"
        assert "len(output_data)" in script or "is not None" in script

        # Verify it works
        namespace: dict = {}
        exec(script, namespace)
        verify = namespace["verify"]

        # Non-empty list should pass
        passed, _ = verify({}, [1, 2, 3])
        assert passed is True

        # Empty list should fail
        passed, _ = verify({}, [])
        assert passed is False

    def test_translates_positive_values(self) -> None:
        """Should translate 'positive' invariants."""
        contract = Contract(
            function_signature="def get_value() -> int",
            intent_summary="Gets a positive value",
            invariants=[
                Invariant(
                    name="positive_output",
                    logic="Output is positive",
                    criticality=Criticality.HIGH,
                    on_fail=OnFail.ERROR,
                )
            ],
        )
        script = generate_verification_script(contract, "test.py::get_value")

        namespace: dict = {}
        exec(script, namespace)
        verify = namespace["verify"]

        # Positive number should pass
        passed, _ = verify({}, 5)
        assert passed is True

        # Zero should fail (not positive)
        passed, _ = verify({}, 0)
        assert passed is False

        # Negative should fail
        passed, _ = verify({}, -1)
        assert passed is False

    def test_translates_non_negative_values(self) -> None:
        """Should translate 'non-negative' invariants."""
        contract = Contract(
            function_signature="def get_count() -> int",
            intent_summary="Gets a count (non-negative)",
            invariants=[
                Invariant(
                    name="non_negative_output",
                    logic="Output is non-negative",
                    criticality=Criticality.HIGH,
                    on_fail=OnFail.ERROR,
                )
            ],
        )
        script = generate_verification_script(contract, "test.py::get_count")

        namespace: dict = {}
        exec(script, namespace)
        verify = namespace["verify"]

        # Positive should pass
        passed, _ = verify({}, 5)
        assert passed is True

        # Zero should pass (non-negative)
        passed, _ = verify({}, 0)
        assert passed is True

        # Negative should fail
        passed, _ = verify({}, -1)
        assert passed is False

    def test_translates_not_none(self) -> None:
        """Should translate 'not None' invariants."""
        contract = Contract(
            function_signature="def get_result() -> Any",
            intent_summary="Gets some result",
            invariants=[
                Invariant(
                    name="not_none",
                    logic="Output is not None",
                    criticality=Criticality.HIGH,
                    on_fail=OnFail.ERROR,
                )
            ],
        )
        script = generate_verification_script(contract, "test.py::get_result")

        namespace: dict = {}
        exec(script, namespace)
        verify = namespace["verify"]

        # Non-None should pass
        passed, _ = verify({}, "something")
        assert passed is True

        passed, _ = verify({}, 0)  # 0 is not None
        assert passed is True

        # None should fail
        passed, _ = verify({}, None)
        assert passed is False

    def test_translates_type_checks(self) -> None:
        """Should translate type check invariants."""
        contract = Contract(
            function_signature="def get_items() -> list",
            intent_summary="Gets items as a list",
            invariants=[
                Invariant(
                    name="is_list",
                    logic="Output is a list",
                    criticality=Criticality.HIGH,
                    on_fail=OnFail.ERROR,
                )
            ],
        )
        script = generate_verification_script(contract, "test.py::get_items")

        namespace: dict = {}
        exec(script, namespace)
        verify = namespace["verify"]

        # List should pass
        passed, _ = verify({}, [1, 2, 3])
        assert passed is True

        # Dict should fail
        passed, _ = verify({}, {"a": 1})
        assert passed is False


# =============================================================================
# Determinism Tests
# =============================================================================


class TestDeterminism:
    """Tests for script generation determinism."""

    def test_same_contract_same_script(self, simple_contract: Contract) -> None:
        """Same contract should always generate same script."""
        script1 = generate_verification_script(simple_contract, "test.py::double")
        script2 = generate_verification_script(simple_contract, "test.py::double")

        assert script1 == script2

    def test_same_contract_same_hash(self, simple_contract: Contract) -> None:
        """Same script should have same hash."""
        script1 = generate_verification_script(simple_contract, "test.py::double")
        script2 = generate_verification_script(simple_contract, "test.py::double")

        assert compute_script_hash(script1) == compute_script_hash(script2)

    def test_different_contracts_different_scripts(
        self, simple_contract: Contract, multi_invariant_contract: Contract
    ) -> None:
        """Different contracts should generate different scripts."""
        script1 = generate_verification_script(simple_contract, "test.py::func1")
        script2 = generate_verification_script(
            multi_invariant_contract, "test.py::func2"
        )

        assert script1 != script2


# =============================================================================
# Error Message Tests
# =============================================================================


class TestErrorMessages:
    """Tests for verification failure messages."""

    def test_failure_message_contains_invariant_name(self) -> None:
        """Failure message should identify which invariant failed."""
        contract = Contract(
            function_signature="def get_items() -> list",
            intent_summary="Gets items",
            invariants=[
                Invariant(
                    name="non_empty_output",
                    logic="Output is not empty",
                    criticality=Criticality.HIGH,
                    on_fail=OnFail.ERROR,
                )
            ],
        )
        script = generate_verification_script(contract, "test.py::get_items")

        namespace: dict = {}
        exec(script, namespace)
        verify = namespace["verify"]

        passed, message = verify({}, [])
        assert passed is False
        assert "non_empty_output" in message

    def test_success_message_shows_count(self, multi_invariant_contract: Contract) -> None:
        """Success message should show how many invariants passed."""
        script = generate_verification_script(
            multi_invariant_contract, "test.py::process_items"
        )

        # The invariants may be optimistically passed for untranslatable logic
        assert "3 invariant" in script  # Should reference 3 invariants


# =============================================================================
# Edge Cases
# =============================================================================


class TestEdgeCases:
    """Tests for edge cases in script generation."""

    def test_invariant_with_special_chars_in_logic(self) -> None:
        """Should handle special characters in logic description."""
        contract = Contract(
            function_signature="def check() -> bool",
            intent_summary="Checks something",
            invariants=[
                Invariant(
                    name="special_check",
                    logic='Output contains "quoted" text and \\backslash',
                    criticality=Criticality.HIGH,
                    on_fail=OnFail.ERROR,
                )
            ],
        )
        script = generate_verification_script(contract, "test.py::check")

        # Should compile without errors
        compile(script, "<string>", "exec")

    def test_very_long_invariant_logic(self) -> None:
        """Should handle very long logic descriptions."""
        long_logic = "This is a very long logic description. " * 20
        contract = Contract(
            function_signature="def check() -> bool",
            intent_summary="Checks something with complex rules",
            invariants=[
                Invariant(
                    name="complex_check",
                    logic=long_logic,
                    criticality=Criticality.HIGH,
                    on_fail=OnFail.ERROR,
                )
            ],
        )
        script = generate_verification_script(contract, "test.py::check")

        # Should compile without errors
        compile(script, "<string>", "exec")

    def test_many_invariants(self) -> None:
        """Should handle many invariants."""
        invariants = [
            Invariant(
                name=f"invariant_{i}",
                logic=f"Check number {i}",
                criticality=Criticality.MEDIUM,
                on_fail=OnFail.WARN,
            )
            for i in range(20)
        ]
        contract = Contract(
            function_signature="def complex_func() -> dict",
            intent_summary="A function with many invariants",
            invariants=invariants,
        )
        script = generate_verification_script(contract, "test.py::complex_func")

        # Should compile
        compile(script, "<string>", "exec")

        # Should have all check functions
        for i in range(1, 21):
            assert f"_check_invariant_{i}" in script
