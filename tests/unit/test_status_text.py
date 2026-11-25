"""The default text of `drspec status`: the counts and one line per block, the actionable lines kept (the backlog and the retry wait, the
embedding backlog, the parser upgrade, the yaml files PyYAML does not read, the MCP registration note), a block that is all zero not printed,
the partial-parse list behind `--details`. `--json` is the same as before. In memory: a 23-file project scanned once for the module (no `init`,
no process); the unit tests use the dicts the status data has."""

from __future__ import annotations

import json

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.core.status_text import short_lines
from drspec.db import init_schema

runner = CliRunner()

EMPTY = {
    "verification_evidence": {"passed": 0, "failed": 0, "partial": 0, "unverified": 0, "error": 0, "stale": 0},
    "artifacts": {"total": 0, "by_status": {"PENDING": 0, "STALE": 0, "VERIFIED": 0, "BROKEN": 0, "NEEDS_REVIEW": 0}},
    "backlog": {"files_pending_rescan": 0, "files_pending_discovery": 0, "files_in_retry_wait": 0},
    "contracts": {"total": 0, "confidence": {"average": 0.0, "distribution": {}}},
    "edges": {"calls": {"total": 0, "by_confidence": {}}, "references": {"total": 0, "by_confidence": {}}},
    "summary": {"items_needing_attention": 0, "completion_rate": 0.0},
}


def _with(**changes) -> dict:
    return {**EMPTY, **changes}


class TestTheDefaultLines:
    def test_an_all_zero_status_prints_only_the_lines_that_are_not_zero_blocks(self):
        assert short_lines(EMPTY) == ["Items Needing Attention: 0, completion 0.0%"]

    def test_the_artifacts_and_the_verification_keep_every_number(self):
        lines = short_lines(_with(
            verification_evidence={"passed": 1, "failed": 0, "partial": 0, "unverified": 39, "error": 0, "stale": 0},
            artifacts={"total": 40, "by_status": {"PENDING": 39, "STALE": 0, "VERIFIED": 1, "BROKEN": 0, "NEEDS_REVIEW": 0}},
        ))
        assert "Verification: unverified 39, passed 1, failed 0, partial 0, error 0, stale 0" in lines
        assert "Artifacts: 40 (pending 39, stale 0, verified 1, broken 0, needs review 0)" in lines

    def test_a_retry_wait_and_a_rescan_backlog_are_one_actionable_line(self):
        lines = short_lines(_with(backlog={"files_pending_rescan": 2, "files_pending_discovery": 0, "files_in_retry_wait": 1}))
        assert "Backlog: 2 pending rescan, 0 pending discovery, 1 in retry wait" in lines

    def test_an_embedding_backlog_is_on_its_line(self):
        message = "10 of 40 embedded (top_n = 2000): the others are not found by meaning"  # (T-156's sentence, as the scan writes it)
        embedding = {"enabled": True, "embedded": 10, "total": 40, "top_n": 2000, "backlog": 30, "state": "running", "message": message}
        assert f"Embedding: {message} [running], backlog 30" in short_lines(_with(backlog={"embedding": embedding}))

    def test_the_parser_upgrade_is_one_line_with_its_message(self):
        lines = short_lines(_with(parser_upgrade={"files_queued": 3, "message": "3 file(s) that an older parser read in part were queued for a re-read: 1 now parse whole"}))
        assert any(line.startswith("Parser Upgrade: 3 file(s)") for line in lines)

    def test_yaml_files_that_pyyaml_does_not_read_are_one_actionable_line(self):
        lines = short_lines(_with(yaml_config={"files": 4, "keys": 9, "files_with_problems": 0, "files_skipped": 4, "files_without_pyyaml": 4}))
        assert "Yaml Config: 4 file(s), 9 keys, 0 with problems, 4 skipped; 4 not read: PyYAML is not installed (pip install pyyaml)" in lines

    def test_yaml_files_that_are_read_are_counted_without_the_pyyaml_part(self):
        lines = short_lines(_with(yaml_config={"files": 2, "keys": 5, "files_with_problems": 1, "files_skipped": 0, "files_without_pyyaml": 0}))
        assert lines == ["Items Needing Attention: 0, completion 0.0%", "Yaml Config: 2 file(s), 5 keys, 1 with problems, 0 skipped"]

    def test_the_mcp_registration_note_is_a_line(self):
        note = "drspec MCP server: not registered for codex (run 'drspec init --ide codex')."
        assert f"MCP Server: {note}" in short_lines(_with(mcp_server={"registered": [], "not_registered": ["codex"], "note": note}))

    def test_the_partial_parse_is_one_line_with_its_counts(self):
        lines = short_lines(_with(partially_parsed={"files": 134, "functions_skipped": 5, "functions_partial": 9, "examples": [{"file": "a"}]}))
        assert "Partially Parsed: 134 files, 5 functions skipped, 9 partial (use --details to list them)" in lines
        assert not any("a" == line.strip() for line in lines)  # (the examples are not printed)

    def test_warnings_without_a_lost_function_are_one_line(self):
        assert "Read With Warnings: 3 file(s) (use --details to list them)" in short_lines(_with(files_read_with_warnings=3))

    def test_a_language_is_one_line_with_its_unresolved_share(self):
        lines = short_lines(_with(python={"files": 160, "files_partially_parsed": 0, "skipped_constructs": 0, "calls": 3340, "unresolved_calls": 3016, "unresolved_share": 90.3, "library_calls": 0}))
        assert "Python: 160 files, 0 partially parsed, 3,340 calls, 3016 unresolved (90.3%)" in lines

    def test_a_language_with_no_files_is_not_printed(self):
        assert not any(line.startswith("Rust") for line in short_lines(_with(rust={"files": 0, "calls": 0})))

    def test_generated_folders_are_one_line(self):
        assert "Generated folders left out: a, b (scan.include reads one)" in short_lines(_with(generated_folders={"left_out": ["generated folder: a", "generated folder: b"]}))

    def test_the_database_and_the_parsers_are_one_line_each(self):
        lines = short_lines(_with(database={"file_mb": 2.1, "used_mb": 1.3, "free_mb": 0.8, "free_percent": 37.5, "largest": []}, parsers={"count": 11, "held_down_by": "memory"}))
        assert "Database: 2.1 MB file, 1.3 MB used, 0.8 MB free (37.5%)" in lines and "Parsers: 11 (held down by memory)" in lines

    def test_the_item_count_is_always_there(self):
        assert "Items Needing Attention: 40, completion 0.0%" in short_lines(_with(summary={"items_needing_attention": 40, "completion_rate": 0.0}))


class TestTheCommandOnAProject:
    """A 23-file project, scanned once for the module (in memory): the default is under 40 lines, `--details` is the old text, and `--json` is
    the same whatever the text form."""

    @pytest.fixture(scope="class")
    def project(self, tmp_path_factory):
        from tests.fakes import KeptDuckDB

        folder = tmp_path_factory.mktemp("status23")
        with pytest.MonkeyPatch.context() as patch:
            KeptDuckDB().install(patch)
            (folder / "_drspec").mkdir()
            for i in range(20):
                (folder / f"m{i}.cpp").write_text(f"int f{i}(int a) {{ return a + {i}; }}\nint g{i}(int b) {{ return b * 2; }}\n")
            (folder / "other.py").write_text("x = 1\n")
            (folder / "b.py").write_text("y = 2\n")
            (folder / "README.md").write_text("# readme\n")
            connection = duckdb.connect(str(folder / "_drspec" / "contracts.db"))  # pattern-ok: duckdb-file kept in memory by `KeptDuckDB`
            init_schema(connection)
            connection.close()
            patch.chdir(folder)
            assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
            yield folder

    def test_the_default_is_under_forty_lines_and_has_the_numbers(self, project):
        text = runner.invoke(app, ["status"]).output.strip()
        assert len(text.split("\n")) < 40
        assert "Artifacts: 40 (pending 40" in text and "Items Needing Attention: 40" in text and "Cpp: 20 files" in text

    def test_details_is_the_whole_text(self, project):
        text = runner.invoke(app, ["status", "--details"]).output
        assert "Verification Evidence:" in text and "Artifacts:" in text and "Database:" in text and len(text.split("\n")) > 60

    def test_the_json_is_the_same_with_and_without_details(self, project):
        plain = runner.invoke(app, ["--json", "status"]).output
        details = runner.invoke(app, ["--json", "status", "--details"]).output
        assert plain == details and json.loads(plain)["data"]["artifacts"]["total"] == 40
        assert set(json.loads(plain)["data"]) >= {"verification_evidence", "artifacts", "backlog", "edges", "summary", "database"}
