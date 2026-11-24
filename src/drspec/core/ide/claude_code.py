"""Claude Code IDE launcher generator."""

from __future__ import annotations

from pathlib import Path

from drspec.core import contract_guidance
from drspec.core.ide.base import ACTIVATION_TEMPLATE, AgentMetadata, BaseIdeSetup


class ClaudeCodeSetup(BaseIdeSetup):
    """Claude Code IDE launcher generator.

    Creates `.claude/commands/drspec/*.md` files for Claude Code CLI.
    """

    def __init__(self) -> None:
        """Initialize Claude Code setup."""
        super().__init__("claude-code", "Claude Code")

    def get_output_dir(self, project_dir: Path) -> Path:
        """Get output directory for Claude Code commands.

        Args:
            project_dir: Project root directory.

        Returns:
            Path to .claude/commands/drspec/ directory.
        """
        return project_dir / ".claude" / "commands" / "drspec"

    def get_file_extension(self) -> str:
        """Get file extension for Claude Code commands.

        Returns:
            ".md" extension.
        """
        return ".md"

    def get_skill_path(self, project_dir: Path) -> Path:
        """The skill that says when to run the contract workflow: `.claude/skills/drspec-contracts/SKILL.md`."""
        return project_dir / ".claude" / "skills" / contract_guidance.SKILL_NAME / "SKILL.md"

    def write_skill(self, project_dir: Path) -> Path:
        """Write the guidance (when to run the contract workflow in a sub-agent, how, and the cap). The file is drspec's own, so a
        second `init` replaces it: it is updated, never repeated."""
        path = self.get_skill_path(project_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contract_guidance.skill_file())
        return path

    def setup(self, project_dir: Path, agents=None) -> list[str]:
        """The launcher files (returned), and the skill beside them."""
        created = super().setup(project_dir, agents)
        self.write_skill(project_dir)
        return created

    def generate_launcher(self, agent: AgentMetadata) -> str:
        """Generate Claude Code command file for an agent.

        Args:
            agent: Agent metadata dictionary.

        Returns:
            Claude Code .md file content with YAML frontmatter.
        """
        activation = ACTIVATION_TEMPLATE.format(agent_file=agent["file"])

        return f"""---
name: 'drspec-{agent['name']}'
description: 'DrSpec {agent['display_name']} Agent - {agent['description']} ({agent['persona']})'
---

{activation}
"""
