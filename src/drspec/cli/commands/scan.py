"""Scan command - Scan source files for functions."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, List, Optional

import typer

from drspec.cli.output import ErrorCode, error_response, output, success_response
from drspec.core.scan_service import MAX_LISTED, bounded_errors, new_scan_id, partial_report, run_scan, summarize_by_reason
from drspec.worker_ipc import WorkerIPCError, get_worker_connection

__all__ = ["MAX_LISTED", "bounded_errors", "partial_report", "scan_command", "summarize_by_reason"]


def _relative_path(path: Path, root: Path) -> str:
    """The path as it is stored: relative to the project folder when it lies inside it."""
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def project_folder_of(start: Path) -> Path:
    """The nearest folder at or above `start` that holds a drspec database (`_drspec/contracts.db`), or `start` when
    there is none. Like git, a command run in a subfolder works on the project it is in."""
    for folder in [start, *start.parents]:
        if (folder / "_drspec" / "contracts.db").exists():
            return folder
    return start


def resolve_targets(targets: Optional[List[str]], root: Optional[Path] = None) -> tuple[list[str], Optional[str]]:
    """What a scan was asked about, as the worker wants it: paths relative to the project `root` (the current folder
    when not given), and function or section identifiers whose file part is made relative the same way. A target is
    read from where the command runs, so `a.py` typed in `pkg/` is `pkg/a.py` of the project. The second value names
    a target that does not exist from where the command runs, or `None`."""
    base = root if root is not None else Path.cwd()
    resolved: list[str] = []
    for target in targets or []:
        file_part, separator, rest = target.partition("::")
        path = Path(file_part)
        if not path.exists():
            return [], target
        relative = _relative_path(path, base)
        resolved.append(relative + separator + rest)
    return resolved, None


def scan_command(
    ctx: typer.Context,
    targets: Optional[List[str]] = typer.Argument(
        None,
        help=(
            "What to scan: a file, a directory, a function ID (src/a.py::Class.method; the #hash may be left out) "
            "or a document section ID. Defaults to every added, changed or removed file of the project."
        ),
    ),
    recursive: bool = typer.Option(
        True,
        "--recursive/--no-recursive",
        help="Recursively scan directories",
    ),
    deep_scan: bool = typer.Option(
        False,
        "--deep-scan",
        help="Also attempt fallback call resolution, with INFERRED/AMBIGUOUS confidence, for the files this scan reads.",
    ),
    embed: bool = typer.Option(
        False,
        "--embed/--no-embed",
        help=(
            "Compute/backfill local embeddings for the functions, edges, and references of the files this scan reads — "
            "needed for 'drspec search --semantic'. Off by default: embedding is the single "
            "largest cost in a scan (design.md §12) and most scans never use --semantic. "
            "When off, a changed node/edge/reference has its embedding cleared (NULL), "
            "never left stale from before the change."
        ),
    ),
    full: bool = typer.Option(
        False,
        "--full",
        "--force",
        help=(
            "Re-read and re-publish every file and document of the scan whether or not it changed, and re-resolve "
            "every call link. (--force is the old name and means the same.) Combine with --deep-scan or --embed to "
            "apply them to files that did not change."
        ),
    ),
    no_wait: bool = typer.Option(
        False,
        "--no-wait",
        help="Queue the files at normal background priority and return at once; the worker reads them later.",
    ),
    code_only: bool = typer.Option(False, "--code-only", help="Read code files only, not documents."),
    docs_only: bool = typer.Option(False, "--docs-only", help="Read documents only, not code."),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        help="Print the whole summary as before (the settings, every counter, every function re-read). By default: one line for the result and one for each thing that needs attention.",
    ),
) -> None:
    """Scan source files and documents, build the function/dependency graph, and optionally embed both.

    The background worker does the reading: this command asks it for the files that were added, changed or removed
    (found by size and modification time, nothing is opened to check) and waits for them, ahead of anything else
    queued. A file nothing changed in is skipped, and the output says so. Nothing in this command calls an LLM.
    Press Ctrl-C to cancel: batches already published stay, the files not yet read are dropped from the queue.

    Examples:
        drspec scan                           # Everything added, changed or removed in the project
        drspec scan ./src                     # Only what is under ./src
        drspec scan ./src/module.py           # Single file
        drspec scan src/a.py::Class.method    # The file of that function, and what links to it
        drspec scan --full                    # Re-read and re-resolve everything
        drspec scan --no-wait                 # Queue and return
        drspec scan --docs-only               # Documents only
    """
    cli_ctx = ctx.obj or {}
    json_output = cli_ctx.get("json_output", False)
    db_path_override = cli_ctx.get("db_path")

    if code_only and docs_only:
        output(error_response(ErrorCode.INVALID_INPUT, "--code-only and --docs-only exclude each other."), json_output=json_output)
        raise typer.Exit(1)

    project = project_folder_of(Path.cwd())
    resolved, missing = resolve_targets(targets, project)
    if missing is not None:
        output(error_response(ErrorCode.PATH_NOT_FOUND, f"Path does not exist: {missing}", {"path": missing}), json_output=json_output)
        raise typer.Exit(1)

    drspec_dir = project / "_drspec"
    db_path = db_path_override if db_path_override else drspec_dir / "contracts.db"
    if not db_path.parent.exists() or not db_path.exists():
        output(
            error_response(ErrorCode.DB_NOT_INITIALIZED, "DrSpec not initialized. Run 'drspec init' first.", {"expected_path": str(drspec_dir)}),
            json_output=json_output,
        )
        raise typer.Exit(1)

    show_progress = sys.stderr.isatty() and not json_output
    spec = {
        "scan_id": new_scan_id(),
        "targets": resolved,
        "recursive": recursive,
        "full": full,
        "deep": deep_scan,
        "embed": embed,
        "code_only": code_only,
        "docs_only": docs_only,
        "wait": not no_wait,
    }
    try:
        worker_conn = get_worker_connection(db_path)
        try:
            summary = run_scan(worker_conn, spec, on_progress=_progress_line if show_progress else None)
        finally:
            worker_conn.close()
            if show_progress:
                sys.stderr.write("\r\033[K")
                sys.stderr.flush()
    except WorkerIPCError as e:
        output(
            error_response(
                ErrorCode.DATABASE_BUSY,
                f"Could not reach the drspec worker to run this scan: {e}. A scan is run by the worker; there is no scan without one.",
                {"db_path": str(db_path)},
            ),
            json_output=json_output,
        )
        raise typer.Exit(1)
    except Exception as e:  # noqa: BLE001 - the scan reports every failure the same way
        output(error_response(ErrorCode.SCAN_ERROR, f"Error during scan: {str(e)}", {"path": " ".join(targets or ["."])}), json_output=json_output)
        raise typer.Exit(1)

    if summary.get("publish_error"):
        output(
            error_response(ErrorCode.SCAN_ERROR, f"Error during scan: {summary['publish_error']}", {"path": " ".join(targets or ["."]), "files_done": summary.get("files_done", 0)}),
            json_output=json_output,
        )
        raise typer.Exit(1)
    data: dict[str, Any] = {
        "path": resolved[0] if len(resolved) == 1 else (" ".join(resolved) if resolved else str(Path.cwd())),
        "recursive": recursive,
        "deep_scan": deep_scan,
        "embed": embed,
        "force": full,
        "full": full,
        **summary,
    }
    if not json_output:
        data.pop("partial_files", None)  # the whole list is for the JSON; the text shows the files that lose most
        data.pop("partial_files_omitted", None)
    review = summary.get("documentation_sections_to_review", 0)
    if review:
        data["note"] = f"{review} documentation section(s) may need an update (drspec search --status STALE --docs-only)"
    if summary.get("up_to_date") and (deep_scan or embed):
        data["hint"] = "--deep-scan and --embed apply to the files a scan reads, and none changed. Add --full to apply them to every file."
    if summary.get("files_skipped_by_gitignore"):
        data["skipped_note"] = f"{summary['files_skipped_by_gitignore']} file(s) left out because .gitignore ignores them (scan.include names folders to read anyway)"
    from drspec.core.config_keys import YAML_MISSING, yaml_available, yaml_files_in_project

    if not yaml_available():  # said once for the scan, not once per file: the yaml files it lists are not read
        found = yaml_files_in_project(db_path.parent.parent)
        if found:
            note = f"{found} yaml file(s) not read: {YAML_MISSING}."
            data["note"] = f"{data['note']} {note}" if data.get("note") else note
    generated = summary.get("generated_folders") or {}
    if generated:
        data["left_out"] = [f"generated folder: {folder} ({kind})" for folder, kind in sorted(generated.items())]
        data["left_out_note"] = "generated folders are not read by default; to read one, add it to scan.include (drspec config set scan.include devel)"
    if json_output or verbose:
        output(success_response(data), json_output=json_output)
    else:  # the default text: one line for the result, and one for each thing that needs attention (`core/scan_text.py`)
        from drspec.core.scan_text import short_lines

        for line in short_lines(data):
            typer.echo(line)
    if summary.get("state") == "cancelled":
        raise typer.Exit(130)


def _progress_line(run: dict) -> None:
    queued = run.get("queued", 0)
    if run.get("cancelling"):  # Ctrl-C came while the worker was publishing a batch: that batch is finished, not undone
        sys.stderr.write("\rCancelling: the batch being published is finished first (Ctrl-C again to stop waiting)\033[K")
        sys.stderr.flush()
        return
    finished = run.get("done", 0) + run.get("failed", 0)
    batch = run.get("batch")
    if batch:  # the batch the worker is running now: a first scan is one batch, so this is all there is to see for a while
        seconds = int(batch.get("seconds", 0))
        if batch.get("phase") == "publishing":
            line = f"Publishing {batch.get('files_total', 0)} files read ({seconds} s), {finished}/{queued} files done so far"
        else:
            line = f"Scanning {finished + batch.get('files_read', 0)}/{queued} files: reading ({seconds} s)"
        sys.stderr.write(f"\r{line}\033[K")
        sys.stderr.flush()
        return
    sys.stderr.write(
        f"\rScanning {finished}/{queued} files "
        f"(code {run.get('done_code', 0)}, documents {run.get('done_documents', 0)})\033[K"
    )
    sys.stderr.flush()
