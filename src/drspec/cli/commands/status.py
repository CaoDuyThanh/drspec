"""Status command - Show DrSpec project status."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from drspec.cli.output import ErrorCode, error_response, output, success_response
from drspec.core.backlog import maintenance_backlog, project_folder
from drspec.core.language_health import language_blocks
from drspec.core.docs import summary as docs_summary
from drspec.core.parser_version import reread_summary
from drspec.core.partial import cap_names_for_text, partial_files, partial_summary, read_with_warnings
from drspec.core.provenance import contract_freshness, evidence_is_current
from drspec.db import (
    VALID_ARTIFACT_STATUSES,
    count_artifacts,
    count_contracts,
    get_contract_confidence_stats,
    get_edge_stats,
)
from drspec.db.queries import get_contract
from drspec.worker_ipc import WorkerIPCError, get_worker_connection

app = typer.Typer(
    name="status",
    help="Show DrSpec project and background-work status",
    no_args_is_help=False,
)


@app.callback(invoke_without_command=True)
def status_command(
    ctx: typer.Context,
    partial: bool = typer.Option(
        False,
        "--partial",
        help="List every file that was read only in part (a parse error kept some functions out of the index): the file, the first error line, the skipped functions, the number of partial functions and when it was last read.",
    ),
    limit: int = typer.Option(0, "--limit", min=0, help="With --partial: list at most this many files, the worst first (0: all)."),
    output_format: str = typer.Option("text", "--format", help="With --partial: 'text' or 'json' (the same as the global --json)."),
    details: bool = typer.Option(
        False,
        "--details",
        help="The whole status as before: the partly parsed files, the largest tables, the resolution reasons, the confidence distribution. By default: the counts, one line per block.",
    ),
) -> None:
    """Display DrSpec project status.

    Shows artifact statistics, the background backlog (files waiting for a
    re-scan, embedding progress), contract counts, confidence distribution,
    and the code graph's own edge counts.

    Examples:
        drspec status              # The counts, one line per block
        drspec status --details    # The whole status, with the lists (partly parsed files, largest tables)
        drspec status --partial    # Every partly parsed file, with the functions it skipped
    """
    cli_ctx = ctx.obj or {}
    json_output = cli_ctx.get("json_output", False) or output_format.lower() == "json"
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

    if partial:
        try:
            data = partial_files(conn, limit)
            output(success_response(data if json_output else cap_names_for_text(data)), json_output=json_output)
        finally:
            conn.close()
        return

    try:
        artifact_total = count_artifacts(conn)
        artifact_by_status = {}
        for status in VALID_ARTIFACT_STATUSES:
            artifact_by_status[status] = count_artifacts(conn, status=status)

        contract_total = count_contracts(conn)
        confidence_stats = get_contract_confidence_stats(conn)

        edge_stats = get_edge_stats(conn)

        items_pending = artifact_by_status.get("PENDING", 0)
        items_broken = artifact_by_status.get("BROKEN", 0)
        items_needs_review = artifact_by_status.get("NEEDS_REVIEW", 0)
        items_stale = artifact_by_status.get("STALE", 0)
        items_needing_attention = items_pending + items_broken + items_needs_review + items_stale

        evidence_counts = {"passed": 0, "failed": 0, "partial": 0, "unverified": 0, "error": 0, "stale": 0}
        # One query for all the evidence; only a function that has some needs its contract looked up.
        prefix = "verification:"
        evidence_rows = dict(conn.execute("SELECT key, value FROM config WHERE starts_with(key, ?)", [prefix]).fetchall())
        for (function_id,) in conn.execute("SELECT function_id FROM artifacts").fetchall():
            raw = evidence_rows.get(prefix + function_id)
            if raw is None:
                evidence_counts["unverified"] += 1
                continue
            evidence = json.loads(raw)
            contract = get_contract(conn, function_id)
            state = evidence.get("status", "unverified") if contract and evidence_is_current(contract_freshness(conn, function_id, contract), evidence) else "stale"
            evidence_counts[state if state in evidence_counts else "unverified"] += 1

        data = {
            "verification_evidence": evidence_counts,
            "status_semantics": "Lifecycle status; confidence is agent judgment. Verification evidence records supplied samples only.",
            "artifacts": {
                "total": artifact_total,
                "by_status": artifact_by_status,
            },
            "backlog": maintenance_backlog(conn, root=project_folder(db_path)),
            "contracts": {
                "total": contract_total,
                "confidence": confidence_stats,
            },
            "edges": edge_stats,
            "summary": {
                "items_needing_attention": items_needing_attention,
                "completion_rate": round(
                    artifact_by_status.get("VERIFIED", 0) / artifact_total * 100, 1
                ) if artifact_total > 0 else 0.0,
            },
        }

        partial = partial_summary(conn)
        if partial:
            if not json_output:
                partial.pop("all_files", None)  # the whole list is for the JSON; the text shows the files that lose most
            data["partially_parsed"] = partial
        elif read_with_warnings(conn):
            data["files_read_with_warnings"] = read_with_warnings(conn)  # a syntax error somewhere, but no function lost
        reread = reread_summary(conn)
        if reread:  # files an older parser read in part, queued for a re-read when the parser changed
            data["parser_upgrade"] = {
                **reread,
                "message": (
                    f"{reread['files_queued']} file(s) that an older parser read in part were queued for a re-read: "
                    f"{reread['now_clean']} now parse whole, {reread['still_partial']} still lose functions, "
                    f"{reread['read_with_warnings']} are read with warnings only, {reread['waiting']} are waiting"
                ),
            }
        for language_key, block in language_blocks(conn).items():
            data[language_key] = block
        documents = docs_summary(conn)
        if documents:
            data["documents"] = documents
        from drspec.core.config_keys import YAML_MISSING, config_counts

        yaml_files = config_counts(conn)
        if yaml_files["files"] or yaml_files["files_with_problems"] or yaml_files["files_skipped"]:
            if yaml_files["files_without_pyyaml"]:
                yaml_files["message"] = YAML_MISSING  # (the files are not read until PyYAML is installed; the rest of drspec works)
            data["yaml_config"] = yaml_files  # the YAML config files read for `strings get`: files, keys, and those that did not parse or were not read
        from drspec.db.queries import get_member_reference_stats

        members = get_member_reference_stats(conn)
        if members:  # the reads and writes of fields (C++), apart from the call and reference edges above
            data["member_references"] = members
        from drspec.core.scan_service import generated_folders_left_out

        generated = generated_folders_left_out(conn)
        if generated:  # build trees the last scan did not read (a folder named in scan.include is read, and is not here)
            data["generated_folders"] = {
                "left_out": [f"generated folder: {folder} ({kind})" for folder, kind in sorted(generated.items())],
                "note": "generated folders are not read by default; to read one, add it to scan.include (drspec config set scan.include devel)",
            }
        from drspec.core.discovery import project_root_for
        from drspec.core.ide.mcp_registration import registration_status

        mcp = registration_status(project_root_for(db_path) or Path.cwd())
        if mcp:
            data["mcp_server"] = mcp
        from drspec.core.storage_report import database_report

        storage = database_report(conn)
        if storage:
            data["database"] = storage

        from drspec.core.parse_pool import parsers_report

        parsers = parsers_report(conn)
        if parsers:
            data["parsers"] = parsers

        if json_output or details:
            output(success_response(data), json_output=json_output)
        else:  # the default text: the counts, one line per block (`core/status_text.py`)
            from drspec.core.status_text import short_lines

            for line in short_lines(data):
                typer.echo(line)

    except Exception as e:
        response = error_response(
            ErrorCode.STATUS_ERROR,
            f"Error getting status: {str(e)}",
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)
    finally:
        conn.close()
