"""`drspec init` starts the worker so the project is indexed in the background, and `init --scan` scans the whole project
at full speed and waits (or queues it with `--no-wait`). The scan is the one `drspec scan` runs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.core import indexing

runner = CliRunner()


@pytest.fixture
def project(tmp_path, monkeypatch, kept_duckdb, cached_cli_command):
    """Four small files in a folder that is the working folder. `init` is the objective of these tests and runs for real; its database is the
    kept in-memory one (what they check is the command's flags and messages, not a file)."""
    monkeypatch.chdir(tmp_path)
    for index in range(4):
        (tmp_path / f"mod{index}.py").write_text(f"def fn{index}():\n    return {index}\n")
    return tmp_path


def _init(*args: str) -> dict:
    result = runner.invoke(app, ["--json", "init", "--no-ide", "--non-interactive", *args])  # pattern-ok: init-for-setup init is the objective of this file
    return {"exit": result.exit_code, "out": json.loads(result.output) if result.output.strip().startswith("{") else result.output}


class TestStartingTheWorker:
    def test_init_says_what_it_started_and_the_message_names_it(self, project):
        with patch.object(indexing, "start_indexing", wraps=None) as started:
            started.return_value = {"started": True, "message": indexing.BACKGROUND_MESSAGE}
            with patch("drspec.cli.commands.init.start_indexing", started):
                done = _init()
        assert done["exit"] == 0
        data = done["out"]["data"]
        assert data["indexing"]["started"] is True and indexing.BACKGROUND_MESSAGE in data["message"]
        started.assert_called_once()

    def test_a_worker_that_cannot_start_is_a_warning_and_not_a_failed_init(self, project):
        with patch("drspec.cli.commands.init.start_indexing", return_value={"started": False, "warning": "could not start the indexing worker: boom."}):
            done = _init()
        assert done["exit"] == 0 and done["out"]["success"] is True
        assert "boom" in done["out"]["data"]["warning"]
        assert (project / "_drspec" / "contracts.db").exists()

    def test_no_index_starts_nothing(self, project):
        with patch("drspec.cli.commands.init.start_indexing") as started:
            done = _init("--no-index")
        assert done["exit"] == 0 and "indexing" not in done["out"]["data"]
        started.assert_not_called()

    def test_a_second_init_starts_it_too(self, project):
        _init("--no-index")
        with patch("drspec.cli.commands.init.start_indexing", return_value={"started": True, "message": "m"}) as started:
            done = _init()
        assert done["out"]["data"]["already_initialized"] is True and started.call_count == 1

    def test_the_test_mode_variable_starts_nothing_and_says_why(self, project):
        result = indexing.start_indexing(project / "_drspec" / "contracts.db")
        assert result["started"] is False and "DRSPEC_DISABLE_AUTO_WORKER" in result["message"]

    def test_start_indexing_waits_for_the_worker_and_reports_one_that_never_comes(self, project, monkeypatch, fake_time):
        monkeypatch.delenv("DRSPEC_DISABLE_AUTO_WORKER")
        monkeypatch.setattr(indexing, "time", fake_time)  # the wait for the worker is a loop of 0.1 s sleeps: it runs on a clock that moves when slept
        db = project / "_drspec" / "contracts.db"
        with patch("drspec.staleness_worker.spawn_worker_if_not_running") as spawn, patch("drspec.staleness_worker.is_worker_running", return_value=None):
            result = indexing.start_indexing(db, wait_seconds=0.3)
        spawn.assert_called_once()
        assert result["started"] is False and "did not appear" in result["warning"]
        with patch("drspec.staleness_worker.spawn_worker_if_not_running"), patch("drspec.staleness_worker.is_worker_running", side_effect=[None, 1234]):
            assert indexing.start_indexing(db, wait_seconds=2)["started"] is True  # pattern-ok: real-time-assert wait_seconds is an argument of the call (the clock is fake), nothing is measured
        with patch("drspec.staleness_worker.spawn_worker_if_not_running") as spawn, patch("drspec.staleness_worker.is_worker_running", return_value=99):
            result = indexing.start_indexing(db)
        spawn.assert_not_called()
        assert result["started"] is True and result["already_running"] is True
        assert result["message"] == indexing.ALREADY_RUNNING_MESSAGE and "already running" in result["message"]

    def test_a_fresh_start_does_not_say_it_was_already_running(self, project, monkeypatch, fake_time):
        monkeypatch.delenv("DRSPEC_DISABLE_AUTO_WORKER")
        monkeypatch.setattr(indexing, "time", fake_time)
        db = project / "_drspec" / "contracts.db"
        with patch("drspec.staleness_worker.spawn_worker_if_not_running"), patch("drspec.staleness_worker.is_worker_running", side_effect=[None, 1234]):
            result = indexing.start_indexing(db, wait_seconds=2)
        assert "already_running" not in result and result["message"] == indexing.BACKGROUND_MESSAGE
        assert "already" not in result["message"]

    def test_init_on_a_running_worker_says_so_in_its_message(self, project):
        with patch("drspec.cli.commands.init.start_indexing", return_value={"started": True, "already_running": True, "message": indexing.ALREADY_RUNNING_MESSAGE}):
            done = _init()
        assert "already running" in done["out"]["data"]["message"]


class TestScanAtInit:
    def test_scan_indexes_the_whole_project_and_ends_with_the_result(self, project):
        done = _init("--scan")
        data = done["out"]["data"]
        assert done["exit"] == 0
        assert data["scan"]["state"] == "done" and data["scan"]["files_done"] + data["scan"]["files_unchanged"] == 4
        assert data["scan"]["functions_found"] == 4
        assert data["indexing"]["message"] == "scan finished: 4 files, 4 functions"
        status = json.loads(runner.invoke(app, ["--json", "status"]).output)["data"]
        assert status["artifacts"]["total"] == 4

    def test_no_wait_queues_the_files_and_returns(self, project):
        done = _init("--scan", "--no-wait")
        scan = done["out"]["data"]["scan"]
        assert done["exit"] == 0 and scan["state"] == "queued" and scan["files_queued"] + scan["files_unchanged"] == 4
        assert "scan queued" in done["out"]["data"]["indexing"]["message"]

    def test_no_wait_without_scan_changes_nothing(self, project):
        done = _init("--no-wait")
        assert done["exit"] == 0 and "scan" not in done["out"]["data"]

    def test_scan_and_no_index_exclude_each_other_and_nothing_is_set_up(self, project):
        done = _init("--scan", "--no-index")
        assert done["exit"] == 1 and "--no-index" in json.dumps(done["out"])
        assert not (project / "_drspec").exists()

    def test_an_initialised_project_is_scanned_only_when_asked(self, project):
        _init("--no-index")
        again = _init()
        assert "scan" not in again["out"]["data"]
        scanned = _init("--scan")
        assert scanned["out"]["data"]["scan"]["state"] == "done"

    def test_a_scan_that_cannot_run_leaves_the_project_initialised_and_fails_the_command(self, project):
        with patch("drspec.cli.commands.init.scan_whole_project", side_effect=RuntimeError("worker gone")):
            done = _init("--scan")
        assert done["exit"] == 1
        assert done["out"]["data"]["scan"]["state"] == "failed" and "worker gone" in done["out"]["data"]["message"]
        assert (project / "_drspec" / "contracts.db").exists()

    def test_a_cancelled_scan_ends_with_130_and_the_setup_stays(self, project):
        with patch("drspec.cli.commands.init.scan_whole_project", return_value={"state": "cancelled", "files_queued": 4, "files_done": 1}):
            done = _init("--scan")
        assert done["exit"] == 130 and (project / "_drspec" / "contracts.db").exists()
        assert "scan cancelled" in done["out"]["data"]["indexing"]["message"]

    def test_the_help_names_the_options_and_where_the_scan_options_are(self):
        text = " ".join(runner.invoke(app, ["init", "--help"], env={"COLUMNS": "250"}).output.split())  # pattern-ok: init-for-setup init is the objective of this file
        for word in ("--scan", "--no-wait", "--no-index", "drspec scan", "--deep-scan"):
            assert word in text


class TestTheMessages:
    def test_one_line_for_each_state(self):
        assert indexing.scan_message({"state": "done", "files_done": 3, "files_unchanged": 2, "functions_found": 9}) == "scan finished: 5 files, 9 functions"
        assert "queued" in indexing.scan_message({"state": "queued", "files_queued": 7})
        assert "cancelled" in indexing.scan_message({"state": "cancelled"})
        assert "still running" in indexing.scan_message({"state": "still_queued"})


class TestTheMcpTool:
    @pytest.fixture(autouse=True)
    def _in_memory(self, kept_duckdb):
        """`_tool_init` makes a project database: the kept in-memory one (these tests check what the tool starts and returns)."""

    @staticmethod
    def _call(tmp_path, **arguments):
        from drspec.mcp_server import _tool_init

        return _tool_init(tmp_path / "unused.db", {"project_root": str(tmp_path), **arguments})

    def test_init_starts_the_worker(self, tmp_path):
        with patch("drspec.core.indexing.start_indexing", return_value={"started": True, "message": "m"}) as started:
            data, is_error = self._call(tmp_path)
        assert not is_error and data["indexing"]["started"] is True
        started.assert_called_once()

    def test_scan_scans_the_project_and_waits_the_scan_limit(self, tmp_path):
        (tmp_path / "a.py").write_text("def f():\n    return 1\n")
        with patch("drspec.core.indexing.scan_whole_project", return_value={"state": "still_queued", "files_queued": 1}) as scanned:
            data, is_error = self._call(tmp_path, scan=True)
        assert not is_error and data["scan"]["state"] == "still_queued" and "still running" in data["indexing"]["message"]
        assert scanned.call_args.kwargs["max_wait"] == 60.0 and scanned.call_args.kwargs["wait"] is True

    def test_scan_with_no_wait_queues(self, tmp_path):
        with patch("drspec.core.indexing.scan_whole_project", return_value={"state": "queued", "files_queued": 1}) as scanned:
            self._call(tmp_path, scan=True, no_wait=True)
        assert scanned.call_args.kwargs["wait"] is False

    def test_no_index_starts_nothing_and_cannot_be_scanned(self, tmp_path):
        with patch("drspec.core.indexing.start_indexing") as started:
            data, is_error = self._call(tmp_path, no_index=True)
        assert not is_error and "indexing" not in data
        started.assert_not_called()
        message, is_error = self._call(tmp_path, scan=True, no_index=True)
        assert is_error and "no_index" in message

    def test_the_schema_lists_the_options(self):
        from drspec.mcp_server import _TOOLS

        properties = next(tool for tool in _TOOLS if tool["name"] == "init")["inputSchema"]["properties"]
        assert {"scan", "no_wait", "no_index"} <= set(properties)


def _real_env() -> dict:
    env = dict(os.environ)
    env.pop("DRSPEC_DISABLE_AUTO_WORKER", None)
    return env


def _run(cwd: Path, env: dict, *args: str, timeout: float = 180) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "drspec", "--json", *args], cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout)  # pattern-ok: real-subprocess the objective is a real process started by a real init


def _ask_worker(db: Path, sql: str):
    """One question to the running worker over its socket, from this process: no `python -m drspec` is started to ask it (about 1.6 s of
    imports each). `None` while there is no worker to answer."""
    from drspec.worker_ipc import WorkerConnection, _try_connect, socket_path

    sock = _try_connect(socket_path(db), timeout=2.0)
    if sock is None:
        return None
    client = WorkerConnection(sock)
    try:
        return client.execute(sql).fetchone()[0]
    finally:
        client.close()


def _set_pace(db: Path, **settings) -> None:
    """Write settings into the running worker's database over its socket (from this process)."""
    from drspec.core.settings import set_setting
    from drspec.worker_ipc import WorkerConnection, _try_connect, socket_path

    client = WorkerConnection(_try_connect(socket_path(db), timeout=10.0))
    try:
        for key, value in settings.items():
            set_setting(client, key, value)
    finally:
        client.close()


#: The fastest background pace the product allows (`worker.cpu_percent` is capped at 100, the pause between batches at 0): what a test that
#: only needs the lane to FINISH sets, so its wall time is the worker's start and the work, not the pace.
FASTEST = {"worker.cpu_percent": 100, "worker.yield_ms": 0, "worker.rescan_budget": 200, "worker.pass_seconds": 5.0}


@pytest.mark.slow  # pattern-ok: slow-socket-model-marker a real worker process, with integration and a reason
@pytest.mark.integration  # the objective is a real worker process, started by a real `init` process
class TestWithARealWorker:
    """The one behaviour of `init` that needs a process of its own. `init --scan` and `init --scan --no-wait` (what is returned, when) are
    `scan_whole_project(wait=...)`, which `TestScanAtInit` pins on the in-process worker connection: the real-worker versions of those two
    tests were removed (the break `wait=not no_wait` -> `wait=False` / `wait=True` fails the in-process tests, see the submission)."""

    @staticmethod
    def _project(cwd: Path, files: int) -> None:
        for index in range(files):
            (cwd / f"mod{index}.py").write_text(f"def fn{index}():\n    return {index}\n")

    def test_init_alone_starts_a_worker_that_indexes_with_no_other_command(self, tmp_path, wait_until):
        from drspec.staleness_worker import is_worker_running

        env = _real_env()
        self._project(tmp_path, 8)
        db = tmp_path / "_drspec" / "contracts.db"
        try:
            done = _run(tmp_path, env, "init", "--no-ide", "--non-interactive")  # pattern-ok: init-for-setup init is the objective of this file
            assert done.returncode == 0, done.stderr
            assert json.loads(done.stdout)["data"]["indexing"]["started"] is True
            assert is_worker_running(db) is not None
            # nothing else is run to start it: the worker found the files by itself (the objective is that it does, not how fast: the pace
            # is set to the fastest the product allows, over the socket, once the worker answers; the default is cpu_percent 20, yield 50 ms)
            assert wait_until(lambda: _ask_worker(db, "SELECT 1") == 1, timeout=60, interval=0.1)
            _set_pace(db, **FASTEST)
            assert wait_until(lambda: _ask_worker(db, "SELECT COUNT(*) FROM artifacts") == 8, timeout=120, interval=0.2)
        finally:
            _run(tmp_path, env, "worker", "stop")
        assert is_worker_running(db) is None
