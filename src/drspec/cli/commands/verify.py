"""Verify command - Run verification scripts."""

from __future__ import annotations

import json
import sys
from typing import Any

import typer

from drspec.cli.output import ErrorCode, error_response, output, success_response
from drspec.cli.validators import validate_function_id
from drspec.contracts import (
    Contract,
    DEFAULT_TIMEOUT,
    execute_verification,
    generate_verification_script,
)
from drspec.db import get_connection, get_contract

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
    # If input has x and output has y (or vice versa), use as coordinates
    if "x" in input_data and "y" in output_data:
        return {"x": input_data["x"], "y": output_data["y"]}
    if "x" in input_data and "y" in input_data:
        return {"x": input_data["x"], "y": input_data["y"]}
    if "x" in output_data and "y" in output_data:
        return {"x": output_data["x"], "y": output_data["y"]}

    # If output has categories and values, use as bar chart data
    if "categories" in output_data and "values" in output_data:
        return {"categories": output_data["categories"], "values": output_data["values"]}
    if "categories" in input_data and "values" in input_data:
        return {"categories": input_data["categories"], "values": input_data["values"]}

    # Try to find any list-like data in input/output
    x_data = None
    y_data = None

    # Look for list values in input
    for key, value in input_data.items():
        if isinstance(value, list) and len(value) > 0:
            if x_data is None:
                x_data = value
            elif y_data is None:
                y_data = value
                break

    # If we only found one list in input, look in output
    if x_data is not None and y_data is None:
        for key, value in output_data.items():
            if isinstance(value, list) and len(value) > 0:
                y_data = value
                break

    # If we found lists, use them
    if x_data is not None and y_data is not None:
        if len(x_data) == len(y_data):
            return {"x": x_data, "y": y_data}

    # Try to extract single values as bar chart
    categories = []
    values = []
    for key, value in {**input_data, **output_data}.items():
        if isinstance(value, (int, float)):
            categories.append(key)
            values.append(value)

    if categories and values:
        return {"categories": categories, "values": values}

    # Fallback: create simple indexed data from whatever we have
    if x_data is not None:
        return {"x": list(range(len(x_data))), "y": x_data}

    # Last resort: empty data
    return {"categories": ["input", "output"], "values": [0, 0]}


@app.command()
def run(
    ctx: typer.Context,
    function_id: str = typer.Argument(
        ...,
        help="Function ID (format: filepath::function_name)",
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
    pretty = ctx.obj.get("pretty", False) if ctx.obj else False

    # Validate function ID format
    is_valid, error_msg = validate_function_id(function_id)
    if not is_valid:
        output(
            error_response(
                ErrorCode.INVALID_FUNCTION_ID,
                error_msg or "Invalid function ID format",
            ),
            pretty=pretty,
        )
        raise typer.Exit(1)

    try:
        conn = get_connection(db_path)
    except Exception as e:
        output(
            error_response(
                ErrorCode.DB_NOT_INITIALIZED,
                f"Failed to connect to database: {e}",
            ),
            pretty=pretty,
        )
        raise typer.Exit(1)

    # Get the contract
    contract_data = get_contract(conn, function_id)
    if contract_data is None:
        output(
            error_response(
                ErrorCode.CONTRACT_NOT_FOUND,
                f"No contract exists for function '{function_id}'",
            ),
            pretty=pretty,
        )
        raise typer.Exit(1)

    # Parse the contract
    try:
        contract = Contract.from_json(contract_data["contract_json"])
    except Exception as e:
        output(
            error_response(
                ErrorCode.INVALID_JSON,
                f"Failed to parse contract: {e}",
            ),
            pretty=pretty,
        )
        raise typer.Exit(1)

    # Read test data from stdin
    try:
        stdin_data = sys.stdin.read()
        if not stdin_data.strip():
            output(
                error_response(
                    ErrorCode.INVALID_JSON,
                    "No test data provided. Pass JSON via stdin with 'input' and 'output' keys.",
                ),
                pretty=pretty,
            )
            raise typer.Exit(1)

        test_data = json.loads(stdin_data)
    except json.JSONDecodeError as e:
        output(
            error_response(
                ErrorCode.INVALID_JSON,
                f"Failed to parse test data JSON: {e}",
            ),
            pretty=pretty,
        )
        raise typer.Exit(1)

    # Validate test data structure
    if "input" not in test_data:
        output(
            error_response(
                ErrorCode.INVALID_JSON,
                "Test data must have 'input' key",
            ),
            pretty=pretty,
        )
        raise typer.Exit(1)

    if "output" not in test_data:
        output(
            error_response(
                ErrorCode.INVALID_JSON,
                "Test data must have 'output' key",
            ),
            pretty=pretty,
        )
        raise typer.Exit(1)

    input_data = test_data["input"]
    output_data = test_data["output"]

    # Get or generate verification script
    script = contract_data.get("verification_script")
    if not script:
        # Generate the script if not stored
        script = generate_verification_script(contract, function_id)

    # Execute verification
    result = execute_verification(
        script=script,
        input_data=input_data,
        output_data=output_data,
        timeout=timeout,
    )

    # Handle execution errors
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
            pretty=pretty,
        )
        raise typer.Exit(1)

    # Return result
    response_data = {
        "function_id": function_id,
        "passed": result.passed,
        "message": result.message,
        "execution_time_ms": int(result.execution_time * 1000),
        "invariants": {
            "checked": result.invariants_checked,
            "passed": result.invariants_passed,
        },
    }

    # Add failed invariant details if verification failed
    if not result.passed:
        # Try to extract failed invariant name from message
        for invariant in contract.invariants:
            if invariant.name in result.message:
                response_data["failed_invariant"] = {
                    "name": invariant.name,
                    "logic": invariant.logic,
                    "criticality": invariant.criticality.value,
                }
                break

    # Generate visualization if requested
    if visualize:
        try:
            from drspec.visualization.plotter import generate_plot

            # Transform test data to plottable format
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
            # Include error info if visualization fails
            response_data["visualization"] = {
                "generated": False,
                "error": str(e),
            }

    output(success_response(response_data), pretty=pretty)

    # Exit with non-zero if verification failed
    if not result.passed:
        raise typer.Exit(1)


@app.command()
def script(
    ctx: typer.Context,
    function_id: str = typer.Argument(
        ...,
        help="Function ID (format: filepath::function_name)",
    ),
) -> None:
    """Show the verification script for a function's contract.

    Outputs the generated Python verification script that can be used
    to verify the function's behavior.

    Examples:
        drspec verify script "src/foo.py::double"
    """
    db_path = ctx.obj.get("db_path") if ctx.obj else None
    pretty = ctx.obj.get("pretty", False) if ctx.obj else False

    # Validate function ID format
    is_valid, error_msg = validate_function_id(function_id)
    if not is_valid:
        output(
            error_response(
                ErrorCode.INVALID_FUNCTION_ID,
                error_msg or "Invalid function ID format",
            ),
            pretty=pretty,
        )
        raise typer.Exit(1)

    try:
        conn = get_connection(db_path)
    except Exception as e:
        output(
            error_response(
                ErrorCode.DB_NOT_INITIALIZED,
                f"Failed to connect to database: {e}",
            ),
            pretty=pretty,
        )
        raise typer.Exit(1)

    # Get the contract
    contract_data = get_contract(conn, function_id)
    if contract_data is None:
        output(
            error_response(
                ErrorCode.CONTRACT_NOT_FOUND,
                f"No contract exists for function '{function_id}'",
            ),
            pretty=pretty,
        )
        raise typer.Exit(1)

    # Parse the contract
    try:
        contract = Contract.from_json(contract_data["contract_json"])
    except Exception as e:
        output(
            error_response(
                ErrorCode.INVALID_JSON,
                f"Failed to parse contract: {e}",
            ),
            pretty=pretty,
        )
        raise typer.Exit(1)

    # Get or generate verification script
    script_content = contract_data.get("verification_script")
    if not script_content:
        script_content = generate_verification_script(contract, function_id)

    output(
        success_response({
            "function_id": function_id,
            "script": script_content,
        }),
        pretty=pretty,
    )
