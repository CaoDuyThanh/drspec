"""Tests for the `drspec search` command."""

import json
from pathlib import Path

from typer.testing import CliRunner

from drspec.cli.app import app
from tests.helpers import fid

runner = CliRunner()


class TestSearchCommand:
    def test_requires_init(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)  # an empty folder: no project
        result = runner.invoke(app, ["--json", "search", "foo"])

        assert result.exit_code == 1
        response = json.loads(result.output)
        assert response["error"]["code"] == "DB_NOT_INITIALIZED"

    def test_count_and_files_with_matches_are_mutually_exclusive(self, kept_project):

        result = runner.invoke(app, ["--json", "search", "foo", "-c", "-l"])

        assert result.exit_code == 1
        response = json.loads(result.output)
        assert response["error"]["code"] == "INVALID_INPUT"

    def test_semantic_rejects_regex_flag(self, kept_project):

        result = runner.invoke(app, ["--json", "search", "foo", "--semantic", "-E"])

        assert result.exit_code == 1
        response = json.loads(result.output)
        assert response["error"]["code"] == "INVALID_INPUT"

    def test_rejects_invalid_confidence(self, kept_project):

        result = runner.invoke(app, ["--json", "search", "foo", "--confidence", "NOT_A_TIER"])

        assert result.exit_code == 1
        response = json.loads(result.output)
        assert response["error"]["code"] == "INVALID_INPUT"

    def test_flags_work_before_and_after_the_pattern(self, kept_project):
        """`search` must be registered directly on the main app (not as a
        nested Typer sub-app), same as the fix for `scan` — otherwise a
        flag after the positional `pattern` silently fails to parse."""
        Path("a.py").write_text("def process_order():\n    pass\n")
        runner.invoke(app, ["scan", "."])

        before = runner.invoke(app, ["--json", "search", "--include", "python", "process"])
        after = runner.invoke(app, ["--json", "search", "process", "--include", "python"])

        assert before.exit_code == 0
        assert after.exit_code == 0
        assert json.loads(before.output)["data"]["total_matches"] == 1
        assert json.loads(after.output)["data"]["total_matches"] == 1

    def test_end_to_end_literal_search(self, kept_project):
        Path("a.py").write_text(
            "def process_order():\n    pass\n\n\ndef unrelated():\n    pass\n"
        )
        runner.invoke(app, ["scan", "."])

        result = runner.invoke(app, ["--json", "search", "process"])

        assert result.exit_code == 0
        response = json.loads(result.output)
        assert response["data"]["total_matches"] == 1
        assert response["data"]["matches"][0]["function_id"] == fid("a.py", "process_order")

    def test_count_mode_returns_count_not_matches(self, kept_project):
        Path("a.py").write_text("def process_order():\n    pass\n")
        runner.invoke(app, ["scan", "."])

        result = runner.invoke(app, ["--json", "search", "-c", "process"])

        assert result.exit_code == 0
        response = json.loads(result.output)
        assert response["data"]["count"] == 1
        assert "matches" not in response["data"]

    def test_semantic_search_end_to_end(self, kept_project, fake_embedder):
        """The whole path (vectors made at scan time, stored, the nearest ones found for the query, printed) with a fake model: it
        ranks by the words two texts share, so the function that talks about summing item prices is first and the unrelated one after."""
        Path("a.py").write_text(
            "def compute_total(order):\n    return sum(i.price for i in order.items)\n\n\n"
            "def open_socket(host):\n    return connect(host)\n"
        )
        # --embed is required since `drspec scan` doesn't compute
        # embeddings by default.
        runner.invoke(app, ["scan", "--embed", "."])

        result = runner.invoke(app, ["--json", "search", "--semantic", "sum item prices"])

        assert result.exit_code == 0
        response = json.loads(result.output)
        assert response["data"]["semantic"] is True
        matches = response["data"]["matches"]
        assert matches[0]["function_id"] == fid("a.py", "compute_total", "order")
        assert "score" in matches[0]
        assert [m["function_id"] for m in matches][-1] == fid("a.py", "open_socket", "host")  # the unrelated function is ranked after
        assert matches[0]["score"] > matches[-1]["score"]

    def test_semantic_search_without_embed_gives_a_clear_hint(self, kept_project, fake_embedder):
        """'--semantic' with zero embeddings anywhere (the normal state for
        a project that's never run --embed) must say why, not return an
        unexplained empty list."""
        Path("a.py").write_text("def compute_total(order):\n    return 1\n")
        runner.invoke(app, ["scan", "."])

        result = runner.invoke(app, ["--json", "search", "--semantic", "sum item prices"])

        assert result.exit_code == 0
        response = json.loads(result.output)
        assert response["data"]["total_matches"] == 0
        assert "--embed" in response["data"]["message"]


def _project(files):
    """Write the files and scan them in the current folder (a `kept_project`: its database is made, no `init`)."""
    for name, text in files.items():
        Path(name).write_text(text)
    runner.invoke(app, ["scan", "."])


def _search(*args):
    result = runner.invoke(app, ["--json", "search", *args])
    return result, json.loads(result.output)


class TestSearchLines:
    """`--lines` searches the live contents of the indexed files and returns
    flat `file:line:text` hits. Function matches from the database can't do
    this: they carry no line numbers and miss any line outside a function
    body."""

    def test_reports_every_occurrence_labelled_with_its_enclosing_function(self, kept_project):
        _project({"a.py": "import os.path as get_connection\n\n\ndef foo():\n    return get_connection\n"})

        result, response = _search("get_connection", "--lines")

        assert result.exit_code == 0
        assert response["data"]["hits"] == [
            {"file_path": "a.py", "line": 1, "text": "import os.path as get_connection", "function_id": None},
            {"file_path": "a.py", "line": 5, "text": "    return get_connection", "function_id": fid("a.py", "foo")},
        ]
        assert response["data"]["total_hits"] == 2
        assert response["data"]["truncated"] is False

    def test_finds_a_line_that_no_function_body_contains(self, kept_project):
        """A module-level constant appears in no function's stored body, so a
        function search for it returns nothing at all."""
        _project({"a.py": "LIMIT = 5\n\n\ndef foo():\n    return 1\n"})

        _, function_search = _search("LIMIT = 5")
        _, line_search = _search("LIMIT = 5", "--lines")

        assert function_search["data"]["total_matches"] == 0
        assert [(h["line"], h["text"]) for h in line_search["data"]["hits"]] == [(1, "LIMIT = 5")]

    def test_a_line_is_reported_once_however_many_functions_match_in_its_file(self, kept_project):
        _project({"a.py": "def f1():\n    return marker\n\n\ndef f2():\n    return marker\n\n\ndef f3():\n    return marker\n"})

        _, response = _search("marker", "--lines")

        assert response["data"]["total_hits"] == 3
        assert [h["function_id"] for h in response["data"]["hits"]] == [fid("a.py", "f1"), fid("a.py", "f2"), fid("a.py", "f3")]

    def test_regex_mode_applies_to_the_live_search_too(self, kept_project):
        _project({"a.py": "def foo_1():\n    pass\n\n\ndef foo_2():\n    pass\n"})

        _, response = _search("-E", r"^def foo_\d+", "--lines")

        assert [h["line"] for h in response["data"]["hits"]] == [1, 5]

    def test_path_and_include_narrow_the_files_searched(self, kept_project):
        Path("pkg").mkdir()
        _project({"a.py": "def f():\n    return marker\n", "pkg/b.py": "def g():\n    return marker\n"})

        _, by_path = _search("marker", "--lines", "--path", "pkg/")
        _, by_language = _search("marker", "--lines", "--include", "cpp")

        assert [h["file_path"] for h in by_path["data"]["hits"]] == ["pkg/b.py"]
        assert by_language["data"]["hits"] == []

    def test_limit_truncates_and_says_so(self, kept_project):
        _project({"a.py": "def f():\n    a = marker\n    b = marker\n    c = marker\n"})

        _, response = _search("marker", "--lines", "--limit", "2")

        assert response["data"]["total_hits"] == 2
        assert response["data"]["truncated"] is True
        assert [h["line"] for h in response["data"]["hits"]] == [2, 3]

    def test_a_file_deleted_since_the_last_scan_is_reported_not_hidden(self, kept_project):
        _project({"a.py": "def foo():\n    return 1\n"})
        Path("a.py").unlink()

        result, response = _search("foo", "--lines")

        assert result.exit_code == 0
        assert response["data"]["hits"] == []
        assert [f["file_path"] for f in response["data"]["unreadable_files"]] == ["a.py"]

    def test_hits_in_a_possibly_stale_file_are_not_labelled(self, kept_project):
        """Stored line ranges are from the last scan; once a file has
        drifted, naming a function from them could name the wrong one."""
        from drspec.db import enqueue_staleness, get_connection

        _project({"a.py": "def foo():\n    return marker\n"})
        conn = get_connection(Path("_drspec/contracts.db"))
        enqueue_staleness(conn, "a.py", "mtime_changed")
        conn.close()

        _, response = _search("marker", "--lines")

        assert response["data"]["hits"][0]["function_id"] is None
        assert response["data"]["possibly_stale_files"] == ["a.py"]

    def test_an_edit_made_after_the_scan_is_not_labelled_from_the_old_line_ranges(self, kept_project):
        """Before the worker's next poll flags the file, the stored ranges still
        say `foo` is lines 1-2, so a hit on line 2 (now inside `bar`) would
        have been labelled `foo`."""
        _project({"a.py": "def foo():\n    return marker\n"})
        Path("a.py").write_text("def bar():\n    return marker\n\n\ndef foo():\n    return 1\n")

        _, response = _search("marker", "--lines")

        assert [(h["line"], h["function_id"]) for h in response["data"]["hits"]] == [(2, None)]
        assert response["data"]["possibly_stale_files"] == ["a.py"]

    def test_a_file_added_after_the_last_scan_is_searched_and_flagged_not_silently_skipped(self, kept_project):
        _project({"a.py": "def foo():\n    return 1\n"})
        Path("added.py").write_text("def later():\n    return marker\n")

        _, response = _search("marker", "--lines")

        assert [(h["file_path"], h["function_id"]) for h in response["data"]["hits"]] == [("added.py", None)]
        assert response["data"]["unscanned_files"]["examples"] == ["added.py"]

        runner.invoke(app, ["scan", "."])
        _, after_scan = _search("marker", "--lines")

        assert [(h["file_path"], h["function_id"]) for h in after_scan["data"]["hits"]] == [("added.py", fid("added.py", "later"))]
        assert "unscanned_files" not in after_scan["data"]


    def test_a_form_feed_before_a_function_does_not_shift_the_line_or_its_label(self, kept_project):
        _project({"a.py": "def a():\n    return 1\n\x0c\ndef b():\n    return marker\n"})

        _, response = _search("marker", "--lines")

        assert [(h["line"], h["function_id"]) for h in response["data"]["hits"]] == [(5, fid("a.py", "b"))]

    def test_a_file_with_no_functions_is_searched_once_scanned(self, kept_project):
        _project({"a.py": "def foo():\n    return 1\n", "constants.py": "LIMIT = marker\n"})

        _, response = _search("marker", "--lines")

        assert [h["file_path"] for h in response["data"]["hits"]] == ["constants.py"]
        assert "unscanned_files" not in response["data"]

    def test_rejected_when_combined_with_count_or_files_with_matches_or_semantic(self, kept_project):

        for flags in (["-c"], ["-l"], ["--semantic"]):
            result, response = _search("foo", "--lines", *flags)
            assert result.exit_code == 1
            assert response["error"]["code"] == "INVALID_INPUT"

    def test_rejected_when_combined_with_function_level_filters(self, kept_project):

        for flags in (["-v"], ["--status", "PENDING"], ["--confidence", "EXTRACTED"]):
            result, response = _search("foo", "--lines", *flags)
            assert result.exit_code == 1
            assert response["error"]["code"] == "INVALID_INPUT"

    def test_an_invalid_regex_is_a_clean_error(self, kept_project):
        _project({"a.py": "def foo():\n    return 1\n"})

        result, response = _search("-E", "(unclosed", "--lines")

        assert result.exit_code == 1
        assert response["error"]["code"] == "INVALID_INPUT"
        assert "Invalid regex" in response["error"]["message"]

    def test_default_text_output_is_grep_shaped(self, kept_project):
        _project({"a.py": "LIMIT = 5\n\n\ndef foo():\n    return LIMIT\n"})

        result = runner.invoke(app, ["search", "LIMIT", "--lines"])

        assert result.exit_code == 0
        assert "  a.py:1:LIMIT = 5\n" in result.output
        assert "  a.py:5:    return LIMIT  [in %s]" % fid("a.py", "foo") in result.output


class TestSearchContext:
    """`-A`/`-B`/`-C` mirror grep's own meaning, over the same live search."""

    def test_after_context_includes_trailing_lines_for_a_module_level_match(self, kept_project):
        _project({"a.py": "import target\n\n\ndef foo():\n    return target\n"})

        result, response = _search("import target", "-A", "2")

        assert result.exit_code == 0
        (block,) = response["data"]["blocks"]
        assert (block["start_line"], block["end_line"]) == (1, 3)
        assert block["matched_lines"] == [1]
        assert [c["line"] for c in block["context"]] == [1, 2, 3]
        assert block["functions"] == []

    def test_context_flag_sets_both_sides_overridden_by_explicit_after(self, kept_project):
        _project({"a.py": "# line1\n# line2\ntarget_here = 1\n# line4\n# line5\n# line6\n\n\ndef foo():\n    return 1\n"})

        _, response = _search("target_here", "-C", "1", "-A", "3")

        block = response["data"]["blocks"][0]
        # -C 1 gives before=1; explicit -A 3 overrides after to 3.
        assert (block["start_line"], block["end_line"]) == (2, 6)

    def test_overlapping_occurrences_merge_into_one_block(self, kept_project):
        _project({"a.py": "def foo():\n    marker_1 = 1\n    marker_2 = 2\n    return marker_1\n"})

        _, response = _search("-E", r"marker_\d", "-A", "1", "-B", "1")

        (block,) = response["data"]["blocks"]
        assert block["matched_lines"] == [2, 3, 4]
        assert block["functions"] == [fid("a.py", "foo")]

    def test_separate_files_get_separate_blocks(self, kept_project):
        _project({"a.py": "def f():\n    return marker\n", "b.py": "def g():\n    return marker\n"})

        _, response = _search("marker", "-C", "1")

        assert [b["file_path"] for b in response["data"]["blocks"]] == ["a.py", "b.py"]

    def test_default_text_output_marks_matches_and_context_like_grep(self, kept_project):
        _project({"a.py": "X = 1\nneedle = 2\nY = 3\n\n\ndef f():\n    return 1\n"})

        result = runner.invoke(app, ["search", "needle", "-C", "1"])

        assert result.exit_code == 0
        assert "    1- X = 1" in result.output
        assert "    2: needle = 2" in result.output
        assert "    3- Y = 3" in result.output

    def test_rejected_when_combined_with_count_or_files_with_matches_or_semantic(self, kept_project):

        for flags in (["-c"], ["-l"], ["--semantic"]):
            result, response = _search("foo", "-A", "1", *flags)
            assert result.exit_code == 1
            assert response["error"]["code"] == "INVALID_INPUT"
