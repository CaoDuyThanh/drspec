"""Contract command - Manage contracts."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer

from drspec.cli.output import ErrorCode, error_response, output, success_response
from drspec.cli.validators import validate_function_id
from drspec.contracts import validate_contract
from drspec.db import (
    get_artifact,
    get_connection,
    get_contract,
    insert_contract,
    insert_reasoning_trace,
    list_artifacts,
    update_artifact_status,
)
from drspec.db.queries import (
    calculate_confidence_with_findings,
    get_vision_findings,
)

app = typer.Typer(
    name="contract",
    help="Manage function contracts",
)


def find_similar_functions(
    conn,
    function_id: str,
    limit: int = 5,
) -> list[str]:
    """Find similar function IDs for suggestions.

    Args:
        conn: Database connection.
        function_id: The function ID that wasn't found.
        limit: Maximum number of suggestions.

    Returns:
        List of similar function IDs.
    """
    suggestions = []

    # Extract function name from the ID
    if "::" in function_id:
        _, func_name = function_id.split("::", 1)
    else:
        func_name = function_id

    # Search for functions with matching name
    artifacts = list_artifacts(conn, limit=500)
    for artifact in artifacts:
        if func_name.lower() in artifact.function_name.lower():
            suggestions.append(artifact.function_id)
            if len(suggestions) >= limit:
                break

    # If we didn't find enough, also look for partial path matches
    if len(suggestions) < limit and "::" in function_id:
        filepath, _ = function_id.split("::", 1)
        for artifact in artifacts:
            if artifact.function_id not in suggestions:
                if filepath.lower() in artifact.file_path.lower():
                    suggestions.append(artifact.function_id)
                    if len(suggestions) >= limit:
                        break

    return suggestions


@app.command()
def get(
    ctx: typer.Context,
    function_id: str = typer.Argument(
        ...,
        help="Function ID (format: filepath::function_name)",
    ),
) -> None:
    """Get the contract for a specific function.

    Returns the contract JSON including invariants, intent, and confidence.

    Examples:
        drspec contract get src/payments/reconcile.py::reconcile_transactions
        drspec contract get --pretty src/utils.py::helper
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

    # Validate function ID format
    is_valid, error_msg = validate_function_id(function_id)
    if not is_valid:
        response = error_response(
            ErrorCode.INVALID_FUNCTION_ID,
            error_msg,
            {"function_id": function_id, "expected_format": "filepath::function_name"},
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
        # Get the contract
        contract_row = get_contract(conn, function_id)

        if contract_row is None:
            # Contract not found - try fuzzy matching for suggestions
            suggestions = find_similar_functions(conn, function_id)
            response = error_response(
                ErrorCode.CONTRACT_NOT_FOUND,
                f"No contract exists for function '{function_id}'",
                {
                    "function_id": function_id,
                    "suggestions": suggestions,
                },
            )
            output(response, json_output=json_output, pretty=pretty)
            raise typer.Exit(1)

        # Get artifact for status info
        artifact = get_artifact(conn, function_id)
        status = artifact.status if artifact else "UNKNOWN"

        # Parse contract JSON
        contract_data = json.loads(contract_row["contract_json"])

        # Get confidence score (stored as 0.0-1.0, display as 0-100)
        raw_confidence = contract_row["confidence_score"]
        # Handle legacy data that might have been stored as 0-100 instead of 0.0-1.0
        if raw_confidence > 1.0:
            base_confidence = int(raw_confidence)  # Already in 0-100 scale
        else:
            base_confidence = int(raw_confidence * 100)  # Convert 0.0-1.0 to 0-100

        # Get vision findings for this function and calculate adjusted confidence
        findings = get_vision_findings(conn, function_id)
        adjusted_confidence = calculate_confidence_with_findings(base_confidence, findings)

        # Count only NEW (unresolved) findings
        active_findings = sum(1 for f in findings if f.status == "NEW")
        vision_penalty = base_confidence - adjusted_confidence

        # Build response
        data = {
            "function_id": function_id,
            "contract": contract_data,
            "confidence": {
                "base": base_confidence,
                "adjusted": adjusted_confidence,
                "vision_penalty": vision_penalty,
                "active_findings": active_findings,
            },
            "status": status,
            "created_at": contract_row["created_at"].isoformat() if contract_row["created_at"] else None,
            "updated_at": contract_row["updated_at"].isoformat() if contract_row["updated_at"] else None,
        }

        response = success_response(data)
        output(response, json_output=json_output, pretty=pretty)

    except typer.Exit:
        # Re-raise typer.Exit without catching it
        raise
    except json.JSONDecodeError as e:
        response = error_response(
            ErrorCode.INTERNAL_ERROR,
            f"Error parsing stored contract JSON: {str(e)}",
            {"function_id": function_id},
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)
    except Exception as e:
        response = error_response(
            ErrorCode.INTERNAL_ERROR,
            f"Error getting contract: {str(e)}",
            {"function_id": function_id},
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)
    finally:
        conn.close()


@app.command()
def save(
    ctx: typer.Context,
    function_id: str = typer.Argument(
        ...,
        help="Function ID (format: filepath::function_name)",
    ),
    confidence: int = typer.Option(
        ...,
        "--confidence",
        "-c",
        help="Confidence score (0-100)",
        min=0,
        max=100,
    ),
    agent: str = typer.Option(
        "judge",
        "--agent",
        "-a",
        help="Agent name for reasoning trace (default: judge)",
    ),
    trace: Optional[str] = typer.Option(
        None,
        "--trace",
        "-t",
        help="Reasoning trace JSON (optional)",
    ),
) -> None:
    """Save a contract for a function.

    Reads contract JSON from stdin and validates against Pydantic schema
    before saving. Updates artifact status based on confidence score.

    Examples:
        echo '{"function_signature": "...", ...}' | drspec contract save src/utils.py::parse --confidence 78
        drspec contract save src/utils.py::parse --confidence 85 << 'EOF'
        {
            "function_signature": "def parse(text: str) -> dict",
            "intent_summary": "Parses text into structured dictionary",
            "invariants": [...]
        }
        EOF
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

    # Validate function ID format
    is_valid, error_msg = validate_function_id(function_id)
    if not is_valid:
        response = error_response(
            ErrorCode.INVALID_FUNCTION_ID,
            error_msg,
            {"function_id": function_id, "expected_format": "filepath::function_name"},
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)

    # Read contract JSON from stdin
    if sys.stdin.isatty():
        response = error_response(
            ErrorCode.INVALID_INPUT,
            "No contract JSON provided. Pipe contract JSON to stdin.",
            {"example": "echo '{...}' | drspec contract save <function_id> --confidence 80"},
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)

    contract_json_str = sys.stdin.read().strip()
    if not contract_json_str:
        response = error_response(
            ErrorCode.INVALID_INPUT,
            "Empty contract JSON provided.",
            {"function_id": function_id},
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)

    # Validate contract against schema
    validation_result = validate_contract(contract_json_str)
    if not validation_result.success:
        response = error_response(
            ErrorCode.INVALID_SCHEMA,
            validation_result.error.message if validation_result.error else "Contract validation failed",
            validation_result.error.details if validation_result.error else {},
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
        # Check if artifact exists
        artifact = get_artifact(conn, function_id)
        if artifact is None:
            response = error_response(
                ErrorCode.FUNCTION_NOT_FOUND,
                f"No artifact exists for function '{function_id}'. Run 'drspec scan' first.",
                {"function_id": function_id},
            )
            output(response, json_output=json_output, pretty=pretty)
            raise typer.Exit(1)

        # Convert confidence to 0.0-1.0 scale for storage
        confidence_score = confidence / 100.0

        # Determine new status based on confidence
        new_status = "VERIFIED" if confidence >= 70 else "NEEDS_REVIEW"

        # DuckDB has a limitation where UPDATE on a row referenced by foreign keys
        # fails even when only updating non-key columns. To work around this:
        # 1. Save and delete all FK references (contracts, queue, reasoning_traces, vision_findings)
        # 2. Update the artifact status
        # 3. Restore the FK references

        # Save queue entry if any
        queue_row = conn.execute(
            "SELECT priority, status, reason, attempts, max_attempts, error_message FROM queue WHERE function_id = ?",
            [function_id]
        ).fetchone()

        # Save reasoning traces (audit log - must preserve)
        reasoning_traces_rows = conn.execute(
            "SELECT agent, trace_json, created_at FROM reasoning_traces WHERE function_id = ?",
            [function_id]
        ).fetchall()

        # Save vision findings (audit log - must preserve)
        vision_findings_rows = conn.execute(
            """SELECT finding_type, significance, description, location, invariant_implication,
                      status, resolution_note, plot_path, created_at
               FROM vision_findings WHERE function_id = ?""",
            [function_id]
        ).fetchall()

        # Save dependencies (both as caller and callee)
        deps_as_caller = conn.execute(
            "SELECT callee_id, created_at FROM dependencies WHERE caller_id = ?",
            [function_id]
        ).fetchall()
        deps_as_callee = conn.execute(
            "SELECT caller_id, created_at FROM dependencies WHERE callee_id = ?",
            [function_id]
        ).fetchall()

        # Delete all FK references to allow artifact update
        conn.execute("DELETE FROM contracts WHERE function_id = ?", [function_id])
        conn.execute("DELETE FROM queue WHERE function_id = ?", [function_id])
        conn.execute("DELETE FROM reasoning_traces WHERE function_id = ?", [function_id])
        conn.execute("DELETE FROM vision_findings WHERE function_id = ?", [function_id])
        conn.execute("DELETE FROM dependencies WHERE caller_id = ? OR callee_id = ?", [function_id, function_id])

        # Now update artifact status (no FK references blocking this)
        update_artifact_status(conn, function_id, new_status)

        # Insert the new contract
        insert_contract(conn, function_id, contract_json_str, confidence_score)

        # Restore queue entry if it existed (mark as COMPLETED since contract is saved)
        if queue_row:
            conn.execute(
                """
                INSERT INTO queue (function_id, priority, status, reason, attempts, max_attempts, error_message)
                VALUES (?, ?, 'COMPLETED', ?, ?, ?, ?)
                """,
                [function_id, queue_row[0], queue_row[2], queue_row[3], queue_row[4], queue_row[5]]
            )

        # Restore reasoning traces
        for rt_row in reasoning_traces_rows:
            conn.execute(
                "INSERT INTO reasoning_traces (function_id, agent, trace_json, created_at) VALUES (?, ?, ?, ?)",
                [function_id, rt_row[0], rt_row[1], rt_row[2]]
            )

        # Restore vision findings
        for vf_row in vision_findings_rows:
            conn.execute(
                """INSERT INTO vision_findings
                   (function_id, finding_type, significance, description, location,
                    invariant_implication, status, resolution_note, plot_path, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [function_id, vf_row[0], vf_row[1], vf_row[2], vf_row[3],
                 vf_row[4], vf_row[5], vf_row[6], vf_row[7], vf_row[8]]
            )

        # Restore dependencies
        for dep_row in deps_as_caller:
            conn.execute(
                "INSERT INTO dependencies (caller_id, callee_id, created_at) VALUES (?, ?, ?)",
                [function_id, dep_row[0], dep_row[1]]
            )
        for dep_row in deps_as_callee:
            conn.execute(
                "INSERT INTO dependencies (caller_id, callee_id, created_at) VALUES (?, ?, ?)",
                [dep_row[0], function_id, dep_row[1]]
            )

        # Store reasoning trace if provided
        if trace:
            try:
                # Validate trace is valid JSON
                json.loads(trace)
                insert_reasoning_trace(conn, function_id, agent, trace)
            except json.JSONDecodeError:
                # Store as simple string wrapped in JSON
                insert_reasoning_trace(conn, function_id, agent, json.dumps({"note": trace}))

        # Get updated contract for response
        contract_row = get_contract(conn, function_id)
        created_at = contract_row["created_at"].isoformat() if contract_row and contract_row["created_at"] else None

        # Build response
        data = {
            "function_id": function_id,
            "confidence_score": confidence,
            "status": new_status,
            "created_at": created_at,
            "message": "Contract saved successfully",
        }

        response = success_response(data)
        output(response, json_output=json_output, pretty=pretty)

    except typer.Exit:
        raise
    except Exception as e:
        response = error_response(
            ErrorCode.INTERNAL_ERROR,
            f"Error saving contract: {str(e)}",
            {"function_id": function_id},
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)
    finally:
        conn.close()


@app.command(name="list")
def list_contracts(
    ctx: typer.Context,
    status: Optional[str] = typer.Option(
        None,
        "--status",
        "-s",
        help="Filter by status (VERIFIED, NEEDS_REVIEW, STALE, BROKEN)",
    ),
    path: Optional[str] = typer.Option(
        None,
        "--path",
        "-p",
        help="Filter by file path prefix",
    ),
    min_confidence: int = typer.Option(
        0,
        "--min-confidence",
        "-m",
        help="Minimum confidence score (0-100)",
        min=0,
        max=100,
    ),
    limit: int = typer.Option(
        50,
        "--limit",
        "-n",
        help="Maximum number of contracts to return",
        min=1,
        max=500,
    ),
    offset: int = typer.Option(
        0,
        "--offset",
        "-o",
        help="Number of contracts to skip",
        min=0,
    ),
) -> None:
    """List all contracts in the database.

    Returns contract summaries with function IDs and confidence scores.

    Examples:
        drspec contract list
        drspec contract list --status VERIFIED
        drspec contract list --path src/payments/ --min-confidence 70
        drspec contract list --limit 20 --offset 40
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
        # Convert min_confidence to 0.0-1.0 scale
        min_conf_normalized = min_confidence / 100.0

        # Build WHERE clause conditions
        conditions = ["c.confidence_score >= ?"]
        params: list = [min_conf_normalized]

        if status:
            conditions.append("a.status = ?")
            params.append(status.upper())

        if path:
            conditions.append("c.function_id LIKE ?")
            params.append(f"{path}%")

        where_clause = " AND ".join(conditions)

        # Get total count first
        count_sql = f"""
            SELECT COUNT(*)
            FROM contracts c
            JOIN artifacts a ON c.function_id = a.function_id
            WHERE {where_clause}
        """
        total = conn.execute(count_sql, params).fetchone()[0]

        # Query contracts with filters and pagination
        query_sql = f"""
            SELECT
                c.function_id,
                c.confidence_score,
                a.status,
                c.contract_json,
                c.updated_at
            FROM contracts c
            JOIN artifacts a ON c.function_id = a.function_id
            WHERE {where_clause}
            ORDER BY c.updated_at DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        rows = conn.execute(query_sql, params).fetchall()

        # Build contract summaries
        contracts = []
        for row in rows:
            func_id = row[0]

            # Parse contract JSON to get intent_summary and invariant count
            try:
                contract_data = json.loads(row[3])
                intent_summary = contract_data.get("intent_summary", "")
                invariant_count = len(contract_data.get("invariants", []))
            except json.JSONDecodeError:
                intent_summary = ""
                invariant_count = 0

            # Handle legacy data that might have been stored as 0-100 instead of 0.0-1.0
            raw_confidence = row[1]
            if raw_confidence > 1.0:
                base_confidence = int(raw_confidence)  # Already in 0-100 scale
            else:
                base_confidence = int(raw_confidence * 100)  # Convert 0.0-1.0 to 0-100

            # Get vision findings for this function and calculate adjusted confidence
            findings = get_vision_findings(conn, func_id)
            adjusted_confidence = calculate_confidence_with_findings(base_confidence, findings)
            active_findings = sum(1 for f in findings if f.status == "NEW")
            vision_penalty = base_confidence - adjusted_confidence

            contracts.append({
                "function_id": func_id,
                "confidence": {
                    "base": base_confidence,
                    "adjusted": adjusted_confidence,
                    "vision_penalty": vision_penalty,
                    "active_findings": active_findings,
                },
                "status": row[2],
                "intent_summary": intent_summary[:100] + "..." if len(intent_summary) > 100 else intent_summary,
                "invariant_count": invariant_count,
                "updated_at": row[4].isoformat() if row[4] else None,
            })

        # Build response
        data = {
            "contracts": contracts,
            "pagination": {
                "total": total,
                "limit": limit,
                "offset": offset,
                "has_more": offset + len(contracts) < total,
            },
        }

        response = success_response(data)
        output(response, json_output=json_output, pretty=pretty)

    except typer.Exit:
        raise
    except Exception as e:
        response = error_response(
            ErrorCode.INTERNAL_ERROR,
            f"Error listing contracts: {str(e)}",
            {},
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)
    finally:
        conn.close()
