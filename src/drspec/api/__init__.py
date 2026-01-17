"""DrSpec Public API.

This module provides programmatic access to DrSpec functionality.
Use these functions to integrate DrSpec into your tools, scripts, or agents.

Example:
    from drspec import api

    # Query a contract
    contract = api.query_contract("src/utils.py::parse")
    if contract:
        print(contract.intent_summary)

    # Run verification
    result = api.run_verification(
        "src/utils.py::parse",
        input_data={"text": "hello"},
        output_data={"key": "value"}
    )
    if not result.passed:
        print(f"Failed: {result.error}")

    # List pending queue
    queue = api.list_queue(status="PENDING")
    for item in queue:
        print(f"{item.function_id} (priority: {item.priority})")

    # Get dependencies
    graph = api.get_dependencies("src/api.py::handle_request", depth=2)
    for node in graph.nodes:
        print(f"{node.function_id}: has_contract={node.has_contract}")
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, List, Optional

from drspec.contracts.schema import Contract
from drspec.db.connection import get_connection, get_db_path
from drspec.db.queries import (
    QueueItem,
    get_contract as db_get_contract,
    queue_peek,
)
from drspec.db.graph import (
    DependencyGraph,
    get_dependency_graph as db_get_dependency_graph,
)
from drspec.debugging.runtime import (
    RuntimeVerificationResult,
    verify_at_runtime,
)
from drspec.contracts.generator import generate_verification_script


# =============================================================================
# EXCEPTIONS
# =============================================================================


class DrSpecError(Exception):
    """Base exception for DrSpec API errors."""

    pass


class NotInitializedError(DrSpecError):
    """DrSpec is not initialized in this project."""

    pass


class ContractNotFoundError(DrSpecError):
    """No contract exists for the specified function."""

    pass


class VerificationError(DrSpecError):
    """Verification script execution failed."""

    pass


# =============================================================================
# GLOBAL CONFIGURATION
# =============================================================================

_configured_db_path: Optional[Path] = None


def _get_db_path() -> Path:
    """Get the database path from configuration or environment.

    Order of precedence:
    1. Programmatic configuration via configure()
    2. DRSPEC_DB_PATH environment variable
    3. Default: _drspec/contracts.db in current directory

    Returns:
        Path to the database file.
    """
    global _configured_db_path

    if _configured_db_path is not None:
        return _configured_db_path

    env_path = os.environ.get("DRSPEC_DB_PATH")
    if env_path:
        return Path(env_path)

    return get_db_path()


def _ensure_initialized() -> None:
    """Ensure DrSpec is initialized.

    Raises:
        NotInitializedError: If DrSpec is not initialized.
    """
    if not is_initialized():
        raise NotInitializedError(
            "DrSpec is not initialized in this project. "
            "Run 'drspec init' first, or use configure() to specify a custom path."
        )


# =============================================================================
# CONFIGURATION
# =============================================================================


def configure(db_path: Optional[str] = None) -> None:
    """Configure the DrSpec API.

    Args:
        db_path: Path to contracts.db file.
                 If None, uses _drspec/contracts.db in current directory.

    Example:
        >>> configure("/path/to/project/_drspec/contracts.db")
    """
    global _configured_db_path

    if db_path is None:
        _configured_db_path = None
    else:
        _configured_db_path = Path(db_path)


def is_initialized() -> bool:
    """Check if DrSpec is initialized in current directory.

    Returns:
        True if _drspec/ folder and database exist.

    Example:
        >>> if is_initialized():
        ...     contract = query_contract("src/main.py::foo")
    """
    db_path = _get_db_path()
    return db_path.exists()


# =============================================================================
# PUBLIC API FUNCTIONS
# =============================================================================


def query_contract(function_id: str) -> Optional[Contract]:
    """Query a semantic contract by function ID.

    Args:
        function_id: Function identifier in format "filepath::function_name"

    Returns:
        Contract object if found, None if no contract exists.

    Raises:
        NotInitializedError: If DrSpec is not initialized.

    Example:
        >>> contract = query_contract("src/payments/reconcile.py::reconcile")
        >>> if contract:
        ...     print(contract.intent_summary)
        'Matches pending with posted transactions'
    """
    _ensure_initialized()

    conn = get_connection(_get_db_path())
    try:
        contract_row = db_get_contract(conn, function_id)
        if contract_row is None:
            return None
        # Parse contract JSON into Contract object
        return Contract.from_json(contract_row["contract_json"])
    finally:
        conn.close()


def run_verification(
    function_id: str,
    input_data: dict,
    output_data: Any,
    timeout: float = 1.0,
) -> RuntimeVerificationResult:
    """Run contract verification against provided data.

    Args:
        function_id: Function to verify.
        input_data: Input parameters as dictionary.
        output_data: Actual output from function.
        timeout: Maximum execution time in seconds (default: 1.0).

    Returns:
        RuntimeVerificationResult with passed status and details.

    Raises:
        NotInitializedError: If DrSpec is not initialized.
        ContractNotFoundError: If no contract exists for the function.

    Example:
        >>> result = run_verification(
        ...     "src/utils.py::parse",
        ...     input_data={"text": "hello"},
        ...     output_data={"parsed": True}
        ... )
        >>> result.passed
        True
    """
    _ensure_initialized()

    conn = get_connection(_get_db_path())
    try:
        contract_row = db_get_contract(conn, function_id)
        if contract_row is None:
            raise ContractNotFoundError(
                f"No contract found for function: {function_id}"
            )

        # Parse contract JSON into Contract object
        contract = Contract.from_json(contract_row["contract_json"])

        # Generate verification script
        script = generate_verification_script(contract, function_id)

        # Build invariant info for detailed reporting
        invariant_info = [
            {
                "name": inv.name,
                "logic": inv.logic,
                "criticality": inv.criticality.value,
            }
            for inv in contract.invariants
        ]

        # Run verification
        return verify_at_runtime(
            function_id=function_id,
            script=script,
            input_data=input_data,
            output_data=output_data,
            invariant_info=invariant_info,
            timeout=timeout,
        )
    finally:
        conn.close()


def list_queue(
    status: Optional[str] = None,
    priority_min: Optional[int] = None,
    limit: int = 100,
) -> List[QueueItem]:
    """List items in the processing queue.

    Args:
        status: Filter by status (PENDING, IN_PROGRESS, COMPLETED, FAILED).
                If None, returns only PENDING items.
        priority_min: Minimum priority to include (higher = more important).
        limit: Maximum items to return.

    Returns:
        List of QueueItem objects, ordered by priority descending.

    Raises:
        NotInitializedError: If DrSpec is not initialized.

    Example:
        >>> queue = list_queue(status="PENDING", limit=10)
        >>> for item in queue:
        ...     print(f"{item.function_id} (priority: {item.priority})")
    """
    _ensure_initialized()

    conn = get_connection(_get_db_path())
    try:
        # Use include_all=True if status is specified to get all statuses
        include_all = status is not None
        items = queue_peek(conn, count=limit, include_all=include_all)

        # Filter by status if specified
        if status is not None:
            items = [item for item in items if item.status == status]

        # Filter by priority if specified
        if priority_min is not None:
            items = [item for item in items if item.priority >= priority_min]

        return items
    finally:
        conn.close()


def get_dependencies(
    function_id: str,
    depth: int = 2,
    direction: str = "both",
) -> DependencyGraph:
    """Get dependency graph for a function.

    Args:
        function_id: Center node of graph.
        depth: Maximum traversal depth (1-5, default: 2).
        direction: "callees", "callers", or "both".

    Returns:
        DependencyGraph with nodes and edges.

    Raises:
        NotInitializedError: If DrSpec is not initialized.

    Example:
        >>> graph = get_dependencies("src/api.py::handle", depth=1)
        >>> for node in graph.nodes:
        ...     status = "has contract" if node.has_contract else "no contract"
        ...     print(f"{node.function_id}: {status}")
    """
    _ensure_initialized()

    conn = get_connection(_get_db_path())
    try:
        return db_get_dependency_graph(conn, function_id, depth=depth, direction=direction)
    finally:
        conn.close()


# =============================================================================
# MODULE EXPORTS
# =============================================================================

__all__ = [
    # Exceptions
    "DrSpecError",
    "NotInitializedError",
    "ContractNotFoundError",
    "VerificationError",
    # Configuration
    "configure",
    "is_initialized",
    # Public API
    "query_contract",
    "run_verification",
    "list_queue",
    "get_dependencies",
    # Re-exported types for convenience
    "Contract",
    "QueueItem",
    "DependencyGraph",
    "RuntimeVerificationResult",
]
