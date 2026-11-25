"""The drspec init command: what a first init creates, what a re-init with `--force` keeps and rewrites, and what a
worker that holds the database changes. The helpers `init` uses (`update_gitignore`, `copy_agent_templates`,
`is_initialized_database`) are tested directly, with files in `tmp_path` and no `init` run.

One `init` runs once for the session (`_TEMPLATE`); each test works on a copy of its folder, so the first-run checks
share one run and the re-init checks start from an initialised project.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.cli.commands.init import (
    AGENTS_FOLDER,
    DB_NAME,
    DRSPEC_FOLDER,
    GITIGNORE_ENTRY,
    copy_agent_templates,
    is_initialized_database,
    update_gitignore,
)
from drspec.core.resources import get_templates_path

runner = CliRunner()

_TEMPLATE: dict = {}


@pytest.fixture(autouse=True, scope="session")
def _template_project(tmp_path_factory):
    """One first `init` for the session: its folder (copied by the tests) and its JSON answer."""
    root = tmp_path_factory.mktemp("init_template")
    result = runner.invoke(app, ["--json", "init", "--project-root", str(root), "--no-ide"])
    assert result.exit_code == 0, result.output
    _TEMPLATE["root"] = root
    _TEMPLATE["response"] = json.loads(result.output)
    yield


@pytest.fixture
def project(tmp_path) -> Path:
    """A copy of the initialised project folder (1.2 MB, copied in a few milliseconds)."""
    target = tmp_path / "project"
    shutil.copytree(_TEMPLATE["root"], target)
    return target


def _init(root: Path, *flags: str):
    return runner.invoke(app, ["--json", "init", "--project-root", str(root), "--no-ide", *flags])


class TestUpdateGitignore:
    """`update_gitignore` adds the drspec folder once, keeping the lines that are there."""

    @pytest.mark.parametrize(
        "before, returns, after",
        [
            (None, True, f"{GITIGNORE_ENTRY}\n"),
            ("node_modules/\n.env\n", True, f"node_modules/\n.env\n{GITIGNORE_ENTRY}\n"),
            (f"node_modules/\n{GITIGNORE_ENTRY}\n", False, f"node_modules/\n{GITIGNORE_ENTRY}\n"),
            ("node_modules/", True, f"node_modules/\n{GITIGNORE_ENTRY}\n"),  # no trailing newline
        ],
    )
    def test_the_entry_is_added_once(self, tmp_path, before, returns, after):
        gitignore = tmp_path / ".gitignore"
        if before is not None:
            gitignore.write_text(before)
        assert update_gitignore(tmp_path) is returns
        assert gitignore.read_text() == after


class TestCopyAgentTemplates:
    """`copy_agent_templates` copies the markdown templates and makes the folder they go to."""

    @pytest.mark.parametrize("dest_parts", [("_drspec", "agents"), ("nested", "path", "agents")])
    def test_markdown_files_are_copied_and_the_folder_is_made(self, tmp_path, dest_parts):
        src = tmp_path / "templates"
        dest = tmp_path.joinpath(*dest_parts)
        src.mkdir()
        (src / "agent1.md").write_text("# Agent 1")
        (src / "agent2.md").write_text("# Agent 2")
        (src / "not_markdown.txt").write_text("skip me")

        copied = copy_agent_templates(src, dest)

        assert sorted(copied) == ["agent1.md", "agent2.md"]
        assert (dest / "agent1.md").exists() and (dest / "agent2.md").exists()
        assert not (dest / "not_markdown.txt").exists()

    def test_a_missing_source_copies_nothing(self, tmp_path):
        assert copy_agent_templates(tmp_path / "nonexistent", tmp_path / "_drspec" / "agents") == []


class TestGetTemplatesPath:
    def test_frozen_builds_use_the_bundled_folder(self, tmp_path):
        with patch("sys.frozen", True, create=True):
            with patch("sys._MEIPASS", str(tmp_path / "meipass"), create=True):
                assert get_templates_path() == tmp_path / "meipass" / "agents"

    def test_development_uses_the_source_agents_folder(self):
        path = get_templates_path()
        assert path.name == "agents" and path.parent.name == "drspec"


class TestAFirstInit:
    """What the first `init` (run once for the session) leaves behind, checked on a copy of it."""

    def test_it_creates_the_folder_the_database_and_the_agents(self, project):
        folder = project / DRSPEC_FOLDER
        assert (folder / DB_NAME).exists()
        assert (folder / AGENTS_FOLDER / "librarian.md").exists()

    def test_the_database_has_drspec_tables_and_no_queue(self, project):
        conn = duckdb.connect(str(project / DRSPEC_FOLDER / DB_NAME), read_only=True)
        try:
            names = {row[0] for row in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'").fetchall()}
        finally:
            conn.close()
        assert "artifacts" in names and "queue" not in names

    def test_it_adds_the_drspec_entry_to_gitignore(self, project):
        assert _TEMPLATE["response"]["data"]["gitignore_updated"] is True
        assert GITIGNORE_ENTRY in (project / ".gitignore").read_text()

    def test_it_returns_the_documented_keys(self):
        response = _TEMPLATE["response"]
        assert {"success", "data", "error"} <= response.keys()
        assert {"message", "drspec_folder", "database", "agents_folder"} <= response["data"].keys()


class TestAReinit:
    def test_without_force_an_initialised_project_is_reported_and_left_alone(self, project):
        result = _init(project)
        assert result.exit_code == 0
        response = json.loads(result.output)
        assert response["success"] is True and response["data"]["already_initialized"] is True

    @pytest.mark.parametrize("flag", ["--force", "-f"])
    def test_force_keeps_the_rows_of_the_database(self, project, flag):
        conn = duckdb.connect(str(project / DRSPEC_FOLDER / DB_NAME))
        conn.execute(
            "INSERT INTO artifacts (function_id, file_path, function_name, signature, body, code_hash, "
            "language, start_line, end_line) VALUES ('test::foo', 'test.py', 'foo', 'def foo():', 'pass', 'hash', 'python', 1, 2)"
        )
        conn.close()

        result = _init(project, flag)

        assert result.exit_code == 0
        assert json.loads(result.output)["data"]["already_initialized"] is True
        conn = duckdb.connect(str(project / DRSPEC_FOLDER / DB_NAME), read_only=True)
        try:
            assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1
        finally:
            conn.close()

    @pytest.mark.parametrize("mutation", ["edited", "deleted"])
    def test_force_rewrites_the_agent_templates(self, project, mutation):
        agents = project / DRSPEC_FOLDER / AGENTS_FOLDER
        librarian = agents / "librarian.md"
        original = librarian.read_text()
        if mutation == "edited":
            librarian.write_text("# Modified content")
        else:
            shutil.rmtree(agents)

        response = json.loads(_init(project, "--force").output)

        assert librarian.read_text() == original
        assert "librarian.md" in response["data"]["agents_updated"]


class TestAWorkerThatHoldsTheDatabase:
    """A live worker owns the database file. `init` must not open it, and a lock error is not a corrupt database."""

    @pytest.mark.parametrize("flags", [[], ["--force"]])
    def test_a_reinit_does_not_touch_the_database_while_a_worker_holds_it(self, project, flags):
        def lock_error(*args, **kwargs):
            raise duckdb.IOException("Could not set lock on file: held by another process")

        with patch("drspec.cli.commands.init.is_worker_running", return_value=4242), \
                patch("drspec.cli.commands.init.worker_is_outdated", return_value=False), \
                patch("drspec.cli.commands.init.get_connection", side_effect=lock_error):
            result = _init(project, *flags)

        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["already_initialized"] is True

    def test_a_live_worker_means_initialised_without_opening_the_file(self, tmp_path):
        opened = MagicMock(side_effect=AssertionError("the database must not be opened"))
        with patch("drspec.cli.commands.init.is_worker_running", return_value=4242), \
                patch("drspec.cli.commands.init.get_connection", opened):
            assert is_initialized_database(tmp_path / "contracts.db") is True
        opened.assert_not_called()

    def test_a_database_without_the_schema_is_not_initialised(self, tmp_path):
        db = tmp_path / "contracts.db"
        conn = duckdb.connect(str(db))
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
        conn.close()
        with patch("drspec.cli.commands.init.is_worker_running", return_value=None):
            assert is_initialized_database(db) is False

    def test_a_database_without_the_schema_is_rebuilt_by_a_forced_init(self, tmp_path):
        (tmp_path / DRSPEC_FOLDER).mkdir()
        conn = duckdb.connect(str(tmp_path / DRSPEC_FOLDER / DB_NAME))
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
        conn.close()

        result = _init(tmp_path, "--force")

        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["message"] == "DrSpec initialized successfully"
