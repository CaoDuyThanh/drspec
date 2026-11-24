"""Tests for the live line search and the DB helpers it relies on. The index is in memory; the files searched are real ones in `tmp_path` (reading
the files as they are now, not as they were scanned, is what a live search is)."""

from __future__ import annotations

import json

import duckdb
import pytest

from drspec.core.live_search import (
    MAX_LINE_CHARS,
    InvalidPattern,
    _enclosing_function,
    live_search,
)
from drspec.db import init_schema, insert_artifact
from drspec.db.queries import (
    list_function_ranges,
    list_indexed_files,
    upsert_scanned_file,
)

pytestmark = pytest.mark.unit  # an in-memory index and a few small files in tmp_path


def _add(conn, file_path, name, start, end, language="python"):
    insert_artifact(
        conn,
        function_id=f"{file_path}::{name}",
        file_path=file_path,
        function_name=name,
        signature=f"def {name}():",
        body="",
        code_hash=f"hash_{file_path}_{name}",
        language=language,
        start_line=start,
        end_line=end,
        status="PENDING",
    )


@pytest.fixture
def conn():
    connection = duckdb.connect(":memory:")  # the index is read through SQL only: no file
    init_schema(connection)
    yield connection
    connection.close()


class TestEnclosingFunction:
    def test_picks_the_innermost_of_nested_ranges(self):
        ranges = [("outer", 1, 20), ("inner", 5, 8)]
        assert _enclosing_function(ranges, 6) == "inner"
        assert _enclosing_function(ranges, 12) == "outer"

    def test_none_outside_every_range(self):
        assert _enclosing_function([("f", 3, 5)], 9) is None


class TestDbHelpers:
    def test_list_indexed_files_filters_by_language_and_exact_prefix(self, conn):
        _add(conn, "src/a.py", "f", 1, 2)
        _add(conn, "src_x/b.py", "g", 1, 2)
        _add(conn, "src/c.cpp", "h", 1, 2, language="cpp")

        assert list_indexed_files(conn) == ["src/a.py", "src/c.cpp", "src_x/b.py"]
        assert list_indexed_files(conn, language="python") == ["src/a.py", "src_x/b.py"]
        # `_` is a literal character here, not a LIKE wildcard matching `/`.
        assert list_indexed_files(conn, path_prefix="src_") == ["src_x/b.py"]

    def test_list_function_ranges_groups_by_file_and_handles_no_files(self, conn):
        _add(conn, "a.py", "f", 1, 3)
        _add(conn, "a.py", "g", 5, 7)
        _add(conn, "b.py", "h", 1, 2)

        ranges = list_function_ranges(conn, ["a.py", "b.py"])

        assert sorted(ranges["a.py"]) == [("a.py::f", 1, 3), ("a.py::g", 5, 7)]
        assert ranges["b.py"] == [("b.py::h", 1, 2)]
        assert list_function_ranges(conn, []) == {}


class TestLiveSearch:
    def test_a_non_utf8_file_is_reported_unreadable_not_fatal(self, conn, tmp_path):
        _add(conn, "good.py", "f", 1, 2)
        _add(conn, "bad.py", "g", 1, 2)
        (tmp_path / "good.py").write_text("def f():\n    return needle\n")
        (tmp_path / "bad.py").write_bytes(b"def g():\n    return '\xff\xfe'\n")

        data = live_search(conn, "needle", root=tmp_path)

        assert [h["file_path"] for h in data["hits"]] == ["good.py"]
        assert [f["file_path"] for f in data["unreadable_files"]] == ["bad.py"]
        assert data["files_searched"] == 1

    def test_invalid_regex_raises_a_dedicated_error(self, conn, tmp_path):
        with pytest.raises(InvalidPattern, match="Invalid regex"):
            live_search(conn, "(unclosed", regex=True, root=tmp_path)

    def test_ignore_case_applies_to_literal_and_regex_patterns(self, conn, tmp_path):
        _add(conn, "a.py", "f", 1, 2)
        (tmp_path / "a.py").write_text("def f():\n    return Needle\n")

        assert live_search(conn, "needle", root=tmp_path)["total_hits"] == 0
        assert live_search(conn, "needle", ignore_case=True, root=tmp_path)["total_hits"] == 1
        assert live_search(conn, "n.edle", regex=True, ignore_case=True, root=tmp_path)["total_hits"] == 1

    def test_limit_spans_files_and_keeps_file_order(self, conn, tmp_path):
        for name in ("a.py", "b.py"):
            _add(conn, name, "f", 1, 3)
            (tmp_path / name).write_text("def f():\n    x = needle\n    y = needle\n")

        data = live_search(conn, "needle", limit=3, root=tmp_path)

        assert [(h["file_path"], h["line"]) for h in data["hits"]] == [("a.py", 2), ("a.py", 3), ("b.py", 2)]
        assert data["truncated"] is True

        exact = live_search(conn, "needle", limit=4, root=tmp_path)
        assert exact["total_hits"] == 4
        assert exact["truncated"] is False

    def test_context_windows_are_clamped_to_the_file(self, conn, tmp_path):
        _add(conn, "a.py", "f", 1, 2)
        (tmp_path / "a.py").write_text("needle\nsecond\n")

        (block,) = live_search(conn, "needle", before=5, after=5, root=tmp_path)["blocks"]

        assert (block["start_line"], block["end_line"]) == (1, 2)


def _snapshot(conn, tmp_path, name):
    """Record `name` as scanned at its current size/mtime."""
    stat = (tmp_path / name).stat()
    upsert_scanned_file(conn, name, stat.st_mtime, stat.st_size)


class TestFreshnessOfLabels:
    def test_a_file_edited_since_the_scan_is_not_labelled_before_the_worker_notices(self, conn, tmp_path):
        """The staleness queue only learns of an edit on the worker's next
        poll; until then the stored ranges silently named the wrong function."""
        _add(conn, "a.py", "f", 1, 2)
        _add(conn, "b.py", "g", 1, 2)
        for name in ("a.py", "b.py"):
            (tmp_path / name).write_text("def f():\n    return needle\n")
            _snapshot(conn, tmp_path, name)
        (tmp_path / "a.py").write_text("import os\nimport sys\n\n\ndef f():\n    return needle\n")  # edited after the scan

        data = live_search(conn, "needle", root=tmp_path)

        by_file = {h["file_path"]: h["function_id"] for h in data["hits"]}
        assert by_file == {"a.py": None, "b.py": "b.py::g"}
        assert data["possibly_stale_files"] == ["a.py"]
        assert [(row[0], row[1]) for row in conn.execute("SELECT file_path, reason FROM staleness_queue").fetchall()] == [("a.py", "search_drift")]  # left for the worker

    def test_a_deleted_file_is_reported_not_labelled(self, conn, tmp_path):
        _add(conn, "a.py", "f", 1, 2)
        (tmp_path / "a.py").write_text("def f():\n    return needle\n")
        _snapshot(conn, tmp_path, "a.py")
        (tmp_path / "a.py").unlink()

        data = live_search(conn, "needle", root=tmp_path)

        assert data["hits"] == []
        assert [f["file_path"] for f in data["unreadable_files"]] == ["a.py"]


class TestFilesNotInTheGraph:
    def test_a_file_added_since_the_last_scan_is_searched_and_listed(self, conn, tmp_path):
        _add(conn, "a.py", "f", 1, 2)
        (tmp_path / "a.py").write_text("def f():\n    return 1\n")
        _snapshot(conn, tmp_path, "a.py")
        (tmp_path / "new.py").write_text("def added():\n    return needle\n")

        data = live_search(conn, "needle", root=tmp_path)

        assert [(h["file_path"], h["function_id"]) for h in data["hits"]] == [("new.py", None)]
        assert data["unscanned_files"]["count"] == 1
        assert data["unscanned_files"]["examples"] == ["new.py"]
        assert "drspec scan" in data["unscanned_files"]["hint"]
        assert [(row[0], row[1]) for row in conn.execute("SELECT file_path, reason FROM staleness_queue").fetchall()] == [("new.py", "search_unscanned")]

    def test_a_scanned_file_with_no_functions_is_searched_but_not_flagged(self, conn, tmp_path):
        (tmp_path / "constants.py").write_text("LIMIT = needle\n")
        _snapshot(conn, tmp_path, "constants.py")

        data = live_search(conn, "needle", root=tmp_path)

        assert [h["file_path"] for h in data["hits"]] == ["constants.py"]
        assert "unscanned_files" not in data

    def test_nothing_unscanned_means_no_such_key(self, conn, tmp_path):
        _add(conn, "a.py", "f", 1, 2)
        (tmp_path / "a.py").write_text("def f():\n    return needle\n")
        _snapshot(conn, tmp_path, "a.py")

        assert "unscanned_files" not in live_search(conn, "needle", root=tmp_path)

    def test_the_listed_examples_are_capped_but_counted(self, conn, tmp_path):
        for i in range(15):
            (tmp_path / f"new{i:02d}.py").write_text("x = 1\n")

        data = live_search(conn, "needle", root=tmp_path)

        assert data["unscanned_files"]["count"] == 15
        assert len(data["unscanned_files"]["examples"]) == 10

    def test_ignored_directories_and_unsupported_files_are_not_searched(self, conn, tmp_path):
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "dep.js").write_text("var needle;\n")
        (tmp_path / "notes.md").write_text("needle\n")
        (tmp_path / "real.py").write_text("needle = 1\n")

        data = live_search(conn, "needle", root=tmp_path)

        assert [h["file_path"] for h in data["hits"]] == ["real.py"]

    def test_scope_narrowing_applies_to_files_on_disk(self, conn, tmp_path):
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "b.py").write_text("needle = 1\n")
        (tmp_path / "pkg" / "c.py").write_text("needle = 2\n")
        (tmp_path / "top.py").write_text("needle = 3\n")
        (tmp_path / "util.js").write_text("var needle = 4;\n")

        def files(**kwargs):
            return [h["file_path"] for h in live_search(conn, "needle", root=tmp_path, **kwargs)["hits"]]

        assert files(path_prefix="pkg/") == ["pkg/b.py", "pkg/c.py"]
        assert files(path_prefix="pkg/b") == ["pkg/b.py"]  # a partial name, not a directory
        assert files(path_prefix="top.py") == ["top.py"]  # a single file
        assert files(language="javascript") == ["util.js"]


class TestLineNumbersMatchGrep:
    """`str.splitlines()` also splits on form feed, vertical tab and U+2028,
    so every later line number drifted from what `grep -n` and an editor show."""

    @pytest.mark.parametrize(
        "separator",
        ["\x0c", "\x0b", "\x1c", "\x85", "\u2028", "\u2029"],
        ids=["form-feed", "vertical-tab", "file-separator", "next-line", "line-separator", "paragraph-separator"],
    )
    def test_characters_that_are_not_newlines_do_not_shift_line_numbers(self, conn, tmp_path, separator):
        (tmp_path / "a.py").write_bytes(f"a = 1\n# before{separator}after\nneedle = 2\n".encode("utf-8"))

        (hit,) = live_search(conn, "needle", root=tmp_path)["hits"]

        assert hit["line"] == 3

    def test_crlf_files_report_the_same_numbers_and_no_trailing_carriage_return(self, conn, tmp_path):
        (tmp_path / "a.py").write_bytes(b"a = 1\r\nb = 2\r\nneedle = 3\r\n")

        (hit,) = live_search(conn, "needle", root=tmp_path)["hits"]

        assert (hit["line"], hit["text"]) == (3, "needle = 3")

    def test_a_file_without_a_final_newline_and_an_empty_file(self, conn, tmp_path):
        (tmp_path / "a.py").write_text("x = 1\nneedle = 2")
        (tmp_path / "empty.py").write_text("")

        data = live_search(conn, "needle", root=tmp_path)

        assert [(h["file_path"], h["line"]) for h in data["hits"]] == [("a.py", 2)]
        assert data["files_searched"] == 2

    def test_a_blank_last_line_is_still_a_line(self, conn, tmp_path):
        (tmp_path / "a.py").write_text("needle\n\n\n")

        (hit,) = live_search(conn, "needle", root=tmp_path)["hits"]
        (block,) = live_search(conn, "needle", after=5, root=tmp_path)["blocks"]

        assert hit["line"] == 1
        assert block["end_line"] == 3


class TestLongLines:
    """A minified bundle is a single line; one hit used to return all of it
    (a 300,000-character response)."""

    def test_a_long_line_is_clipped_around_the_match(self, conn, tmp_path):
        (tmp_path / "bundle.js").write_text("x" * 5000 + "needle" + "y" * 5000 + "\n")

        (hit,) = live_search(conn, "needle", root=tmp_path)["hits"]

        assert "needle" in hit["text"]
        assert len(hit["text"]) <= MAX_LINE_CHARS + 2  # the window plus an ellipsis at each end
        assert hit["text"].startswith("…") and hit["text"].endswith("…")
        assert hit["line_length"] == 10006


    def test_a_match_at_the_start_or_end_keeps_a_full_window(self, conn, tmp_path):
        (tmp_path / "start.js").write_text("needle" + "y" * 5000 + "\n")
        (tmp_path / "end.js").write_text("x" * 5000 + "needle\n")

        hits = {h["file_path"]: h["text"] for h in live_search(conn, "needle", root=tmp_path)["hits"]}

        assert hits["start.js"].startswith("needle") and hits["start.js"].endswith("…")
        assert hits["end.js"].startswith("…") and hits["end.js"].endswith("needle")
        assert all(len(text) <= MAX_LINE_CHARS + 1 for text in hits.values())

    def test_lines_within_the_limit_are_returned_whole_without_a_length_key(self, conn, tmp_path):
        line = "n" * (MAX_LINE_CHARS - len("needle")) + "needle"
        (tmp_path / "a.py").write_text(line + "\n")

        (hit,) = live_search(conn, "needle", root=tmp_path)["hits"]

        assert hit["text"] == line
        assert "line_length" not in hit

    def test_context_lines_are_clipped_too(self, conn, tmp_path):
        (tmp_path / "a.js").write_text("short\n" + "z" * 5000 + "\nneedle\n")

        (block,) = live_search(conn, "needle", before=2, root=tmp_path)["blocks"]

        long_entry = block["context"][1]
        assert len(long_entry["text"]) <= MAX_LINE_CHARS + 1
        assert long_entry["line_length"] == 5000
        assert "line_length" not in block["context"][0]

    def test_the_whole_response_stays_small_for_a_minified_file(self, conn, tmp_path):
        (tmp_path / "min.js").write_text("var x=" + "0," * 150000 + "needle;\n")

        assert len(json.dumps(live_search(conn, "needle", root=tmp_path))) < 2000
