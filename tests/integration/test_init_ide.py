"""Integration tests for drspec init with IDE integration."""

import json
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.cli.commands.init import DRSPEC_FOLDER, AGENTS_FOLDER


runner = CliRunner()


class TestInitWithIDE:
    """Tests for drspec init with --ide flag."""

    def test_init_with_cursor_flag(self):
        """Test init creates Cursor rules with --ide cursor."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                result = runner.invoke(app, ["init", "--ide", "cursor"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["success"] is True

                # Check Cursor files created
                cursor_dir = Path.cwd() / ".cursor" / "rules" / "drspec"
                assert cursor_dir.exists()
                assert (cursor_dir / "librarian.mdc").exists()
                assert (cursor_dir / "proposer.mdc").exists()
                assert len(list(cursor_dir.glob("*.mdc"))) == 6

                # Check JSON response
                assert response["data"]["ide_integrations"]["cursor"]["enabled"] is True
                assert response["data"]["ide_integrations"]["cursor"]["files_created"] == 6

    def test_init_with_claude_code_flag(self):
        """Test init creates Claude Code commands with --ide claude-code."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                result = runner.invoke(app, ["init", "--ide", "claude-code"])

                assert result.exit_code == 0
                response = json.loads(result.output)

                # Check Claude Code files created
                claude_dir = Path.cwd() / ".claude" / "commands" / "drspec"
                assert claude_dir.exists()
                assert (claude_dir / "librarian.md").exists()
                assert len(list(claude_dir.glob("*.md"))) == 6

                # Check JSON response
                assert response["data"]["ide_integrations"]["claude-code"]["enabled"] is True

    def test_init_with_github_copilot_flag(self):
        """Test init creates GitHub Copilot agents with --ide github-copilot."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                result = runner.invoke(app, ["init", "--ide", "github-copilot"])

                assert result.exit_code == 0
                response = json.loads(result.output)

                # Check GitHub Copilot files created
                copilot_dir = Path.cwd() / ".github" / "agents"
                assert copilot_dir.exists()
                assert (copilot_dir / "drspec-librarian.agent.md").exists()
                assert len(list(copilot_dir.glob("*.agent.md"))) == 6

                # Check JSON response
                assert response["data"]["ide_integrations"]["github-copilot"]["enabled"] is True

    def test_init_with_codex_flag(self):
        """Test init creates Codex prompts with --ide codex."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                result = runner.invoke(app, ["init", "--ide", "codex"])

                assert result.exit_code == 0
                response = json.loads(result.output)

                # Check Codex files created (project-local by default)
                codex_dir = Path.cwd() / ".codex" / "prompts"
                assert codex_dir.exists()
                assert (codex_dir / "drspec-librarian.md").exists()
                assert len(list(codex_dir.glob("drspec-*.md"))) == 6

                # Check JSON response
                assert response["data"]["ide_integrations"]["codex"]["enabled"] is True

    def test_init_with_multiple_ides(self):
        """Test init with multiple --ide flags."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                result = runner.invoke(app, [
                    "init",
                    "--ide", "cursor",
                    "--ide", "claude-code",
                ])

                assert result.exit_code == 0
                response = json.loads(result.output)

                # Check both IDEs configured
                assert response["data"]["ide_integrations"]["cursor"]["enabled"] is True
                assert response["data"]["ide_integrations"]["claude-code"]["enabled"] is True
                assert response["data"]["ide_integrations"]["github-copilot"]["enabled"] is False
                assert response["data"]["ide_integrations"]["codex"]["enabled"] is False

                # Verify files exist
                cursor_dir = Path.cwd() / ".cursor" / "rules" / "drspec"
                claude_dir = Path.cwd() / ".claude" / "commands" / "drspec"
                assert cursor_dir.exists()
                assert claude_dir.exists()

    def test_init_with_invalid_ide(self):
        """Test init with invalid IDE name fails."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                result = runner.invoke(app, ["init", "--ide", "invalid-ide"])

                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["success"] is False
                assert response["error"]["code"] == "INVALID_IDE"
                assert "invalid-ide" in response["error"]["message"]


class TestInitNoIDE:
    """Tests for drspec init with --no-ide flag."""

    def test_init_no_ide_skips_integration(self):
        """Test --no-ide skips IDE integration."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                result = runner.invoke(app, ["init", "--no-ide"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["success"] is True

                # No IDE integration in response
                assert response["data"]["ide_integrations"] is None

                # No IDE folders created
                assert not (Path.cwd() / ".cursor").exists()
                assert not (Path.cwd() / ".claude").exists()
                assert not (Path.cwd() / ".github" / "agents").exists()
                assert not (Path.cwd() / ".codex").exists()


class TestInitNonInteractive:
    """Tests for drspec init with --non-interactive flag."""

    def test_init_non_interactive_skips_prompts(self):
        """Test --non-interactive skips all prompts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                result = runner.invoke(app, ["init", "--non-interactive"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["success"] is True

                # No IDE integration (no prompts means no selection)
                assert response["data"]["ide_integrations"] is None

    def test_init_non_interactive_with_ide_flag(self):
        """Test --non-interactive with explicit --ide flag works."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                result = runner.invoke(app, [
                    "init",
                    "--non-interactive",
                    "--ide", "cursor",
                ])

                assert result.exit_code == 0
                response = json.loads(result.output)

                # IDE configured via explicit flag
                assert response["data"]["ide_integrations"]["cursor"]["enabled"] is True


class TestReinitWithIDE:
    """Tests for re-running drspec init with IDE options."""

    def test_reinit_updates_ide_launchers(self):
        """Test re-init with --force updates IDE launchers."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                # First init without IDE
                runner.invoke(app, ["init", "--no-ide"])

                cursor_dir = Path.cwd() / ".cursor" / "rules" / "drspec"
                assert not cursor_dir.exists()

                # Re-init with IDE (use --force to bypass confirmation)
                result = runner.invoke(app, ["init", "--force", "--ide", "cursor"])
                assert result.exit_code == 0

                # Cursor files now exist
                assert cursor_dir.exists()
                assert len(list(cursor_dir.glob("*.mdc"))) == 6

    def test_reinit_adds_new_ide(self):
        """Test re-init with --force can add new IDE to existing setup."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                # First init with Cursor
                runner.invoke(app, ["init", "--ide", "cursor"])

                cursor_dir = Path.cwd() / ".cursor" / "rules" / "drspec"
                assert cursor_dir.exists()

                # Re-init with Claude Code (use --force to bypass confirmation)
                result = runner.invoke(app, ["init", "--force", "--ide", "claude-code"])
                assert result.exit_code == 0

                # Both exist now
                claude_dir = Path.cwd() / ".claude" / "commands" / "drspec"
                assert cursor_dir.exists()  # Still there
                assert claude_dir.exists()  # Added


class TestOverwriteConfirmation:
    """Tests for overwrite confirmation behavior."""

    def test_reinit_non_interactive_skips_overwrite(self):
        """Test --non-interactive skips overwrite on re-init."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                # First init
                runner.invoke(app, ["init", "--ide", "cursor"])

                # Modify agent file
                agents_folder = Path.cwd() / "_drspec" / "agents"
                librarian = agents_folder / "librarian.md"
                librarian.write_text("# Modified by user")

                # Re-init with --non-interactive should skip overwrite
                result = runner.invoke(app, ["init", "--non-interactive", "--ide", "cursor"])
                assert result.exit_code == 0
                response = json.loads(result.output)

                # Should report overwrite skipped
                assert response["data"]["overwrite_skipped"] is True

                # File should NOT be overwritten
                assert librarian.read_text() == "# Modified by user"

    def test_reinit_force_overwrites(self):
        """Test --force bypasses confirmation and overwrites."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                # First init
                runner.invoke(app, ["init", "--ide", "cursor"])

                # Modify agent file
                agents_folder = Path.cwd() / "_drspec" / "agents"
                librarian = agents_folder / "librarian.md"
                original_content = librarian.read_text()
                librarian.write_text("# Modified by user")

                # Re-init with --force should overwrite
                result = runner.invoke(app, ["init", "--force", "--ide", "cursor"])
                assert result.exit_code == 0
                response = json.loads(result.output)

                # Should NOT have overwrite_skipped
                assert "overwrite_skipped" not in response["data"]

                # File should be overwritten
                assert librarian.read_text() == original_content

    def test_fresh_init_no_confirmation_needed(self):
        """Test fresh init doesn't ask for confirmation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                # Fresh init should work without confirmation
                result = runner.invoke(app, ["init", "--ide", "cursor"])
                assert result.exit_code == 0
                response = json.loads(result.output)

                # Should NOT have overwrite_skipped
                assert "overwrite_skipped" not in response["data"]
                assert response["data"]["message"] == "DrSpec initialized successfully"


class TestLauncherContent:
    """Tests for generated launcher file content."""

    def test_cursor_launcher_content(self):
        """Test Cursor launcher has correct content."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init", "--ide", "cursor"])

                librarian = Path.cwd() / ".cursor" / "rules" / "drspec" / "librarian.mdc"
                content = librarian.read_text()

                # Check frontmatter
                assert "---" in content
                assert "description: DrSpec Librarian Agent (Iris)" in content
                assert "alwaysApply: false" in content
                assert "globs:" in content

                # Check activation
                assert "<agent-activation CRITICAL=" in content
                assert "@_drspec/agents/librarian.md" in content

    def test_claude_code_launcher_content(self):
        """Test Claude Code launcher has correct content."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init", "--ide", "claude-code"])

                librarian = Path.cwd() / ".claude" / "commands" / "drspec" / "librarian.md"
                content = librarian.read_text()

                # Check frontmatter with slash command name
                assert "name: 'drspec-librarian'" in content
                assert "description: 'DrSpec Librarian Agent" in content

    def test_github_copilot_launcher_content(self):
        """Test GitHub Copilot launcher has correct content."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init", "--ide", "github-copilot"])

                librarian = Path.cwd() / ".github" / "agents" / "drspec-librarian.agent.md"
                content = librarian.read_text()

                # Check tools array
                assert 'tools: ["changes","edit","fetch"' in content
                assert "# DrSpec Librarian Agent" in content

    def test_codex_launcher_content(self):
        """Test Codex launcher has correct content (no frontmatter)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init", "--ide", "codex"])

                librarian = Path.cwd() / ".codex" / "prompts" / "drspec-librarian.md"
                content = librarian.read_text()

                # No frontmatter
                assert not content.startswith("---")
                assert content.startswith("# DrSpec Librarian Agent (Iris)")
