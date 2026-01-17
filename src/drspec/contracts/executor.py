"""Script execution engine for DrSpec verification.

This module provides safe execution of verification scripts in isolated
subprocesses with timeout and error handling.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Optional


# Default timeout in seconds
DEFAULT_TIMEOUT = 1.0


@dataclass
class VerificationResult:
    """Result of executing a verification script.

    Attributes:
        passed: Whether all invariants passed.
        message: Human-readable result message.
        execution_time: Time taken in seconds.
        error: Error code if execution failed (None, TIMEOUT, EXECUTION_ERROR, PARSE_ERROR).
        invariants_checked: Number of invariants checked.
        invariants_passed: Number of invariants that passed.
    """

    passed: bool
    message: str
    execution_time: float
    error: Optional[str] = None
    invariants_checked: int = 0
    invariants_passed: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "passed": self.passed,
            "message": self.message,
            "execution_time": self.execution_time,
            "error": self.error,
            "invariants_checked": self.invariants_checked,
            "invariants_passed": self.invariants_passed,
        }


def execute_verification(
    script: str,
    input_data: dict[str, Any],
    output_data: Any,
    timeout: float = DEFAULT_TIMEOUT,
) -> VerificationResult:
    """Execute a verification script in an isolated subprocess.

    The script is executed in a separate Python process with timeout
    protection. Input and output data are passed via stdin as JSON.

    Args:
        script: Python verification script source code.
        input_data: Dictionary of input parameters to verify.
        output_data: The function's output to verify.
        timeout: Maximum execution time in seconds (default: 1.0).

    Returns:
        VerificationResult with pass/fail status and execution details.
    """
    start_time = time.time()

    # Create a wrapper script that imports the verify function and runs it
    wrapper_script = _create_wrapper_script(script)

    # Create temporary script file
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

        # Prepare input data as JSON
        input_json = json.dumps({
            "input": input_data,
            "output": output_data,
        })

        # Execute in subprocess
        try:
            result = subprocess.run(
                [sys.executable, script_path],
                input=input_json,
                capture_output=True,
                timeout=timeout,
                text=True,
                env=_get_safe_env(),
            )

            execution_time = time.time() - start_time

            # Check for subprocess errors
            if result.returncode != 0:
                error_msg = result.stderr.strip() if result.stderr else "Unknown error"
                return VerificationResult(
                    passed=False,
                    message=f"Script execution failed: {error_msg}",
                    execution_time=execution_time,
                    error="EXECUTION_ERROR",
                )

            # Parse the output
            try:
                output = json.loads(result.stdout)
                return VerificationResult(
                    passed=output.get("passed", False),
                    message=output.get("message", "No message"),
                    execution_time=execution_time,
                    error=None,
                    invariants_checked=output.get("invariants_checked", 0),
                    invariants_passed=output.get("invariants_passed", 0),
                )
            except json.JSONDecodeError as e:
                return VerificationResult(
                    passed=False,
                    message=f"Failed to parse script output: {e}",
                    execution_time=execution_time,
                    error="PARSE_ERROR",
                )

        except subprocess.TimeoutExpired:
            execution_time = time.time() - start_time
            return VerificationResult(
                passed=False,
                message=f"Verification timed out after {timeout} seconds",
                execution_time=execution_time,
                error="TIMEOUT",
            )

        except Exception as e:
            execution_time = time.time() - start_time
            return VerificationResult(
                passed=False,
                message=f"Execution error: {str(e)}",
                execution_time=execution_time,
                error="EXECUTION_ERROR",
            )

    finally:
        # Clean up temporary file
        if script_path and os.path.exists(script_path):
            try:
                os.unlink(script_path)
            except OSError:
                pass  # Best effort cleanup


def _create_wrapper_script(verification_script: str) -> str:
    """Create a wrapper script that executes the verification and outputs JSON.

    Args:
        verification_script: The verification script source code.

    Returns:
        Complete wrapper script with JSON I/O handling.
    """
    return f'''{verification_script}

# Wrapper code to execute verification and output JSON
import sys
import json

def _main():
    try:
        # Read input from stdin
        input_json = sys.stdin.read()
        data = json.loads(input_json)

        # Execute verification
        passed, message = verify(data["input"], data["output"])

        # Count invariants (estimate from message)
        invariants_checked = 0
        invariants_passed = 0
        if passed:
            # Try to extract count from success message like "All 3 invariant(s) passed"
            import re
            match = re.search(r"(\\d+)\\s+invariant", message)
            if match:
                invariants_checked = int(match.group(1))
                invariants_passed = invariants_checked
        else:
            # For failures, we know at least one failed
            invariants_checked = 1

        # Output result as JSON
        result = {{
            "passed": passed,
            "message": message,
            "invariants_checked": invariants_checked,
            "invariants_passed": invariants_passed,
        }}
        print(json.dumps(result))

    except Exception as e:
        result = {{
            "passed": False,
            "message": f"Verification error: {{str(e)}}",
            "invariants_checked": 0,
            "invariants_passed": 0,
        }}
        print(json.dumps(result))

if __name__ == "__main__":
    _main()
'''


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


def validate_script(script: str) -> tuple[bool, Optional[str]]:
    """Validate that a verification script is syntactically correct.

    Args:
        script: Python source code to validate.

    Returns:
        Tuple of (is_valid, error_message).
    """
    try:
        compile(script, "<verification_script>", "exec")
        return True, None
    except SyntaxError as e:
        return False, f"Syntax error at line {e.lineno}: {e.msg}"
    except Exception as e:
        return False, f"Compilation error: {str(e)}"
