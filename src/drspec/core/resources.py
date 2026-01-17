"""Resource access utilities for DrSpec.

Handles path resolution for bundled resources, working correctly in both
development mode (running from source) and production mode (PyInstaller binary).
"""

from __future__ import annotations

import sys
from pathlib import Path


def _get_base_path() -> Path:
    """Get the base path for bundled resources.

    Returns:
        Path to base directory containing agents and other resources.
        - In PyInstaller frozen mode: sys._MEIPASS
        - In development mode: src/ directory (containing agents/)
    """
    if getattr(sys, "frozen", False):
        # Running as PyInstaller bundle
        return Path(sys._MEIPASS)  # type: ignore
    else:
        # Running in development - go up from core/resources.py to src/
        # src/drspec/core/resources.py -> src/drspec/core -> src/drspec -> src
        return Path(__file__).parent.parent.parent


def get_templates_path() -> Path:
    """Get the path to bundled agent templates.

    Returns:
        Path to agents/ directory.

    Example:
        >>> templates = get_templates_path()
        >>> (templates / "librarian.md").exists()
        True
    """
    if getattr(sys, "frozen", False):
        # In frozen mode, agents are bundled at root level
        return Path(sys._MEIPASS) / "agents"  # type: ignore
    else:
        # In development/installed mode, agents are in drspec/agents/
        # Go from core/resources.py -> core -> drspec -> drspec/agents
        return Path(__file__).parent.parent / "agents"


def get_schema_path() -> Path:
    """Get the path to the database schema file.

    Returns:
        Path to drspec/db/schema.sql file.

    Note:
        In frozen mode, this is in the bundled drspec/db directory.
        In development mode, this is in src/drspec/db directory.
    """
    if getattr(sys, "frozen", False):
        return _get_base_path() / "drspec" / "db" / "schema.sql"
    else:
        return Path(__file__).parent.parent / "db" / "schema.sql"


def list_template_files() -> list[str]:
    """List all available agent template files.

    Returns:
        List of template filenames (e.g., ["librarian.md", "proposer.md", ...]).
    """
    templates_path = get_templates_path()
    if not templates_path.exists():
        return []
    return [f.name for f in templates_path.glob("*.md")]
