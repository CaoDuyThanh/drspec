"""The default text of `scan`: one line for the result (the counts that are not zero) and one line for each thing that needs attention;
`--verbose` is the whole summary as before. JSON and the MCP JSON are the same data as before. In memory: the summaries are dicts, and the
CLI and MCP checks use a fake scan result (no worker, no process)."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from drspec import mcp_server
from drspec.cli.app import app
from drspec.core import scan_text
from drspec.core.scan_text import COUNTS, short_lines, short_text

runner = CliRunner()
pytestmark = pytest.mark.unit

CLEAN = {
    "message": "Scanned 21 file(s), found 40 function(s)",
    "files_scanned": 21, "functions_found": 40, "functions_new": 40, "functions_changed": 0, "functions_unchanged": 0,
    "resolution_reasons": {"total": 40, "by_reason": {"forced": 40}, "examples": {}},
    "carried_over": {"total": 0, "examples": {}}, "contracts_invalidated": {"total": 0, "by_reason": {}, "examples": {}},
    "files_done": 21, "files_failed": 0, "files_queued": 21, "state": "done", "up_to_date": False,
}


def _with(**changes) -> dict:
    return {**CLEAN, **changes}


class TestTheDefaultText:
    def test_a_clean_scan_is_two_lines(self):
        lines = short_lines(CLEAN)
        assert lines[0] == "✓ Scanned 21 file(s), found 40 function(s) (new 40)"
        assert lines[1] == "40 function(s) re-resolved (forced 40); list: --verbose" and len(lines) == 2

    def test_a_long_list_of_re_resolved_functions_is_a_count_with_verbose_named(self):
        assert "list: --verbose" in short_text(_with(resolution_reasons={"total": 20, "by_reason": {"forced": 20}, "examples": {}}))

    def test_an_up_to_date_scan_says_so_in_one_line(self):
        assert short_lines({"message": "Up to date, nothing to do", "state": "done", "files_done": 0, "up_to_date": True}) == [
            "✓ Up to date, nothing to do"
        ]

    def test_every_counter_that_is_not_zero_is_in_the_first_line(self):
        for key, label in COUNTS:
            first = short_lines(_with(**{key: 3}))[0]
            assert f"{label} 3" in first, key

    def test_carried_over_and_invalidated_contracts_are_counted_in_the_first_line(self):
        first = short_lines(_with(carried_over={"total": 4, "examples": {}}, contracts_invalidated={"total": 2, "by_reason": {}, "examples": {}}))[0]
        assert "carried over 4" in first and "contracts invalidated 2" in first

    def test_a_failed_file_is_one_line_with_the_file_and_the_message(self):
        lines = short_lines(_with(files_failed=1, error_count=1, errors=[{"file": "src/a.cpp", "message": "cannot read", "line": None, "column": None}]))
        assert "failed 1" in lines[0]
        assert "1 file(s) could not be read: src/a.cpp: cannot read (list: --verbose)" in lines

    def test_several_failed_files_name_the_first_and_count_the_rest(self):
        lines = short_lines(_with(files_failed=3, error_count=3, errors=[{"file": "a.cpp", "message": "boom"}]))
        assert "3 file(s) could not be read: a.cpp: boom (and 2 more; list: --verbose)" in lines

    def test_six_syntax_errors_in_one_file_are_one_line_with_the_true_numbers(self):
        """The file was parsed in part: it is not a file that could not be read, and its six errors are one file, not six."""
        errors = [{"file": "src/broken.cpp", "message": "Syntax error: ERROR", "line": line, "column": 1} for line in (7, 2, 3, 3, 4, 7)]
        lines = short_lines(_with(files_failed=0, error_count=6, errors=errors))
        assert "1 file(s) with 6 syntax error(s): src/broken.cpp (first at line 7: Syntax error: ERROR)" in lines
        assert not any("could not be read" in line for line in lines)
        assert sum("broken.cpp" in line for line in lines) == 1  # said once

    def test_two_really_failed_files_say_so_and_count_the_other(self):
        errors = [{"file": "a.cpp", "message": "m", "line": None}, {"file": "b.cpp", "message": "n", "line": None}]
        lines = short_lines(_with(files_failed=2, error_count=2, errors=errors))
        assert "2 file(s) could not be read: a.cpp: m (and 1 more; list: --verbose)" in lines

    def test_a_removed_file_gets_its_note_as_one_line(self):
        note = "1 file(s) with 2 function(s) were removed from the index: they are ignored now"
        lines = short_lines(_with(files_removed_as_ignored=1, functions_removed_as_ignored=2, removed_note=note))
        assert note in lines and "files removed as ignored 1" in lines[0]

    def test_queued_files_are_the_message_and_the_worker_behind_line(self):
        lines = short_lines({"message": "5 file(s) queued; the worker reads them in the background", "state": "queued", "files_queued": 5, "files_remaining": 5})
        assert lines[0] == "✓ 5 file(s) queued; the worker reads them in the background"
        assert "the worker is behind: 5 file(s) still queued" in lines

    def test_a_yaml_file_not_read_is_one_line(self):
        note = "1 yaml file(s) not read: PyYAML is not installed: yaml files are not read (install it with: pip install pyyaml)."
        assert note in short_lines(_with(note=note))

    def test_a_file_parsed_in_part_is_one_line_with_its_counts(self):
        lines = short_lines(_with(files_partially_parsed=2, functions_skipped=3, functions_partial=1, files_read_with_warnings=1, skipped_constructs=4))
        assert "2 file(s) parsed in part (3 function(s) skipped, 1 partial): drspec status --partial lists them" in lines
        assert "1 file(s) read with parser warnings" in lines and "4 construct(s) skipped by the parser" in lines

    def test_the_generated_folders_are_listed_up_to_five_and_counted_beyond(self):
        few = [f"generated folder: d{i} (catkin)" for i in range(2)]
        assert "generated folders left out: d0 (catkin), d1 (catkin); read one with: drspec config set scan.include <folder>" in short_lines(_with(left_out=few))
        many = [f"generated folder: d{i} (catkin)" for i in range(7)]
        assert "7 generated folder(s) left out; list: --verbose" in short_lines(_with(left_out=many))

    def test_documentation_to_review_and_the_hint_are_kept(self):
        lines = short_lines(_with(note="2 documentation section(s) may need an update (drspec search --status STALE --docs-only)", hint="--deep-scan applies."))
        assert "2 documentation section(s) may need an update (drspec search --status STALE --docs-only)" in lines and "--deep-scan applies." in lines

    def test_a_problem_of_each_kind_is_one_line_and_none_is_dropped(self):
        problems = _with(
            files_failed=1, error_count=1, errors=[{"file": "b.cpp", "message": "m"}], carried_over={"total": 2, "examples": {}},
            functions_removed=1, functions_new=1, files_remaining=3, note="1 yaml file(s) not read: x.", files_partially_parsed=1,
            functions_skipped=1, functions_partial=0,
        )
        lines = short_lines(problems)
        for needle in ("failed 1", "carried over 2", "removed 1", "new 1", "1 file(s) could not be read: b.cpp: m", "the worker is behind: 3",
                       "1 yaml file(s) not read", "1 file(s) parsed in part"):
            assert any(needle in line for line in lines), needle

    def test_the_old_summary_lines_are_not_printed_by_default(self):
        text = short_text(CLEAN)
        assert "Functions Found" not in text and "Files Queued" not in text and "Resolution Reasons" not in text


class TestTheCommandOutput:
    @pytest.fixture
    def fake_scan(self, monkeypatch, tmp_path):
        from drspec.cli import app as app_module  # noqa: F401  (the command is loaded through the app)
        from drspec.cli.commands import scan as scan_command

        drspec = tmp_path / "_drspec"
        drspec.mkdir()
        (drspec / "contracts.db").write_text("")  # (exists: the command checks it; nothing is opened)
        monkeypatch.setattr(scan_command, "get_worker_connection", lambda db_path: type("C", (), {"close": lambda self: None})())
        monkeypatch.setattr(scan_command, "run_scan", lambda conn, spec, on_progress=None: dict(CLEAN))
        monkeypatch.chdir(tmp_path)
        return tmp_path

    def test_the_default_is_two_lines_and_verbose_is_the_old_output(self, fake_scan):
        short = runner.invoke(app, ["scan", "."])
        assert short.exit_code == 0, short.output
        assert short.output.strip().split("\n") == ["✓ Scanned 21 file(s), found 40 function(s) (new 40)", "40 function(s) re-resolved (forced 40); list: --verbose"]
        verbose = runner.invoke(app, ["scan", ".", "--verbose"]).output
        assert "Functions Found: 40" in verbose and "Resolution Reasons:" in verbose and len(verbose) > 2 * len(short.output)

    def test_json_is_the_same_with_and_without_verbose(self, fake_scan):
        plain = runner.invoke(app, ["--json", "scan", "."]).output
        verbose = runner.invoke(app, ["--json", "scan", ".", "--verbose"]).output
        assert plain == verbose and json.loads(plain)["data"]["functions_found"] == 40

    def test_json_does_not_use_the_short_text(self, fake_scan, monkeypatch):
        monkeypatch.setattr(scan_text, "short_lines", lambda data: (_ for _ in ()).throw(AssertionError("JSON must not be shortened")))
        assert json.loads(runner.invoke(app, ["--json", "scan", "."]).output)["success"] is True


class TestTheMcpTool:
    @pytest.fixture
    def canned(self, monkeypatch, tmp_path):
        monkeypatch.setitem(mcp_server._TOOL_HANDLERS, "scan", lambda db_path, arguments: (dict(CLEAN, path="."), False))
        return tmp_path / "_drspec" / "contracts.db"

    def _text(self, db_path, arguments):
        response = mcp_server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "scan", "arguments": arguments}}, db_path)
        return response["result"]["content"][0]["text"]

    def test_the_text_is_the_short_form_and_verbose_gives_the_old_one(self, canned):
        assert self._text(canned, {}).split("\n")[0] == "✓ Scanned 21 file(s), found 40 function(s) (new 40)"
        assert "Functions Found: 40" in self._text(canned, {"verbose": True})

    def test_compact_is_the_short_form_too(self, canned):
        assert self._text(canned, {"format": "compact"}).split("\n")[0].startswith("✓ Scanned 21")

    def test_json_is_the_whole_summary(self, canned):
        data = json.loads(self._text(canned, {"format": "json"}))
        assert data["functions_found"] == 40 and data["message"] == CLEAN["message"]
