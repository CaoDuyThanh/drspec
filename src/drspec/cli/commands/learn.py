"""Learn command - Analyze bug fixes and strengthen contracts."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

import typer

from drspec.cli.output import ErrorCode, error_response, output, success_response
from drspec.db import get_contract
from drspec.contracts.schema import Contract
from drspec.cli.validators import resolve_or_exit
from drspec.worker_ipc import WorkerIPCError, get_worker_connection

app = typer.Typer(
    name="learn",
    help="Analyze bug fixes and strengthen contracts",
    no_args_is_help=True,
)


@app.command("analyze")
def learn_analyze(
    ctx: typer.Context,
    commit_range: str = typer.Argument(
        ...,
        help="Git commit range to analyze (e.g., HEAD~10..HEAD, v1.0.0..v1.1.0)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        "-n",
        help="Show what would be learned without making changes",
    ),
    bug_fixes_only: bool = typer.Option(
        True,
        "--bug-fixes-only/--all-commits",
        help="Only analyze commits that appear to be bug fixes",
    ),
    repo_path: Optional[Path] = typer.Option(
        None,
        "--repo",
        "-r",
        help="Path to git repository (defaults to current directory)",
    ),
) -> None:
    """Analyze git commits for bug patterns and strengthen contracts.

    Examines commit diffs to extract failure patterns from bug fixes,
    then suggests or applies contract improvements.

    Examples:
        drspec learn analyze HEAD~10..HEAD
        drspec learn analyze v1.0.0..v1.1.0 --dry-run
        drspec learn analyze HEAD~5..HEAD --all-commits
    """
    cli_ctx = ctx.obj or {}
    json_output = cli_ctx.get("json_output", False)
    db_path_override = cli_ctx.get("db_path")
    db_path = db_path_override or (Path.cwd() / "_drspec" / "contracts.db")
    repo = str(repo_path) if repo_path else "."

    try:
        subprocess.run(
            ["git", "-C", repo, "rev-parse", "--git-dir"],
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        response = error_response(
            ErrorCode.VALIDATION_ERROR,
            "Not a git repository",
            {"path": repo},
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)

    parts = commit_range.split("..")
    if len(parts) != 2:
        response = error_response(
            ErrorCode.VALIDATION_ERROR,
            "Invalid commit range format. Use 'start..end' (e.g., HEAD~10..HEAD)",
            {"commit_range": commit_range},
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)

    start_ref, end_ref = parts

    try:
        from drspec.learning.diff import analyze_commit_range
        from drspec.learning.patterns import extract_all_patterns
        from drspec.learning.strengthening import strengthen_contract
        from drspec.learning.history import (
            LearningEvent,
            insert_learning_event,
            init_learning_schema,
        )

        analyses = analyze_commit_range(
            start_ref=start_ref,
            end_ref=end_ref,
            repo_path=repo,
            bug_fixes_only=bug_fixes_only,
        )

        if not analyses:
            response = success_response({
                "commits_analyzed": 0,
                "patterns_found": 0,
                "suggestions": 0,
                "results": [],
            })
            output(response, json_output=json_output)
            raise typer.Exit(0)

        # DB connection is optional: pattern extraction and strengthening
        # suggestions still work without it, just without an existing-
        # contract lookup or an event history record.
        if db_path.parent.exists() and db_path.exists():
            try:
                conn = get_worker_connection(db_path)
                init_learning_schema(conn)
            except WorkerIPCError:
                conn = None
        else:
            conn = None

        total_patterns = 0
        total_suggestions = 0
        results = []

        for analysis in analyses:
            commit = analysis.commit
            patterns = extract_all_patterns(
                commit.files,
                analysis.modified_functions,
            )

            total_patterns += len(patterns)

            for func_id, file_path, func_name in analysis.modified_functions:
                func_patterns = [
                    p for p in patterns
                    if p.file_path == file_path and (
                        p.function_name == func_name or
                        (p.function_name and func_name in p.function_name)
                    )
                ]

                if not func_patterns:
                    func_patterns = [p for p in patterns if p.file_path == file_path]

                existing_contract = None
                if conn:
                    contract_row = get_contract(conn, func_id)
                    if contract_row:
                        try:
                            existing_contract = Contract.from_json(contract_row["contract_json"])
                        except Exception:
                            pass

                strengthening = strengthen_contract(
                    function_id=func_id,
                    patterns=func_patterns,
                    existing_contract=existing_contract,
                )

                if strengthening.new_invariants:
                    total_suggestions += len(strengthening.new_invariants)

                if conn and not dry_run and func_patterns:
                    for pattern in func_patterns:
                        event = LearningEvent(
                            commit_sha=commit.commit_sha,
                            commit_message=commit.message,
                            function_id=func_id,
                            pattern_type=pattern.pattern_type,
                            pattern_description=pattern.description,
                            contract_modified=False,  # this command only suggests, never applies
                            confidence_boost=strengthening.confidence_boost,
                            new_invariants_added=0,
                            invariants_validated=len(strengthening.validated_invariants),
                        )
                        insert_learning_event(conn, event)

                results.append({
                    "commit": commit.short_sha,
                    "commit_message": commit.message.split("\n")[0],
                    **strengthening.to_dict(),
                })

        if conn:
            conn.close()

        response = success_response({
            "commits_analyzed": len(analyses),
            "patterns_found": total_patterns,
            "suggestions": total_suggestions,
            "results": results,
            "dry_run": dry_run,
        })
        output(response, json_output=json_output)

    except subprocess.CalledProcessError as e:
        response = error_response(
            ErrorCode.EXECUTION_ERROR,
            f"Git error: {e.stderr.decode() if e.stderr else str(e)}",
            {"commit_range": commit_range},
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)
    except Exception as e:
        response = error_response(
            ErrorCode.EXECUTION_ERROR,
            f"Analysis error: {str(e)}",
            {"commit_range": commit_range},
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)


@app.command("history")
def learn_history(
    ctx: typer.Context,
    function_id: Optional[str] = typer.Option(
        None,
        "--function",
        "-f",
        help="Filter by function ID",
    ),
    limit: int = typer.Option(
        20,
        "--limit",
        "-l",
        help="Maximum number of events to show",
    ),
) -> None:
    """Show learning history.

    Display previous learning events from bug fix analysis.

    Examples:
        drspec learn history
        drspec learn history --function src/utils.py::parse
        drspec learn history --limit 50
    """
    cli_ctx = ctx.obj or {}
    json_output = cli_ctx.get("json_output", False)
    db_path_override = cli_ctx.get("db_path")
    db_path = db_path_override or (Path.cwd() / "_drspec" / "contracts.db")

    if not db_path.parent.exists() or not db_path.exists():
        response = error_response(
            ErrorCode.DB_NOT_INITIALIZED,
            "DrSpec not initialized. Run 'drspec init' first.",
            {},
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

    if function_id:
        function_id = resolve_or_exit(conn, function_id, json_output)

    try:
        from drspec.learning.history import get_learning_history, init_learning_schema

        init_learning_schema(conn)
        events = get_learning_history(conn, function_id=function_id, limit=limit)

        if not events:
            output(success_response({"events": []}), json_output=json_output)
            raise typer.Exit(0)

        response = success_response({
            "events": [e.to_dict() for e in events],
            "count": len(events),
        })
        output(response, json_output=json_output)

    finally:
        conn.close()


@app.command("stats")
def learn_stats(
    ctx: typer.Context,
) -> None:
    """Show learning statistics.

    Display summary statistics of all learning events.

    Examples:
        drspec learn stats
    """
    cli_ctx = ctx.obj or {}
    json_output = cli_ctx.get("json_output", False)
    db_path_override = cli_ctx.get("db_path")
    db_path = db_path_override or (Path.cwd() / "_drspec" / "contracts.db")

    if not db_path.parent.exists() or not db_path.exists():
        response = error_response(
            ErrorCode.DB_NOT_INITIALIZED,
            "DrSpec not initialized. Run 'drspec init' first.",
            {},
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

    try:
        from drspec.learning.history import get_learning_stats, init_learning_schema

        init_learning_schema(conn)
        stats = get_learning_stats(conn)

        response = success_response(stats)
        output(response, json_output=json_output)

    finally:
        conn.close()
