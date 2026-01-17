"""Tests for core resources module."""

from pathlib import Path
from unittest.mock import patch

import pytest

from drspec.core.resources import (
    get_templates_path,
    get_schema_path,
    list_template_files,
    _get_base_path,
)


class TestGetBasePath:
    """Tests for the _get_base_path function."""

    def test_returns_path_in_development(self):
        """In development, returns src/ directory."""
        base = _get_base_path()
        # Should be src/ directory with agents folder
        assert (base / "agents").exists() or (base / "drspec").exists()

    def test_returns_path_object(self):
        """Returns a Path object."""
        base = _get_base_path()
        assert isinstance(base, Path)


class TestGetTemplatesPath:
    """Tests for get_templates_path function."""

    def test_returns_templates_agents_path(self):
        """Returns path ending with agents/."""
        path = get_templates_path()
        assert path.name == "agents"
        # In development, agents are in src/drspec/agents
        assert path.parent.name == "drspec"

    def test_templates_directory_exists(self):
        """Templates directory should exist in development."""
        path = get_templates_path()
        assert path.exists()

    def test_contains_agent_templates(self):
        """Templates directory should contain agent markdown files."""
        path = get_templates_path()
        templates = list(path.glob("*.md"))
        assert len(templates) >= 3  # At minimum: librarian, proposer, critic


class TestGetSchemaPath:
    """Tests for get_schema_path function."""

    def test_returns_schema_sql_path(self):
        """Returns path to schema.sql."""
        path = get_schema_path()
        assert path.name == "schema.sql"
        assert path.parent.name == "db"

    def test_schema_file_exists(self):
        """Schema file should exist."""
        path = get_schema_path()
        assert path.exists()

    def test_schema_file_is_readable(self):
        """Schema file should be readable."""
        path = get_schema_path()
        content = path.read_text()
        assert "CREATE TABLE" in content
        assert "artifacts" in content


class TestListTemplateFiles:
    """Tests for list_template_files function."""

    def test_returns_list_of_filenames(self):
        """Returns list of .md filenames."""
        files = list_template_files()
        assert isinstance(files, list)
        assert all(f.endswith(".md") for f in files)

    def test_includes_required_templates(self):
        """Includes all required agent templates."""
        files = list_template_files()
        # Should have at least these templates
        assert "librarian.md" in files
        assert "proposer.md" in files
        assert "critic.md" in files

    def test_includes_all_five_templates(self):
        """Includes all five agent templates."""
        files = list_template_files()
        required = ["librarian.md", "proposer.md", "critic.md", "judge.md", "debugger.md"]
        for template in required:
            assert template in files, f"Missing template: {template}"
