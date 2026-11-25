from __future__ import annotations

import json
import os
import shutil
import signal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.staleness_worker import is_worker_running

runner = CliRunner()


@pytest.fixture(scope="module")
def _initialised(tmp_path_factory):
    """A project after `init`, made once (250 ms); each test works on a copy."""
    root = tmp_path_factory.mktemp("initialised") / "p"
    root.mkdir()
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(root)
        runner.invoke(app, ["init", "--project-root", str(root)])
    return root


@pytest.fixture
def project(_initialised, tmp_path, monkeypatch):
    root = tmp_path / "p"
    shutil.copytree(_initialised, root)
    monkeypatch.chdir(root)
    return root


class TestWorkerStatus:
    def test_reports_not_running_before_init(self, tmp_path):
        tmpdir = str(tmp_path)
        with runner.isolated_filesystem(temp_dir=tmpdir):
            result = runner.invoke(app, ["--json", "worker", "status"])

            assert result.exit_code == 0
            response = json.loads(result.output)
            assert response["data"]["running"] is False
            assert response["data"]["pid"] is None

    def test_reports_not_running_and_queue_size_after_init(self, project):
        result = runner.invoke(app, ["--json", "worker", "status"])

        assert result.exit_code == 0
        response = json.loads(result.output)
        assert response["data"]["running"] is False
        assert response["data"]["staleness_queue_size"] == 0


class TestWorkerStop:
    def test_stopping_when_nothing_is_running_is_not_an_error(self, project):
        result = runner.invoke(app, ["--json", "worker", "stop"])

        assert result.exit_code == 0
        response = json.loads(result.output)
        assert response["data"]["stopped"] is False


class TestARunningWorker:
    def test_status_then_stop_of_a_worker_that_holds_the_pid_file(self, project, monkeypatch):
        """The commands see a worker through its PID file and stop it with a signal: here the PID file is this process's (a worker that is up), and
        the signal is a recorder that makes the worker leave (it releases the file), instead of a second process."""
        from drspec import staleness_worker
        from drspec.staleness_worker import acquire_singleton, release_singleton

        db_path = Path.cwd() / "_drspec" / "contracts.db"
        assert acquire_singleton(db_path)
        signalled = []

        def kill(pid, signal_number):
            if signal_number == 0:  # (is this process alive: asked by the PID file check)
                return
            signalled.append((pid, signal_number))
            release_singleton(db_path)

        monkeypatch.setattr(staleness_worker.os, "kill", kill)
        response = json.loads(runner.invoke(app, ["--json", "worker", "status"]).output)
        assert response["data"]["running"] is True
        assert response["data"]["pid"] == os.getpid()

        stop = runner.invoke(app, ["--json", "worker", "stop"])
        assert json.loads(stop.output)["data"]["stopped"] is True
        assert signalled == [(os.getpid(), signal.SIGTERM)]
        assert is_worker_running(db_path) is None
