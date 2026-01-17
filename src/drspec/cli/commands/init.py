"""Init command - Initialize DrSpec in a project."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import List, Optional

import typer

from drspec.cli.output import output, success_response, error_response
from drspec.contracts import DEFAULT_CONFIDENCE_THRESHOLD, set_confidence_threshold
from drspec.core.resources import get_templates_path
from drspec.db.connection import ensure_db_directory, get_connection, init_schema

# Default paths
DRSPEC_FOLDER = "_drspec"
DB_NAME = "contracts.db"
AGENTS_FOLDER = "agents"
GITIGNORE_ENTRY = "_drspec/"


app = typer.Typer(
    name="init",
    help="Initialize DrSpec in the current project",
    no_args_is_help=False,
)


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
        # Check if already present (as line or with trailing newline)
        lines = content.splitlines()
        if GITIGNORE_ENTRY in lines or GITIGNORE_ENTRY.rstrip("/") in lines:
            return False

        # Append to existing file
        with gitignore_path.open("a") as f:
            # Add newline if file doesn't end with one
            if content and not content.endswith("\n"):
                f.write("\n")
            f.write(f"{GITIGNORE_ENTRY}\n")
    else:
        # Create new .gitignore
        gitignore_path.write_text(f"{GITIGNORE_ENTRY}\n")

    return True


def check_existing_files(project_root: Path, agents_folder: Path, selected_ides: list[str]) -> dict:
    """Check for existing config files that would be overwritten.

    Args:
        project_root: Project root directory.
        agents_folder: Path to agents folder.
        selected_ides: List of selected IDE identifiers.

    Returns:
        Dictionary with existing file counts by category.
    """
    existing = {
        "agents": 0,
        "cursor": 0,
        "claude-code": 0,
        "github-copilot": 0,
        "codex": 0,
    }

    # Check agents folder
    if agents_folder.exists():
        existing["agents"] = len(list(agents_folder.glob("*.md")))

    # Check IDE launcher folders
    if "cursor" in selected_ides:
        cursor_dir = project_root / ".cursor" / "rules" / "drspec"
        if cursor_dir.exists():
            existing["cursor"] = len(list(cursor_dir.glob("*.mdc")))

    if "claude-code" in selected_ides:
        claude_dir = project_root / ".claude" / "commands" / "drspec"
        if claude_dir.exists():
            existing["claude-code"] = len(list(claude_dir.glob("*.md")))

    if "github-copilot" in selected_ides:
        copilot_dir = project_root / ".github" / "agents"
        if copilot_dir.exists():
            existing["github-copilot"] = len(list(copilot_dir.glob("drspec-*.agent.md")))

    if "codex" in selected_ides:
        codex_dir = project_root / ".codex" / "prompts"
        if codex_dir.exists():
            existing["codex"] = len(list(codex_dir.glob("drspec-*.md")))

    return existing


def prompt_overwrite_confirmation(existing: dict, force: bool, non_interactive: bool) -> bool:
    """Prompt user to confirm overwriting existing files.

    Args:
        existing: Dictionary with existing file counts.
        force: If True, skip confirmation.
        non_interactive: If True and not force, skip overwrite.

    Returns:
        True if should proceed with overwrite, False otherwise.
    """
    from drspec.core.ide.prompts import prompt_yes_no, is_interactive

    # Count total existing files
    total = sum(existing.values())
    if total == 0:
        return True  # No existing files, proceed

    # Force flag bypasses confirmation
    if force:
        return True

    # Non-interactive mode without force = skip overwrite (safe default)
    if non_interactive:
        return False

    # Not in TTY = skip overwrite
    if not is_interactive():
        return False

    # Show warning and prompt
    print("\n⚠️  DrSpec is already initialized in this project.")
    print("\nThe following files will be overwritten:")

    if existing["agents"] > 0:
        print(f"  • _drspec/agents/*.md ({existing['agents']} files)")
    if existing["cursor"] > 0:
        print(f"  • .cursor/rules/drspec/*.mdc ({existing['cursor']} files)")
    if existing["claude-code"] > 0:
        print(f"  • .claude/commands/drspec/*.md ({existing['claude-code']} files)")
    if existing["github-copilot"] > 0:
        print(f"  • .github/agents/drspec-*.agent.md ({existing['github-copilot']} files)")
    if existing["codex"] > 0:
        print(f"  • .codex/prompts/drspec-*.md ({existing['codex']} files)")

    print()
    return prompt_yes_no("Overwrite existing files?", default=False)


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
        # Copy top-level .md files
        for template_file in templates_src.glob("*.md"):
            dest_file = agents_dest / template_file.name
            shutil.copy2(template_file, dest_file)
            copied.append(template_file.name)

        # Copy subdirectories (e.g., helpers/)
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

    results = {}

    for ide_name in ["cursor", "claude-code", "github-copilot", "codex"]:
        if ide_name in selected_ides:
            # Special handling for Codex global install
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
            except Exception as e:
                results[ide_name] = {
                    "enabled": False,
                    "error": str(e),
                }
        else:
            results[ide_name] = {"enabled": False}

    return results


def _get_output_settings(ctx: typer.Context) -> tuple[bool, bool]:
    """Get output settings from context.

    Returns:
        Tuple of (json_output, pretty).
    """
    if ctx.obj is None:
        return True, False
    return ctx.obj.get("json_output", True), ctx.obj.get("pretty", False)


@app.callback(invoke_without_command=True)
def init_command(
    ctx: typer.Context,
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Force re-initialization, bypass overwrite confirmation",
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
        drspec init --force                   # Overwrite without confirmation
        drspec init --project-root /path/to/project  # Explicit project root
    """
    from drspec.core.ide.prompts import detect_project_root, prompt_project_root, is_interactive

    json_output, pretty = _get_output_settings(ctx)
    cwd = Path.cwd()

    # Step 0: Determine project root
    if project_root_path:
        # Explicit path provided via CLI
        project_root = project_root_path.expanduser().resolve()
        if not project_root.exists():
            project_root.mkdir(parents=True, exist_ok=True)
    else:
        # Auto-detect project root
        detected_root = detect_project_root(cwd)

        if non_interactive:
            # Non-interactive: use detected root silently
            project_root = detected_root
        elif is_interactive():
            # Interactive: prompt if cwd != detected
            project_root = prompt_project_root(cwd, detected_root)
        else:
            # Non-TTY: use detected root
            project_root = detected_root
    drspec_folder = project_root / DRSPEC_FOLDER
    db_path = drspec_folder / DB_NAME
    agents_folder = drspec_folder / AGENTS_FOLDER

    try:
        # Determine selected IDEs (before checking for existing files)
        selected_ides: list[str] = []
        if no_ide:
            selected_ides = []
        elif ide:
            # Validate IDE names
            valid_ides = {"cursor", "claude-code", "github-copilot", "codex"}
            for ide_name in ide:
                if ide_name not in valid_ides:
                    output(error_response(
                        "INVALID_IDE",
                        f"Unknown IDE: {ide_name}. Valid options: {', '.join(sorted(valid_ides))}",
                        {"ide": ide_name, "valid_options": sorted(valid_ides)},
                    ), json_output=json_output, pretty=pretty)
                    raise typer.Exit(1)
            selected_ides = list(ide)
        elif not non_interactive:
            # Interactive mode - prompt for IDE selection (after overwrite check)
            # We'll do this later, after confirming overwrite
            pass

        # Check if already initialized
        is_reinit = drspec_folder.exists() and db_path.exists()

        if is_reinit:
            # Verify DB is valid
            try:
                conn = get_connection(db_path)
                conn.execute("SELECT 1 FROM artifacts LIMIT 1")
                conn.close()
            except Exception:
                # DB exists but is corrupt - treat as fresh init
                is_reinit = False

        # For re-init: check existing AGENT files and prompt for confirmation FIRST
        # (before asking about IDE selection - per architect spec)
        if is_reinit:
            # Step 1: Check for existing agent files FIRST
            existing_agents = 0
            if agents_folder.exists():
                existing_agents = len(list(agents_folder.glob("*.md")))

            # Step 2: Prompt for overwrite confirmation if agent files exist
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
                    # User declined overwrite - exit early
                    output(success_response({
                        "message": "DrSpec already initialized (no changes made)",
                        "drspec_folder": str(drspec_folder),
                        "database": str(db_path),
                        "agents_folder": str(agents_folder),
                        "already_initialized": True,
                        "overwrite_skipped": True,
                    }), json_output=json_output, pretty=pretty)
                    return

            # Step 3: THEN ask about IDE selection (only after user confirmed overwrite)
            if not ide and not no_ide and not non_interactive:
                from drspec.core.ide.prompts import prompt_ide_selection
                if is_interactive():
                    selected_ides = prompt_ide_selection(project_root)

            # Step 4: Copy agents and set up IDE integrations
            templates_path = get_templates_path()
            copied_templates = copy_agent_templates(templates_path, agents_folder)

            # Set up IDE integrations
            ide_results = {}
            if selected_ides:
                ide_results = setup_ide_integrations(
                    project_root, selected_ides, codex_global
                )

            output(success_response({
                "message": "DrSpec already initialized",
                "drspec_folder": str(drspec_folder),
                "database": str(db_path),
                "agents_folder": str(agents_folder),
                "agents_updated": copied_templates,
                "ide_integrations": ide_results if ide_results else None,
                "already_initialized": True,
            }), json_output=json_output, pretty=pretty)
            return

        # Fresh init - prompt for IDE selection if interactive
        if not ide and not no_ide and not non_interactive:
            from drspec.core.ide.prompts import prompt_ide_selection
            if is_interactive():
                selected_ides = prompt_ide_selection(project_root)

        # Create _drspec folder
        drspec_folder.mkdir(parents=True, exist_ok=True)

        # Ensure db directory and create connection
        ensure_db_directory(db_path)
        conn = get_connection(db_path)

        # Initialize schema (rebuild if force)
        init_schema(conn, rebuild=force)

        # Set confidence threshold in config
        set_confidence_threshold(conn, confidence_threshold)

        conn.close()

        # Copy agent templates
        templates_path = get_templates_path()
        copied_templates = copy_agent_templates(templates_path, agents_folder)

        # Update .gitignore
        gitignore_updated = update_gitignore(project_root)

        # Set up IDE integrations
        ide_results = {}
        if selected_ides:
            ide_results = setup_ide_integrations(
                project_root, selected_ides, codex_global
            )

        # Return success response
        output(success_response({
            "message": "DrSpec initialized successfully",
            "drspec_folder": str(drspec_folder),
            "database": str(db_path),
            "agents_folder": str(agents_folder),
            "templates_copied": copied_templates,
            "ide_integrations": ide_results if ide_results else None,
            "gitignore_updated": gitignore_updated,
            "confidence_threshold": confidence_threshold,
        }), json_output=json_output, pretty=pretty)

    except typer.Exit:
        # Re-raise typer.Exit without additional error message
        raise

    except PermissionError as e:
        output(error_response(
            "PERMISSION_DENIED",
            f"Cannot create _drspec/ folder: {e}",
            {"path": str(drspec_folder)},
        ), json_output=json_output, pretty=pretty)
        raise typer.Exit(1)

    except Exception as e:
        output(error_response(
            "INIT_FAILED",
            f"Initialization failed: {e}",
            {"error_type": type(e).__name__},
        ), json_output=json_output, pretty=pretty)
        raise typer.Exit(1)
