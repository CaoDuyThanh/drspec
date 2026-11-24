"""Init command - Initialize DrSpec in a project."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import List, Optional

import typer

from drspec.cli.output import output, success_response, error_response
from drspec.contracts import DEFAULT_CONFIDENCE_THRESHOLD, set_confidence_threshold
from drspec.cli.commands.scan import _progress_line
from drspec.core.indexing import scan_message, scan_whole_project, start_indexing
from drspec.core.resources import get_templates_path
from drspec.db.connection import ensure_db_directory, get_connection, init_schema
from drspec.db.fk_migration import ensure_fk_layout
from drspec.db.identity_migration import ensure_identity_layout
from drspec.db.migration import MigrationError, ensure_embedding_layout
from drspec.staleness_worker import is_worker_running, stop_worker
from drspec.worker_ipc import worker_is_outdated

# Default paths
DRSPEC_FOLDER = "_drspec"
DB_NAME = "contracts.db"
AGENTS_FOLDER = "agents"
GITIGNORE_ENTRY = "_drspec/"


app = typer.Typer(
    name="init",
    help=(
        "Initialize DrSpec in the current project. After the setup the worker is started and indexes the project in the "
        "background (about 20% of the machine); --scan scans it at full speed and waits, as 'drspec scan' does. The scan "
        "options (--deep-scan, --embed, --code-only, --docs-only) are options of 'drspec scan'."
    ),
    no_args_is_help=False,
)


def is_initialized_database(db_path: Path) -> bool:
    """Whether `db_path` is a usable drspec database.

    A live worker has the file open and already initialized the schema;
    probing it directly would fail on the lock, which a caller would
    misread as a corrupt database and re-initialize over.
    """
    if is_worker_running(db_path) is not None:
        return True
    try:
        conn = get_connection(db_path)
        try:
            conn.execute("SELECT 1 FROM artifacts LIMIT 1")
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - any failure to open/query means "not usable"
        return False
    return True


def update_gitignore(project_root: Path) -> bool:
    """Update .gitignore to include _drspec/ if not already present.

    Args:
        project_root: Project root directory.

    Returns:
        True if gitignore was updated, False if already had entry.
    """
    gitignore_path = project_root / ".gitignore"

    if gitignore_path.exists():
        content = gitignore_path.read_text()
        # entry may be stored with or without the trailing slash
        lines = content.splitlines()
        if GITIGNORE_ENTRY in lines or GITIGNORE_ENTRY.rstrip("/") in lines:
            return False

        with gitignore_path.open("a") as f:
            if content and not content.endswith("\n"):
                f.write("\n")
            f.write(f"{GITIGNORE_ENTRY}\n")
    else:
        gitignore_path.write_text(f"{GITIGNORE_ENTRY}\n")

    return True


def copy_agent_templates(templates_src: Path, agents_dest: Path) -> list[str]:
    """Copy agent templates to _drspec/agents/.

    Args:
        templates_src: Source templates directory.
        agents_dest: Destination agents directory.

    Returns:
        List of copied template filenames and directories.
    """
    agents_dest.mkdir(parents=True, exist_ok=True)
    copied = []

    if templates_src.exists():
        for template_file in templates_src.glob("*.md"):
            dest_file = agents_dest / template_file.name
            shutil.copy2(template_file, dest_file)
            copied.append(template_file.name)

        for subdir in templates_src.iterdir():
            if subdir.is_dir():
                dest_subdir = agents_dest / subdir.name
                if dest_subdir.exists():
                    shutil.rmtree(dest_subdir)
                shutil.copytree(subdir, dest_subdir)
                copied.append(f"{subdir.name}/")

    return copied


def setup_ide_integrations(
    project_root: Path,
    selected_ides: list[str],
    codex_global: bool = False,
) -> dict:
    """Set up IDE integrations by generating launcher files.

    Args:
        project_root: Project root directory.
        selected_ides: List of IDE identifiers to set up.
        codex_global: If True, install Codex to ~/.codex/prompts/.

    Returns:
        Dictionary with IDE integration results.
    """
    from drspec.core.ide import IDE_REGISTRY
    from drspec.core.ide.codex import CodexSetup
    from drspec.core.ide.mcp_registration import register as register_mcp_server

    results = {}

    for ide_name in ["cursor", "claude-code", "github-copilot", "codex"]:
        if ide_name in selected_ides:
            if ide_name == "codex":
                setup = CodexSetup(global_install=codex_global)
            else:
                setup_class = IDE_REGISTRY.get(ide_name)
                if setup_class is None:
                    results[ide_name] = {"enabled": False, "error": "Unknown IDE"}
                    continue
                setup = setup_class()

            try:
                created_files = setup.setup(project_root)
                output_dir = setup.get_output_dir(project_root)
                results[ide_name] = {
                    "enabled": True,
                    "path": str(output_dir),
                    "files_created": len(created_files),
                }
                if hasattr(setup, "get_skill_path"):  # (Claude Code: the skill that says when to run the contract workflow)
                    results[ide_name]["skill"] = str(setup.get_skill_path(project_root).relative_to(project_root))
                # The tools themselves (`mcp__drspec__search` and the rest) come from the MCP server the client is
                # told to start, so the provider's MCP file gets the drspec entry too.
                registration = register_mcp_server(ide_name, project_root, codex_global)
                results[ide_name]["mcp_server"] = registration.as_dict()
            except Exception as e:
                results[ide_name] = {
                    "enabled": False,
                    "error": str(e),
                }
        else:
            results[ide_name] = {"enabled": False}

    return results


def mcp_notes(ide_results: dict) -> list:
    """One line per provider about its MCP server entry: what was written (and to restart the client), what was left
    alone and why, and the lines to add by hand when the file is outside the project."""
    notes = []
    for name, result in ide_results.items():
        registration = result.get("mcp_server") if isinstance(result, dict) else None
        if not registration or registration["state"] == "present":
            continue
        notes.append(f"{name}: {registration['message']}")
        notes.extend(f"    {line}" for line in registration.get("lines", []))
    return notes


def _get_output_settings(ctx: typer.Context) -> bool:
    """Get the json_output setting from context."""
    if ctx.obj is None:
        return False
    return ctx.obj.get("json_output", False)


@app.callback(invoke_without_command=True)
def init_command(
    ctx: typer.Context,
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="On an already-initialized project, overwrite the agent templates without asking. Never clears the database.",
    ),
    confidence_threshold: int = typer.Option(
        DEFAULT_CONFIDENCE_THRESHOLD,
        "--confidence-threshold",
        "-c",
        help=f"Confidence threshold for VERIFIED status (0-100, default: {DEFAULT_CONFIDENCE_THRESHOLD})",
        min=0,
        max=100,
    ),
    ide: Optional[List[str]] = typer.Option(
        None,
        "--ide",
        help="IDE to configure (cursor, claude-code, github-copilot, codex). Can be specified multiple times.",
    ),
    no_ide: bool = typer.Option(
        False,
        "--no-ide",
        help="Skip IDE integration entirely",
    ),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help="Skip all interactive prompts (use defaults or explicit flags)",
    ),
    codex_global: bool = typer.Option(
        False,
        "--codex-global",
        help="Install Codex prompts globally to ~/.codex/prompts/ instead of project folder",
    ),
    project_root_path: Optional[Path] = typer.Option(
        None,
        "--project-root",
        "-p",
        help="Project root directory (default: auto-detect or current directory)",
    ),
    scan: bool = typer.Option(
        False,
        "--scan",
        help=(
            "After the setup, scan the whole project at full speed and wait for it, as 'drspec scan' does "
            "(Ctrl-C cancels the scan; the setup stays). Without it the worker indexes the project in the "
            "background, at about 20% of the machine."
        ),
    ),
    no_wait: bool = typer.Option(
        False,
        "--no-wait",
        help="With --scan: queue the files at background priority and return at once, as 'scan --no-wait' does.",
    ),
    no_index: bool = typer.Option(
        False,
        "--no-index",
        help="Do not start the worker: the setup only. (The next command that needs the worker starts it.)",
    ),
) -> None:
    """Initialize DrSpec database and configuration in the current project.

    Creates _drspec/ folder with contracts.db and agent templates.
    Optionally generates IDE launcher files for Cursor, Claude Code, GitHub Copilot, and Codex.

    Examples:
        drspec init                           # Interactive mode
        drspec init --ide cursor              # Set up Cursor only
        drspec init --ide cursor --ide claude-code  # Multiple IDEs
        drspec init --no-ide                  # Skip IDE integration
        drspec init --non-interactive         # Use defaults, no prompts
        drspec init --force                   # Overwrite agent templates without asking
        drspec init --project-root /path/to/project  # Explicit project root
        drspec init --scan                    # Set up, then scan the whole project at full speed and wait
        drspec init --scan --no-wait          # Set up, queue the whole project and return at once
        drspec init --no-index                # Set up only; start no worker

    After the setup the worker is started, and it indexes the project in the background (about 20% of the machine,
    one parser when the machine is busy). 'drspec scan' finishes the index at full speed; the scan options
    (--deep-scan, --embed, --code-only, --docs-only) are options of 'drspec scan'.
    """
    from drspec.core.ide.prompts import detect_project_root, prompt_project_root, is_interactive

    json_output = _get_output_settings(ctx)
    cwd = Path.cwd()

    if project_root_path:
        project_root = project_root_path.expanduser().resolve()
        if not project_root.exists():
            project_root.mkdir(parents=True, exist_ok=True)
    else:
        detected_root = detect_project_root(cwd)

        if non_interactive:
            project_root = detected_root
        elif is_interactive():
            project_root = prompt_project_root(cwd, detected_root)
        else:
            project_root = detected_root
    drspec_folder = project_root / DRSPEC_FOLDER
    db_path = drspec_folder / DB_NAME
    agents_folder = drspec_folder / AGENTS_FOLDER

    if scan and no_index:
        output(error_response("INVALID_INPUT", "--scan needs the worker; it cannot be combined with --no-index."), json_output=json_output)
        raise typer.Exit(1)

    def finish(data: dict) -> None:
        """The result of a setup that worked: what was started, and with --scan the scan, as one result."""
        exit_code = 0
        if not no_index:
            data["indexing"] = start_indexing(db_path)
        if scan:
            show_progress = sys.stderr.isatty() and not json_output
            try:
                summary = scan_whole_project(
                    db_path, wait=not no_wait, on_progress=_progress_line if show_progress else None
                )
            except Exception as e:  # noqa: BLE001 - the setup is done; the scan's failure is part of the result
                data["scan"] = {"state": "failed", "error": str(e)}
                data["message"] = f"{data.get('message', 'DrSpec initialized')}; the scan failed: {e}"
                exit_code = 1
            else:
                if show_progress:
                    sys.stderr.write("\r\033[K")
                    sys.stderr.flush()
                if summary.get("publish_error"):
                    exit_code = 1
                elif summary.get("state") == "cancelled":
                    exit_code = 130
                data["scan"] = summary
                data["indexing"] = {"started": True, "message": scan_message(summary)}
        elif data.get("indexing", {}).get("started"):
            data["message"] = f"{data.get('message', 'DrSpec initialized')}; {data['indexing']['message']}"
        elif data.get("indexing", {}).get("warning"):
            data["warning"] = data["indexing"]["warning"]
        output(success_response(data), json_output=json_output)
        if exit_code:
            raise typer.Exit(exit_code)

    try:
        # resolved before the existing-files check below, since that check
        # depends on which IDEs are selected
        selected_ides: list[str] = []
        if no_ide:
            selected_ides = []
        elif ide:
            valid_ides = {"cursor", "claude-code", "github-copilot", "codex"}
            for ide_name in ide:
                if ide_name not in valid_ides:
                    output(error_response(
                        "INVALID_IDE",
                        f"Unknown IDE: {ide_name}. Valid options: {', '.join(sorted(valid_ides))}",
                        {"ide": ide_name, "valid_options": sorted(valid_ides)},
                    ), json_output=json_output)
                    raise typer.Exit(1)
            selected_ides = list(ide)
        elif not non_interactive:
            # deferred until after the overwrite check below
            pass

        is_reinit = drspec_folder.exists() and db_path.exists()

        if is_reinit and not force and is_worker_running(db_path) is not None and worker_is_outdated(db_path):
            # A worker from before an upgrade never ran the current rebuilds, and keeps
            # old code running on this file. Replace it; the next command starts a current one.
            try:
                stop_worker(db_path)
            except OSError as e:
                output(error_response("WORKER_OUTDATED", f"The worker runs older code than the installed one and could not be stopped: {e}. Run 'drspec worker stop'.", {"db_path": str(db_path)}), json_output=json_output)
                raise typer.Exit(1)

        if is_reinit and not force and is_worker_running(db_path) is None:
            # A current worker has already done this at its own start.
            try:
                ensure_embedding_layout(db_path)
                ensure_fk_layout(db_path)
                ensure_identity_layout(db_path)
            except MigrationError as e:
                output(error_response("MIGRATION_FAILED", str(e), {"db_path": str(db_path)}), json_output=json_output)
                raise typer.Exit(1)

        if is_reinit and not is_initialized_database(db_path):
            is_reinit = False  # an unusable DB is treated as a fresh init

        # overwrite confirmation must happen before IDE selection, so a user
        # who declines isn't asked IDE questions for nothing
        if is_reinit:
            existing_agents = 0
            if agents_folder.exists():
                existing_agents = len(list(agents_folder.glob("*.md")))

            if existing_agents > 0:
                from drspec.core.ide.prompts import prompt_yes_no

                should_overwrite = False
                if force:
                    should_overwrite = True
                elif non_interactive:
                    should_overwrite = False  # Safe default
                elif is_interactive():
                    print("\n⚠️  DrSpec is already initialized in this project.")
                    print("\nThe following files will be overwritten:")
                    print(f"  • _drspec/agents/*.md ({existing_agents} files)")
                    print()
                    should_overwrite = prompt_yes_no("Overwrite existing agent files?", default=False)
                else:
                    should_overwrite = False  # Non-TTY, safe default

                if not should_overwrite:
                    finish({
                        "message": "DrSpec already initialized (no changes made)",
                        "drspec_folder": str(drspec_folder),
                        "database": str(db_path),
                        "agents_folder": str(agents_folder),
                        "already_initialized": True,
                        "overwrite_skipped": True,
                    })
                    return

            if not ide and not no_ide and not non_interactive:
                from drspec.core.ide.prompts import prompt_ide_selection
                if is_interactive():
                    selected_ides = prompt_ide_selection(project_root)

            templates_path = get_templates_path()
            copied_templates = copy_agent_templates(templates_path, agents_folder)

            ide_results = {}
            if selected_ides:
                ide_results = setup_ide_integrations(
                    project_root, selected_ides, codex_global
                )

            finish({
                "message": "DrSpec already initialized",
                "drspec_folder": str(drspec_folder),
                "database": str(db_path),
                "agents_folder": str(agents_folder),
                "agents_updated": copied_templates,
                "ide_integrations": ide_results if ide_results else None,
                "mcp_server": mcp_notes(ide_results) or None,
                "already_initialized": True,
            })
            return

        if not ide and not no_ide and not non_interactive:
            from drspec.core.ide.prompts import prompt_ide_selection
            if is_interactive():
                selected_ides = prompt_ide_selection(project_root)

        drspec_folder.mkdir(parents=True, exist_ok=True)

        ensure_db_directory(db_path)
        conn = get_connection(db_path)

        init_schema(conn, rebuild=force)
        set_confidence_threshold(conn, confidence_threshold)

        conn.close()

        templates_path = get_templates_path()
        copied_templates = copy_agent_templates(templates_path, agents_folder)

        gitignore_updated = update_gitignore(project_root)

        ide_results = {}
        if selected_ides:
            ide_results = setup_ide_integrations(
                project_root, selected_ides, codex_global
            )

        finish({
            "message": "DrSpec initialized successfully",
            "drspec_folder": str(drspec_folder),
            "database": str(db_path),
            "agents_folder": str(agents_folder),
            "templates_copied": copied_templates,
            "ide_integrations": ide_results if ide_results else None,
            "mcp_server": mcp_notes(ide_results) or None,
            "gitignore_updated": gitignore_updated,
            "confidence_threshold": confidence_threshold,
        })

    except typer.Exit:
        raise

    except PermissionError as e:
        output(error_response(
            "PERMISSION_DENIED",
            f"Cannot create _drspec/ folder: {e}",
            {"path": str(drspec_folder)},
        ), json_output=json_output)
        raise typer.Exit(1)

    except Exception as e:
        output(error_response(
            "INIT_FAILED",
            f"Initialization failed: {e}",
            {"error_type": type(e).__name__},
        ), json_output=json_output)
        raise typer.Exit(1)
