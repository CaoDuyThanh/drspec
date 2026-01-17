"""Codex CLI launcher generator."""

from __future__ import annotations

from pathlib import Path

from drspec.core.ide.base import ACTIVATION_TEMPLATE, AgentMetadata, BaseIdeSetup


class CodexSetup(BaseIdeSetup):
    """Codex CLI launcher generator.

    Creates `~/.codex/prompts/drspec-*.md` (global) or
    `.codex/prompts/drspec-*.md` (project) files for Codex.
    """

    def __init__(self, global_install: bool = False) -> None:
        """Initialize Codex setup.

        Args:
            global_install: If True, install to ~/.codex/prompts/.
                           If False, install to .codex/prompts/.
        """
        super().__init__("codex", "Codex")
        self.global_install = global_install

    def get_output_dir(self, project_dir: Path) -> Path:
        """Get output directory for Codex prompts.

        Args:
            project_dir: Project root directory (ignored for global install).

        Returns:
            Path to prompts directory.
        """
        if self.global_install:
            return Path.home() / ".codex" / "prompts"
        return project_dir / ".codex" / "prompts"

    def get_file_extension(self) -> str:
        """Get file extension for Codex prompts.

        Returns:
            ".md" extension.
        """
        return ".md"

    def get_filename(self, agent: AgentMetadata) -> str:
        """Get filename for agent launcher.

        Codex uses `drspec-{name}.md` format.

        Args:
            agent: Agent metadata dictionary.

        Returns:
            Filename for the launcher.
        """
        return f"drspec-{agent['name']}{self.get_file_extension()}"

    def generate_launcher(self, agent: AgentMetadata) -> str:
        """Generate Codex prompt file.

        Args:
            agent: Agent metadata dictionary.

        Returns:
            Codex .md file content (no frontmatter, plain markdown).
        """
        activation = ACTIVATION_TEMPLATE.format(agent_file=agent["file"])

        return f"""# DrSpec {agent['display_name']} Agent ({agent['persona']})

{activation}
"""
