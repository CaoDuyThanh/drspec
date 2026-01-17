"""GitHub Copilot IDE launcher generator."""

from __future__ import annotations

from pathlib import Path

from drspec.core.ide.base import ACTIVATION_TEMPLATE, AgentMetadata, BaseIdeSetup


class GitHubCopilotSetup(BaseIdeSetup):
    """GitHub Copilot IDE launcher generator.

    Creates `.github/agents/drspec-*.agent.md` files for GitHub Copilot.
    """

    def __init__(self) -> None:
        """Initialize GitHub Copilot setup."""
        super().__init__("github-copilot", "GitHub Copilot")

    def get_output_dir(self, project_dir: Path) -> Path:
        """Get output directory for GitHub Copilot agents.

        Args:
            project_dir: Project root directory.

        Returns:
            Path to .github/agents/ directory.
        """
        return project_dir / ".github" / "agents"

    def get_file_extension(self) -> str:
        """Get file extension for GitHub Copilot agents.

        Returns:
            ".agent.md" extension.
        """
        return ".agent.md"

    def get_filename(self, agent: AgentMetadata) -> str:
        """Get filename for agent launcher.

        GitHub Copilot uses `drspec-{name}.agent.md` format.

        Args:
            agent: Agent metadata dictionary.

        Returns:
            Filename for the launcher.
        """
        return f"drspec-{agent['name']}{self.get_file_extension()}"

    def generate_launcher(self, agent: AgentMetadata) -> str:
        """Generate GitHub Copilot agent file.

        Args:
            agent: Agent metadata dictionary.

        Returns:
            GitHub Copilot .agent.md file content with YAML frontmatter.
        """
        activation = ACTIVATION_TEMPLATE.format(agent_file=agent["file"])

        # GitHub Copilot requires double quotes and JSON array for tools
        return f'''---
description: "DrSpec {agent['display_name']} Agent - {agent['description']} ({agent['persona']})"
tools: ["changes","edit","fetch","problems","runCommands","runTasks","search","todos"]
---

# DrSpec {agent['display_name']} Agent

{activation}
'''
