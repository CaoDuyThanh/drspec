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
from dataclasses import dataclass, field
from typing import Any, Optional


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

    status: str = "unverified"
    results: list[dict[str, Any]] = field(default_factory=list)
    invariants_failed: int = 0
    invariants_skipped: int = 0
    invariants_error: int = 0

    def __post_init__(self) -> None:
        if self.error:
            self.status = "error"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "status": self.status,
            "results": self.results,
            "invariants_failed": self.invariants_failed,
            "invariants_skipped": self.invariants_skipped,
            "invariants_error": self.invariants_error,
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

    wrapper_script = _create_wrapper_script(script)

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
            "input": input_data,
            "output": output_data,
        })

        try:
            result = subprocess.run(
                [sys.executable, *(["--drspec-internal-verify"] if getattr(sys, "frozen", False) else []), script_path],
                input=input_json,
                capture_output=True,
                timeout=timeout,
                text=True,
                env=_get_safe_env(),
            )

            execution_time = time.time() - start_time

            if result.returncode != 0:
                error_msg = result.stderr.strip() if result.stderr else "Unknown error"
                return VerificationResult(
                    passed=False,
                    message=f"Script execution failed: {error_msg}",
                    execution_time=execution_time,
                    error="EXECUTION_ERROR",
                )

            try:
                output = json.loads(result.stdout)
                return VerificationResult(
                    passed=output.get("passed", False),
                    message=output.get("message", "No message"),
                    execution_time=execution_time,
                    error=output.get("error"),
                    status=output.get("status", "unverified"),
                    results=output.get("results", []),
                    invariants_failed=output.get("invariants_failed", 0),
                    invariants_skipped=output.get("invariants_skipped", 0),
                    invariants_error=output.get("invariants_error", 0),
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
        result = verify(data["input"], data["output"])
        if not isinstance(result, dict):
            # Custom legacy scripts remain usable, but have no per-rule evidence.
            passed, message = result
            result = {{"passed": bool(passed), "message": message,
                       "status": "legacy", "invariants_checked": 0, "invariants_passed": 0}}

        print(json.dumps(result))

    except Exception as e:
        result = {{
            "passed": False,
            "message": f"Verification error: {{str(e)}}",
            "status": "error",
            "error": "EXECUTION_ERROR",
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

    # PyInstaller child processes reuse the already extracted runtime.
    if getattr(sys, "frozen", False):
        safe_env.update({key: value for key, value in os.environ.items() if key.startswith("_PYI_")})

    if "PYTHONPATH" in os.environ:
        safe_env["PYTHONPATH"] = os.environ["PYTHONPATH"]

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
