"""The drspec scan command: which files a scan looks at, what its summary says, and the errors it gives before any work.

The rules a scan follows (which files `_scope` keeps, what `summarize_by_reason` and `bounded_errors` cut, how the text
is laid out) are pure functions: they are tested directly, with strings and dicts. Two tests run a real scan, because
the scan itself is what they check: a tree of files read end to end, and a scan that fails half way.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.cli.commands.scan import resolve_targets
from drspec.cli.output import render_text, success_response
from drspec.core.graph_sync import SyncStats
from drspec.core.scan_service import (
    _scope,
    bounded_errors,
    new_run,
    stats_dict,
    summarize,
    summarize_by_reason,
)
from drspec.worker_ipc import WorkerIPCError

runner = CliRunner()

_TEMPLATE: dict = {}


@pytest.fixture(autouse=True, scope="session")
def _template_drspec(tmp_path_factory):
    """The `_drspec` folder that `drspec init` makes, built once for the session and copied by the tests that scan."""
    root = tmp_path_factory.mktemp("scan_template")
    result = runner.invoke(app, ["init", "--project-root", str(root)])
    assert result.exit_code == 0, result.output
    _TEMPLATE["drspec"] = root / "_drspec"
    yield


@pytest.fixture(autouse=True)
def _listing_without_git(monkeypatch):
    """A scan lists the project's files; with git it would ask `git` what it tracks. The listing here is the folder's
    files, read from disk, so no git runs."""
    from drspec.core import project_files

    monkeypatch.setattr(project_files, "inside_git", lambda root: False)


def _project_from_template(folder: Path) -> Path:
    """A folder with a copy of the initialised `_drspec` (no `init` per test)."""
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copytree(_TEMPLATE["drspec"], folder / "_drspec")
    return folder


class TestWhereAScanLooks:
    """Which files a scan's targets select: a pure function of the path strings."""

    @pytest.mark.parametrize(
        "paths, recursive, candidate, expected",
        [
            (["src"], True, "src/core/engine.py", True),
            (["src"], False, "src/core/engine.py", False),
            (["src"], False, "src/module.py", True),
            (["src"], True, "other.py", False),
            (["."], False, "a/b.py", False),
            (["."], False, "a.py", True),
            (["."], True, "a/b.py", True),
        ],
    )
    def test_the_targets_and_recursion_select_the_right_files(self, paths, recursive, candidate, expected):
        assert _scope(paths, recursive)(candidate) is expected

    def test_no_target_means_no_filter(self):
        assert _scope([], True) is None

    def test_a_target_that_does_not_exist_is_named_and_nothing_is_resolved(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert resolve_targets(["nope.py"]) == ([], "nope.py")


class TestTheCommandRefusesBeforeAnyWork:
    """The error codes that come before a scan reads anything. No `init` and no scan: a folder with an empty database
    file, or none, is all the command looks at."""

    def test_a_folder_that_is_not_initialised_says_so(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["--json", "scan"])
        assert result.exit_code == 1
        assert json.loads(result.output)["error"]["code"] == "DB_NOT_INITIALIZED"

    def test_a_missing_path_is_named_as_given(self, tmp_path, monkeypatch):
        (tmp_path / "_drspec").mkdir()
        (tmp_path / "_drspec" / "contracts.db").write_text("")
        monkeypatch.chdir(tmp_path)
        missing = str(tmp_path / "no-such-folder")
        result = runner.invoke(app, ["--json", "scan", missing])
        assert result.exit_code == 1
        error = json.loads(result.output)["error"]
        assert error["code"] == "PATH_NOT_FOUND" and error["message"].endswith(missing)

    def test_a_worker_that_cannot_be_reached_is_a_clean_error(self, tmp_path, monkeypatch):
        (tmp_path / "_drspec").mkdir()
        (tmp_path / "_drspec" / "contracts.db").write_text("")
        (tmp_path / "a.py").write_text("def foo():\n    pass\n")
        monkeypatch.chdir(tmp_path)
        with patch("drspec.cli.commands.scan.get_worker_connection", side_effect=WorkerIPCError("could not reach the drspec worker")):
            result = runner.invoke(app, ["--json", "scan", "."])
        assert result.exit_code == 1
        assert json.loads(result.output)["error"]["code"] == "DATABASE_BUSY"


class TestOneTreeScan:
    """A real scan of one small tree, end to end: the languages, nesting, an error file and an empty folder, then a
    second scan that reads nothing, and a `--full` scan. One test, so the tree is built and scanned once."""

    def test_the_tree_is_read_once_then_unchanged_then_in_full(self, tmp_path, monkeypatch):
        project = _project_from_template(tmp_path / "project")
        (project / "test.py").write_text("def hello():\n    return 'world'\n\n\ndef add(a, b):\n    return a + b\n")
        (project / "src" / "core").mkdir(parents=True)
        (project / "src" / "module.py").write_text("def process():\n    pass\n")
        (project / "src" / "core" / "engine.py").write_text("def bar():\n    pass\n")
        (project / "app.js").write_text("function one() { return 1; }\nfunction two() { return 2; }\n")
        (project / "bad.py").write_text("def broken(:\n")
        (project / "empty").mkdir()
        monkeypatch.chdir(project)

        result = runner.invoke(app, ["--json", "scan"])
        assert result.exit_code == 0
        response = json.loads(result.output)
        assert "success" in response and "data" in response and "error" in response
        data = response["data"]
        assert data["files_scanned"] == 5 and data["functions_found"] == 6 and data["functions_new"] == 6
        assert data["error_count"] == 1
        assert data["resolution_reasons"]["total"] == 6

        again = json.loads(runner.invoke(app, ["--json", "scan"]).output)["data"]
        assert again["up_to_date"] is True and again["files_unchanged"] == 5
        assert again["message"] == "Up to date, nothing to do"

        full = json.loads(runner.invoke(app, ["--json", "scan", "--full"]).output)["data"]
        assert full["functions_unchanged"] == 6 and full["functions_new"] == 0

        only_top = json.loads(runner.invoke(app, ["--json", "scan", "--full", "--no-recursive", "src"]).output)["data"]
        assert only_top["files_scanned"] == 1
        empty = json.loads(runner.invoke(app, ["--json", "scan", "--full", "empty"]).output)["data"]
        assert empty["files_scanned"] == 0 and empty["functions_found"] == 0


class TestAScanThatFailsHalfWay:
    """A real scan. The failure is after the publish has written rows, and the scan must leave none of them behind."""

    def test_a_failure_after_the_write_leaves_the_database_as_it_was(self, tmp_path, monkeypatch):
        project = _project_from_template(tmp_path / "project")
        (project / "a.py").write_text("def foo():\n    pass\n")
        monkeypatch.chdir(project)

        import drspec.staleness_worker as worker_module

        real_sync = worker_module.sync_scan_results

        def sync_then_fail(*args, **kwargs):
            real_sync(*args, **kwargs)
            raise RuntimeError("boom")

        with patch("drspec.staleness_worker.sync_scan_results", side_effect=sync_then_fail):
            result = runner.invoke(app, ["--json", "scan", "."])
        assert result.exit_code == 1
        assert json.loads(result.output)["success"] is False

        connection = duckdb.connect(str(project / "_drspec" / "contracts.db"), read_only=True)
        try:
            assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
        finally:
            connection.close()


class TestTheSummaryIsBounded:
    """The pure functions that cut a summary: counts stay whole, lists stop at twenty, and the text stays short."""

    def test_a_large_summary_counts_everything_and_lists_twenty(self):
        summary = summarize_by_reason({f"a.py::f{i}": "symbols_changed" for i in range(30)})
        assert summary["total"] == 30
        assert summary["by_reason"] == {"symbols_changed": 30}
        assert len(summary["examples"]) == 20

    def test_a_small_summary_lists_every_entry(self):
        summary = summarize_by_reason({"a.py::f": "forced", "b.py::g": "new_file"})
        assert len(summary["examples"]) == 2

    def test_errors_are_capped_but_counted(self):
        data = bounded_errors([{"file": f"bad{i}.py"} for i in range(25)])
        assert data["error_count"] == 25 and len(data["errors"]) == 20 and data["errors_omitted"] == 5

    def test_twenty_errors_are_all_listed_with_no_omission_stated(self):
        data = bounded_errors([{"file": f"bad{i}.py"} for i in range(20)])
        assert len(data["errors"]) == 20 and "errors_omitted" not in data

    def test_the_text_output_stays_short_for_a_large_scan(self):
        run = new_run("x", 1)
        run["stats"] = stats_dict(SyncStats(resolution_reasons={f"a.py::f{i}": "symbols_changed" for i in range(300)}, functions_new=300))
        run["functions_found"] = 300
        data = {"path": ".", **summarize({"queued": 1, "scan_id": "x"}, run, "done")}
        assert len(render_text(success_response(data)).splitlines()) < 80
