"""Deps command - Get function dependencies."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from drspec.cli.output import ErrorCode, error_response, output, success_response
from drspec.cli.validators import resolve_or_exit
from drspec.core import docs as documents
from drspec.core.docs import documented_in
from drspec.core.changed_notice import add_changed_notice, take_before
from drspec.core.index_notice import add_index_notice
from drspec.core.dependency_info import find_similar_functions, get_dependency_info
from drspec.core import usage
from drspec.core.staleness import stale_paths_checked
from drspec.core.partial import add_callers_caveat, not_found_with_reason
from drspec.core.unresolved import add_unresolved_callers
from drspec.db import (
    count_unused_artifacts,
    get_artifact,
    list_unused_artifacts,
    only_test_caller_function_ids,
)
from drspec.worker_ipc import WorkerIPCError, get_worker_connection

app = typer.Typer(
    name="deps",
    help="Get function dependency information",
)


@app.command()
def get(
    ctx: typer.Context,
    function_id: str = typer.Argument(
        ...,
        help="Function ID (format: filepath::name#hash as printed by search; the #hash may be left out when only one function in the file has that name)",
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
    cli_ctx = ctx.obj or {}
    json_output = cli_ctx.get("json_output", False)
    db_path_override = cli_ctx.get("db_path")

    drspec_dir = Path.cwd() / "_drspec"
    if db_path_override:
        db_path = db_path_override
    else:
        db_path = drspec_dir / "contracts.db"

    if not db_path.parent.exists() or not db_path.exists():
        response = error_response(
            ErrorCode.DB_NOT_INITIALIZED,
            "DrSpec not initialized. Run 'drspec init' first.",
            {"expected_path": str(drspec_dir)},
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)

    try:
        conn = get_worker_connection(db_path, read_only=True)
    except WorkerIPCError as e:
        response = error_response(
            ErrorCode.DATABASE_BUSY,
            f"Could not reach the drspec worker: {e}",
            {"db_path": str(db_path)},
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)

    target = documents.document_target(conn, function_id)
    if target is not None:  # a document section or a document: the functions it describes
        try:
            data = documents.section_dependencies(conn, *target)
        finally:
            conn.close()
        output(success_response(data), json_output=json_output)
        return

    function_id = resolve_or_exit(conn, function_id, json_output)
    before = take_before(conn)  # taken before the answer's reads (see changed_notice)

    try:
        artifact = get_artifact(conn, function_id)

        if artifact is None:
            similar = find_similar_functions(conn, function_id)

            message, details = not_found_with_reason(
                conn, f"Function not found: {function_id}", function_id,
                {"function_id": function_id, "suggestions": similar if similar else []},
            )
            response = error_response(ErrorCode.FUNCTION_NOT_FOUND, message, details)
            output(response, json_output=json_output)
            raise typer.Exit(1)

        contract_result = conn.execute(
            "SELECT function_id FROM contracts WHERE function_id = ?",
            [function_id],
        ).fetchone()
        has_contract = contract_result is not None

        callees, callers = get_dependency_info(conn, function_id, depth)

        if direction == "callees":
            callers = []
        elif direction == "callers":
            callees = []
        usage.record_usage(conn, usage.dependency_items(function_id, callees, callers), "usage.lookup_weight")

        callees_with_contracts = sum(1 for c in callees if c["has_contract"])
        callers_with_contracts = sum(1 for c in callers if c["has_contract"])

        data = {
            "function_id": function_id,
            "file_path": artifact.file_path,
            "function_name": artifact.function_name,
            "status": artifact.status,
            "has_contract": has_contract,
            "possibly_stale": artifact.file_path in stale_paths_checked(conn, [artifact.file_path]),
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

        data = add_unresolved_callers(conn, data, artifact, direction)
        data = add_callers_caveat(conn, data, direction)

        documented = documented_in(conn, artifact.function_id)
        if documented:
            data["documented_in"] = documented
        add_index_notice(conn, data)
        add_changed_notice(conn, data, db_path.parent.parent, [artifact.file_path, *(e["file_path"] for e in callees + callers)], before=before)
        response = success_response(data)
        output(response, json_output=json_output)

    except typer.Exit:
        raise
    except Exception as e:
        response = error_response(
            ErrorCode.INTERNAL_ERROR,
            f"Error getting dependencies: {str(e)}",
        )
        output(response, json_output=json_output)
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
        help="Output file path (default: _drspec/plots/{hash}_deps.png, or .html with --html)",
    ),
    html_output: bool = typer.Option(
        False,
        "--html",
        help=(
            "Generate an interactive HTML page instead of a static PNG (design.md §16) — "
            "click a node/edge for full detail, filter by name/file, pan/zoom. Open it with "
            "VS Code's built-in local-file preview (right-click the file -> Open Preview), "
            "no server or install needed. Fully self-contained: no network call, no CDN."
        ),
    ),
) -> None:
    """Generate a dependency graph visualization.

    Creates a PNG image (or, with --html, an interactive HTML page)
    showing function dependencies with nodes colored by contract status:
    - Green: VERIFIED
    - Yellow: NEEDS_REVIEW
    - Gray: PENDING
    - Orange: STALE
    - Red: BROKEN

    Examples:
        drspec deps plot "src/payments/reconcile.py::reconcile"
        drspec deps plot "src/foo.py::process" --depth 3 --direction callers
        drspec deps plot "src/foo.py::process" --output ./my-graph.png
        drspec deps plot "src/foo.py::process" --html
    """
    cli_ctx = ctx.obj or {}
    json_output = cli_ctx.get("json_output", False)
    db_path_override = cli_ctx.get("db_path")

    drspec_dir = Path.cwd() / "_drspec"
    if db_path_override:
        db_path = db_path_override
    else:
        db_path = drspec_dir / "contracts.db"

    if not db_path.parent.exists() or not db_path.exists():
        response = error_response(
            ErrorCode.DB_NOT_INITIALIZED,
            "DrSpec not initialized. Run 'drspec init' first.",
            {"expected_path": str(drspec_dir)},
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)

    try:
        conn = get_worker_connection(db_path, read_only=True)
    except WorkerIPCError as e:
        response = error_response(
            ErrorCode.DATABASE_BUSY,
            f"Could not reach the drspec worker: {e}",
            {"db_path": str(db_path)},
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)

    function_id = resolve_or_exit(conn, function_id, json_output)

    try:
        artifact = get_artifact(conn, function_id)

        if artifact is None:
            similar = find_similar_functions(conn, function_id)

            response = error_response(
                ErrorCode.FUNCTION_NOT_FOUND,
                f"Function not found: {function_id}",
                {
                    "function_id": function_id,
                    "suggestions": similar if similar else [],
                },
            )
            output(response, json_output=json_output)
            raise typer.Exit(1)

        from drspec.visualization.grapher import generate_dependency_graph, generate_dependency_graph_html

        generate_fn = generate_dependency_graph_html if html_output else generate_dependency_graph
        result = generate_fn(
            conn=conn,
            function_id=function_id,
            depth=depth,
            direction=direction,  # type: ignore
            output_file=output_path,
        )

        data = {
            "function_id": function_id,
            "output_path": result.path,
            "html": html_output,
            "nodes": result.nodes,
            "edges": result.edges,
            "depth": depth,
            "center_function": artifact.function_name,
            "width": result.width,
            "height": result.height,
        }

        response = success_response(data)
        output(response, json_output=json_output)

    except typer.Exit:
        raise
    except Exception as e:
        response = error_response(
            ErrorCode.INTERNAL_ERROR,
            f"Error generating graph: {str(e)}",
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)
    finally:
        conn.close()


@app.command()
def unused(
    ctx: typer.Context,
    path: Optional[str] = typer.Option(None, "--path", help="Filter by file path prefix — e.g. exclude a known entry-point directory."),
    limit: int = typer.Option(100, "--limit", help="Maximum results to return.", min=1),
) -> None:
    """List functions with no real-code caller/reference anywhere —
    a dead-code *candidate* list, not proof. `only_test_callers` tells
    apart a function with zero callers from one only called by tests.

    Only dunder/magic methods are excluded automatically; CLI entry
    points and reflection/framework-invoked functions still show up
    here, so use --path to exclude a directory known to be full of those.

    Examples:
        drspec deps unused
        drspec deps unused --path src/drspec/core
        drspec deps unused --limit 20
    """
    cli_ctx = ctx.obj or {}
    json_output = cli_ctx.get("json_output", False)
    db_path_override = cli_ctx.get("db_path")

    drspec_dir = Path.cwd() / "_drspec"
    if db_path_override:
        db_path = db_path_override
    else:
        db_path = drspec_dir / "contracts.db"

    if not db_path.parent.exists() or not db_path.exists():
        response = error_response(
            ErrorCode.DB_NOT_INITIALIZED,
            "DrSpec not initialized. Run 'drspec init' first.",
            {"expected_path": str(drspec_dir)},
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)

    try:
        conn = get_worker_connection(db_path, read_only=True)
    except WorkerIPCError as e:
        response = error_response(
            ErrorCode.DATABASE_BUSY,
            f"Could not reach the drspec worker: {e}",
            {"db_path": str(db_path)},
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)

    try:
        results = list_unused_artifacts(conn, path_prefix=path, limit=limit)
        total_count = count_unused_artifacts(conn, path_prefix=path)
        stale_paths = stale_paths_checked(conn, [a.file_path for a in results])
        only_test_called = only_test_caller_function_ids(conn, [a.function_id for a in results])

        data = {
            "total_count": total_count,
            "returned": len(results),
            "limit": limit,
            "path_filter": path,
            "caveat": (
                "Includes both functions with no caller/reference at all, and ones whose "
                "*only* caller/reference is itself a test (design.md §23 — see "
                "'only_test_callers' per function) — a dead-code candidate list, not proof "
                "either way. CLI entry points and anything reflection/framework-invoked are "
                "not excluded automatically (only dunder/magic methods are); use --path to "
                "exclude a known entry-point directory."
            ),
            "functions": [
                {
                    "function_id": a.function_id,
                    "function_name": a.function_name,
                    "file_path": a.file_path,
                    "language": a.language,
                    "status": a.status,
                    "possibly_stale": a.file_path in stale_paths,
                    "only_test_callers": a.function_id in only_test_called,
                }
                for a in results
            ],
        }

        response = success_response(data)
        output(response, json_output=json_output)

    except typer.Exit:
        raise
    except Exception as e:
        response = error_response(
            ErrorCode.INTERNAL_ERROR,
            f"Error listing unused functions: {str(e)}",
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)
    finally:
        conn.close()
