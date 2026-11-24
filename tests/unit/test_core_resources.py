"""Tests for core resources module: where the agent templates and the schema are, from source and from a PyInstaller bundle.

From source the paths are in the package folder; frozen, they are under `sys._MEIPASS`, at the places `pyinstaller.spec` puts them. A frozen
path that no longer matches the spec is a binary that cannot find its templates, which nothing else would notice before a release."""

import re
import sys
from pathlib import Path

import pytest

import drspec
import drspec.db.connection
from drspec.core.resources import (
    _get_base_path,
    get_schema_path,
    get_templates_path,
    list_template_files,
)

pytestmark = pytest.mark.unit  # paths, and reading files of the repository

PACKAGE = Path(drspec.__file__).parent
TEMPLATES = {"critic.md", "debugger.md", "judge.md", "librarian.md", "proposer.md", "vision_analyst.md"}


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    """The module as PyInstaller runs it: `sys.frozen` set and `sys._MEIPASS` the folder of the bundle."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    return tmp_path


class TestGetBasePath:
    def test_from_source_it_is_the_folder_that_holds_the_package(self):
        assert _get_base_path() == PACKAGE.parent  # src/, which holds drspec/

    def test_frozen_it_is_the_folder_of_the_bundle(self, frozen):
        assert _get_base_path() == frozen
        assert isinstance(_get_base_path(), Path)


class TestGetTemplatesPath:
    def test_from_source_the_templates_are_in_the_package(self):
        assert get_templates_path() == PACKAGE / "agents"
        assert get_templates_path().is_dir()

    def test_the_templates_are_the_six_agents(self):
        assert {p.name for p in get_templates_path().glob("*.md")} == TEMPLATES

    def test_frozen_they_are_in_agents_under_the_bundle(self, frozen):
        assert get_templates_path() == frozen / "agents"


class TestGetSchemaPath:
    def test_from_source_it_is_the_file_init_schema_reads(self):
        # two places find `schema.sql` (here and `db.connection.init_schema`): they must find the same file
        assert get_schema_path() == Path(drspec.db.connection.__file__).parent / "schema.sql"
        assert get_schema_path() == PACKAGE / "db" / "schema.sql"

    def test_the_schema_file_declares_the_project_tables(self):
        content = get_schema_path().read_text()

        assert {"artifacts", "contracts", "dependencies", "config"} <= set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", content))

    def test_frozen_it_is_in_drspec_db_under_the_bundle(self, frozen):
        assert get_schema_path() == frozen / "drspec" / "db" / "schema.sql"


class TestListTemplateFiles:
    def test_returns_the_markdown_files_of_the_templates_folder(self):
        files = list_template_files()

        assert isinstance(files, list)
        assert sorted(files) == sorted(TEMPLATES)  # the six agents, and not the `helpers` and `templates` folders next to them

    def test_includes_all_five_templates(self):
        files = list_template_files()
        required = ["librarian.md", "proposer.md", "critic.md", "judge.md", "debugger.md"]
        for template in required:
            assert template in files, f"Missing template: {template}"

    def test_frozen_it_lists_the_bundle_and_only_markdown(self, frozen):
        (frozen / "agents").mkdir()
        (frozen / "agents" / "a.md").write_text("x")
        (frozen / "agents" / "b.txt").write_text("x")
        (frozen / "agents" / "sub").mkdir()

        assert list_template_files() == ["a.md"]
