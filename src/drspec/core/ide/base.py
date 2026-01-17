"""Base class for IDE-specific launcher generators."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TypedDict


class AgentMetadata(TypedDict):
    """Metadata for an agent used in launcher generation."""

    name: str  # e.g., "librarian"
    file: str  # e.g., "librarian.md"
    display_name: str  # e.g., "Librarian"
    persona: str  # e.g., "Iris"
    description: str  # e.g., "Codebase Navigator and Context Provider"


# Agent metadata for all DrSpec agents
AGENT_METADATA: list[AgentMetadata] = [
    {
        "name": "librarian",
        "file": "librarian.md",
        "display_name": "Librarian",
        "persona": "Iris",
        "description": "Codebase Navigator and Context Provider",
    },
    {
        "name": "proposer",
        "file": "proposer.md",
        "display_name": "Proposer",
        "persona": "Marcus",
        "description": "Contract Proposer in the Architect Council",
    },
    {
        "name": "critic",
        "file": "critic.md",
        "display_name": "Critic",
        "persona": "Diana",
        "description": "Contract Critic in the Architect Council",
    },
    {
        "name": "judge",
        "file": "judge.md",
        "display_name": "Judge",
        "persona": "Solomon",
        "description": "Final Arbiter in the Architect Council",
    },
    {
        "name": "debugger",
        "file": "debugger.md",
        "display_name": "Debugger",
        "persona": "Sherlock",
        "description": "Root Cause Investigator",
    },
    {
        "name": "vision-analyst",
        "file": "vision_analyst.md",
        "display_name": "Vision Analyst",
        "persona": "Aurora",
        "description": "Visual Pattern Detector",
    },
]

# Common activation template used by all IDEs
ACTIVATION_TEMPLATE = """You must fully embody this agent's persona and follow all activation instructions exactly as specified. NEVER break character until given an exit command.

<agent-activation CRITICAL="TRUE">
1. LOAD the FULL agent file from @_drspec/agents/{agent_file}
2. READ its entire contents - this contains the complete agent persona, menu, and instructions
3. Execute ALL activation steps exactly as written in the agent file
4. Follow the agent's persona and workflow precisely
5. Stay in character throughout the session
</agent-activation>"""


class BaseIdeSetup(ABC):
    """Base class for IDE-specific launcher generators."""

    def __init__(self, name: str, display_name: str):
        """Initialize IDE setup.

        Args:
            name: IDE identifier (e.g., "cursor", "claude-code").
            display_name: Human-readable IDE name (e.g., "Cursor").
        """
        self.name = name
        self.display_name = display_name

    @abstractmethod
    def get_output_dir(self, project_dir: Path) -> Path:
        """Get the output directory for launcher files.

        Args:
            project_dir: Project root directory.

        Returns:
            Path to the output directory.
        """
        pass

    @abstractmethod
    def generate_launcher(self, agent: AgentMetadata) -> str:
        """Generate launcher content for an agent.

        Args:
            agent: Agent metadata dictionary.

        Returns:
            Launcher file content as string.
        """
        pass

    @abstractmethod
    def get_file_extension(self) -> str:
        """Get file extension for launcher files.

        Returns:
            File extension including the dot (e.g., ".mdc").
        """
        pass

    def get_filename(self, agent: AgentMetadata) -> str:
        """Get filename for agent launcher.

        Args:
            agent: Agent metadata dictionary.

        Returns:
            Filename for the launcher.
        """
        return f"{agent['name']}{self.get_file_extension()}"

    def setup(self, project_dir: Path, agents: list[AgentMetadata] | None = None) -> list[str]:
        """Generate all launcher files.

        Args:
            project_dir: Project root directory.
            agents: List of agent metadata (defaults to AGENT_METADATA).

        Returns:
            List of created file paths (relative to project_dir).
        """
        if agents is None:
            agents = AGENT_METADATA

        output_dir = self.get_output_dir(project_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        created = []
        for agent in agents:
            content = self.generate_launcher(agent)
            filename = self.get_filename(agent)
            filepath = output_dir / filename
            filepath.write_text(content)
            # Return path relative to project_dir
            try:
                rel_path = filepath.relative_to(project_dir)
                created.append(str(rel_path))
            except ValueError:
                # If not relative (e.g., Codex global), use absolute
                created.append(str(filepath))

        return created

    def cleanup(self, project_dir: Path) -> int:
        """Remove old launcher files.

        Args:
            project_dir: Project root directory.

        Returns:
            Count of files removed.
        """
        output_dir = self.get_output_dir(project_dir)
        if not output_dir.exists():
            return 0

        count = 0
        ext = self.get_file_extension()
        for file in output_dir.iterdir():
            if file.is_file() and file.name.endswith(ext):
                file.unlink()
                count += 1

        return count
