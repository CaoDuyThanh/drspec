"""Tests for contract query functions (Story 5-1).

These tests verify the debugger agent's contract query API:
- query_contract: Single contract lookup
- query_contracts: Batch contract lookup
- search_contracts: Partial match search
"""

from __future__ import annotations

import json
import time

import pytest

from drspec.db import (
    get_connection,
    init_schema,
    insert_artifact,
    insert_contract,
    query_contract,
    query_contracts,
    search_contracts,
    ContractDetails,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def db_with_contracts(tmp_path):
    """Create a database with artifacts and contracts for testing."""
    db_path = tmp_path / "_drspec" / "drspec.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = get_connection(str(db_path))
    init_schema(conn)

    # Insert test artifacts
    artifacts_data = [
        {
            "function_id": "src/math.py::calculate_sum",
            "file_path": "src/math.py",
            "function_name": "calculate_sum",
            "signature": "def calculate_sum(a: int, b: int) -> int",
            "body": "def calculate_sum(a: int, b: int) -> int:\n    return a + b",
            "code_hash": "hash_sum",
            "language": "python",
            "start_line": 1,
            "end_line": 3,
        },
        {
            "function_id": "src/math.py::calculate_product",
            "file_path": "src/math.py",
            "function_name": "calculate_product",
            "signature": "def calculate_product(a: int, b: int) -> int",
            "body": "def calculate_product(a: int, b: int) -> int:\n    return a * b",
            "code_hash": "hash_product",
            "language": "python",
            "start_line": 5,
            "end_line": 7,
        },
        {
            "function_id": "src/utils.py::validate_input",
            "file_path": "src/utils.py",
            "function_name": "validate_input",
            "signature": "def validate_input(data: dict) -> bool",
            "body": "def validate_input(data: dict) -> bool:\n    return bool(data)",
            "code_hash": "hash_validate",
            "language": "python",
            "start_line": 1,
            "end_line": 3,
        },
        {
            "function_id": "src/utils.py::process_data",
            "file_path": "src/utils.py",
            "function_name": "process_data",
            "signature": "def process_data(data: list) -> list",
            "body": "def process_data(data: list) -> list:\n    return [x * 2 for x in data]",
            "code_hash": "hash_process",
            "language": "python",
            "start_line": 5,
            "end_line": 7,
        },
    ]

    for artifact in artifacts_data:
        insert_artifact(conn, **artifact)

    # Insert contracts for some artifacts
    contracts_data = [
        {
            "function_id": "src/math.py::calculate_sum",
            "contract_json": json.dumps({
                "preconditions": ["a >= 0", "b >= 0"],
                "postconditions": ["result == a + b"],
                "invariants": [],
            }),
            "confidence_score": 0.85,
            "verification_script": "assert calculate_sum(1, 2) == 3",
        },
        {
            "function_id": "src/math.py::calculate_product",
            "contract_json": json.dumps({
                "preconditions": [],
                "postconditions": ["result == a * b"],
                "invariants": [],
            }),
            "confidence_score": 0.75,
            "verification_script": None,  # No verification script
        },
        {
            "function_id": "src/utils.py::validate_input",
            "contract_json": json.dumps({
                "preconditions": ["data is not None"],
                "postconditions": ["result is bool"],
                "invariants": [],
            }),
            "confidence_score": 0.60,
            "verification_script": "assert validate_input({}) == False",
        },
    ]

    for contract in contracts_data:
        insert_contract(conn, **contract)

    return conn


@pytest.fixture
def empty_db(tmp_path):
    """Create an empty database for testing."""
    db_path = tmp_path / "_drspec" / "drspec.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = get_connection(str(db_path))
    init_schema(conn)
    return conn


# =============================================================================
# ContractDetails Model Tests
# =============================================================================


class TestContractDetailsModel:
    """Tests for ContractDetails dataclass."""

    def test_contract_details_has_expected_fields(self, db_with_contracts):
        """Should have all expected fields."""
        details = query_contract(db_with_contracts, "src/math.py::calculate_sum")

        assert details is not None
        assert hasattr(details, "function_id")
        assert hasattr(details, "contract_json")
        assert hasattr(details, "confidence_score")
        assert hasattr(details, "status")
        assert hasattr(details, "file_path")
        assert hasattr(details, "function_name")
        assert hasattr(details, "created_at")
        assert hasattr(details, "updated_at")
        assert hasattr(details, "has_verification_script")

    def test_contract_details_is_correct_type(self, db_with_contracts):
        """Should return ContractDetails instance."""
        details = query_contract(db_with_contracts, "src/math.py::calculate_sum")

        assert isinstance(details, ContractDetails)


# =============================================================================
# query_contract Tests (AC: 1, 2, 4, 6)
# =============================================================================


class TestQueryContract:
    """Tests for query_contract function."""

    def test_returns_contract_details(self, db_with_contracts):
        """Should return full contract details for existing contract."""
        details = query_contract(db_with_contracts, "src/math.py::calculate_sum")

        assert details is not None
        assert details.function_id == "src/math.py::calculate_sum"
        assert details.function_name == "calculate_sum"
        assert details.file_path == "src/math.py"
        assert abs(details.confidence_score - 0.85) < 0.001  # Float comparison
        assert details.status == "PENDING"

    def test_includes_contract_json(self, db_with_contracts):
        """Should include contract JSON."""
        details = query_contract(db_with_contracts, "src/math.py::calculate_sum")

        assert details is not None
        contract = json.loads(details.contract_json)
        assert "preconditions" in contract
        assert "postconditions" in contract

    def test_includes_has_verification_script_true(self, db_with_contracts):
        """Should indicate presence of verification script."""
        details = query_contract(db_with_contracts, "src/math.py::calculate_sum")

        assert details is not None
        assert details.has_verification_script is True

    def test_includes_has_verification_script_false(self, db_with_contracts):
        """Should indicate absence of verification script."""
        details = query_contract(db_with_contracts, "src/math.py::calculate_product")

        assert details is not None
        assert details.has_verification_script is False

    def test_includes_timestamps(self, db_with_contracts):
        """Should include created_at and updated_at timestamps."""
        details = query_contract(db_with_contracts, "src/math.py::calculate_sum")

        assert details is not None
        assert details.created_at is not None
        assert details.updated_at is not None

    def test_returns_none_for_nonexistent_contract(self, db_with_contracts):
        """Should return None for non-existent contract (AC: 6)."""
        details = query_contract(db_with_contracts, "nonexistent::function")

        assert details is None

    def test_returns_none_for_artifact_without_contract(self, db_with_contracts):
        """Should return None for artifact that has no contract."""
        # process_data exists as artifact but has no contract
        details = query_contract(db_with_contracts, "src/utils.py::process_data")

        assert details is None

    def test_query_performance_under_100ms(self, db_with_contracts):
        """Should return in under 100ms (NFR2)."""
        start = time.perf_counter()
        _ = query_contract(db_with_contracts, "src/math.py::calculate_sum")
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert elapsed_ms < 100, f"Query took {elapsed_ms:.2f}ms, expected < 100ms"


# =============================================================================
# query_contracts (Batch) Tests (AC: 5)
# =============================================================================


class TestQueryContracts:
    """Tests for query_contracts batch function."""

    def test_returns_multiple_contracts(self, db_with_contracts):
        """Should return multiple contracts in single query."""
        function_ids = [
            "src/math.py::calculate_sum",
            "src/math.py::calculate_product",
        ]
        results = query_contracts(db_with_contracts, function_ids)

        assert len(results) == 2
        assert "src/math.py::calculate_sum" in results
        assert "src/math.py::calculate_product" in results

    def test_returns_dict_of_contract_details(self, db_with_contracts):
        """Should return dict mapping function_id to ContractDetails."""
        function_ids = ["src/math.py::calculate_sum"]
        results = query_contracts(db_with_contracts, function_ids)

        assert isinstance(results, dict)
        assert isinstance(results["src/math.py::calculate_sum"], ContractDetails)

    def test_omits_nonexistent_contracts(self, db_with_contracts):
        """Should omit non-existent contracts from results (AC: 6)."""
        function_ids = [
            "src/math.py::calculate_sum",
            "nonexistent::function",
            "src/utils.py::process_data",  # Exists as artifact but no contract
        ]
        results = query_contracts(db_with_contracts, function_ids)

        assert len(results) == 1
        assert "src/math.py::calculate_sum" in results
        assert "nonexistent::function" not in results
        assert "src/utils.py::process_data" not in results

    def test_returns_empty_dict_for_empty_list(self, db_with_contracts):
        """Should return empty dict for empty input list."""
        results = query_contracts(db_with_contracts, [])

        assert results == {}

    def test_returns_empty_dict_when_all_missing(self, db_with_contracts):
        """Should return empty dict when all requested contracts are missing."""
        function_ids = ["nonexistent::a", "nonexistent::b"]
        results = query_contracts(db_with_contracts, function_ids)

        assert results == {}

    def test_batch_query_performance(self, db_with_contracts):
        """Should handle batch query efficiently."""
        function_ids = [
            "src/math.py::calculate_sum",
            "src/math.py::calculate_product",
            "src/utils.py::validate_input",
        ]

        start = time.perf_counter()
        _ = query_contracts(db_with_contracts, function_ids)
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert elapsed_ms < 100, f"Batch query took {elapsed_ms:.2f}ms, expected < 100ms"


# =============================================================================
# search_contracts Tests (AC: 3)
# =============================================================================


class TestSearchContracts:
    """Tests for search_contracts partial match function."""

    def test_finds_by_function_name_prefix(self, db_with_contracts):
        """Should find contracts by function name prefix."""
        results = search_contracts(db_with_contracts, "calculate")

        assert len(results) == 2
        function_names = [r.function_name for r in results]
        assert "calculate_sum" in function_names
        assert "calculate_product" in function_names

    def test_finds_by_partial_function_id(self, db_with_contracts):
        """Should find contracts by partial function_id match."""
        results = search_contracts(db_with_contracts, "math.py")

        assert len(results) == 2

    def test_exact_match_first(self, db_with_contracts):
        """Should prioritize exact function name matches."""
        results = search_contracts(db_with_contracts, "validate_input")

        assert len(results) == 1
        assert results[0].function_name == "validate_input"

    def test_respects_limit(self, db_with_contracts):
        """Should respect limit parameter."""
        results = search_contracts(db_with_contracts, "calculate", limit=1)

        assert len(results) == 1

    def test_returns_empty_for_no_match(self, db_with_contracts):
        """Should return empty list when no matches found."""
        results = search_contracts(db_with_contracts, "nonexistent_pattern")

        assert results == []

    def test_returns_empty_for_empty_pattern(self, db_with_contracts):
        """Should return empty list for empty pattern."""
        results = search_contracts(db_with_contracts, "")

        assert results == []

    def test_search_results_are_contract_details(self, db_with_contracts):
        """Should return ContractDetails instances."""
        results = search_contracts(db_with_contracts, "calculate")

        assert all(isinstance(r, ContractDetails) for r in results)

    def test_search_includes_all_metadata(self, db_with_contracts):
        """Should include all metadata in search results."""
        results = search_contracts(db_with_contracts, "validate_input")

        assert len(results) == 1
        details = results[0]
        assert details.function_id == "src/utils.py::validate_input"
        assert abs(details.confidence_score - 0.60) < 0.001  # Float comparison
        assert details.has_verification_script is True


# =============================================================================
# Edge Cases
# =============================================================================


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def test_query_with_special_characters(self, db_with_contracts):
        """Should handle function IDs with special characters."""
        # The :: separator is part of function_id format
        result = query_contract(db_with_contracts, "src/math.py::calculate_sum")
        assert result is not None

    def test_empty_database(self, empty_db):
        """Should handle empty database gracefully."""
        assert query_contract(empty_db, "any::function") is None
        assert query_contracts(empty_db, ["any::function"]) == {}
        assert search_contracts(empty_db, "any") == []

    def test_case_sensitive_search(self, db_with_contracts):
        """Should perform case-sensitive search."""
        # Original is "calculate_sum", searching for "Calculate" should not match prefix
        results = search_contracts(db_with_contracts, "Calculate")

        # May or may not match depending on LIKE behavior
        # DuckDB LIKE is case-sensitive by default
        function_names = [r.function_name for r in results]
        assert "calculate_sum" not in function_names or len(results) == 0


# =============================================================================
# Index Tests
# =============================================================================


class TestIndexes:
    """Tests verifying indexes improve performance."""

    def test_function_name_index_exists(self, db_with_contracts):
        """Should have index on artifacts.function_name."""
        # Query index metadata
        result = db_with_contracts.execute(
            "SELECT index_name FROM duckdb_indexes() WHERE table_name = 'artifacts'"
        ).fetchall()

        index_names = [r[0] for r in result]
        assert "idx_artifacts_function_name" in index_names
