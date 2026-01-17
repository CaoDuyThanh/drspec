"""Tests for the drspec init command."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.cli.commands.init import (
    DRSPEC_FOLDER,
    DB_NAME,
    AGENTS_FOLDER,
    GITIGNORE_ENTRY,
    update_gitignore,
    copy_agent_templates,
)
from drspec.cli.output import success_response, error_response
from drspec.core.resources import get_templates_path


runner = CliRunner()


class TestInitHelpers:
    """Tests for init command helper functions."""

    def test_success_response(self):
        """Test success response creation."""
        data = {"key": "value"}
        response = success_response(data)

        assert response["success"] is True
        assert response["data"] == data
        assert response["error"] is None

    def test_error_response(self):
        """Test error response creation."""
        response = error_response("TEST_ERROR", "Something went wrong")

        assert response["success"] is False
        assert response["data"] is None
        assert response["error"]["code"] == "TEST_ERROR"
        assert response["error"]["message"] == "Something went wrong"
        assert response["error"]["details"] == {}

    def test_error_response_with_details(self):
        """Test error response creation with details."""
        details = {"path": "/some/path"}
        response = error_response("TEST_ERROR", "Something went wrong", details)

        assert response["error"]["details"] == details


class TestUpdateGitignore:
    """Tests for the update_gitignore function."""

    def test_update_gitignore_creates_new_file(self):
        """Test creating new .gitignore with _drspec/ entry."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)

            result = update_gitignore(project_root)

            assert result is True
            gitignore = project_root / ".gitignore"
            assert gitignore.exists()
            content = gitignore.read_text()
            assert GITIGNORE_ENTRY in content

    def test_update_gitignore_appends_to_existing(self):
        """Test appending to existing .gitignore."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            gitignore = project_root / ".gitignore"
            gitignore.write_text("node_modules/\n.env\n")

            result = update_gitignore(project_root)

            assert result is True
            content = gitignore.read_text()
            assert "node_modules/" in content
            assert ".env" in content
            assert GITIGNORE_ENTRY in content

    def test_update_gitignore_skips_if_present(self):
        """Test skipping if _drspec/ already in .gitignore."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            gitignore = project_root / ".gitignore"
            gitignore.write_text(f"node_modules/\n{GITIGNORE_ENTRY}\n")

            result = update_gitignore(project_root)

            assert result is False

    def test_update_gitignore_handles_no_trailing_newline(self):
        """Test handling file without trailing newline."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            gitignore = project_root / ".gitignore"
            gitignore.write_text("node_modules/")  # No trailing newline

            result = update_gitignore(project_root)

            assert result is True
            content = gitignore.read_text()
            # Should have newline before _drspec/
            assert "node_modules/\n" in content
            assert GITIGNORE_ENTRY in content


class TestCopyAgentTemplates:
    """Tests for the copy_agent_templates function."""

    def test_copy_agent_templates_copies_files(self):
        """Test copying template files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "templates"
            dest = Path(tmpdir) / "_drspec" / "agents"
            src.mkdir()

            # Create sample templates
            (src / "agent1.md").write_text("# Agent 1")
            (src / "agent2.md").write_text("# Agent 2")
            (src / "not_markdown.txt").write_text("skip me")

            copied = copy_agent_templates(src, dest)

            assert len(copied) == 2
            assert "agent1.md" in copied
            assert "agent2.md" in copied
            assert dest.exists()
            assert (dest / "agent1.md").exists()
            assert (dest / "agent2.md").exists()
            assert not (dest / "not_markdown.txt").exists()

    def test_copy_agent_templates_creates_dest_dir(self):
        """Test destination directory is created if missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "templates"
            dest = Path(tmpdir) / "nested" / "path" / "agents"
            src.mkdir()
            (src / "test.md").write_text("# Test")

            copied = copy_agent_templates(src, dest)

            assert len(copied) == 1
            assert dest.exists()

    def test_copy_agent_templates_handles_missing_source(self):
        """Test handling missing source directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "nonexistent"
            dest = Path(tmpdir) / "_drspec" / "agents"

            copied = copy_agent_templates(src, dest)

            assert copied == []


class TestGetTemplatesPath:
    """Tests for template path resolution."""

    def test_get_templates_path_development(self):
        """Test template path in development mode."""
        path = get_templates_path()
        # Should return path ending with drspec/agents (inside package)
        assert path.name == "agents"
        assert path.parent.name == "drspec"

    def test_get_templates_path_frozen(self):
        """Test template path in PyInstaller frozen mode."""
        with patch("sys.frozen", True, create=True):
            with patch("sys._MEIPASS", "/tmp/meipass", create=True):
                path = get_templates_path()
                assert str(path) == "/tmp/meipass/agents"


class TestInitCommand:
    """Tests for the init CLI command."""

    def test_init_creates_drspec_folder(self):
        """Test init creates _drspec folder structure."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                result = runner.invoke(app, ["init"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["success"] is True

                drspec_folder = Path.cwd() / DRSPEC_FOLDER
                assert drspec_folder.exists()
                assert (drspec_folder / DB_NAME).exists()
                assert (drspec_folder / AGENTS_FOLDER).exists()

    def test_init_creates_database_with_schema(self):
        """Test init creates database with proper schema."""
        import duckdb

        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                result = runner.invoke(app, ["init"])

                assert result.exit_code == 0

                db_path = Path.cwd() / DRSPEC_FOLDER / DB_NAME
                conn = duckdb.connect(str(db_path))

                # Verify tables exist
                tables = conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
                ).fetchall()
                table_names = [t[0] for t in tables]

                assert "artifacts" in table_names
                assert "contracts" in table_names
                assert "queue" in table_names
                assert "dependencies" in table_names
                assert "reasoning_traces" in table_names

                conn.close()

    def test_init_updates_gitignore(self):
        """Test init updates .gitignore."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                result = runner.invoke(app, ["init"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["data"]["gitignore_updated"] is True

                gitignore = Path.cwd() / ".gitignore"
                assert gitignore.exists()
                assert GITIGNORE_ENTRY in gitignore.read_text()

    def test_init_already_initialized(self):
        """Test init when already initialized returns success."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                # First init
                runner.invoke(app, ["init"])

                # Second init without force
                result = runner.invoke(app, ["init"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["success"] is True
                assert response["data"]["already_initialized"] is True

    def test_init_force_reinitializes(self):
        """Test init --force rebuilds database."""
        import duckdb

        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                # First init
                runner.invoke(app, ["init"])

                # Add some data
                db_path = Path.cwd() / DRSPEC_FOLDER / DB_NAME
                conn = duckdb.connect(str(db_path))
                conn.execute(
                    "INSERT INTO artifacts (function_id, file_path, function_name, signature, body, code_hash, "
                    "language, start_line, end_line) "
                    "VALUES ('test::foo', 'test.py', 'foo', 'def foo():', 'pass', 'hash', 'python', 1, 2)"
                )
                conn.close()

                # Force reinit - with --force, it bypasses confirmation but keeps data
                # because we're in re-init path. To actually rebuild DB, delete folder first.
                result = runner.invoke(app, ["init", "--force"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["success"] is True
                # --force on existing project bypasses confirmation, still reports already_initialized
                assert response["data"]["already_initialized"] is True

                # Data preserved (re-init doesn't rebuild DB)
                conn = duckdb.connect(str(db_path))
                count = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
                conn.close()
                assert count == 1  # Data preserved

    def test_init_returns_json_response(self):
        """Test init returns properly structured JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                result = runner.invoke(app, ["init"])

                response = json.loads(result.output)

                assert "success" in response
                assert "data" in response
                assert "error" in response
                assert "message" in response["data"]
                assert "drspec_folder" in response["data"]
                assert "database" in response["data"]
                assert "agents_folder" in response["data"]

    def test_init_short_flag(self):
        """Test init -f short flag for force."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                runner.invoke(app, ["init"])
                result = runner.invoke(app, ["init", "-f"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["success"] is True

    def test_init_restores_deleted_agents(self):
        """Test that init restores agents if they were deleted."""
        import shutil

        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                # First init
                result = runner.invoke(app, ["init"])
                assert result.exit_code == 0

                agents_folder = Path.cwd() / DRSPEC_FOLDER / AGENTS_FOLDER
                assert agents_folder.exists()
                assert (agents_folder / "librarian.md").exists()

                # Delete agents folder
                shutil.rmtree(agents_folder)
                assert not agents_folder.exists()

                # Re-init with --force should restore agents
                result = runner.invoke(app, ["init", "--force"])
                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["success"] is True
                assert response["data"]["already_initialized"] is True

                # Agents should be restored
                assert agents_folder.exists()
                assert (agents_folder / "librarian.md").exists()
                assert "librarian.md" in response["data"]["agents_updated"]

    def test_init_updates_agents_on_reinit(self):
        """Test that re-init with --force updates agents (enables upgrades)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                # First init
                runner.invoke(app, ["init"])

                agents_folder = Path.cwd() / DRSPEC_FOLDER / AGENTS_FOLDER
                librarian = agents_folder / "librarian.md"
                original_content = librarian.read_text()

                # Modify the agent file (simulate user edit)
                librarian.write_text("# Modified content")
                assert librarian.read_text() == "# Modified content"

                # Re-init with --force should overwrite with original content
                result = runner.invoke(app, ["init", "--force"])
                assert result.exit_code == 0
                response = json.loads(result.output)

                # Agent should be overwritten with source version
                assert librarian.read_text() == original_content
                assert "librarian.md" in response["data"]["agents_updated"]

    def test_init_does_not_modify_db_on_reinit(self):
        """Test that re-init with --force doesn't modify the database."""
        import duckdb

        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                # First init
                runner.invoke(app, ["init"])

                # Add some data to DB
                db_path = Path.cwd() / DRSPEC_FOLDER / DB_NAME
                conn = duckdb.connect(str(db_path))
                conn.execute(
                    "INSERT INTO artifacts (function_id, file_path, function_name, signature, body, code_hash, "
                    "language, start_line, end_line) "
                    "VALUES ('test::foo', 'test.py', 'foo', 'def foo():', 'pass', 'hash', 'python', 1, 2)"
                )
                conn.close()

                # Re-init with --force (doesn't rebuild DB, just bypasses confirmation)
                result = runner.invoke(app, ["init", "--force"])
                assert result.exit_code == 0

                # Data should still be there
                conn = duckdb.connect(str(db_path))
                count = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
                conn.close()
                assert count == 1  # Data preserved
