"""A limited function search says there is more, puts the function with the name asked for first, and names an
unknown language."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from drspec import mcp_server
from drspec.cli.app import app

runner = CliRunner()

pytestmark = pytest.mark.component  # the command runner over a project scanned in this process; its database is in memory (`kept_project`)


def _run(*arguments: str) -> dict:
    result = runner.invoke(app, ["--json", *arguments])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["data"]


@pytest.fixture
def project(kept_project):
    tmp_path = kept_project  # the working folder, with an empty project database: `init` is not what these tests are about
    (tmp_path / "a_first.py").write_text("def other():\n    return insert_thing(1)\n\n\ndef _insert_thing_helper():\n    return 1\n")
    (tmp_path / "b_second.py").write_text("def insert_thing_twice():\n    return 2\n\n\ndef uses():\n    return insert_thing(3)\n")
    (tmp_path / "z_last.py").write_text("def insert_thing(x):\n    return x\n")
    assert runner.invoke(app, ["scan"]).exit_code == 0
    return tmp_path


class TestACutOffIsNotSilent:
    def test_the_total_is_the_real_one_and_the_result_says_it_was_cut(self, project):
        data = _run("search", "insert_thing", "--limit", "1")
        assert [m["function_name"] for m in data["matches"]] == ["insert_thing"] and data["total_matches"] == 5 and data["truncated"] is True
        assert data["note"] == "Showing 1 of 5 matching functions, best match first; raise --limit to see more."

    def test_the_text_output_says_so_like_line_mode_does(self, project):
        text = runner.invoke(app, ["search", "insert_thing", "--limit", "2"]).output
        assert text.splitlines()[:5] == ["Pattern: insert_thing", "Semantic: No", "Files Searched: 3", "Total Matches: 5", "Truncated: Yes"]
        assert text.endswith("Note: Showing 2 of 5 matching functions, best match first; raise --limit to see more.\n")
        assert "    ID: insert_thing#" in text and "    ID: insert_thing_twice#" in text and "ID: other" not in text  # the two best, and no more

    def test_a_search_within_the_limit_is_not_marked(self, project):
        data = _run("search", "insert_thing", "--limit", "50")
        assert data["total_matches"] == len(data["matches"]) == 5 and data["truncated"] is False and "note" not in data

    def test_exactly_the_limit_is_not_a_cut(self, project):
        data = _run("search", "insert_thing", "--limit", "5")
        assert data["truncated"] is False and data["total_matches"] == 5 and len(data["matches"]) == 5 and "note" not in data

    def test_the_mcp_tool_says_it_too(self, project):
        result, failed = mcp_server._tool_search(Path.cwd() / "_drspec" / "contracts.db", {"pattern": "insert_thing", "limit": 2})
        assert not failed and result["total_matches"] == 5 and result["truncated"] is True
        assert result["note"] == "Showing 2 of 5 matching functions, best match first; raise limit to see more."  # the MCP parameter is `limit`, not `--limit`
        assert [m["function_name"] for m in result["matches"]] == ["insert_thing", "insert_thing_twice"]


class TestTheNameAskedForComesFirst:
    def test_the_exact_name_is_first_whatever_file_it_is_in(self, project):
        names = [m["function_name"] for m in _run("search", "insert_thing")["matches"]]
        # the exact name first (file order would put a_first.py's functions first), then names containing it (one starting with it before one
        # that has it inside), then the functions whose body only mentions it, in file order
        assert names == ["insert_thing", "insert_thing_twice", "_insert_thing_helper", "other", "uses"]

    def test_a_small_limit_keeps_the_definition(self, project):
        assert [m["function_name"] for m in _run("search", "insert_thing", "--limit", "1")["matches"]] == ["insert_thing"]

    def test_case_insensitive_and_regex_rank_the_same_way(self, project):
        assert [m["function_name"] for m in _run("search", "-i", "INSERT_THING", "--limit", "1")["matches"]] == ["insert_thing"]
        assert [m["function_name"] for m in _run("search", "-E", "insert_thing", "--limit", "1")["matches"]] == ["insert_thing"]

    def test_without_a_pattern_the_order_stays_by_file(self, project):
        matches = [(m["file_path"], m["function_name"]) for m in _run("search", "--status", "PENDING")["matches"]]
        assert matches == [
            ("a_first.py", "other"), ("a_first.py", "_insert_thing_helper"), ("b_second.py", "insert_thing_twice"), ("b_second.py", "uses"), ("z_last.py", "insert_thing"),
        ]  # by file, then by line


class TestAnUnknownLanguageIsNamed:
    def test_the_cli_names_it_and_lists_the_known_ones(self, project):
        result = runner.invoke(app, ["--json", "search", "x", "--include", "nosuchlang"])
        assert result.exit_code == 1
        error = json.loads(result.output)["error"]
        assert (error["code"], error["message"]) == (
            "INVALID_INPUT", "Unknown language for --include: 'nosuchlang'. Known: c, cpp, javascript, python, rust, tsx, typescript.",
        )

    def test_a_known_language_with_no_file_still_says_nothing_was_searched(self, project):
        data = _run("search", "x", "--include", "rust")
        assert data["files_searched"] == 0 and data["total_matches"] == 0 and data["matches"] == []
        assert data["note"].startswith("Nothing was searched: no file drspec has indexed matches this language filter.")

    def test_the_mcp_tool_names_it_too(self, project):
        result, failed = mcp_server._tool_search(Path.cwd() / "_drspec" / "contracts.db", {"pattern": "x", "language": "nosuchlang"})
        assert failed and result == "Unknown language: 'nosuchlang'. Known: c, cpp, javascript, python, rust, tsx, typescript."
