"""Tests for IDE launcher generators."""

from pathlib import Path


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
        assert len(AGENT_METADATA) == 6

    def test_agent_metadata_has_required_fields(self):
        required_fields = {"name", "file", "display_name", "persona", "description"}
        for agent in AGENT_METADATA:
            assert set(agent.keys()) == required_fields

    def test_agent_names_are_unique(self):
        names = [agent["name"] for agent in AGENT_METADATA]
        assert len(names) == len(set(names))

    def test_expected_agents_present(self):
        names = {agent["name"] for agent in AGENT_METADATA}
        expected = {"librarian", "proposer", "critic", "judge", "debugger", "vision-analyst"}
        assert names == expected


class TestIDERegistry:
    """Tests for IDE registry."""

    def test_registry_has_all_ides(self):
        expected = {"cursor", "claude-code", "github-copilot", "codex"}
        assert set(IDE_REGISTRY.keys()) == expected

    def test_registry_values_are_base_ide_setup(self):
        for ide_name, setup_class in IDE_REGISTRY.items():
            assert issubclass(setup_class, BaseIdeSetup)


class TestCursorSetup:
    """Tests for Cursor launcher generator."""

    def test_get_output_dir(self, tmp_path: Path):
        setup = CursorSetup()
        output_dir = setup.get_output_dir(tmp_path)
        assert output_dir == tmp_path / ".cursor" / "rules" / "drspec"

    def test_get_file_extension(self):
        setup = CursorSetup()
        assert setup.get_file_extension() == ".mdc"

    def test_generate_launcher_has_frontmatter(self):
        setup = CursorSetup()
        content = setup.generate_launcher(AGENT_METADATA[0])
        assert content.startswith("---\n")
        assert "alwaysApply: false" in content
        assert "globs:" in content

    def test_generate_launcher_has_activation(self):
        setup = CursorSetup()
        content = setup.generate_launcher(AGENT_METADATA[0])
        assert "<agent-activation CRITICAL=" in content
        assert "@_drspec/agents/librarian.md" in content

    def test_setup_creates_mdc_files(self, tmp_path: Path):
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
        setup = ClaudeCodeSetup()
        output_dir = setup.get_output_dir(tmp_path)
        assert output_dir == tmp_path / ".claude" / "commands" / "drspec"

    def test_get_file_extension(self):
        setup = ClaudeCodeSetup()
        assert setup.get_file_extension() == ".md"

    def test_generate_launcher_has_name_in_frontmatter(self):
        setup = ClaudeCodeSetup()
        content = setup.generate_launcher(AGENT_METADATA[0])
        assert "name: 'drspec-librarian'" in content

    def test_generate_launcher_has_description(self):
        setup = ClaudeCodeSetup()
        content = setup.generate_launcher(AGENT_METADATA[0])
        assert "description: 'DrSpec Librarian Agent" in content

    def test_setup_creates_md_files(self, tmp_path: Path):
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
        setup = GitHubCopilotSetup()
        output_dir = setup.get_output_dir(tmp_path)
        assert output_dir == tmp_path / ".github" / "agents"

    def test_get_file_extension(self):
        setup = GitHubCopilotSetup()
        assert setup.get_file_extension() == ".agent.md"

    def test_get_filename_has_drspec_prefix(self):
        setup = GitHubCopilotSetup()
        filename = setup.get_filename(AGENT_METADATA[0])
        assert filename == "drspec-librarian.agent.md"

    def test_generate_launcher_has_tools_array(self):
        setup = GitHubCopilotSetup()
        content = setup.generate_launcher(AGENT_METADATA[0])
        assert 'tools: ["changes","edit","fetch"' in content

    def test_generate_launcher_has_double_quotes(self):
        setup = GitHubCopilotSetup()
        content = setup.generate_launcher(AGENT_METADATA[0])
        assert 'description: "DrSpec Librarian Agent' in content

    def test_setup_creates_agent_md_files(self, tmp_path: Path):
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
        setup = CodexSetup(global_install=False)
        output_dir = setup.get_output_dir(tmp_path)
        assert output_dir == tmp_path / ".codex" / "prompts"

    def test_get_output_dir_global(self, tmp_path: Path, monkeypatch):
        """A global install goes to the person's home folder; here that is a folder of the test, so what is asserted does not depend on
        the machine, and nothing can be written to the real home (the guard of `tests/tools/outside_guard.py` would refuse it)."""
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        output_dir = CodexSetup(global_install=True).get_output_dir(tmp_path / "project")
        assert output_dir == home / ".codex" / "prompts"
        assert not output_dir.exists()  # (looking up the folder creates nothing)

    def test_a_global_setup_writes_only_under_the_home_folder(self, tmp_path: Path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        created = CodexSetup(global_install=True).setup(tmp_path / "project")
        assert len(created) == 6 and all(home / ".codex" / "prompts" in Path(path).parents for path in created)
        assert not (tmp_path / "project" / ".codex").exists()  # nothing in the project

    def test_get_file_extension(self):
        setup = CodexSetup()
        assert setup.get_file_extension() == ".md"

    def test_get_filename_has_drspec_prefix(self):
        setup = CodexSetup()
        filename = setup.get_filename(AGENT_METADATA[0])
        assert filename == "drspec-librarian.md"

    def test_generate_launcher_no_frontmatter(self):
        setup = CodexSetup()
        content = setup.generate_launcher(AGENT_METADATA[0])
        assert not content.startswith("---")
        assert content.startswith("# DrSpec Librarian Agent")

    def test_generate_launcher_has_persona(self):
        setup = CodexSetup()
        content = setup.generate_launcher(AGENT_METADATA[0])
        assert "(Iris)" in content

    def test_setup_creates_md_files_project(self, tmp_path: Path):
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
        setup = CursorSetup()

        setup.setup(tmp_path)
        output_dir = setup.get_output_dir(tmp_path)
        assert len(list(output_dir.glob("*.mdc"))) == 6

        removed = setup.cleanup(tmp_path)
        assert removed == 6
        assert len(list(output_dir.glob("*.mdc"))) == 0

    def test_cleanup_handles_missing_directory(self, tmp_path: Path):
        setup = CursorSetup()
        removed = setup.cleanup(tmp_path)
        assert removed == 0

    def test_cleanup_only_removes_correct_extension(self, tmp_path: Path):
        setup = CursorSetup()

        setup.setup(tmp_path)
        output_dir = setup.get_output_dir(tmp_path)

        other_file = output_dir / "other.txt"
        other_file.write_text("should not be deleted")

        setup.cleanup(tmp_path)
        assert other_file.exists()
