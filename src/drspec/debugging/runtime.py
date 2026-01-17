"""Runtime verification for DrSpec debugger agent.

This module provides APIs for verifying function behavior at runtime,
designed for use by the debugger agent during debugging sessions.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import os
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

# Default timeout in seconds (duplicated from executor.py to avoid circular import)
DEFAULT_TIMEOUT = 1.0


def _get_safe_env() -> dict[str, str]:
    """Get a safe environment for subprocess execution.

    Returns a minimal environment that:
    - Includes PATH for Python execution
    - Excludes sensitive environment variables
    - Sets PYTHONDONTWRITEBYTECODE to avoid .pyc files

    Returns:
        Dictionary of environment variables.
    """
    safe_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    }

    # Add Python-related paths if available
    if "PYTHONPATH" in os.environ:
        safe_env["PYTHONPATH"] = os.environ["PYTHONPATH"]

    # Add virtual environment if active
    if "VIRTUAL_ENV" in os.environ:
        safe_env["VIRTUAL_ENV"] = os.environ["VIRTUAL_ENV"]

    return safe_env


# =============================================================================
# Result Models
# =============================================================================


@dataclass
class InvariantResult:
    """Result of checking a single invariant.

    Attributes:
        name: Invariant name/identifier.
        passed: Whether this invariant passed.
        criticality: Criticality level (HIGH, MEDIUM, LOW).
        message: Descriptive message, especially for failures.
        expected: Expected value or condition (if available).
        actual: Actual value observed (if available).
    """

    name: str
    passed: bool
    criticality: str
    message: Optional[str] = None
    expected: Optional[Any] = None
    actual: Optional[Any] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "name": self.name,
            "passed": self.passed,
            "criticality": self.criticality,
            "message": self.message,
            "expected": self.expected,
            "actual": self.actual,
        }


@dataclass
class RuntimeVerificationResult:
    """Result of runtime verification.

    Attributes:
        function_id: Function that was verified.
        passed: Whether all invariants passed.
        invariants: List of per-invariant results.
        execution_time_ms: Time taken in milliseconds.
        error: Error message if execution failed.
    """

    function_id: str
    passed: bool
    invariants: list[InvariantResult] = field(default_factory=list)
    execution_time_ms: float = 0.0
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "function_id": self.function_id,
            "passed": self.passed,
            "invariants": [inv.to_dict() for inv in self.invariants],
            "execution_time_ms": self.execution_time_ms,
            "error": self.error,
        }

    @property
    def failed_invariants(self) -> list[InvariantResult]:
        """Get list of failed invariants."""
        return [inv for inv in self.invariants if not inv.passed]

    @property
    def critical_failures(self) -> list[InvariantResult]:
        """Get list of failed HIGH criticality invariants."""
        return [inv for inv in self.invariants if not inv.passed and inv.criticality == "HIGH"]


# =============================================================================
# Data Serialization
# =============================================================================


def serialize_for_verification(data: Any) -> Any:
    """Serialize complex data types for verification script.

    Handles:
    - datetime objects → ISO format string with type marker
    - Decimal objects → string representation with type marker
    - Objects with __dict__ → dict representation
    - Lists and tuples → recursively serialized lists
    - Dicts → recursively serialized dicts
    - None → None
    - Primitives → as-is

    Args:
        data: Any data to serialize.

    Returns:
        JSON-serializable representation.
    """
    if data is None:
        return None

    if isinstance(data, datetime):
        return {"__type__": "datetime", "value": data.isoformat()}

    if isinstance(data, Decimal):
        return {"__type__": "decimal", "value": str(data)}

    if isinstance(data, bytes):
        return {"__type__": "bytes", "value": data.decode("utf-8", errors="replace")}

    if isinstance(data, (set, frozenset)):
        return {"__type__": "set", "value": [serialize_for_verification(item) for item in data]}

    if isinstance(data, (list, tuple)):
        return [serialize_for_verification(item) for item in data]

    if isinstance(data, dict):
        return {str(k): serialize_for_verification(v) for k, v in data.items()}

    if hasattr(data, "__dict__") and not isinstance(data, type):
        # Object with attributes - serialize as dict with type info
        return {
            "__type__": type(data).__name__,
            **{k: serialize_for_verification(v) for k, v in data.__dict__.items()},
        }

    # Primitives: int, float, str, bool
    return data


def deserialize_from_verification(data: Any) -> Any:
    """Deserialize data from verification format.

    Reverses serialize_for_verification for common types.

    Args:
        data: Serialized data.

    Returns:
        Deserialized data (best effort).
    """
    if data is None:
        return None

    if isinstance(data, dict):
        if "__type__" in data:
            type_name = data["__type__"]
            if type_name == "datetime":
                return datetime.fromisoformat(data["value"])
            if type_name == "decimal":
                return Decimal(data["value"])
            if type_name == "bytes":
                return data["value"].encode("utf-8")
            if type_name == "set":
                return set(deserialize_from_verification(item) for item in data["value"])
            # Other types: return as dict without __type__ marker
            return {k: deserialize_from_verification(v) for k, v in data.items() if k != "__type__"}

        return {k: deserialize_from_verification(v) for k, v in data.items()}

    if isinstance(data, list):
        return [deserialize_from_verification(item) for item in data]

    return data


# =============================================================================
# Runtime Verification API
# =============================================================================


def verify_at_runtime(
    function_id: str,
    script: str,
    input_data: dict[str, Any],
    output_data: Any,
    invariant_info: Optional[list[dict[str, str]]] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> RuntimeVerificationResult:
    """Verify function behavior against contract at runtime.

    This is the primary API for the debugger agent to verify actual
    function behavior against its contract.

    Args:
        function_id: Function ID being verified.
        script: Verification script source code.
        input_data: Dictionary of input parameters from actual execution.
        output_data: Actual output from function execution.
        invariant_info: Optional list of invariant metadata for detailed reporting.
            Each dict should have: name, logic, criticality.
        timeout: Maximum execution time in seconds (default: 1.0).

    Returns:
        RuntimeVerificationResult with detailed per-invariant results.
    """
    start_time = time.time()

    # Serialize complex data
    serialized_input = serialize_for_verification(input_data)
    serialized_output = serialize_for_verification(output_data)

    # Create enhanced wrapper script for detailed reporting
    wrapper_script = _create_detailed_wrapper(script, invariant_info or [])

    # Execute verification
    script_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".py",
            delete=False,
            encoding="utf-8",
        ) as f:
            f.write(wrapper_script)
            script_path = f.name

        input_json = json.dumps({
            "input": serialized_input,
            "output": serialized_output,
        })

        try:
            result = subprocess.run(
                [sys.executable, script_path],
                input=input_json,
                capture_output=True,
                timeout=timeout,
                text=True,
                env=_get_safe_env(),
            )

            execution_time_ms = (time.time() - start_time) * 1000

            if result.returncode != 0:
                error_msg = result.stderr.strip() if result.stderr else "Unknown error"
                return RuntimeVerificationResult(
                    function_id=function_id,
                    passed=False,
                    execution_time_ms=execution_time_ms,
                    error=f"Script execution failed: {error_msg}",
                )

            try:
                output = json.loads(result.stdout)
                return _parse_detailed_result(function_id, output, execution_time_ms)
            except json.JSONDecodeError as e:
                return RuntimeVerificationResult(
                    function_id=function_id,
                    passed=False,
                    execution_time_ms=execution_time_ms,
                    error=f"Failed to parse script output: {e}",
                )

        except subprocess.TimeoutExpired:
            execution_time_ms = (time.time() - start_time) * 1000
            return RuntimeVerificationResult(
                function_id=function_id,
                passed=False,
                execution_time_ms=execution_time_ms,
                error=f"Verification timed out after {timeout} seconds",
            )

        except Exception as e:
            execution_time_ms = (time.time() - start_time) * 1000
            return RuntimeVerificationResult(
                function_id=function_id,
                passed=False,
                execution_time_ms=execution_time_ms,
                error=f"Execution error: {str(e)}",
            )

    finally:
        if script_path and os.path.exists(script_path):
            try:
                os.unlink(script_path)
            except OSError:
                pass


def _create_detailed_wrapper(script: str, invariant_info: list[dict[str, str]]) -> str:
    """Create wrapper script with detailed per-invariant reporting.

    Args:
        script: Original verification script.
        invariant_info: List of invariant metadata.

    Returns:
        Enhanced wrapper script.
    """
    # Encode invariant info as JSON for the script
    invariant_json = json.dumps(invariant_info)

    return f'''{script}

# Enhanced wrapper for detailed reporting
import sys
import json
import re

def _main():
    invariant_info = {invariant_json}

    try:
        # Read input from stdin
        input_json = sys.stdin.read()
        data = json.loads(input_json)

        input_data = data["input"]
        output_data = data["output"]

        # Check each invariant individually
        invariant_results = []
        all_passed = True

        # Find all _check_invariant_N functions
        check_funcs = [(name, func) for name, func in globals().items()
                       if name.startswith('_check_invariant_') and callable(func)]
        check_funcs.sort(key=lambda x: int(re.search(r'(\\d+)', x[0]).group(1)))

        for i, (func_name, check_func) in enumerate(check_funcs):
            try:
                passed = check_func(input_data, output_data)
            except Exception as e:
                passed = False

            # Get invariant info if available
            info = invariant_info[i] if i < len(invariant_info) else {{}}
            name = info.get('name', f'invariant_{{i+1}}')
            criticality = info.get('criticality', 'MEDIUM')
            logic = info.get('logic', '')

            result = {{
                "name": name,
                "passed": passed,
                "criticality": criticality,
                "message": None if passed else f"Invariant violated: {{logic}}",
                "expected": None,
                "actual": None,
            }}

            invariant_results.append(result)
            if not passed:
                all_passed = False

        # If no check functions found, try the main verify function
        if not check_funcs:
            passed, message = verify(input_data, output_data)
            invariant_results = [{{
                "name": "contract",
                "passed": passed,
                "criticality": "HIGH",
                "message": None if passed else message,
                "expected": None,
                "actual": None,
            }}]
            all_passed = passed

        result = {{
            "passed": all_passed,
            "invariants": invariant_results,
        }}
        print(json.dumps(result))

    except Exception as e:
        result = {{
            "passed": False,
            "invariants": [{{
                "name": "execution",
                "passed": False,
                "criticality": "HIGH",
                "message": f"Verification error: {{str(e)}}",
                "expected": None,
                "actual": None,
            }}],
        }}
        print(json.dumps(result))

if __name__ == "__main__":
    _main()
'''


def _parse_detailed_result(
    function_id: str,
    output: dict[str, Any],
    execution_time_ms: float,
) -> RuntimeVerificationResult:
    """Parse detailed verification output.

    Args:
        function_id: Function being verified.
        output: JSON output from verification script.
        execution_time_ms: Execution time.

    Returns:
        RuntimeVerificationResult with parsed invariant results.
    """
    invariants = []
    for inv_data in output.get("invariants", []):
        invariants.append(InvariantResult(
            name=inv_data.get("name", "unknown"),
            passed=inv_data.get("passed", False),
            criticality=inv_data.get("criticality", "MEDIUM"),
            message=inv_data.get("message"),
            expected=inv_data.get("expected"),
            actual=inv_data.get("actual"),
        ))

    return RuntimeVerificationResult(
        function_id=function_id,
        passed=output.get("passed", False),
        invariants=invariants,
        execution_time_ms=execution_time_ms,
        error=None,
    )


def verify_contract_at_runtime(
    conn,  # DuckDB connection
    function_id: str,
    input_data: dict[str, Any],
    output_data: Any,
    timeout: float = DEFAULT_TIMEOUT,
) -> RuntimeVerificationResult:
    """Convenience function to verify using stored contract.

    Fetches the contract and verification script from the database,
    then performs runtime verification.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to verify.
        input_data: Dictionary of input parameters.
        output_data: Actual function output.
        timeout: Maximum execution time in seconds.

    Returns:
        RuntimeVerificationResult with verification results.

    Note:
        This function imports from drspec.db to avoid circular imports.
    """
    from drspec.contracts.generator import generate_verification_script
    from drspec.contracts.schema import Contract
    from drspec.db import get_contract

    start_time = time.time()

    # Get contract from database
    contract_data = get_contract(conn, function_id)
    if contract_data is None:
        return RuntimeVerificationResult(
            function_id=function_id,
            passed=False,
            execution_time_ms=(time.time() - start_time) * 1000,
            error=f"No contract found for function: {function_id}",
        )

    # Parse contract
    try:
        contract = Contract.from_json(contract_data["contract_json"])
    except Exception as e:
        return RuntimeVerificationResult(
            function_id=function_id,
            passed=False,
            execution_time_ms=(time.time() - start_time) * 1000,
            error=f"Failed to parse contract: {e}",
        )

    # Get or generate verification script
    script = contract_data.get("verification_script")
    if not script:
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

    # Perform verification
    return verify_at_runtime(
        function_id=function_id,
        script=script,
        input_data=input_data,
        output_data=output_data,
        invariant_info=invariant_info,
        timeout=timeout,
    )
