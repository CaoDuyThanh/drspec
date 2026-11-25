"""Verify command - Run verification scripts."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer

from drspec.cli.output import ErrorCode, error_response, output, success_response
from drspec.cli.validators import resolve_or_exit, validate_function_id
from drspec.contracts import (
    DEFAULT_TIMEOUT,
    Contract,
    execute_verification,
    generate_verification_script,
)
from drspec.core.provenance import contract_freshness
from drspec.db import get_contract
from drspec.db.queries import get_artifact_code_hash, set_config, update_artifact_status
from drspec.worker_ipc import WorkerIPCError, get_worker_connection

app = typer.Typer(
    name="verify",
    help="Run contract verification scripts",
)


def _transform_to_plot_data(input_data: dict[str, Any], output_data: Any) -> dict[str, Any]:
    """Transform verification test data into plottable format.

    Attempts to extract x/y data or categories/values from the input/output.

    Args:
        input_data: Input data from test case.
        output_data: Output data from test case.

    Returns:
        Dictionary suitable for generate_plot().
    """
    if "x" in input_data and "y" in output_data:
        return {"x": input_data["x"], "y": output_data["y"]}
    if "x" in input_data and "y" in input_data:
        return {"x": input_data["x"], "y": input_data["y"]}
    if "x" in output_data and "y" in output_data:
        return {"x": output_data["x"], "y": output_data["y"]}

    if "categories" in output_data and "values" in output_data:
        return {"categories": output_data["categories"], "values": output_data["values"]}
    if "categories" in input_data and "values" in input_data:
        return {"categories": input_data["categories"], "values": input_data["values"]}

    x_data = None
    y_data = None

    for key, value in input_data.items():
        if isinstance(value, list) and len(value) > 0:
            if x_data is None:
                x_data = value
            elif y_data is None:
                y_data = value
                break

    if x_data is not None and y_data is None:
        for key, value in output_data.items():
            if isinstance(value, list) and len(value) > 0:
                y_data = value
                break

    if x_data is not None and y_data is not None:
        if len(x_data) == len(y_data):
            return {"x": x_data, "y": y_data}

    categories = []
    values = []
    for key, value in {**input_data, **output_data}.items():
        if isinstance(value, (int, float)):
            categories.append(key)
            values.append(value)

    if categories and values:
        return {"categories": categories, "values": values}

    if x_data is not None:
        return {"x": list(range(len(x_data))), "y": x_data}

    return {"categories": ["input", "output"], "values": [0, 0]}


@app.command()
def run(
    ctx: typer.Context,
    function_id: str = typer.Argument(
        ...,
        help="Function ID (format: filepath::name#hash as printed by search; the #hash may be left out when only one function in the file has that name)",
    ),
    timeout: float = typer.Option(
        DEFAULT_TIMEOUT,
        "--timeout",
        "-t",
        help="Timeout in seconds for script execution",
    ),
    visualize: bool = typer.Option(
        False,
        "--visualize",
        "-V",
        help="Generate data visualization plot",
    ),
    plot_type: str = typer.Option(
        "auto",
        "--plot-type",
        help="Plot type: auto, line, scatter, bar",
    ),
) -> None:
    """Run verification script for a function's contract.

    Reads input/output data from stdin as JSON and verifies it against
    the function's contract.

    Examples:
        echo '{"input": {"x": 5}, "output": 10}' | drspec verify run "src/foo.py::double"

        drspec verify run "src/foo.py::process" --timeout 2.0 << 'EOF'
        {"input": {"items": [1, 2, 3]}, "output": [2, 4, 6]}
        EOF
    """
    db_path = ctx.obj.get("db_path") if ctx.obj else None
    json_output = ctx.obj.get("json_output", False) if ctx.obj else False
    resolved_db_path = db_path or (Path.cwd() / "_drspec" / "contracts.db")

    is_valid, error_msg = validate_function_id(function_id)
    if not is_valid:
        output(
            error_response(
                ErrorCode.INVALID_FUNCTION_ID,
                error_msg or "Invalid function ID format",
            ),
            json_output=json_output,
        )
        raise typer.Exit(1)

    if not resolved_db_path.parent.exists() or not resolved_db_path.exists():
        output(
            error_response(
                ErrorCode.DB_NOT_INITIALIZED,
                "DrSpec not initialized. Run 'drspec init' first.",
            ),
            json_output=json_output,
        )
        raise typer.Exit(1)

    try:
        conn = get_worker_connection(resolved_db_path)
    except WorkerIPCError as e:
        output(
            error_response(
                ErrorCode.DATABASE_BUSY,
                f"Could not reach the drspec worker: {e}",
            ),
            json_output=json_output,
        )
        raise typer.Exit(1)

    function_id = resolve_or_exit(conn, function_id, json_output)

    try:
        contract_data = get_contract(conn, function_id)
        if contract_data is None:
            output(
                error_response(
                    ErrorCode.CONTRACT_NOT_FOUND,
                    f"No contract exists for function '{function_id}'",
                ),
                json_output=json_output,
            )
            raise typer.Exit(1)

        try:
            contract = Contract.from_json(contract_data["contract_json"])
        except Exception as e:
            output(
                error_response(
                    ErrorCode.INVALID_JSON,
                    f"Failed to parse contract: {e}",
                ),
                json_output=json_output,
            )
            raise typer.Exit(1)

        try:
            stdin_data = sys.stdin.read()
            if not stdin_data.strip():
                output(
                    error_response(
                        ErrorCode.INVALID_JSON,
                        "No test data provided. Pass JSON via stdin with 'input' and 'output' keys.",
                    ),
                    json_output=json_output,
                )
                raise typer.Exit(1)

            test_data = json.loads(stdin_data)
        except json.JSONDecodeError as e:
            output(
                error_response(
                    ErrorCode.INVALID_JSON,
                    f"Failed to parse test data JSON: {e}",
                ),
                json_output=json_output,
            )
            raise typer.Exit(1)

        if not isinstance(test_data, dict) or "input" not in test_data:
            output(
                error_response(
                    ErrorCode.INVALID_JSON,
                    "Test data must have 'input' key",
                ),
                json_output=json_output,
            )
            raise typer.Exit(1)

        if "output" not in test_data:
            output(
                error_response(
                    ErrorCode.INVALID_JSON,
                    "Test data must have 'output' key",
                ),
                json_output=json_output,
            )
            raise typer.Exit(1)

        if not isinstance(test_data["input"], dict):
            output(error_response(ErrorCode.INVALID_JSON, "Test data 'input' must be an object"), json_output=json_output)
            raise typer.Exit(1)
        input_data = test_data["input"]
        output_data = test_data["output"]

        script = contract_data.get("verification_script")
        if not script or ("Generated by DrSpec" in script and "DRSPEC_VERIFIER_VERSION = 2" not in script):
            script = generate_verification_script(contract, function_id)

        result = execute_verification(
            script=script,
            input_data=input_data,
            output_data=output_data,
            timeout=timeout,
        )

        current_hash = get_artifact_code_hash(conn, function_id)
        freshness = contract_freshness(conn, function_id, contract_data)
        current = freshness["current"]
        evidence = dict(result.to_dict(), source_hash=current_hash,
                        context_hash=freshness["current_context_hash"],
                        contract_context_hash=freshness["contract_context_hash"],
                        contract_source_hash=contract_data.get("source_hash"),
                        contract_hash=hashlib.sha256(contract_data["contract_json"].encode()).hexdigest(),
                        script_hash=hashlib.sha256(script.encode()).hexdigest(),
                        sample_hash=hashlib.sha256(json.dumps(test_data, sort_keys=True).encode()).hexdigest(),
                        verified_at=datetime.now(timezone.utc).isoformat(), current=current)
        set_config(conn, "verification:" + function_id, json.dumps(evidence))
        # Confidence alone cannot establish executable verification.
        fully_verified = current and result.status == "passed" and result.passed
        if fully_verified:
            update_artifact_status(conn, function_id, "VERIFIED")
        elif current:
            update_artifact_status(conn, function_id, "NEEDS_REVIEW")

        if result.error:
            output(
                error_response(
                    ErrorCode.VERIFICATION_FAILED,
                    "Verification script execution failed",
                    details={
                        "error_type": result.error,
                        "message": result.message,
                    },
                ),
                json_output=json_output,
            )
            raise typer.Exit(1)

        response_data = {
            "function_id": function_id,
            "passed": fully_verified,
            "status": result.status,
            "current": current,
            "source_hash": current_hash,
            "contract_source_hash": contract_data.get("source_hash"),
            "context_hash": freshness["current_context_hash"],
            "contract_context_hash": freshness["contract_context_hash"],
            "results": result.results,
            "message": result.message,
            "execution_time_ms": int(result.execution_time * 1000),
            "invariants": {
                "checked": result.invariants_checked,
                "passed": result.invariants_passed,
                "failed": result.invariants_failed,
                "skipped": result.invariants_skipped,
                "error": result.invariants_error,
            },
        }

        if not result.passed:
            # heuristic: look for the invariant's name in the free-text failure message
            for invariant in contract.invariants:
                if invariant.name in result.message:
                    response_data["failed_invariant"] = {
                        "name": invariant.name,
                        "logic": invariant.logic,
                        "criticality": invariant.criticality.value,
                    }
                    break

        if visualize:
            try:
                from drspec.visualization.plotter import generate_plot

                plot_data = _transform_to_plot_data(input_data, output_data)

                plot_result = generate_plot(
                    data=plot_data,
                    plot_type=plot_type,  # type: ignore[arg-type]
                    title=f"Verification Data: {function_id}",
                )

                response_data["visualization"] = {
                    "generated": True,
                    "path": plot_result.path,
                    "plot_type": plot_result.plot_type,
                    "width": plot_result.width,
                    "height": plot_result.height,
                    "data_points": plot_result.data_points,
                }
            except Exception as e:
                response_data["visualization"] = {
                    "generated": False,
                    "error": str(e),
                }

        output(success_response(response_data), json_output=json_output)
    finally:
        conn.close()

    if not fully_verified:
        raise typer.Exit(1)


@app.command()
def script(
    ctx: typer.Context,
    function_id: str = typer.Argument(
        ...,
        help="Function ID (format: filepath::name#hash as printed by search; the #hash may be left out when only one function in the file has that name)",
    ),
) -> None:
    """Show the verification script for a function's contract.

    Outputs the generated Python verification script that can be used
    to verify the function's behavior.

    Examples:
        drspec verify script "src/foo.py::double"
    """
    db_path = ctx.obj.get("db_path") if ctx.obj else None
    json_output = ctx.obj.get("json_output", False) if ctx.obj else False
    resolved_db_path = db_path or (Path.cwd() / "_drspec" / "contracts.db")

    is_valid, error_msg = validate_function_id(function_id)
    if not is_valid:
        output(
            error_response(
                ErrorCode.INVALID_FUNCTION_ID,
                error_msg or "Invalid function ID format",
            ),
            json_output=json_output,
        )
        raise typer.Exit(1)

    if not resolved_db_path.parent.exists() or not resolved_db_path.exists():
        output(
            error_response(
                ErrorCode.DB_NOT_INITIALIZED,
                "DrSpec not initialized. Run 'drspec init' first.",
            ),
            json_output=json_output,
        )
        raise typer.Exit(1)

    try:
        conn = get_worker_connection(resolved_db_path, read_only=True)
    except WorkerIPCError as e:
        output(
            error_response(
                ErrorCode.DATABASE_BUSY,
                f"Could not reach the drspec worker: {e}",
            ),
            json_output=json_output,
        )
        raise typer.Exit(1)

    function_id = resolve_or_exit(conn, function_id, json_output)

    try:
        contract_data = get_contract(conn, function_id)
        if contract_data is None:
            output(
                error_response(
                    ErrorCode.CONTRACT_NOT_FOUND,
                    f"No contract exists for function '{function_id}'",
                ),
                json_output=json_output,
            )
            raise typer.Exit(1)

        try:
            contract = Contract.from_json(contract_data["contract_json"])
        except Exception as e:
            output(
                error_response(
                    ErrorCode.INVALID_JSON,
                    f"Failed to parse contract: {e}",
                ),
                json_output=json_output,
            )
            raise typer.Exit(1)

        script_content = contract_data.get("verification_script")
        if not script_content or ("Generated by DrSpec" in script_content and "DRSPEC_VERIFIER_VERSION = 2" not in script_content):
            script_content = generate_verification_script(contract, function_id)
    finally:
        conn.close()

    output(
        success_response({
            "function_id": function_id,
            "script": script_content,
        }),
        json_output=json_output,
    )
