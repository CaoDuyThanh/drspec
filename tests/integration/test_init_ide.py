import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from drspec.cli.app import app

pytestmark = pytest.mark.usefixtures("fixed_launcher")  # `drspec` is looked up on the PATH: the answer is fixed, not the machine's

runner = CliRunner()


class TestInitWithIDE:
    def test_init_with_cursor_flag(self, tmp_path):
        tmpdir = str(tmp_path)
        with runner.isolated_filesystem(temp_dir=tmpdir):
            result = runner.invoke(app, ["--json", "init", "--project-root", str(Path.cwd()), "--ide", "cursor"])

            assert result.exit_code == 0
            response = json.loads(result.output)
            assert response["success"] is True

            cursor_dir = Path.cwd() / ".cursor" / "rules" / "drspec"
            assert cursor_dir.exists()
            assert (cursor_dir / "librarian.mdc").exists()
            assert (cursor_dir / "proposer.mdc").exists()
            assert len(list(cursor_dir.glob("*.mdc"))) == 6

            assert response["data"]["ide_integrations"]["cursor"]["enabled"] is True
            assert response["data"]["ide_integrations"]["cursor"]["files_created"] == 6

            content = (cursor_dir / "librarian.mdc").read_text()
            assert "---" in content
            assert "description: DrSpec Librarian Agent (Iris)" in content
            assert "alwaysApply: false" in content
            assert "globs:" in content
            assert "<agent-activation CRITICAL=" in content
            assert "@_drspec/agents/librarian.md" in content

    def test_init_with_claude_code_flag(self, tmp_path):
        tmpdir = str(tmp_path)
        with runner.isolated_filesystem(temp_dir=tmpdir):
            result = runner.invoke(app, ["--json", "init", "--project-root", str(Path.cwd()), "--ide", "claude-code"])

            assert result.exit_code == 0
            response = json.loads(result.output)

            claude_dir = Path.cwd() / ".claude" / "commands" / "drspec"
            assert claude_dir.exists()
            assert (claude_dir / "librarian.md").exists()
            assert len(list(claude_dir.glob("*.md"))) == 6

            assert response["data"]["ide_integrations"]["claude-code"]["enabled"] is True

            content = (claude_dir / "librarian.md").read_text()
            assert "name: 'drspec-librarian'" in content
            assert "description: 'DrSpec Librarian Agent" in content

    def test_init_with_github_copilot_flag(self, tmp_path):
        tmpdir = str(tmp_path)
        with runner.isolated_filesystem(temp_dir=tmpdir):
            result = runner.invoke(app, ["--json", "init", "--project-root", str(Path.cwd()), "--ide", "github-copilot"])

            assert result.exit_code == 0
            response = json.loads(result.output)

            copilot_dir = Path.cwd() / ".github" / "agents"
            assert copilot_dir.exists()
            assert (copilot_dir / "drspec-librarian.agent.md").exists()
            assert len(list(copilot_dir.glob("*.agent.md"))) == 6

            assert response["data"]["ide_integrations"]["github-copilot"]["enabled"] is True

            content = (copilot_dir / "drspec-librarian.agent.md").read_text()
            assert 'tools: ["changes","edit","fetch"' in content
            assert "# DrSpec Librarian Agent" in content

    def test_init_with_codex_flag(self, tmp_path):
        tmpdir = str(tmp_path)
        with runner.isolated_filesystem(temp_dir=tmpdir):
            result = runner.invoke(app, ["--json", "init", "--project-root", str(Path.cwd()), "--ide", "codex"])

            assert result.exit_code == 0
            response = json.loads(result.output)

            # codex prompts are project-local by default
            codex_dir = Path.cwd() / ".codex" / "prompts"
            assert codex_dir.exists()
            assert (codex_dir / "drspec-librarian.md").exists()
            assert len(list(codex_dir.glob("drspec-*.md"))) == 6

            assert response["data"]["ide_integrations"]["codex"]["enabled"] is True

            content = (codex_dir / "drspec-librarian.md").read_text()
            assert not content.startswith("---")
            assert content.startswith("# DrSpec Librarian Agent (Iris)")

    def test_init_with_multiple_ides(self, tmp_path):
        tmpdir = str(tmp_path)
        with runner.isolated_filesystem(temp_dir=tmpdir):
            result = runner.invoke(app, ["--json",
                "init", "--project-root", str(Path.cwd()),
                "--ide", "cursor",
                "--ide", "claude-code",
            ])

            assert result.exit_code == 0
            response = json.loads(result.output)

            assert response["data"]["ide_integrations"]["cursor"]["enabled"] is True
            assert response["data"]["ide_integrations"]["claude-code"]["enabled"] is True
            assert response["data"]["ide_integrations"]["github-copilot"]["enabled"] is False
            assert response["data"]["ide_integrations"]["codex"]["enabled"] is False

            cursor_dir = Path.cwd() / ".cursor" / "rules" / "drspec"
            claude_dir = Path.cwd() / ".claude" / "commands" / "drspec"
            assert cursor_dir.exists()
            assert claude_dir.exists()

    def test_init_with_invalid_ide(self, tmp_path):
        tmpdir = str(tmp_path)
        with runner.isolated_filesystem(temp_dir=tmpdir):
            result = runner.invoke(app, ["--json", "init", "--project-root", str(Path.cwd()), "--ide", "invalid-ide"])

            assert result.exit_code == 1
            response = json.loads(result.output)
            assert response["success"] is False
            assert response["error"]["code"] == "INVALID_IDE"
            assert "invalid-ide" in response["error"]["message"]


class TestInitNoIDE:
    def test_init_no_ide_skips_integration(self, tmp_path):
        tmpdir = str(tmp_path)
        with runner.isolated_filesystem(temp_dir=tmpdir):
            result = runner.invoke(app, ["--json", "init", "--project-root", str(Path.cwd()), "--no-ide"])

            assert result.exit_code == 0
            response = json.loads(result.output)
            assert response["success"] is True

            assert response["data"]["ide_integrations"] is None

            assert not (Path.cwd() / ".cursor").exists()
            assert not (Path.cwd() / ".claude").exists()
            assert not (Path.cwd() / ".github" / "agents").exists()
            assert not (Path.cwd() / ".codex").exists()


class TestInitNonInteractive:
    def test_init_non_interactive_skips_prompts(self, tmp_path):
        tmpdir = str(tmp_path)
        with runner.isolated_filesystem(temp_dir=tmpdir):
            result = runner.invoke(app, ["--json", "init", "--project-root", str(Path.cwd()), "--non-interactive"])

            assert result.exit_code == 0
            response = json.loads(result.output)
            assert response["success"] is True

            assert response["data"]["ide_integrations"] is None

    def test_init_non_interactive_with_ide_flag(self, tmp_path):
        tmpdir = str(tmp_path)
        with runner.isolated_filesystem(temp_dir=tmpdir):
            result = runner.invoke(app, ["--json",
                "init", "--project-root", str(Path.cwd()),
                "--non-interactive",
                "--ide", "cursor",
            ])

            assert result.exit_code == 0
            response = json.loads(result.output)

            assert response["data"]["ide_integrations"]["cursor"]["enabled"] is True


class TestReinitWithIDE:
    def test_reinit_updates_ide_launchers(self, tmp_path):
        tmpdir = str(tmp_path)
        with runner.isolated_filesystem(temp_dir=tmpdir):
            runner.invoke(app, ["init", "--project-root", str(Path.cwd()), "--no-ide"])

            cursor_dir = Path.cwd() / ".cursor" / "rules" / "drspec"
            assert not cursor_dir.exists()

            result = runner.invoke(app, ["init", "--project-root", str(Path.cwd()), "--force", "--ide", "cursor"])
            assert result.exit_code == 0

            assert cursor_dir.exists()
            assert len(list(cursor_dir.glob("*.mdc"))) == 6

    def test_reinit_adds_new_ide(self, tmp_path):
        tmpdir = str(tmp_path)
        with runner.isolated_filesystem(temp_dir=tmpdir):
            runner.invoke(app, ["init", "--project-root", str(Path.cwd()), "--ide", "cursor"])

            cursor_dir = Path.cwd() / ".cursor" / "rules" / "drspec"
            assert cursor_dir.exists()

            result = runner.invoke(app, ["init", "--project-root", str(Path.cwd()), "--force", "--ide", "claude-code"])
            assert result.exit_code == 0

            claude_dir = Path.cwd() / ".claude" / "commands" / "drspec"
            assert cursor_dir.exists()
            assert claude_dir.exists()


class TestOverwriteConfirmation:
    def test_reinit_non_interactive_skips_overwrite(self, tmp_path):
        tmpdir = str(tmp_path)
        with runner.isolated_filesystem(temp_dir=tmpdir):
            runner.invoke(app, ["init", "--project-root", str(Path.cwd()), "--ide", "cursor"])

            agents_folder = Path.cwd() / "_drspec" / "agents"
            librarian = agents_folder / "librarian.md"
            librarian.write_text("# Modified by user")

            result = runner.invoke(app, ["--json", "init", "--project-root", str(Path.cwd()), "--non-interactive", "--ide", "cursor"])
            assert result.exit_code == 0
            response = json.loads(result.output)

            assert response["data"]["overwrite_skipped"] is True

            assert librarian.read_text() == "# Modified by user"

    def test_reinit_force_overwrites(self, tmp_path):
        tmpdir = str(tmp_path)
        with runner.isolated_filesystem(temp_dir=tmpdir):
            runner.invoke(app, ["init", "--project-root", str(Path.cwd()), "--ide", "cursor"])

            agents_folder = Path.cwd() / "_drspec" / "agents"
            librarian = agents_folder / "librarian.md"
            original_content = librarian.read_text()
            librarian.write_text("# Modified by user")

            result = runner.invoke(app, ["--json", "init", "--project-root", str(Path.cwd()), "--force", "--ide", "cursor"])
            assert result.exit_code == 0
            response = json.loads(result.output)

            assert "overwrite_skipped" not in response["data"]

            assert librarian.read_text() == original_content

    def test_fresh_init_no_confirmation_needed(self, tmp_path):
        tmpdir = str(tmp_path)
        with runner.isolated_filesystem(temp_dir=tmpdir):
            result = runner.invoke(app, ["--json", "init", "--project-root", str(Path.cwd()), "--ide", "cursor"])
            assert result.exit_code == 0
            response = json.loads(result.output)

            assert "overwrite_skipped" not in response["data"]
            assert response["data"]["message"] == "DrSpec initialized successfully"
