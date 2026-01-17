"""Learn command - Analyze bug fixes and strengthen contracts."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

import typer

from drspec.cli.output import ErrorCode, error_response, output, success_response
from drspec.db import get_connection, get_contract
from drspec.contracts.schema import Contract

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
    # Get CLI context
    cli_ctx = ctx.obj or {}
    json_output = cli_ctx.get("json_output", True)
    pretty = cli_ctx.get("pretty", False)
    repo = str(repo_path) if repo_path else "."

    # Verify we're in a git repository
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
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)

    # Parse commit range
    parts = commit_range.split("..")
    if len(parts) != 2:
        response = error_response(
            ErrorCode.VALIDATION_ERROR,
            "Invalid commit range format. Use 'start..end' (e.g., HEAD~10..HEAD)",
            {"commit_range": commit_range},
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)

    start_ref, end_ref = parts

    try:
        # Import learning module
        from drspec.learning.diff import analyze_commit_range
        from drspec.learning.patterns import extract_all_patterns
        from drspec.learning.strengthening import strengthen_contract
        from drspec.learning.history import (
            LearningEvent,
            insert_learning_event,
            init_learning_schema,
        )

        # Analyze commits
        if not json_output:
            typer.echo(f"Analyzing commits from {start_ref} to {end_ref}...")

        analyses = analyze_commit_range(
            start_ref=start_ref,
            end_ref=end_ref,
            repo_path=repo,
            bug_fixes_only=bug_fixes_only,
        )

        if not analyses:
            if not json_output:
                typer.echo("No commits found in range (or no bug fixes if --bug-fixes-only)")
            response = success_response({
                "commits_analyzed": 0,
                "patterns_found": 0,
                "suggestions": 0,
                "results": [],
            })
            output(response, json_output=json_output, pretty=pretty)
            raise typer.Exit(0)

        # Get database connection
        try:
            conn = get_connection()
            init_learning_schema(conn)
        except FileNotFoundError:
            conn = None

        # Process each analysis
        total_patterns = 0
        total_suggestions = 0
        results = []

        for analysis in analyses:
            commit = analysis.commit
            if not json_output:
                typer.echo(f"\n{'─' * 60}")
                typer.echo(f"Commit: {commit.short_sha} - {commit.message.split(chr(10))[0][:50]}")

                if commit.is_bug_fix:
                    typer.echo(f"  Bug fix confidence: {analysis.bug_fix_confidence:.0%}")
                    if commit.issue_refs:
                        typer.echo(f"  Issue references: {', '.join(commit.issue_refs)}")

            # Extract patterns
            patterns = extract_all_patterns(
                commit.files,
                analysis.modified_functions,
            )

            total_patterns += len(patterns)

            if patterns and not json_output:
                typer.echo(f"  Patterns found: {len(patterns)}")

            # Process each modified function
            for func_id, file_path, func_name in analysis.modified_functions:
                if not json_output:
                    typer.echo(f"\n  Function: {func_id}")

                # Get relevant patterns for this function
                func_patterns = [
                    p for p in patterns
                    if p.file_path == file_path and (
                        p.function_name == func_name or
                        (p.function_name and func_name in p.function_name)
                    )
                ]

                if not func_patterns:
                    func_patterns = [p for p in patterns if p.file_path == file_path]

                # Get existing contract
                existing_contract = None
                if conn:
                    contract_row = get_contract(conn, func_id)
                    if contract_row:
                        try:
                            existing_contract = Contract.from_json(contract_row["contract_json"])
                            if not json_output:
                                typer.echo("    Has existing contract: Yes")
                        except Exception:
                            pass

                # Strengthen contract
                strengthening = strengthen_contract(
                    function_id=func_id,
                    patterns=func_patterns,
                    existing_contract=existing_contract,
                )

                # Report results
                if not json_output:
                    if strengthening.validated_invariants:
                        typer.echo(
                            f"    Validated invariants: {', '.join(strengthening.validated_invariants)}"
                        )

                    if strengthening.new_invariants:
                        typer.echo("    Suggested new invariants:")
                        for inv in strengthening.new_invariants:
                            typer.echo(f"      - {inv.name}: {inv.logic}")

                    if strengthening.confidence_boost > 0:
                        typer.echo(
                            f"    Suggested confidence boost: +{strengthening.confidence_boost:.0%}"
                        )

                if strengthening.new_invariants:
                    total_suggestions += len(strengthening.new_invariants)

                # Record learning event
                if conn and not dry_run and func_patterns:
                    for pattern in func_patterns:
                        event = LearningEvent(
                            commit_sha=commit.commit_sha,
                            commit_message=commit.message,
                            function_id=func_id,
                            pattern_type=pattern.pattern_type,
                            pattern_description=pattern.description,
                            contract_modified=False,  # Not auto-applying changes
                            confidence_boost=strengthening.confidence_boost,
                            new_invariants_added=0,
                            invariants_validated=len(strengthening.validated_invariants),
                        )
                        insert_learning_event(conn, event)

                results.append(strengthening.to_dict())

        if conn:
            conn.close()

        # Summary
        if not json_output:
            typer.echo(f"\n{'═' * 60}")
            typer.echo("Summary:")
            typer.echo(f"  Commits analyzed: {len(analyses)}")
            typer.echo(f"  Patterns found: {total_patterns}")
            typer.echo(f"  Invariant suggestions: {total_suggestions}")

            if dry_run:
                typer.echo("\n  (Dry run - no changes made)")

        response = success_response({
            "commits_analyzed": len(analyses),
            "patterns_found": total_patterns,
            "suggestions": total_suggestions,
            "results": results,
            "dry_run": dry_run,
        })
        output(response, json_output=json_output, pretty=pretty)

    except subprocess.CalledProcessError as e:
        response = error_response(
            ErrorCode.EXECUTION_ERROR,
            f"Git error: {e.stderr.decode() if e.stderr else str(e)}",
            {"commit_range": commit_range},
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)
    except Exception as e:
        response = error_response(
            ErrorCode.EXECUTION_ERROR,
            f"Analysis error: {str(e)}",
            {"commit_range": commit_range},
        )
        output(response, json_output=json_output, pretty=pretty)
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
    json_output = cli_ctx.get("json_output", True)
    pretty = cli_ctx.get("pretty", False)

    try:
        conn = get_connection()
    except FileNotFoundError:
        response = error_response(
            ErrorCode.DB_NOT_INITIALIZED,
            "DrSpec not initialized. Run 'drspec init' first.",
            {},
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)

    try:
        from drspec.learning.history import get_learning_history, init_learning_schema

        init_learning_schema(conn)
        events = get_learning_history(conn, function_id=function_id, limit=limit)

        if not events:
            if not json_output:
                typer.echo("No learning history found.")
            response = success_response({"events": []})
            output(response, json_output=json_output, pretty=pretty)
            raise typer.Exit(0)

        if not json_output:
            typer.echo(f"Learning History ({len(events)} events):")
            typer.echo("─" * 60)

            for event in events:
                date_str = event.created_at.strftime("%Y-%m-%d %H:%M") if event.created_at else "?"
                typer.echo(f"\n[{event.commit_sha[:7]}] {date_str}")
                if event.function_id:
                    typer.echo(f"  Function: {event.function_id}")
                if event.pattern_type:
                    typer.echo(f"  Pattern: {event.pattern_type.value}")
                if event.pattern_description:
                    typer.echo(f"  Description: {event.pattern_description}")
                if event.invariants_validated > 0:
                    typer.echo(f"  Validated invariants: {event.invariants_validated}")

        response = success_response({
            "events": [e.to_dict() for e in events],
            "count": len(events),
        })
        output(response, json_output=json_output, pretty=pretty)

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
    json_output = cli_ctx.get("json_output", True)
    pretty = cli_ctx.get("pretty", False)

    try:
        conn = get_connection()
    except FileNotFoundError:
        response = error_response(
            ErrorCode.DB_NOT_INITIALIZED,
            "DrSpec not initialized. Run 'drspec init' first.",
            {},
        )
        output(response, json_output=json_output, pretty=pretty)
        raise typer.Exit(1)

    try:
        from drspec.learning.history import get_learning_stats, init_learning_schema

        init_learning_schema(conn)
        stats = get_learning_stats(conn)

        if not json_output:
            typer.echo("Learning Statistics:")
            typer.echo("─" * 40)
            typer.echo(f"  Total events: {stats['total_events']}")
            typer.echo(f"  Unique commits: {stats['unique_commits']}")
            typer.echo(f"  Functions affected: {stats['unique_functions']}")
            typer.echo(f"  Contracts modified: {stats['contracts_modified']}")
            typer.echo(f"  Invariants added: {stats['total_invariants_added']}")
            typer.echo(f"  Invariants validated: {stats['total_invariants_validated']}")
            typer.echo(f"  Avg confidence boost: {stats['avg_confidence_boost']:.2%}")

            if stats["pattern_distribution"]:
                typer.echo("\nPattern Distribution:")
                for pattern, count in stats["pattern_distribution"].items():
                    typer.echo(f"  {pattern}: {count}")

        response = success_response(stats)
        output(response, json_output=json_output, pretty=pretty)

    finally:
        conn.close()
