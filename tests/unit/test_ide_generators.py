"""Tests for IDE launcher generators."""

import tempfile
from pathlib import Path

import pytest

from drspec.core.ide import (
    AGENT_METADATA,
    CursorSetup,
    ClaudeCodeSetup,
    GitHubCopilotSetup,
    CodexSetup,
    IDE_REGISTRY,
)
from drspec.core.ide.base import BaseIdeSetup


class TestAgentMetadata:
    """Tests for agent metadata."""

    def test_agent_metadata_has_all_agents(self):
        """Test that all 6 agents are defined."""
        assert len(AGENT_METADATA) == 6

    def test_agent_metadata_has_required_fields(self):
        """Test that each agent has required fields."""
        required_fields = {"name", "file", "display_name", "persona", "description"}
        for agent in AGENT_METADATA:
            assert set(agent.keys()) == required_fields

    def test_agent_names_are_unique(self):
        """Test that agent names are unique."""
        names = [agent["name"] for agent in AGENT_METADATA]
        assert len(names) == len(set(names))

    def test_expected_agents_present(self):
        """Test that expected agents are present."""
        names = {agent["name"] for agent in AGENT_METADATA}
        expected = {"librarian", "proposer", "critic", "judge", "debugger", "vision-analyst"}
        assert names == expected


class TestIDERegistry:
    """Tests for IDE registry."""

    def test_registry_has_all_ides(self):
        """Test that all supported IDEs are in registry."""
        expected = {"cursor", "claude-code", "github-copilot", "codex"}
        assert set(IDE_REGISTRY.keys()) == expected

    def test_registry_values_are_base_ide_setup(self):
        """Test that all registry values inherit from BaseIdeSetup."""
        for ide_name, setup_class in IDE_REGISTRY.items():
            assert issubclass(setup_class, BaseIdeSetup)


class TestCursorSetup:
    """Tests for Cursor launcher generator."""

    def test_get_output_dir(self, tmp_path: Path):
        """Test output directory is .cursor/rules/drspec/."""
        setup = CursorSetup()
        output_dir = setup.get_output_dir(tmp_path)
        assert output_dir == tmp_path / ".cursor" / "rules" / "drspec"

    def test_get_file_extension(self):
        """Test file extension is .mdc."""
        setup = CursorSetup()
        assert setup.get_file_extension() == ".mdc"

    def test_generate_launcher_has_frontmatter(self):
        """Test generated launcher has YAML frontmatter."""
        setup = CursorSetup()
        content = setup.generate_launcher(AGENT_METADATA[0])
        assert content.startswith("---\n")
        assert "alwaysApply: false" in content
        assert "globs:" in content

    def test_generate_launcher_has_activation(self):
        """Test generated launcher has activation instructions."""
        setup = CursorSetup()
        content = setup.generate_launcher(AGENT_METADATA[0])
        assert "<agent-activation CRITICAL=" in content
        assert "@_drspec/agents/librarian.md" in content

    def test_setup_creates_mdc_files(self, tmp_path: Path):
        """Test setup creates .mdc files for all agents."""
        setup = CursorSetup()
        created = setup.setup(tmp_path)

        assert len(created) == 6
        output_dir = setup.get_output_dir(tmp_path)
        assert output_dir.exists()

        for agent in AGENT_METADATA:
            filename = f"{agent['name']}.mdc"
            assert (output_dir / filename).exists()


class TestClaudeCodeSetup:
    """Tests for Claude Code launcher generator."""

    def test_get_output_dir(self, tmp_path: Path):
        """Test output directory is .claude/commands/drspec/."""
        setup = ClaudeCodeSetup()
        output_dir = setup.get_output_dir(tmp_path)
        assert output_dir == tmp_path / ".claude" / "commands" / "drspec"

    def test_get_file_extension(self):
        """Test file extension is .md."""
        setup = ClaudeCodeSetup()
        assert setup.get_file_extension() == ".md"

    def test_generate_launcher_has_name_in_frontmatter(self):
        """Test generated launcher has name field for slash command."""
        setup = ClaudeCodeSetup()
        content = setup.generate_launcher(AGENT_METADATA[0])
        assert "name: 'drspec-librarian'" in content

    def test_generate_launcher_has_description(self):
        """Test generated launcher has description field."""
        setup = ClaudeCodeSetup()
        content = setup.generate_launcher(AGENT_METADATA[0])
        assert "description: 'DrSpec Librarian Agent" in content

    def test_setup_creates_md_files(self, tmp_path: Path):
        """Test setup creates .md files for all agents."""
        setup = ClaudeCodeSetup()
        created = setup.setup(tmp_path)

        assert len(created) == 6
        output_dir = setup.get_output_dir(tmp_path)
        assert output_dir.exists()

        for agent in AGENT_METADATA:
            filename = f"{agent['name']}.md"
            assert (output_dir / filename).exists()


class TestGitHubCopilotSetup:
    """Tests for GitHub Copilot launcher generator."""

    def test_get_output_dir(self, tmp_path: Path):
        """Test output directory is .github/agents/."""
        setup = GitHubCopilotSetup()
        output_dir = setup.get_output_dir(tmp_path)
        assert output_dir == tmp_path / ".github" / "agents"

    def test_get_file_extension(self):
        """Test file extension is .agent.md."""
        setup = GitHubCopilotSetup()
        assert setup.get_file_extension() == ".agent.md"

    def test_get_filename_has_drspec_prefix(self):
        """Test filename has drspec- prefix."""
        setup = GitHubCopilotSetup()
        filename = setup.get_filename(AGENT_METADATA[0])
        assert filename == "drspec-librarian.agent.md"

    def test_generate_launcher_has_tools_array(self):
        """Test generated launcher has tools array."""
        setup = GitHubCopilotSetup()
        content = setup.generate_launcher(AGENT_METADATA[0])
        assert 'tools: ["changes","edit","fetch"' in content

    def test_generate_launcher_has_double_quotes(self):
        """Test generated launcher uses double quotes in YAML."""
        setup = GitHubCopilotSetup()
        content = setup.generate_launcher(AGENT_METADATA[0])
        assert 'description: "DrSpec Librarian Agent' in content

    def test_setup_creates_agent_md_files(self, tmp_path: Path):
        """Test setup creates .agent.md files for all agents."""
        setup = GitHubCopilotSetup()
        created = setup.setup(tmp_path)

        assert len(created) == 6
        output_dir = setup.get_output_dir(tmp_path)
        assert output_dir.exists()

        for agent in AGENT_METADATA:
            filename = f"drspec-{agent['name']}.agent.md"
            assert (output_dir / filename).exists()


class TestCodexSetup:
    """Tests for Codex launcher generator."""

    def test_get_output_dir_project(self, tmp_path: Path):
        """Test project-local output directory is .codex/prompts/."""
        setup = CodexSetup(global_install=False)
        output_dir = setup.get_output_dir(tmp_path)
        assert output_dir == tmp_path / ".codex" / "prompts"

    def test_get_output_dir_global(self, tmp_path: Path):
        """Test global output directory is ~/.codex/prompts/."""
        setup = CodexSetup(global_install=True)
        output_dir = setup.get_output_dir(tmp_path)
        assert output_dir == Path.home() / ".codex" / "prompts"

    def test_get_file_extension(self):
        """Test file extension is .md."""
        setup = CodexSetup()
        assert setup.get_file_extension() == ".md"

    def test_get_filename_has_drspec_prefix(self):
        """Test filename has drspec- prefix."""
        setup = CodexSetup()
        filename = setup.get_filename(AGENT_METADATA[0])
        assert filename == "drspec-librarian.md"

    def test_generate_launcher_no_frontmatter(self):
        """Test generated launcher has no YAML frontmatter."""
        setup = CodexSetup()
        content = setup.generate_launcher(AGENT_METADATA[0])
        assert not content.startswith("---")
        assert content.startswith("# DrSpec Librarian Agent")

    def test_generate_launcher_has_persona(self):
        """Test generated launcher includes persona name."""
        setup = CodexSetup()
        content = setup.generate_launcher(AGENT_METADATA[0])
        assert "(Iris)" in content

    def test_setup_creates_md_files_project(self, tmp_path: Path):
        """Test setup creates .md files in project folder."""
        setup = CodexSetup(global_install=False)
        created = setup.setup(tmp_path)

        assert len(created) == 6
        output_dir = setup.get_output_dir(tmp_path)
        assert output_dir.exists()

        for agent in AGENT_METADATA:
            filename = f"drspec-{agent['name']}.md"
            assert (output_dir / filename).exists()


class TestBaseIdeSetupCleanup:
    """Tests for BaseIdeSetup cleanup functionality."""

    def test_cleanup_removes_launcher_files(self, tmp_path: Path):
        """Test cleanup removes launcher files."""
        setup = CursorSetup()

        # Create files
        setup.setup(tmp_path)
        output_dir = setup.get_output_dir(tmp_path)
        assert len(list(output_dir.glob("*.mdc"))) == 6

        # Cleanup
        removed = setup.cleanup(tmp_path)
        assert removed == 6
        assert len(list(output_dir.glob("*.mdc"))) == 0

    def test_cleanup_handles_missing_directory(self, tmp_path: Path):
        """Test cleanup handles missing directory gracefully."""
        setup = CursorSetup()
        removed = setup.cleanup(tmp_path)
        assert removed == 0

    def test_cleanup_only_removes_correct_extension(self, tmp_path: Path):
        """Test cleanup only removes files with correct extension."""
        setup = CursorSetup()

        # Create files
        setup.setup(tmp_path)
        output_dir = setup.get_output_dir(tmp_path)

        # Add a file with different extension
        other_file = output_dir / "other.txt"
        other_file.write_text("should not be deleted")

        # Cleanup
        setup.cleanup(tmp_path)
        assert other_file.exists()
