"""A semantic search says how much it searched, and `status` does not say "up to date" when only a small share is embedded: a function
without an embedding cannot be found by its meaning, and the result and the status must not let that pass for "nothing there"."""

from __future__ import annotations

import json
import re
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.core.embedding_backlog import embedding_status
from drspec.core.settings import read_all_settings
from drspec.mcp_server import _tool_search, handle_message

runner = CliRunner()


@pytest.fixture
def partly_embedded(kept_project, fake_embedder):
    """Two functions of `a.py` embedded (`--embed`), one of `b.py` and one of `docs/c.py` scanned without."""
    Path("a.py").write_text("def compute_total(order):\n    return sum(i.price for i in order.items)\n\n\ndef open_socket(host):\n    return connect(host)\n")
    assert runner.invoke(app, ["scan", "--embed", "a.py"]).exit_code == 0
    Path("b.py").write_text("def parse_config(path):\n    return path\n")
    (kept_project / "docs").mkdir()
    Path("docs/c.py").write_text("def other():\n    return 1\n")
    assert runner.invoke(app, ["scan", "b.py", "docs/c.py"]).exit_code == 0
    return kept_project


def _search(*args):
    result = runner.invoke(app, ["--json", "search", "--semantic", "sum item prices", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["data"]


class TestTheResultSaysHowMuchItSearched:
    def test_it_names_the_functions_searched_and_the_total_and_says_the_others_cannot_be_found(self, partly_embedded):
        data = _search()
        assert (data["searched"], data["functions_in_scope"]) == (2, 4)
        note = data["coverage_note"]
        assert "Searched 2 of 4 functions" in note and "cannot be found by its meaning" in note
        assert "`drspec scan <paths> --embed`" in note and "`drspec config set embedding.top_n <N>` (now 2000)" in note

    def test_the_numbers_follow_the_scope_of_the_search(self, partly_embedded):
        assert (_search("--path", "a.py")["searched"], _search("--path", "a.py")["functions_in_scope"]) == (2, 2)
        data = _search("--path", "docs")
        assert (data["searched"], data["functions_in_scope"]) == (0, 1) and "Searched 0 of 1 functions" in data["coverage_note"]

    def test_a_scope_where_every_function_has_an_embedding_has_no_note(self, partly_embedded):
        data = _search("--path", "a.py")
        assert "coverage_note" not in data

    def test_the_text_output_has_the_line(self, partly_embedded):
        text = runner.invoke(app, ["search", "--semantic", "sum item prices"]).output
        assert "Searched: 2" in text and "Functions In Scope: 4" in text and "Searched 2 of 4 functions" in text

    def test_the_mcp_tool_says_it_too_in_json_and_in_compact(self, partly_embedded):
        db_path = partly_embedded / "_drspec" / "contracts.db"
        result, failed = _tool_search(db_path, {"pattern": "sum item prices", "semantic": True})
        assert not failed and (result["searched"], result["functions_in_scope"]) == (2, 4) and "cannot be found by its meaning" in result["coverage_note"]
        response = handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search", "arguments": {"pattern": "sum item prices", "semantic": True, "format": "compact"}}},
            db_path,
        )
        text = response["result"]["content"][0]["text"]
        assert text.startswith("~") and re.search(r"searched: 2\b", text) and re.search(r"functions_in_scope: 4\b", text) and "cannot be found by its meaning" in text


class TestTheStatusDoesNotSayUpToDateForASmallShare:
    def _status(self, project):
        connection = duckdb.connect(str(project / "_drspec" / "contracts.db"))  # pattern-ok: duckdb-file kept in memory by `kept_duckdb`
        try:
            return embedding_status(connection, read_all_settings(connection), 0.0)
        finally:
            connection.close()

    def test_a_partial_share_says_how_many_and_the_top_n(self, partly_embedded):
        status = self._status(partly_embedded)
        assert (status["embedded"], status["total"], status["top_n"], status["backlog"]) == (2, 4, 2000, 0)
        assert status["message"] == "2 of 4 embedded (top_n = 2000): the others are not found by meaning"
        assert "up to date" not in status["message"]

    def test_the_cli_status_shows_the_count_the_total_and_the_top_n(self, partly_embedded):
        data = json.loads(runner.invoke(app, ["--json", "status"]).output)["data"]
        embedding = data["backlog"]["embedding"]
        assert (embedding["embedded"], embedding["total"], embedding["top_n"]) == (2, 4, 2000) and "up to date" not in embedding["message"]
        assert "2 of 4 embedded (top_n = 2000)" in runner.invoke(app, ["status"]).output

    def test_everything_embedded_is_up_to_date(self, kept_project, fake_embedder):
        Path("a.py").write_text("def one():\n    return 1\n")
        assert runner.invoke(app, ["scan", "--embed", "."]).exit_code == 0
        status = self._status(kept_project)
        assert status["embedded"] == status["total"] and status["message"] == "embedding up to date"

    def test_an_owed_backlog_says_the_count_too(self, partly_embedded, monkeypatch):
        from drspec.core import embedding_backlog

        monkeypatch.setattr(embedding_backlog, "find_backlog", lambda *args: ["x", "y", "z"])
        message = self._status(partly_embedded)["message"]
        assert message == "embedding: 3 waiting (2 of 4 embedded, top_n = 2000)"
