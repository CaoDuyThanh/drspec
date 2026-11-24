"""A manual scan settles the worker's queue rows; the work-finding commands run
without a pattern; the embedding process is held to its configured threads."""

from __future__ import annotations

import json
import os
import sys

import duckdb
import pytest
from typer.testing import CliRunner

from drspec import mcp_server
from drspec.cli.app import app
from drspec.core.settings import SETTINGS, SettingError, set_setting
from drspec.db import get_connection, init_schema
from drspec.db.queries import (
    enqueue_staleness,
    enqueue_staleness_many,
    get_rescan_backlog,
    list_scanned_files,
    list_staleness_queue,
    record_rescan_failure,
    settle_scanned_files,
    upsert_scanned_file,
)
from drspec.embedding_supervisor import (
    THREAD_ENVIRONMENT,
    EmbeddingSupervisor,
    embedding_environment,
    spawn_embedding_process,
)
from tests.fakes import unreadable

runner = CliRunner()


@pytest.fixture
def project(kept_project):
    """Three small files in a project folder with an empty in-memory project database (`kept_project`); not scanned yet."""
    for name in ("a", "b", "c"):
        (kept_project / f"{name}.py").write_text(f"def {name}_fn():\n    return 1\n")
    return kept_project


def _db(project):
    connection = get_connection(project / "_drspec" / "contracts.db")
    init_schema(connection)
    return connection


def _memory_db():
    """For a test that checks SQL only: no project, no file, no commit that waits for a disk."""
    connection = duckdb.connect(":memory:")
    init_schema(connection)
    return connection


class TestAManualScanSettlesTheQueue:
    def test_rows_queued_before_a_scan_are_gone_after_it(self, project):
        connection = _db(project)
        for name in ("a.py", "b.py", "c.py"):
            enqueue_staleness(connection, name, "new_file")
        record_rescan_failure(connection, "b.py", "boom", None, None, 1000.0)
        connection.close()

        assert runner.invoke(app, ["scan", "."]).exit_code == 0

        connection = _db(project)
        assert list_staleness_queue(connection) == []
        assert connection.execute("SELECT COUNT(*) FROM rescan_backoff").fetchone()[0] == 0
        assert get_rescan_backlog(connection, 2000.0) == {"files_pending_rescan": 0, "files_pending_discovery": 0, "files_in_retry_wait": 0, "files_unreadable": 0}
        connection.close()

    def test_a_file_the_scan_could_not_read_leaves_the_scan_and_waits_for_its_retry(self, project, monkeypatch):
        unreadable(monkeypatch, project / "b.py")  # a file mode does not stop root: the read itself is made to fail
        result = runner.invoke(app, ["--json", "scan", "."])
        data = json.loads(result.output)["data"]
        connection = _db(project)
        queue = list_staleness_queue(connection)
        waiting = connection.execute("SELECT COUNT(*) FROM rescan_backoff WHERE file_path = 'b.py'").fetchone()[0]
        requests = connection.execute("SELECT COUNT(*) FROM scan_requests").fetchone()[0]
        connection.close()
        assert data["files_failed"] == 1 and data["files_done"] == 2 and data["error_count"] >= 1
        assert queue == ["b.py"] and waiting == 1 and requests == 0  # the scan did not wait for it forever

    def test_a_file_deleted_before_the_scan_leaves_no_row_or_snapshot(self, project):
        assert runner.invoke(app, ["scan", "."]).exit_code == 0
        (project / "b.py").unlink()
        connection = _db(project)
        enqueue_staleness(connection, "b.py", "missing")
        connection.close()

        assert runner.invoke(app, ["scan", "."]).exit_code == 0

        connection = _db(project)
        assert list_staleness_queue(connection) == []
        assert "b.py" not in [row[0] for row in list_scanned_files(connection)]
        assert connection.execute("SELECT COUNT(*) FROM artifacts WHERE file_path = 'b.py'").fetchone()[0] == 0
        connection.close()

    def test_the_helper_clears_the_queue_the_backoff_and_the_snapshots_of_missing_files(self, tmp_path):
        (tmp_path / "a.py").write_text("def a():\n    return 1\n")  # (its own folder: nothing of the repository or the process folder is read)
        connection = _memory_db()
        upsert_scanned_file(connection, "a.py", 1.0, 5)
        upsert_scanned_file(connection, "gone.py", 1.0, 5)
        for name in ("a.py", "gone.py", "other.py"):
            enqueue_staleness(connection, name, "mtime_changed")
        record_rescan_failure(connection, "a.py", "boom", None, None, 1.0)
        settle_scanned_files(connection, ["a.py", "gone.py"], root=tmp_path)
        assert list_staleness_queue(connection) == ["other.py"]
        assert connection.execute("SELECT COUNT(*) FROM rescan_backoff").fetchone()[0] == 0
        assert [row[0] for row in list_scanned_files(connection)] == ["a.py"]  # only the missing file's snapshot goes
        connection.close()

    def test_a_large_list_is_handled_in_chunks(self, tmp_path):
        connection = _memory_db()
        names = [f"f{i}.py" for i in range(1200)]
        enqueue_staleness_many(connection, [(name, "new_file") for name in names])
        settle_scanned_files(connection, names, root=tmp_path)
        assert list_staleness_queue(connection) == []
        connection.close()

    def test_the_workers_own_rescan_uses_the_same_helper(self):
        import inspect

        from drspec import staleness_worker

        assert "settle_scanned_files" in inspect.getsource(staleness_worker._remediate_queued_files)


class TestSearchByStatusNeedsNoPattern:
    @pytest.fixture
    def scanned(self, project):
        assert runner.invoke(app, ["scan", "."]).exit_code == 0
        return project

    def test_the_documented_command_lists_functions_without_a_contract(self, scanned):
        result = runner.invoke(app, ["--json", "search", "--status", "PENDING"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["total_matches"] == 3 and {m["function_name"] for m in data["matches"]} == {"a_fn", "b_fn", "c_fn"}

    def test_the_path_and_limit_options_work_with_it(self, scanned):
        data = json.loads(runner.invoke(app, ["--json", "search", "--status", "PENDING", "--path", "a.py", "--limit", "5"]).output)["data"]
        assert [m["function_name"] for m in data["matches"]] == ["a_fn"]

    def test_a_status_with_nothing_in_it_is_an_empty_list_not_an_error(self, scanned):
        result = runner.invoke(app, ["--json", "search", "--status", "STALE"])
        assert result.exit_code == 0 and json.loads(result.output)["data"]["total_matches"] == 0

    def test_a_pattern_is_still_required_without_a_status(self, scanned):
        result = runner.invoke(app, ["--json", "search"])
        assert result.exit_code == 1
        assert json.loads(result.output)["error"]["code"] == "INVALID_INPUT"

    @pytest.mark.parametrize("extra", [["--semantic"], ["--lines"]])
    def test_it_cannot_be_left_out_where_an_empty_query_means_nothing(self, scanned, extra):
        result = runner.invoke(app, ["--json", "search", *extra])
        assert result.exit_code == 1

    def test_count_works_without_a_pattern_when_a_status_is_given(self, scanned):
        data = json.loads(runner.invoke(app, ["--json", "search", "--status", "PENDING", "-c"]).output)["data"]
        assert data["count"] == 3

    def test_the_tool_does_not_require_a_pattern_in_its_schema(self):
        schema = next(tool for tool in mcp_server._TOOLS if tool["name"] == "search")["inputSchema"]
        assert "pattern" not in schema.get("required", [])
        assert "status" in schema["properties"]

    def test_the_tool_lists_by_status_without_a_pattern(self, scanned):
        reply = mcp_server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search", "arguments": {"status": "PENDING", "format": "json"}}},
            scanned / "_drspec" / "contracts.db",
        )
        assert reply["result"]["isError"] is False
        assert json.loads(reply["result"]["content"][0]["text"])["total_matches"] == 3

    @pytest.mark.parametrize("arguments", [{}, {"semantic": True, "status": "PENDING"}, {"lines": True, "status": "PENDING"}])
    def test_the_tool_still_needs_a_pattern_otherwise(self, scanned, arguments):
        reply = mcp_server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search", "arguments": arguments}},
            scanned / "_drspec" / "contracts.db",
        )
        assert reply["result"]["isError"] is True and "pattern" in reply["result"]["content"][0]["text"]


class TestTheEmbeddingProcessIsHeldToItsThreads:
    def test_every_thread_pool_is_capped_to_the_setting(self):
        environment = embedding_environment(3, base={"PATH": "tools", "OMP_NUM_THREADS": "64"})
        assert environment["PATH"] == "tools"
        for name in THREAD_ENVIRONMENT:
            assert environment[name] == "3"
        assert environment["TOKENIZERS_PARALLELISM"] == "false"
        assert {"OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "RAYON_NUM_THREADS"} <= set(THREAD_ENVIRONMENT)

    def test_the_child_is_started_with_that_environment(self, tmp_path, monkeypatch):
        captured = {}

        class Popen:
            def __init__(self, command, **kwargs):
                captured["command"], captured["kwargs"] = command, kwargs

        monkeypatch.setattr("drspec.embedding_supervisor.subprocess.Popen", Popen)
        (tmp_path / "_drspec").mkdir()
        spawn_embedding_process(tmp_path / "_drspec" / "contracts.db", threads=2)
        assert captured["command"][:3] == [sys.executable, "-m", "drspec"]
        assert captured["kwargs"]["env"]["OMP_NUM_THREADS"] == "2"
        assert captured["kwargs"]["env"]["TOKENIZERS_PARALLELISM"] == "false"

    def test_the_supervisor_passes_the_setting_on(self, tmp_path):
        from drspec.core.embedding_backlog import Item
        from drspec.db import insert_artifact
        from drspec.db.queries import add_usage_pending, fold_usage_pending

        connection = _memory_db()  # only SQL is read: the supervisor is handed a path it passes to the (fake) spawn
        insert_artifact(
            connection, function_id="a.py::f", file_path="a.py", function_name="f", signature="def f():",
            body="def f(): pass", code_hash="h", language="python", start_line=1, end_line=2,
        )
        add_usage_pending(connection, [("node", "a.py::f", "", 1.0)])
        fold_usage_pending(connection, 1000.0, 7.0)
        set_setting(connection, "embedding.threads", 1)
        seen = []
        supervisor = EmbeddingSupervisor(spawn=lambda db_path, threads=None: seen.append(threads) or type("P", (), {"poll": lambda self: None})())
        from drspec.core.settings import read_all_settings

        supervisor.tick(connection, tmp_path / "contracts.db", read_all_settings(connection), 1000.0)
        assert seen == [1]
        del Item
        connection.close()

    def test_the_setting_cannot_exceed_the_number_of_cores(self, tmp_path):

        cores = os.cpu_count() or 1
        assert SETTINGS["embedding.threads"].maximum == cores
        connection = _memory_db()
        with pytest.raises(SettingError, match="at most"):
            set_setting(connection, "embedding.threads", cores + 1)
        connection.close()


class TestAStopSurvivesASwallowedSignal:
    """DuckDB runs `import pandas` on its first parameterized query and drops any
    exception raised while it does; a SIGTERM landing then must still stop the worker."""

    def test_the_worker_stops_even_when_the_exception_is_swallowed(self, tmp_path, monkeypatch):
        from drspec import staleness_worker as worker

        monkeypatch.chdir(tmp_path)  # run_worker moves the process to the project folder and does not move it back: restored after this test
        db_path = tmp_path / "_drspec" / "contracts.db"
        db_path.parent.mkdir()
        connection = get_connection(db_path)  # pattern-ok: duckdb-file the worker process stop: a real file and socket are the objective
        init_schema(connection)
        connection.close()
        passes = []

        def swallowed_signal(conn, path, settings=None, detect=True):
            passes.append(1)
            if len(passes) > 2:  # (a worker that did not stop: end the test's loop here instead of letting it run to its idle timeout)
                raise SystemExit("the worker went on after the stop")
            try:
                worker._raise_system_exit(15, None)
            except SystemExit:
                pass  # as the import machinery under DuckDB does
            return {"detected": 0, "discovered": 0, "remediated": 0, "pause": 0.0, "pause_reason": "none"}

        monkeypatch.setattr(worker, "run_one_pass", swallowed_signal)
        with pytest.raises(SystemExit):
            worker.run_worker(db_path, idle_timeout=30, poll_interval=0.05)
        assert len(passes) == 1  # (it stopped at the pass that was signalled: no clock, no 5-second allowance)
        assert not (db_path.parent / "staleness_worker.pid").exists()
        assert not (db_path.parent / "worker.sock").exists()

    def test_the_handler_sets_the_flag_and_still_raises(self):
        from drspec import staleness_worker as worker

        worker._stop_requested.clear()
        with pytest.raises(SystemExit):
            worker._raise_system_exit(15, None)
        assert worker._stop_requested.is_set()
        with pytest.raises(SystemExit):
            worker._exit_if_stop_requested()
        worker._stop_requested.clear()
        worker._exit_if_stop_requested()  # nothing requested: no exit


class _Counting:
    """Counts the requests a command sends to the database."""

    def __init__(self, inner, counter):
        self._inner = inner
        self._counter = counter

    def execute(self, *args, **kwargs):
        self._counter.append(args[0])
        return self._inner.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class TestStatusDoesNotQueryPerFunction:
    @pytest.fixture
    def many(self, kept_project):
        from drspec.db import insert_artifact
        from drspec.db.queries import set_config

        tmp_path = kept_project
        connection = get_connection(tmp_path / "_drspec" / "contracts.db")
        for i in range(80):
            insert_artifact(
                connection, function_id=f"a.py::f{i}", file_path="a.py", function_name=f"f{i}", signature=f"def f{i}():",
                body=f"def f{i}(): pass", code_hash=f"h{i}", language="python", start_line=1, end_line=2,
            )
        set_config(connection, "verification:a.py::f3", json.dumps({"status": "passed"}))
        set_config(connection, "verification:a.py::f4", json.dumps({"status": "failed"}))
        set_config(connection, "verification:gone.py::deleted", json.dumps({"status": "passed"}))  # no such function
        connection.close()
        return tmp_path

    def test_the_number_of_requests_does_not_grow_with_the_functions(self, many, monkeypatch):
        from drspec.cli.commands import status as status_module

        sent = []
        real = status_module.get_worker_connection
        monkeypatch.setattr(status_module, "get_worker_connection", lambda *a, **k: _Counting(real(*a, **k), sent))
        result = runner.invoke(app, ["--json", "status"])
        assert result.exit_code == 0, result.output
        assert len(sent) < 45, f"{len(sent)} requests for 80 functions"  # a fixed number of requests (40 now, one of them the generated folders of T-47); one per function would be 80 and more
        assert sum("verification:" in sql or "starts_with" in sql for sql in sent) >= 1

    def test_the_evidence_counts_are_unchanged(self, many):
        data = json.loads(runner.invoke(app, ["--json", "status"]).output)["data"]
        # 78 functions have no evidence; two have some but no contract to be current against.
        assert data["verification_evidence"] == {"passed": 0, "failed": 0, "partial": 0, "unverified": 78, "error": 0, "stale": 2}


class TestASearchSaysWhenItSearchedNothing:
    @pytest.fixture
    def scanned(self, project):
        (project / "notes.toml").write_text('[tool]\nname = "pandas"\n')
        assert runner.invoke(app, ["scan", "."]).exit_code == 0
        return project

    def test_a_path_with_no_indexed_file_says_so(self, scanned):
        data = json.loads(runner.invoke(app, ["--json", "search", "pandas", "--path", "notes.toml"]).output)["data"]
        assert data["total_matches"] == 0 and data["files_searched"] == 0
        assert "Nothing was searched" in data["note"] and "grep" in data["note"]

    def test_a_searched_path_with_no_hits_is_not_mistaken_for_it(self, scanned):
        data = json.loads(runner.invoke(app, ["--json", "search", "no_such_text", "--path", "a.py"]).output)["data"]
        assert data["total_matches"] == 0 and data["files_searched"] == 1 and "note" not in data

    def test_the_text_output_prints_the_label(self, scanned):
        result = runner.invoke(app, ["search", "pandas", "--path", "notes.toml"])
        assert "Files Searched: 0" in result.output and "Nothing was searched" in result.output

    def test_the_count_and_file_modes_say_it_too(self, scanned):
        for flag in ("--count", "--files-with-matches"):
            data = json.loads(runner.invoke(app, ["--json", "search", "pandas", flag, "--path", "notes.toml"]).output)["data"]
            assert data["files_searched"] == 0 and "note" in data

    def test_a_language_filter_that_matches_no_file_says_so(self, scanned):
        data = json.loads(runner.invoke(app, ["--json", "search", "x", "--include", "cpp"]).output)["data"]
        assert data["files_searched"] == 0 and "language filter" in data["note"]

    def test_the_tool_reports_it_as_well(self, scanned):
        reply = mcp_server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search", "arguments": {"pattern": "pandas", "path_prefix": "notes.toml", "format": "json"}}},
            scanned / "_drspec" / "contracts.db",
        )
        data = json.loads(reply["result"]["content"][0]["text"])
        assert data["files_searched"] == 0 and "Nothing was searched" in data["note"]
