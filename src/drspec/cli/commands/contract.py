"""Contract command - Manage contracts."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer

from drspec.cli.output import ErrorCode, error_response, output, success_response
from drspec.cli.validators import resolve_or_exit, validate_function_id
from drspec.contracts import validate_contract
from drspec.core.carry_over import carry_over_record
from drspec.core.provenance import contract_freshness, evidence_is_current
from drspec.db import (
    get_artifact,
    get_contract,
    insert_contract,
    insert_reasoning_trace,
    update_artifact_status,
)
from drspec.db.queries import (
    calculate_confidence_with_findings,
    get_vision_findings,
)
from drspec.worker_ipc import WorkerIPCError, get_worker_connection

app = typer.Typer(
    name="contract",
    help="Manage function contracts",
)


def find_similar_functions(
    conn,
    function_id: str,
    limit: int = 5,
) -> list[str]:
    """Similar function IDs for suggestions (one implementation for every command: `core.dependency_info.find_similar_functions`)."""
    from drspec.core.dependency_info import find_similar_functions as shared

    return shared(conn, function_id, limit)


def _check_coverage(contract_data: dict) -> dict:
    from drspec.contracts.generator import _translate_invariant_logic
    rules = contract_data.get("invariants", [])
    supported = sum(_translate_invariant_logic(r["logic"], r["name"]) is not None for r in rules)
    return {"supported": supported, "total": len(rules), "unsupported": len(rules) - supported}


@app.command()
def get(
    ctx: typer.Context,
    function_id: str = typer.Argument(
        ...,
        help="Function ID (format: filepath::name#hash as printed by search; the #hash may be left out when only one function in the file has that name)",
    ),
) -> None:
    """Get the contract for a specific function.

    Returns the contract JSON including invariants, intent, and confidence.

    Examples:
        drspec contract get src/payments/reconcile.py::reconcile_transactions
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

    is_valid, error_msg = validate_function_id(function_id)
    if not is_valid:
        response = error_response(
            ErrorCode.INVALID_FUNCTION_ID,
            error_msg,
            {"function_id": function_id, "expected_format": "filepath::function_name"},
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
        contract_row = get_contract(conn, function_id)

        if contract_row is None:
            suggestions = find_similar_functions(conn, function_id)
            response = error_response(
                ErrorCode.CONTRACT_NOT_FOUND,
                f"No contract exists for function '{function_id}'",
                {
                    "function_id": function_id,
                    "suggestions": suggestions,
                },
            )
            output(response, json_output=json_output)
            raise typer.Exit(1)

        artifact = get_artifact(conn, function_id)
        status = artifact.status if artifact else "UNKNOWN"

        contract_data = json.loads(contract_row["contract_json"])

        # confidence_score is stored as 0.0-1.0; displayed as 0-100
        raw_confidence = contract_row["confidence_score"]
        # some legacy rows were stored as 0-100 instead of 0.0-1.0
        if raw_confidence > 1.0:
            base_confidence = int(raw_confidence)
        else:
            base_confidence = int(raw_confidence * 100)

        findings = get_vision_findings(conn, function_id)
        adjusted_confidence = calculate_confidence_with_findings(base_confidence, findings)

        # "NEW" = unresolved finding
        active_findings = sum(1 for f in findings if f.status == "NEW")
        vision_penalty = base_confidence - adjusted_confidence

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
            "freshness": contract_freshness(conn, function_id, contract_row),
            "verification": dict(contract_row["verification"], current=evidence_is_current(contract_freshness(conn, function_id, contract_row), contract_row["verification"])) if contract_row.get("verification") else None,
            "check_coverage": _check_coverage(contract_data),
            "created_at": contract_row["created_at"].isoformat() if contract_row["created_at"] else None,
            "updated_at": contract_row["updated_at"].isoformat() if contract_row["updated_at"] else None,
        }
        carried = carry_over_record(conn, function_id)
        if carried is not None:
            data["carried_over"] = carried

        response = success_response(data)
        output(response, json_output=json_output)

    except typer.Exit:
        # avoid being swallowed by the broader except Exception below
        raise
    except json.JSONDecodeError as e:
        response = error_response(
            ErrorCode.INTERNAL_ERROR,
            f"Error parsing stored contract JSON: {str(e)}",
            {"function_id": function_id},
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)
    except Exception as e:
        response = error_response(
            ErrorCode.INTERNAL_ERROR,
            f"Error getting contract: {str(e)}",
            {"function_id": function_id},
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)
    finally:
        conn.close()


@app.command()
def save(
    ctx: typer.Context,
    function_id: str = typer.Argument(
        ...,
        help="Function ID (format: filepath::name#hash as printed by search; the #hash may be left out when only one function in the file has that name)",
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

    is_valid, error_msg = validate_function_id(function_id)
    if not is_valid:
        response = error_response(
            ErrorCode.INVALID_FUNCTION_ID,
            error_msg,
            {"function_id": function_id, "expected_format": "filepath::function_name"},
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)

    if sys.stdin.isatty():
        response = error_response(
            ErrorCode.INVALID_INPUT,
            "No contract JSON provided. Pipe contract JSON to stdin.",
            {"example": "echo '{...}' | drspec contract save <function_id> --confidence 80"},
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)

    contract_json_str = sys.stdin.read().strip()
    if not contract_json_str:
        response = error_response(
            ErrorCode.INVALID_INPUT,
            "Empty contract JSON provided.",
            {"function_id": function_id},
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)

    validation_result = validate_contract(contract_json_str)
    if not validation_result.success:
        response = error_response(
            ErrorCode.INVALID_SCHEMA,
            validation_result.error.message if validation_result.error else "Contract validation failed",
            validation_result.error.details if validation_result.error else {},
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)

    try:
        conn = get_worker_connection(db_path)
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
            response = error_response(
                ErrorCode.FUNCTION_NOT_FOUND,
                f"No artifact exists for function '{function_id}'. Run 'drspec scan' first.",
                {"function_id": function_id},
            )
            output(response, json_output=json_output)
            raise typer.Exit(1)

        confidence_score = confidence / 100.0

        new_status = "NEEDS_REVIEW"

        # update_artifact_status commits internally as part of its DuckDB FK
        # workaround; safe here since this isn't inside a larger transaction.
        update_artifact_status(conn, function_id, new_status)
        insert_contract(conn, function_id, contract_json_str, confidence_score)

        if trace:
            try:
                json.loads(trace)
                insert_reasoning_trace(conn, function_id, agent, trace)
            except json.JSONDecodeError:
                insert_reasoning_trace(conn, function_id, agent, json.dumps({"note": trace}))

        contract_row = get_contract(conn, function_id)
        created_at = contract_row["created_at"].isoformat() if contract_row and contract_row["created_at"] else None

        data = {
            "function_id": function_id,
            "confidence_score": confidence,
            "status": new_status,
            "created_at": created_at,
            "message": "Contract saved successfully",
        }

        response = success_response(data)
        output(response, json_output=json_output)

    except typer.Exit:
        raise
    except Exception as e:
        response = error_response(
            ErrorCode.INTERNAL_ERROR,
            f"Error saving contract: {str(e)}",
            {"function_id": function_id},
        )
        output(response, json_output=json_output)
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
        min_conf_normalized = min_confidence / 100.0

        conditions = ["c.confidence_score >= ?"]
        params: list = [min_conf_normalized]

        if status:
            conditions.append("a.status = ?")
            params.append(status.upper())

        if path:
            conditions.append("c.function_id LIKE ?")
            params.append(f"{path}%")

        where_clause = " AND ".join(conditions)

        count_sql = f"""
            SELECT COUNT(*)
            FROM contracts c
            JOIN artifacts a ON c.function_id = a.function_id
            WHERE {where_clause}
        """
        total = conn.execute(count_sql, params).fetchone()[0]

        query_sql = f"""
            SELECT
                c.function_id,
                c.confidence_score,
                a.status,
                c.contract_json,
                c.updated_at,
                a.file_path
            FROM contracts c
            JOIN artifacts a ON c.function_id = a.function_id
            WHERE {where_clause}
            ORDER BY c.updated_at DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        rows = conn.execute(query_sql, params).fetchall()

        contracts = []
        for row in rows:
            func_id = row[0]

            try:
                contract_data = json.loads(row[3])
                intent_summary = contract_data.get("intent_summary", "")
                invariant_count = len(contract_data.get("invariants", []))
            except json.JSONDecodeError:
                intent_summary = ""
                invariant_count = 0

            # some legacy rows were stored as 0-100 instead of 0.0-1.0
            raw_confidence = row[1]
            if raw_confidence > 1.0:
                base_confidence = int(raw_confidence)
            else:
                base_confidence = int(raw_confidence * 100)

            findings = get_vision_findings(conn, func_id)
            adjusted_confidence = calculate_confidence_with_findings(base_confidence, findings)
            active_findings = sum(1 for f in findings if f.status == "NEW")
            vision_penalty = base_confidence - adjusted_confidence

            contracts.append({
                "function_id": func_id,
                "file_path": row[5],
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
        output(response, json_output=json_output)

    except typer.Exit:
        raise
    except Exception as e:
        response = error_response(
            ErrorCode.INTERNAL_ERROR,
            f"Error listing contracts: {str(e)}",
            {},
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)
    finally:
        conn.close()
