"""Tests for verification script generator."""

from __future__ import annotations

import pytest

from drspec.contracts import (
    Contract,
    Criticality,
    Invariant,
    IOExample,
    OnFail,
    compute_script_hash,
    generate_verification_script,
)

pytestmark = pytest.mark.unit  # pure functions, and the generated text run with `exec`: nothing else

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def simple_contract() -> Contract:
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
        script = generate_verification_script(simple_contract, "test.py::double")

        namespace: dict = {}
        exec(compile(script, "<string>", "exec"), namespace)  # it compiles and runs to the end of its definitions
        assert callable(namespace["verify"])  # and defines the function the executor calls

    def test_contains_verify_function(self, simple_contract: Contract) -> None:
        script = generate_verification_script(simple_contract, "test.py::double")

        assert "def verify(" in script
        assert "input_data" in script
        assert "output_data" in script

    def test_contains_function_id_in_docstring(self, simple_contract: Contract) -> None:
        script = generate_verification_script(simple_contract, "test.py::double")

        assert "test.py::double" in script

    def test_contains_intent_summary(self, simple_contract: Contract) -> None:
        script = generate_verification_script(simple_contract, "test.py::double")

        assert "Doubles the input value" in script

    def test_contains_check_functions(self, simple_contract: Contract) -> None:
        script = generate_verification_script(simple_contract, "test.py::double")

        assert "_check_invariant_1" in script

    def test_multiple_invariants_generate_multiple_checks(
        self, multi_invariant_contract: Contract
    ) -> None:
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
        script = generate_verification_script(simple_contract, "test.py::double")

        namespace: dict = {}
        exec(script, namespace)
        verify = namespace["verify"]

        result = verify({"x": 5}, 10)
        assert isinstance(result, dict)
        assert isinstance(result["passed"], bool)
        assert isinstance(result["results"], list)
        assert result["status"] == "unverified"

    def test_verify_passes_for_valid_data(self) -> None:
        # non_empty is translatable to a real check, unlike some other invariant logic
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

        result = verify({}, [1, 2, 3])
        passed, message = result["passed"], result["message"]
        assert passed is True
        assert "passed" in message.lower()

    def test_verify_handles_exception(self, simple_contract: Contract) -> None:
        script = generate_verification_script(simple_contract, "test.py::double")

        namespace: dict = {}
        exec(script, namespace)
        verify = namespace["verify"]

        # None args may trip up checks that assume a real value; must not crash
        result = verify(None, None)
        passed, message = result["passed"], result["message"]
        assert isinstance(passed, bool)
        assert isinstance(message, str)


# =============================================================================
# Invariant Translation Tests
# =============================================================================


class TestInvariantTranslation:
    """Tests for invariant logic translation."""

    def test_translates_non_empty_output(self) -> None:
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

        # a real check, not just a "return True" stub
        assert "len(output_data)" in script or "is not None" in script

        namespace: dict = {}
        exec(script, namespace)
        verify = namespace["verify"]

        result = verify({}, [1, 2, 3])
        passed, _ = result["passed"], result["message"]
        assert passed is True

        result = verify({}, [])
        passed, _ = result["passed"], result["message"]
        assert passed is False

    def test_translates_positive_values(self) -> None:
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

        result = verify({}, 5)
        passed, _ = result["passed"], result["message"]
        assert passed is True

        # zero is not positive
        result = verify({}, 0)
        passed, _ = result["passed"], result["message"]
        assert passed is False

        result = verify({}, -1)
        passed, _ = result["passed"], result["message"]
        assert passed is False

    def test_translates_non_negative_values(self) -> None:
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

        result = verify({}, 5)
        passed, _ = result["passed"], result["message"]
        assert passed is True

        # zero counts as non-negative
        result = verify({}, 0)
        passed, _ = result["passed"], result["message"]
        assert passed is True

        result = verify({}, -1)
        passed, _ = result["passed"], result["message"]
        assert passed is False

    def test_translates_not_none(self) -> None:
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

        result = verify({}, "something")
        passed, _ = result["passed"], result["message"]
        assert passed is True

        result = verify({}, 0)
        passed, _ = result["passed"], result["message"]  # 0 is not None
        assert passed is True

        result = verify({}, None)
        passed, _ = result["passed"], result["message"]
        assert passed is False

    def test_translates_type_checks(self) -> None:
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

        result = verify({}, [1, 2, 3])
        passed, _ = result["passed"], result["message"]
        assert passed is True

        result = verify({}, {"a": 1})
        passed, _ = result["passed"], result["message"]
        assert passed is False


# =============================================================================
# Determinism Tests
# =============================================================================


class TestDeterminism:
    """Tests for script generation determinism."""

    def test_same_contract_same_script(self, simple_contract: Contract) -> None:
        script1 = generate_verification_script(simple_contract, "test.py::double")
        script2 = generate_verification_script(simple_contract, "test.py::double")

        assert script1 == script2

    def test_same_contract_same_hash(self, simple_contract: Contract) -> None:
        script1 = generate_verification_script(simple_contract, "test.py::double")
        script2 = generate_verification_script(simple_contract, "test.py::double")

        assert compute_script_hash(script1) == compute_script_hash(script2)

    def test_different_contracts_different_scripts(
        self, simple_contract: Contract, multi_invariant_contract: Contract
    ) -> None:
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

        result = verify({}, [])
        passed, _message = result["passed"], result["message"]
        assert passed is False
        assert result["results"][0]["name"] == "non_empty_output"

    def test_success_message_shows_count(self, multi_invariant_contract: Contract) -> None:
        script = generate_verification_script(
            multi_invariant_contract, "test.py::process_items"
        )

        # the untranslatable no_duplicates invariant must be explicitly skipped, not silently dropped
        namespace = {}
        exec(script, namespace)
        result = namespace["verify"]({}, [1, 2])
        assert len(result["results"]) == 3
        assert result["invariants_skipped"] == 1


# =============================================================================
# Edge Cases
# =============================================================================


class TestEdgeCases:
    """Tests for edge cases in script generation."""

    def test_invariant_with_special_chars_in_logic(self) -> None:
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

        namespace: dict = {}
        exec(compile(script, "<string>", "exec"), namespace)  # it compiles and runs to the end of its definitions
        assert callable(namespace["verify"])  # and defines the function the executor calls

    def test_very_long_invariant_logic(self) -> None:
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

        namespace: dict = {}
        exec(compile(script, "<string>", "exec"), namespace)  # it compiles and runs to the end of its definitions
        assert callable(namespace["verify"])  # and defines the function the executor calls

    def test_many_invariants(self) -> None:
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

        compile(script, "<string>", "exec")

        for i in range(1, 21):
            assert f"_check_invariant_{i}" in script
