"""Deps command - Get function dependencies."""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any, Optional

import typer

from drspec.cli.output import ErrorCode, error_response, output, success_response
from drspec.db import (
    get_artifact,
    get_callees,
    get_callers,
    get_connection,
    list_artifacts,
)

app = typer.Typer(
    name="deps",
    help="Get function dependency information",
)


def _get_dependency_info(
    conn: Any,
    function_id: str,
    depth: int = 1,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Get callees and callers with contract status.

    Args:
        conn: DuckDB connection.
        function_id: Function ID to get dependencies for.
        depth: Maximum depth of dependency traversal.

    Returns:
        Tuple of (callees, callers) lists with dependency info.
    """
    callees = []
    callers = []

    # BFS for callees
    visited_callees = {function_id}
    queue: deque[tuple[str, int]] = deque()

    # Start with direct callees
    for callee_id in get_callees(conn, function_id):
        if callee_id not in visited_callees:
            visited_callees.add(callee_id)
            queue.append((callee_id, 1))

    while queue:
        current_id, current_depth = queue.popleft()

        # Get artifact info
        artifact = get_artifact(conn, current_id)

        # Check contract status
        contract_result = conn.execute(
            "SELECT function_id FROM contracts WHERE function_id = ?",
            [current_id],
        ).fetchone()
        has_contract = contract_result is not None

        callee_info = {
            "function_id": current_id,
            "function_name": artifact.function_name if artifact else current_id.split("::")[-1],
            "file_path": artifact.file_path if artifact else "",
            "depth": current_depth,
            "has_contract": has_contract,
            "status": artifact.status if artifact else "UNKNOWN",
        }
        callees.append(callee_info)

        # Continue BFS if not at max depth
        if current_depth < depth:
            for next_callee in get_callees(conn, current_id):
                if next_callee not in visited_callees:
                    visited_callees.add(next_callee)
                    queue.append((next_callee, current_depth + 1))

    # BFS for callers
    visited_callers = {function_id}
    queue = deque()

    # Start with direct callers
    for caller_id in get_callers(conn, function_id):
        if caller_id not in visited_callers:
            visited_callers.add(caller_id)
            queue.append((caller_id, 1))

    while queue:
        current_id, current_depth = queue.popleft()

        # Get artifact info
        artifact = get_artifact(conn, current_id)

        # Check contract status
        contract_result = conn.execute(
            "SELECT function_id FROM contracts WHERE function_id = ?",
            [current_id],
        ).fetchone()
        has_contract = contract_result is not None

        caller_info = {
            "function_id": current_id,
            "function_name": artifact.function_name if artifact else current_id.split("::")[-1],
            "file_path": artifact.file_path if artifact else "",
            "depth": current_depth,
            "has_contract": has_contract,
            "status": artifact.status if artifact else "UNKNOWN",
        }
        callers.append(caller_info)

        # Continue BFS if not at max depth
        if current_depth < depth:
            for next_caller in get_callers(conn, current_id):
                if next_caller not in visited_callers:
                    visited_callers.add(next_caller)
                    queue.append((next_caller, current_depth + 1))

    # Sort by depth, then by function_id
    callees.sort(key=lambda x: (x["depth"], x["function_id"]))
    callers.sort(key=lambda x: (x["depth"], x["function_id"]))

    return callees, callers


def _find_similar_functions(conn: Any, function_id: str, limit: int = 5) -> list[str]:
    """Find functions with similar names.

    Args:
        conn: DuckDB connection.
        function_id: Function ID that wasn't found.
        limit: Maximum number of suggestions.

    Returns:
        List of similar function IDs.
    """
    # Extract function name from ID
    if "::" in function_id:
        func_name = function_id.split("::")[-1]
    else:
        func_name = function_id

    # Get all artifacts and find similar names
    artifacts = list_artifacts(conn, limit=1000)

    # Simple substring matching
    similar = []
    for artifact in artifacts:
        if func_name.lower() in artifact.function_name.lower():
            similar.append(artifact.function_id)
        elif artifact.function_name.lower() in func_name.lower():
            similar.append(artifact.function_id)

    return similar[:limit]


@app.command()
def get(
    ctx: typer.Context,
    function_id: str = typer.Argument(
        ...,
        help="Function ID (format: filepath::function_name)",
    ),
    depth: int = typer.Option(
        1,
        "--depth",
        "-d",
        help="Depth of dependency tree to return (default: 1)",
        min=1,
        max=5,
    ),
    direction: str = typer.Option(
        "both",
        "--direction",
        help="Direction: callers, callees, or both",
    ),
) -> None:
    """Get dependency information for a function.

    Returns functions that call this function (callers) and
    functions that this function calls (callees).

    Examples:
        drspec deps get "src/payments/reconcile.py::reconcile"
        drspec deps get "src/payments/reconcile.py::reconcile" --depth 2
        drspec deps get "src/payments/reconcile.py::reconcile" --direction callees
    """
    # Get CLI context
    cli_ctx = ctx.obj or {}
    json_output = cli_ctx.get("json_output", True)
    pretty = cli_ctx.get("pretty", False)
    db_path_override = cli_ctx.get("db_path")

    # Check if database exists
    drspec_dir = Path.cwd() / "_drspec"
    if db_path_override:
        db_path = db_path_override
    else:
        db_path = drspec_dir / "contracts.db"

    if not db_path.parent.exists():
        response = error_response(
            ErrorCode.DB_NOT_INITIALIZED,
            "DrSpec not initialized. Run 'drspec init' first.",
            {"expected_path": str(drspec_dir)},
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)

    try:
        conn = get_connection(db_path)
    except FileNotFoundError:
        response = error_response(
            ErrorCode.DB_NOT_INITIALIZED,
            "DrSpec not initialized. Run 'drspec init' first.",
            {"db_path": str(db_path)},
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)

    try:
        # Check if function exists
        artifact = get_artifact(conn, function_id)

        if artifact is None:
            # Find similar functions for suggestion
            similar = _find_similar_functions(conn, function_id)

            response = error_response(
                ErrorCode.FUNCTION_NOT_FOUND,
                f"Function not found: {function_id}",
                {
                    "function_id": function_id,
                    "suggestions": similar if similar else [],
                },
            )
            output(response, json_output=json_output, pretty=pretty)
            raise typer.Exit(1)

        # Check if function has a contract
        contract_result = conn.execute(
            "SELECT function_id FROM contracts WHERE function_id = ?",
            [function_id],
        ).fetchone()
        has_contract = contract_result is not None

        # Get dependencies
        callees, callers = _get_dependency_info(conn, function_id, depth)

        # Filter by direction
        if direction == "callees":
            callers = []
        elif direction == "callers":
            callees = []

        # Calculate summary
        callees_with_contracts = sum(1 for c in callees if c["has_contract"])
        callers_with_contracts = sum(1 for c in callers if c["has_contract"])

        # Build response
        data = {
            "function_id": function_id,
            "file_path": artifact.file_path,
            "function_name": artifact.function_name,
            "status": artifact.status,
            "has_contract": has_contract,
            "callees": callees,
            "callers": callers,
            "summary": {
                "total_callees": len(callees),
                "total_callers": len(callers),
                "callees_with_contracts": callees_with_contracts,
                "callers_with_contracts": callers_with_contracts,
                "depth": depth,
            },
        }

        response = success_response(data)
        output(response, json_output=json_output, pretty=pretty)

    except typer.Exit:
        raise
    except Exception as e:
        response = error_response(
            ErrorCode.INTERNAL_ERROR,
            f"Error getting dependencies: {str(e)}",
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)
    finally:
        conn.close()


@app.command()
def plot(
    ctx: typer.Context,
    function_id: str = typer.Argument(
        ...,
        help="Function ID to plot dependencies for",
    ),
    depth: int = typer.Option(
        2,
        "--depth",
        "-d",
        help="Dependency depth (default: 2)",
        min=1,
        max=5,
    ),
    direction: str = typer.Option(
        "both",
        "--direction",
        help="Direction: callers, callees, or both",
    ),
    output_path: Optional[str] = typer.Option(
        None,
        "--output",
        "-o",
        help="Output file path (default: _drspec/plots/{hash}_deps.png)",
    ),
) -> None:
    """Generate a dependency graph visualization.

    Creates a PNG image showing function dependencies with nodes colored
    by contract status:
    - Green: VERIFIED
    - Yellow: NEEDS_REVIEW
    - Gray: PENDING
    - Orange: STALE
    - Red: BROKEN

    Examples:
        drspec deps plot "src/payments/reconcile.py::reconcile"
        drspec deps plot "src/foo.py::process" --depth 3 --direction callers
        drspec deps plot "src/foo.py::process" --output ./my-graph.png
    """
    # Get CLI context
    cli_ctx = ctx.obj or {}
    json_output = cli_ctx.get("json_output", True)
    pretty = cli_ctx.get("pretty", False)
    db_path_override = cli_ctx.get("db_path")

    # Check if database exists
    drspec_dir = Path.cwd() / "_drspec"
    if db_path_override:
        db_path = db_path_override
    else:
        db_path = drspec_dir / "contracts.db"

    if not db_path.parent.exists():
        response = error_response(
            ErrorCode.DB_NOT_INITIALIZED,
            "DrSpec not initialized. Run 'drspec init' first.",
            {"expected_path": str(drspec_dir)},
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)

    try:
        conn = get_connection(db_path)
    except FileNotFoundError:
        response = error_response(
            ErrorCode.DB_NOT_INITIALIZED,
            "DrSpec not initialized. Run 'drspec init' first.",
            {"db_path": str(db_path)},
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)

    try:
        # Check if function exists
        artifact = get_artifact(conn, function_id)

        if artifact is None:
            # Find similar functions for suggestion
            similar = _find_similar_functions(conn, function_id)

            response = error_response(
                ErrorCode.FUNCTION_NOT_FOUND,
                f"Function not found: {function_id}",
                {
                    "function_id": function_id,
                    "suggestions": similar if similar else [],
                },
            )
            output(response, json_output=json_output, pretty=pretty)
            raise typer.Exit(1)

        # Generate the graph
        from drspec.visualization.grapher import generate_dependency_graph

        # Determine output directory
        output_dir = str(Path(output_path).parent) if output_path else "_drspec/plots"

        result = generate_dependency_graph(
            conn=conn,
            function_id=function_id,
            depth=depth,
            direction=direction,  # type: ignore
            output_dir=output_dir,
        )

        # Build response
        data = {
            "function_id": function_id,
            "output_path": result.path,
            "nodes": result.nodes,
            "edges": result.edges,
            "depth": depth,
            "center_function": artifact.function_name,
            "width": result.width,
            "height": result.height,
        }

        response = success_response(data)
        output(response, json_output=json_output, pretty=pretty)

    except typer.Exit:
        raise
    except Exception as e:
        response = error_response(
            ErrorCode.INTERNAL_ERROR,
            f"Error generating graph: {str(e)}",
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)
    finally:
        conn.close()
