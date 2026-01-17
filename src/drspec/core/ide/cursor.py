"""Cursor IDE launcher generator."""

from __future__ import annotations

from pathlib import Path

from drspec.core.ide.base import ACTIVATION_TEMPLATE, AgentMetadata, BaseIdeSetup


class CursorSetup(BaseIdeSetup):
    """Cursor IDE launcher generator.

    Creates `.cursor/rules/drspec/*.mdc` files for Cursor IDE.
    """

    def __init__(self) -> None:
        """Initialize Cursor setup."""
        super().__init__("cursor", "Cursor")

    def get_output_dir(self, project_dir: Path) -> Path:
        """Get output directory for Cursor rules.

        Args:
            project_dir: Project root directory.

        Returns:
            Path to .cursor/rules/drspec/ directory.
        """
        return project_dir / ".cursor" / "rules" / "drspec"

    def get_file_extension(self) -> str:
        """Get file extension for Cursor rules.

        Returns:
            ".mdc" extension.
        """
        return ".mdc"

    def generate_launcher(self, agent: AgentMetadata) -> str:
        """Generate Cursor rule file for an agent.

        Args:
            agent: Agent metadata dictionary.

        Returns:
            Cursor .mdc file content.
        """
        activation = ACTIVATION_TEMPLATE.format(agent_file=agent["file"])

        return f"""---
description: DrSpec {agent['display_name']} Agent ({agent['persona']})
globs:
alwaysApply: false
---

{activation}
"""
