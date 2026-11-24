"""`drspec scan` asks the worker for what changed and waits; the worker reads the files in batches, in a top tier, and a
scan's results add up across batches. The same publish serves code and documents, and an unchanged project costs
nothing."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.cli.output import output, success_response
from drspec.core import docs as docs_core
from drspec.core import scan_service
from drspec.core.scan_service import merge_stats, summarize_by_reason
from drspec.core.settings import set_setting
from drspec.db import get_connection, init_schema
from drspec.db.queries import (
    enqueue_staleness_many,
    list_staleness_queue,
    next_rescan_batch,
)
from tests.fakes import unreadable

runner = CliRunner()

OLD_SUMMARY_FIELDS = (
    "files_scanned functions_found functions_new functions_changed functions_unchanged functions_removed carried_over "
    "resolution_reasons contracts_invalidated embeddings_updated edges_inserted edges_updated edges_promoted edges_deleted "
    "references_inserted references_updated references_deleted documents_indexed documentation_sections_to_review "
    "path recursive deep_scan embed force"
).split()
NEW_SUMMARY_FIELDS = "files_queued files_done files_failed files_cancelled files_unchanged neighbours_re_resolved up_to_date state".split()


def _scan(*arguments: str) -> dict:
    result = runner.invoke(app, ["--json", "scan", *arguments])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["data"]


def _db(project: Path):
    connection = get_connection(project / "_drspec" / "contracts.db")
    init_schema(connection)
    return connection


@pytest.fixture(scope="module")
def _initialised_project(tmp_path_factory, initialised_template):
    """The project as written and set up once for the module: an initialised copy of the session's project and `config set`
    (about 230 ms each time). Each test gets a copy of it (see `project`)."""
    template = tmp_path_factory.mktemp("initialised")
    shutil.copytree(initialised_template, template, dirs_exist_ok=True)
    (template / "pkg").mkdir()
    (template / "docs").mkdir()
    (template / "pkg" / "a.py").write_text("def helper(x):\n    return x + 1\n\n\ndef run(y):\n    return helper(y) * 2\n")
    (template / "pkg" / "b.py").write_text("from pkg.a import helper\n\n\ndef use():\n    return helper(3)\n")
    (template / "docs" / "guide.md").write_text("# Guide\n\n## Helper\n\nCall `helper` to add one.\n")
    (template / "docs" / "other.md").write_text("# Other\n\nNothing about code here.\n")
    with pytest.MonkeyPatch.context() as patch_in_module:  # a module-scoped fixture has no `monkeypatch`; this puts the folder back
        patch_in_module.chdir(template)
        assert runner.invoke(app, ["config", "set", "docs.enabled", "true"]).exit_code == 0
    return template


@pytest.fixture(scope="module")
def _scanned_project(_initialised_project, tmp_path_factory):
    """The initialised project with one scan already done, once for the module. Tests that need a project that has
    been scanned, and whose objective is not the first scan, copy this (`project_scanned`)."""
    template = tmp_path_factory.mktemp("scanned")
    shutil.copytree(_initialised_project, template / "project")
    folder = template / "project"
    with pytest.MonkeyPatch.context() as patch_in_module:
        patch_in_module.chdir(folder)
        assert runner.invoke(app, ["scan"]).exit_code == 0
    return folder


@pytest.fixture
def project_scanned(_scanned_project, tmp_path, monkeypatch):
    """A copy of the scanned project (copytree keeps the modification times, so an unchanged file stays unchanged)."""
    target = tmp_path / "project"
    shutil.copytree(_scanned_project, target)
    monkeypatch.chdir(target)
    return target


@pytest.fixture
def project(_initialised_project, tmp_path, monkeypatch):
    target = tmp_path / "project"
    shutil.copytree(_initialised_project, target)
    monkeypatch.chdir(target)
    return target


#: Tests whose listing comes from `_git_says` (the listing git would print, no git run). The others run the file
#: listing without asking git at all.
_GIT_IGNORE_TESTS = {
    "test_a_file_that_became_ignored_leaves_the_index_at_the_next_scan",
    "test_an_edited_file_that_git_would_ignore_can_still_be_named",
    "test_scan_include_brings_back_an_ignored_folder",
    "test_the_summary_says_how_many_files_the_gitignore_left_out",
}


@pytest.fixture(autouse=True)
def _listing_without_git(request, monkeypatch):
    if request.node.name in _GIT_IGNORE_TESTS:
        return
    from drspec.core import project_files

    monkeypatch.setattr(project_files, "inside_git", lambda root: False)


@pytest.fixture(autouse=True)
def _no_checkpoint_when_a_project_database_closes(monkeypatch):
    """Closing the project database writes its checkpoint to disk, which costs about 0.4 s a close on a shared disk.
    A test's database is thrown away with its folder, so the checkpoint is skipped (the write-ahead log still holds
    everything, and the next open reads it)."""
    from drspec.worker_ipc import _DirectConnection

    original_close = _DirectConnection.close

    def close(self) -> None:
        self._conn.execute("PRAGMA disable_checkpoint_on_shutdown")
        original_close(self)

    monkeypatch.setattr(_DirectConnection, "close", close)


def _queued(project: Path, **options) -> dict:
    """What a scan would QUEUE for this project, with nothing read, parsed or published: `start_scan` without a drain,
    on an in-memory database (the queue is the same as the first scan's, since nothing is stored yet)."""
    conn = duckdb.connect(":memory:")
    init_schema(conn)
    set_setting(conn, "docs.enabled", True)
    spec = {"scan_id": "queue-only", "targets": [], "recursive": True, "full": False, "deep": False, "embed": False,
            "code_only": False, "docs_only": False, "wait": False, **options}
    try:
        return scan_service.start_scan(conn, project / "_drspec" / "contracts.db", spec)
    finally:
        conn.close()


def _git_says(monkeypatch, project: Path, ignored=()) -> None:
    """Feed the file listing that `git` would print for this project, with `ignored` as the files git ignores. drspec
    reads what git prints and does not match `.gitignore` patterns itself, so the listing is given directly and no
    `git` runs."""
    from drspec.core import project_files

    everything = sorted(
        path.relative_to(project).as_posix()
        for path in project.rglob("*")
        if path.is_file() and not path.relative_to(project).as_posix().startswith("_drspec/")
    )
    ignored = set(ignored)

    def listing(root, *arguments):
        if arguments[:2] == ("rev-parse", "--is-inside-work-tree"):
            return ["true"]
        if not arguments or arguments[0] != "ls-files":
            return None
        specs = [argument[len(":(literal)"):] for argument in arguments if argument.startswith(":(literal)")]
        if "--ignored" in arguments:
            names = [name for name in everything if name in ignored]
        else:
            names = [name for name in everything if name not in ignored]
        return [name for name in names if not specs or any(name == spec or name.startswith(spec.rstrip("/") + "/") for spec in specs)]

    monkeypatch.setattr(project_files, "_git_lines", listing)


def _nothing_to_read(connection, spec, on_progress=None) -> dict:
    """A stand-in for `run_scan` that reads nothing: the summary a scan of an unchanged project gives."""
    return scan_service.summarize({"scan_id": spec["scan_id"], "queued": 0}, None, "done")


def _edit(path: Path, text: str) -> None:
    path.write_text(text)
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 2))  # a clear change in the modification time whatever the clock


class TestTheFirstAndTheUnchangedScan:
    def test_the_first_scan_reads_everything_and_reports_what_it_always_did(self, project):
        data = _scan()
        for field in OLD_SUMMARY_FIELDS + NEW_SUMMARY_FIELDS:
            assert field in data, field
        assert data["files_queued"] == data["files_done"] == 4 and data["files_scanned"] == 2
        assert data["functions_new"] == 3 and data["functions_found"] == 3 and data["documents_indexed"] == 2
        assert data["state"] == "done" and data["up_to_date"] is False and data["edges_inserted"] == 2

    def test_a_second_scan_reads_nothing_and_says_so(self, project_scanned):
        with patch.object(docs_core, "ingest_document") as ingest:
            data = _scan()
        assert data["up_to_date"] is True and data["files_queued"] == 0 and data["files_unchanged"] == 4
        assert data["message"] == "Up to date, nothing to do"
        assert ingest.call_count == 0

    def test_editing_one_document_ingests_exactly_one(self, project_scanned):
        _edit(project_scanned / "docs" / "other.md", "# Other\n\nNow it is longer than before.\n")
        real = docs_core.ingest_document
        with patch.object(docs_core, "ingest_document", side_effect=real) as ingest:
            data = _scan()
        assert ingest.call_count == 1 and data["files_queued"] == 1 and data["documents_indexed"] == 1

    def test_the_output_is_the_same_text_a_person_reads(self, capsys):
        output(success_response({"files_queued": 4, "state": "done", "message": "Scanned 4 files"}), json_output=False)
        text = capsys.readouterr().out
        assert "Files Queued: 4" in text and "State: done" in text


class TestWhatAScanIsAskedAbout:
    def test_a_file_a_directory_and_a_function(self, project_scanned):
        _edit(project_scanned / "pkg" / "a.py", "def helper(x):\n    return x + 2\n\n\ndef run(y):\n    return helper(y) * 2\n")
        _edit(project_scanned / "pkg" / "b.py", "from pkg.a import helper\n\n\ndef use():\n    return helper(4)\n")
        one = _scan("pkg/a.py")
        assert one["files_queued"] == 1 and one["functions_changed"] == 1
        rest = _scan("pkg")
        assert rest["files_queued"] == 1  # a.py was done; b.py is the one left
        assert _scan("pkg")["up_to_date"] is True

    def test_a_function_id_with_or_without_its_hash_means_its_file(self, project_scanned):
        function_id = json.loads(runner.invoke(app, ["--json", "search", "helper", "--code-only"]).output)["data"]["matches"][0]["function_id"]
        _edit(project_scanned / "pkg" / "a.py", "def helper(x):\n    return x + 3\n\n\ndef run(y):\n    return helper(y) * 2\n")
        assert _scan(function_id.split("#")[0])["files_queued"] == 1
        _edit(project_scanned / "pkg" / "a.py", "def helper(x):\n    return x + 4\n\n\ndef run(y):\n    return helper(y) * 2\n")
        assert _scan(function_id)["files_queued"] == 1

    def test_a_document_section_means_its_document(self, project_scanned):
        section = json.loads(runner.invoke(app, ["--json", "search", "Helper", "--docs-only"]).output)["data"]["matches"][0]["section_id"]
        _edit(project_scanned / "docs" / "guide.md", "# Guide\n\n## Helper\n\nCall `helper` to add two.\n")
        assert _scan(section)["files_queued"] == 1

    def test_without_recursion_only_the_direct_children(self, project):
        (project / "pkg" / "deep").mkdir()
        (project / "pkg" / "deep" / "c.py").write_text("def c():\n    pass\n")
        assert _queued(project, targets=["pkg"], recursive=False, code_only=True)["queued"] == 2  # a.py, b.py; not deep/c.py

    def test_a_path_that_does_not_exist_is_an_error(self, project):
        result = runner.invoke(app, ["--json", "scan", "nope.py"])
        assert result.exit_code == 1 and json.loads(result.output)["error"]["code"] == "PATH_NOT_FOUND"

    def test_code_only_and_docs_only(self, project):
        assert _queued(project, code_only=True)["queued"] == 2
        assert _queued(project, docs_only=True)["queued"] == 2

    def test_both_together_are_refused(self, project):
        assert runner.invoke(app, ["--json", "scan", "--code-only", "--docs-only"]).exit_code == 1

    def test_an_edited_file_that_git_would_ignore_can_still_be_named(self, project, monkeypatch):
        _git_says(monkeypatch, project, ignored=["pkg/b.py"])
        data = _scan("pkg/b.py")  # asked for by name: read although the project's list leaves it out
        assert data["files_queued"] == 1 and data["functions_found"] == 1


class TestFullAndTheOptionsItCarries:
    def test_full_reads_everything_again_and_finds_it_unchanged(self, project_scanned):
        data = _scan("--full")
        assert data["files_queued"] == 4 and data["functions_unchanged"] == 3 and data["functions_new"] == 0
        assert data["force"] is True and data["full"] is True

    def test_force_is_the_old_name_of_full(self, project_scanned):
        with patch("drspec.cli.commands.scan.run_scan", side_effect=_nothing_to_read) as run:
            runner.invoke(app, ["--json", "scan", "--force"])
        assert run.call_args.args[1]["full"] is True

    def test_embed_and_deep_scan_apply_to_the_files_read_and_a_hint_says_so(self, project_scanned):
        with patch("drspec.cli.commands.scan.run_scan", side_effect=_nothing_to_read):
            data = json.loads(runner.invoke(app, ["--json", "scan", "--deep-scan"]).output)["data"]
        assert data["up_to_date"] is True and "--full" in data["hint"]

    def test_the_options_travel_with_the_queued_files(self, project):
        connection = _db(project)
        result = scan_service.start_scan(connection, project / "_drspec" / "contracts.db", {"scan_id": "s1", "deep": True, "embed": True, "full": False, "wait": True})
        rows = connection.execute("SELECT deep, embed, full_scan FROM scan_requests").fetchall()
        assert result["queued"] == 4 and set(rows) == {(True, True, False)}
        connection.close()


class TestTheWholeProjectAfterAnEdit:
    def test_an_edited_signature_re_resolves_the_caller_in_a_file_that_was_not_read(self, project_scanned):
        _edit(project_scanned / "pkg" / "a.py", "def helper(x, y=0):\n    return x + y\n\n\ndef run(y):\n    return helper(y) * 2\n")
        data = _scan("pkg/a.py")
        assert data["files_queued"] == 1 and data["neighbours_re_resolved"] >= 1  # b.py's use() was re-resolved, not re-read

    def test_a_linked_section_is_flagged_without_its_document_being_read(self, project_scanned):
        _edit(project_scanned / "pkg" / "a.py", "def helper(x):\n    return x + 100\n\n\ndef run(y):\n    return helper(y) * 2\n")
        with patch.object(docs_core, "ingest_document") as ingest:
            data = _scan("pkg/a.py::helper")
        assert ingest.call_count == 0 and data["documentation_sections_to_review"] == 1
        stale = json.loads(runner.invoke(app, ["--json", "search", "--status", "STALE", "--docs-only"]).output)["data"]
        assert stale["total_matches"] == 1


class TestAFileThatLeavesTheProject:
    def test_a_deleted_file_goes_through_the_queue(self, project_scanned):
        (project_scanned / "pkg" / "b.py").unlink()
        data = _scan()
        assert data["files_queued"] == 1 and data["functions_removed"] == 1

    def test_a_file_that_became_ignored_leaves_the_index_at_the_next_scan(self, project, monkeypatch):
        _git_says(monkeypatch, project)
        _scan()
        _git_says(monkeypatch, project, ignored=["pkg/b.py", "docs/other.md"])
        data = _scan()
        assert data["files_removed_as_ignored"] == 2 and data["files_skipped_by_gitignore"] == 2
        connection = _db(project)
        assert connection.execute("SELECT COUNT(*) FROM artifacts WHERE file_path = 'pkg/b.py'").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM doc_sections WHERE file_path = 'docs/other.md'").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM scanned_files WHERE file_path = 'pkg/b.py'").fetchone()[0] == 0
        connection.close()
        assert _scan()["up_to_date"] is True

    def test_scan_include_brings_back_an_ignored_folder(self, project, monkeypatch):
        _git_says(monkeypatch, project, ignored=["pkg/a.py", "pkg/b.py"])
        assert _scan()["functions_found"] == 0
        assert runner.invoke(app, ["config", "set", "scan.include", "pkg"]).exit_code == 0
        assert _scan()["functions_found"] == 3

    def test_the_summary_says_how_many_files_the_gitignore_left_out(self, project, monkeypatch):
        _git_says(monkeypatch, project, ignored=["docs/guide.md", "docs/other.md"])
        data = _scan()
        assert data["files_skipped_by_gitignore"] == 2 and "gitignore" in data["skipped_note"].lower()


class TestNoWait:
    def test_the_files_are_queued_at_background_priority_and_the_command_returns(self, project):
        data = _scan("--no-wait")
        assert data["state"] == "queued" and data["files_queued"] == 4 and data["files_done"] == 0
        connection = _db(project)
        reasons = {row[0] for row in connection.execute("SELECT reason FROM staleness_queue").fetchall()}
        assert reasons == {scan_service.BACKGROUND_REASON}
        assert connection.execute("SELECT COUNT(*) FROM scan_requests").fetchone()[0] == 4
        connection.close()

    def test_the_worker_later_publishes_them_and_the_record_is_dropped(self, project):
        _scan("--no-wait")
        connection = _db(project)
        from drspec.staleness_worker import _remediate_queued_files

        db_path = project / "_drspec" / "contracts.db"
        _remediate_queued_files(connection, db_path, next_rescan_batch(connection, 100, time.time(), project))
        assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM scan_requests").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM config WHERE starts_with(key, 'scan:run:')").fetchone()[0] == 0
        connection.close()


class TestTheTopTier:
    def test_a_scans_files_pass_everything_already_waiting(self, project):
        connection = _db(project)
        waiting = [(f"old{i}.py", "search_drift") for i in range(100)] + [(f"drift{i}.py", "mtime_changed") for i in range(100)]
        enqueue_staleness_many(connection, waiting)
        scan_service.start_scan(connection, project / "_drspec" / "contracts.db", {"scan_id": "s", "wait": True})
        batch = next_rescan_batch(connection, 4, time.time(), project)
        assert set(batch) == {"pkg/a.py", "pkg/b.py", "docs/guide.md", "docs/other.md"}
        connection.close()

    def test_a_file_already_queued_lower_is_moved_up_not_queued_twice(self, project):
        connection = _db(project)
        enqueue_staleness_many(connection, [("pkg/a.py", "new_file")])
        scan_service.start_scan(connection, project / "_drspec" / "contracts.db", {"scan_id": "s", "wait": True})
        queue = list_staleness_queue(connection)
        assert queue.count("pkg/a.py") == 1
        assert connection.execute("SELECT reason FROM staleness_queue WHERE file_path = 'pkg/a.py'").fetchone()[0] == scan_service.SCAN_REASON
        connection.close()

    def test_a_waiting_scan_makes_the_worker_run_flat_out(self, project):
        from drspec.core.settings import SETTINGS
        from drspec.staleness_worker import _pause_after

        settings = {key: setting.default for key, setting in SETTINGS.items()}
        settings["worker.cpu_percent"] = 1
        with patch("drspec.staleness_worker.pause_reason", return_value=None):  # an idle machine
            assert _pause_after(settings, 100.0, 1.0, True, scanning=False)[1] == "cap"
            assert _pause_after(settings, 100.0, 1.0, True, scanning=True) == (settings["worker.yield_ms"] / 1000.0, "scan")
        with patch("drspec.staleness_worker.pause_reason", return_value="load average is high"):
            assert _pause_after(settings, 0.0, 1.0, True, scanning=False)[1] == "load"
            assert _pause_after(settings, 0.0, 1.0, True, scanning=True)[1] == "scan"


class TestCancelling:
    def test_the_files_not_yet_read_leave_the_queue_and_what_was_published_stays(self, project):
        connection = _db(project)
        db_path = project / "_drspec" / "contracts.db"
        scan_service.start_scan(connection, db_path, {"scan_id": "s", "wait": True})
        from drspec.staleness_worker import _remediate_queued_files

        _remediate_queued_files(connection, db_path, ["pkg/a.py"])  # one batch is published
        before = connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
        assert scan_service.progress(connection, "s")["remaining"] == 3
        result = scan_service.cancel(connection, "s")
        run = scan_service.progress(connection, "s")
        assert result["cancelled"] == 3 and run["finished"] and run["done"] == 1 and run["cancelled"] == 3
        assert list_staleness_queue(connection) == []
        assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == before == 2
        connection.close()

    def test_an_interrupt_cancels_and_exits_130(self, project):
        class Interrupting:
            def __init__(self, inner):
                self.inner = inner
                self.interrupted = False

            def __getattr__(self, name):
                return getattr(self.inner, name)

            def scan_progress(self, scan_id):
                if not self.interrupted:
                    self.interrupted = True
                    raise KeyboardInterrupt
                return self.inner.scan_progress(scan_id)

            def scan_start(self, **spec):  # queue, but do not read: the interrupt arrives first
                connection = self.inner._conn
                return scan_service.start_scan(connection, self.inner._db_path, spec)

        from drspec import worker_ipc

        real = worker_ipc.get_worker_connection
        with patch("drspec.cli.commands.scan.get_worker_connection", side_effect=lambda *a, **k: Interrupting(real(*a, **k))):
            result = runner.invoke(app, ["--json", "scan"])
        assert result.exit_code == 130
        data = json.loads(result.output)["data"]
        assert data["state"] == "cancelled" and data["files_cancelled"] == 4 and data["files_done"] == 0
        connection = _db(project)
        assert list_staleness_queue(connection) == []
        connection.close()


class TestAFailure:
    def test_a_file_that_cannot_be_read_leaves_the_scan_and_the_rest_goes_on(self, project, monkeypatch):
        unreadable(monkeypatch, project / "pkg" / "b.py")  # a file mode does not stop root: the read itself is made to fail
        data = _scan()
        assert data["files_failed"] == 1 and data["files_done"] == 3 and data["error_count"] >= 1

    def test_a_batch_that_cannot_be_published_fails_the_scan_with_the_reason(self, project):
        with patch("drspec.staleness_worker.sync_scan_results", side_effect=RuntimeError("boom")):
            result = runner.invoke(app, ["--json", "scan"])
        assert result.exit_code == 1
        error = json.loads(result.output)["error"]
        assert error["code"] == "SCAN_ERROR" and "boom" in error["message"]
        connection = _db(project)
        assert connection.execute("SELECT COUNT(*) FROM scan_requests").fetchone()[0] == 0  # nobody waits for it
        connection.close()

    def test_no_worker_means_no_scan(self, project):
        from drspec.worker_ipc import WorkerIPCError

        with patch("drspec.cli.commands.scan.get_worker_connection", side_effect=WorkerIPCError("no socket")):
            result = runner.invoke(app, ["--json", "scan"])
        error = json.loads(result.output)["error"]
        assert result.exit_code == 1 and error["code"] == "DATABASE_BUSY" and "no scan without one" in error["message"]


class TestAddingUpABatchedScan:
    def test_counts_add_and_tables_combine(self):
        first = {"functions_new": 2, "edges_inserted": 1, "carried_over": {"a": "b"}, "resolution_reasons": summarize_by_reason({"x": "forced"}), "contracts_invalidated": summarize_by_reason({})}
        second = {"functions_new": 3, "edges_inserted": 4, "carried_over": {"c": "d"}, "resolution_reasons": summarize_by_reason({"y": "forced", "z": "symbols_changed"}), "contracts_invalidated": summarize_by_reason({"q": "IMPORTS_CHANGED"})}
        total = merge_stats(first, second)
        assert total["functions_new"] == 5 and total["edges_inserted"] == 5 and total["carried_over"] == {"a": "b", "c": "d"}
        assert total["resolution_reasons"]["total"] == 3 and total["resolution_reasons"]["by_reason"] == {"forced": 2, "symbols_changed": 1}
        assert total["contracts_invalidated"]["total"] == 1

    def test_a_scan_in_several_batches_gives_the_totals_one_batch_would(self, project):
        db_path = project / "_drspec" / "contracts.db"
        connection = _db(project)
        scan_service.start_scan(connection, db_path, {"scan_id": "s", "wait": True})
        from drspec.staleness_worker import _remediate_queued_files

        for path in ("pkg/a.py", "pkg/b.py", "docs/guide.md", "docs/other.md"):  # one file per batch
            _remediate_queued_files(connection, db_path, [path])
        run = scan_service.progress(connection, "s")
        assert run["finished"] and run["done"] == 4 and run["functions_found"] == 3 and run["documents_indexed"] == 2
        assert run["stats"]["functions_new"] == 3 and run["stats"]["edges_inserted"] == 2  # b.py's call into a.py was resolved when a.py arrived
        connection.close()


class TestOneKeywordTableForAPublish:
    def test_documents_ingested_together_are_stored_as_one_at_a_time(self, tmp_path):
        texts = {
            f"docs/d{i}.md": f"# Doc {i}\n\n## Part {i}\n\nThe widget parser handles tokens number {i} and retries the connection.\n\n## Other\n\nShared words appear in every document: widget, parser.\n"
            for i in range(3)
        }
        settings = {"docs.keywords_per_section": 6, "docs.max_links_per_section": 50, "docs.link_min_score": 0.3, "docs.common_name_limit": 8}

        def stored(together: bool):
            connection = duckdb.connect(":memory:")
            init_schema(connection)
            if together:
                docs_core.publish_documents(connection, texts, (), settings)
            else:
                for path, text in texts.items():
                    docs_core.ingest_document(connection, path, text, settings)  # the table rebuilt from the database each time
            rows = connection.execute("SELECT section_id, keywords, important FROM doc_sections ORDER BY section_id").fetchall()
            connection.close()
            return rows

        assert stored(True) == stored(False)

    def test_the_table_follows_a_replaced_document(self, tmp_path):
        connection = duckdb.connect(":memory:")
        init_schema(connection)
        settings = {"docs.keywords_per_section": 6, "docs.max_links_per_section": 50, "docs.link_min_score": 0.3, "docs.common_name_limit": 8}
        table = docs_core.DocumentFrequency()
        index = docs_core.build_index(connection)
        docs_core.ingest_document(connection, "a.md", "# A\n\nalpha beta gamma\n", settings, index, table)
        docs_core.ingest_document(connection, "a.md", "# A\n\ndelta epsilon\n", settings, index, table)
        fresh = docs_core.DocumentFrequency.from_database(connection)
        assert table.frequency == fresh.frequency and table.count == fresh.count
        connection.close()

    def test_removing_a_document_is_part_of_the_same_call(self, tmp_path):
        connection = duckdb.connect(":memory:")
        init_schema(connection)
        settings = {"docs.keywords_per_section": 6, "docs.max_links_per_section": 50, "docs.link_min_score": 0.3, "docs.common_name_limit": 8}
        docs_core.publish_documents(connection, {"a.md": "# A\n\ntext\n", "b.md": "# B\n\ntext\n"}, (), settings)
        outcome = docs_core.publish_documents(connection, {}, ["a.md"], settings)
        assert outcome == {"documents": 0, "removed": 1}
        assert [row[0] for row in connection.execute("SELECT DISTINCT file_path FROM doc_sections").fetchall()] == ["b.md"]
        connection.close()
