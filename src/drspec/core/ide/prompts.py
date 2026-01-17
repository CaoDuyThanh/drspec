"""Interactive prompts for IDE selection during init using built-in input()."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional


# IDE choices for selection
IDE_CHOICES = [
    ("Cursor", "cursor"),
    ("Claude Code", "claude-code"),
    ("GitHub Copilot", "github-copilot"),
    ("Codex", "codex"),
]


def is_interactive() -> bool:
    """Check if running in an interactive terminal.

    Returns:
        True if stdin is a TTY.
    """
    return sys.stdin.isatty()


def detect_existing_ides(project_dir: Path) -> list[str]:
    """Detect IDEs that already have config folders.

    Args:
        project_dir: Project root directory.

    Returns:
        List of detected IDE identifiers.
    """
    detected = []

    if (project_dir / ".cursor").exists():
        detected.append("cursor")
    if (project_dir / ".claude").exists():
        detected.append("claude-code")
    if (project_dir / ".github").exists():
        detected.append("github-copilot")
    if (project_dir / ".codex").exists() or Path.home().joinpath(".codex").exists():
        detected.append("codex")

    return detected


def prompt_multi_select(prompt: str, choices: List[tuple[str, str]], preselected: List[str] = None) -> List[str]:
    """Prompt for multiple selections using built-in input().

    Args:
        prompt: Question to display.
        choices: List of (display_name, value) tuples.
        preselected: List of values to mark as pre-selected.

    Returns:
        List of selected values.
    """
    if preselected is None:
        preselected = []

    print(f"\n{prompt}")
    print("(Enter numbers separated by commas, e.g., '1,2'. Press Enter to skip.)")

    for i, (display_name, value) in enumerate(choices, 1):
        marker = "*" if value in preselected else " "
        print(f"  {i}. [{marker}] {display_name}")

    print(f"  {len(choices) + 1}. [ ] None (skip IDE integration)")

    if preselected:
        print(f"\n  * = detected existing config")

    while True:
        try:
            response = input("\nSelect options: ").strip()

            # Empty response = skip
            if not response:
                return []

            indices = [int(x.strip()) for x in response.split(",")]

            # Check for "None" option
            if len(choices) + 1 in indices:
                return []

            selected = []
            for idx in indices:
                if 1 <= idx <= len(choices):
                    selected.append(choices[idx - 1][1])  # Return value, not display name
                else:
                    raise ValueError(f"Invalid option: {idx}")

            return selected

        except ValueError as e:
            print(f"Invalid input: {e}. Try again.")


def prompt_choice(prompt: str, choices: List[tuple[str, str]], default: Optional[str] = None) -> str:
    """Prompt for single choice selection using built-in input().

    Args:
        prompt: Question to display.
        choices: List of (display_name, value) tuples.
        default: Default value if user presses Enter.

    Returns:
        Selected value.
    """
    print(f"\n{prompt}")

    for i, (display_name, value) in enumerate(choices, 1):
        default_marker = " (default)" if value == default else ""
        print(f"  {i}. {display_name}{default_marker}")

    while True:
        try:
            response = input("\nSelect option: ").strip()

            # Empty response = default
            if not response and default:
                return default

            idx = int(response)
            if 1 <= idx <= len(choices):
                return choices[idx - 1][1]

            print(f"Invalid option: {idx}. Try again.")

        except ValueError:
            print("Invalid input. Enter a number.")


def prompt_yes_no(message: str, default: bool = True) -> bool:
    """Prompt user for yes/no confirmation using built-in input().

    Args:
        message: Question to ask.
        default: Default value if user just presses Enter.

    Returns:
        True for yes, False for no.
    """
    default_str = "Y/n" if default else "y/N"
    response = input(f"{message} ({default_str}): ").strip().lower()

    if response in ("y", "yes"):
        return True
    elif response in ("n", "no"):
        return False

    return default


def prompt_ide_selection(project_dir: Path) -> list[str]:
    """Prompt user to select IDEs for integration.

    Args:
        project_dir: Project root directory (for auto-detection).

    Returns:
        List of selected IDE identifiers, empty if non-interactive.
    """
    if not is_interactive():
        return []

    # Detect existing IDEs to pre-select
    detected = detect_existing_ides(project_dir)

    return prompt_multi_select(
        "Which AI tools do you use?",
        IDE_CHOICES,
        preselected=detected,
    )


def prompt_codex_location() -> str:
    """Prompt user for Codex installation location.

    Returns:
        "global" for ~/.codex/prompts/ or "project" for .codex/prompts/.
    """
    if not is_interactive():
        return "project"  # Default to project-local

    return prompt_choice(
        "Where should Codex prompts be installed?",
        [
            ("Project folder (.codex/prompts/)", "project"),
            ("Global (~/.codex/prompts/)", "global"),
        ],
        default="project",
    )


def prompt_confidence_threshold(current: int = 70) -> int:
    """Prompt user for confidence threshold.

    Args:
        current: Current/default threshold value.

    Returns:
        Selected threshold value (0-100).
    """
    if not is_interactive():
        return current

    while True:
        response = input(f"Confidence threshold for VERIFIED status (0-100) [{current}]: ").strip()

        if not response:
            return current

        try:
            value = int(response)
            if 0 <= value <= 100:
                return value
            print("Value must be between 0 and 100.")
        except ValueError:
            print("Invalid input. Enter a number.")


# Project root markers (in order of priority)
PROJECT_ROOT_MARKERS = [
    ".git",           # Git repository
    "pyproject.toml", # Python project
    "package.json",   # Node.js project
    "Cargo.toml",     # Rust project
    "go.mod",         # Go project
    "pom.xml",        # Maven project
    "build.gradle",   # Gradle project
    "Makefile",       # Make project
]


def detect_project_root(cwd: Path) -> Path:
    """Detect project root by looking for markers.

    Walks up from cwd looking for .git, pyproject.toml, package.json, etc.

    Args:
        cwd: Current working directory.

    Returns:
        Detected project root, or cwd if no markers found.
    """
    current = cwd.resolve()

    while current != current.parent:
        for marker in PROJECT_ROOT_MARKERS:
            if (current / marker).exists():
                return current
        current = current.parent

    # No markers found, default to current directory
    return cwd.resolve()


def prompt_project_root(cwd: Path, detected: Path) -> Path:
    """Prompt user to confirm or change project root.

    ALWAYS prompts for input, even when cwd == detected.
    Shows detected path as default - user can press Enter to accept.

    Args:
        cwd: Current working directory (unused, kept for API compatibility).
        detected: Auto-detected project root.

    Returns:
        Selected project root path.
    """
    if not is_interactive():
        # Non-interactive: use detected
        return detected

    # Always prompt - show detected as default
    response = input(f"Enter project root path [{detected}]: ").strip()

    if not response:
        print(f"\nInitializing DrSpec at {detected}\n")
        return detected

    selected = Path(response).expanduser().resolve()

    # Validate the path exists
    if not selected.exists():
        print(f"Warning: Path does not exist: {selected}")
        print(f"Creating directory...")
        selected.mkdir(parents=True, exist_ok=True)

    print(f"\nInitializing DrSpec at {selected}\n")
    return selected
