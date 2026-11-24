"""`drspec scan` against a REAL worker process: the one test of that kind in the scan area.

What a scan through a worker has to do is proven in this process, on the in-process worker connection and the real server on a fake socket
pair: it waits for its batches and counts them (`test_scan_pipeline`), `--no-wait` queues and returns (`test_scan_pipeline::TestNoWait`), Ctrl-C
takes the rest off the queue and what was published stays (`test_scan_cancel`), reads are answered while a publish runs
(`test_worker_ipc::TestReadsDuringAPublish`, `test_database_size`). The four tests that used to be here, each with its own `init`, scans and
`worker stop` processes, are those; this one is what none of them can show: a real worker, started by a real scan, that

- publishes the files of a scan and the scan returns when they are published (and the second scan has nothing to do),
- is not held up by the minimum gap between its passes when someone waits for a scan (`worker.wake_min_gap`: the wiring of `_scan_waiting` in the
  worker loop; the function itself is tested in `test_staleness_worker`).

The commands run in THIS process (with the worker enabled, they talk to the worker over its socket): the worker is the only process started.
"""

from __future__ import annotations

import json
import threading

import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.core.settings import set_setting
from drspec.db import get_connection
from drspec.staleness_worker import is_worker_running

pytestmark = [pytest.mark.slow, pytest.mark.integration]  # a real worker process, started by a real scan  # pattern-ok: slow-socket-model-marker the only test of a real worker in the scan area, with integration and a reason

runner = CliRunner()

#: The gap between the worker's passes is the longest the product allows (an hour) and the scan gets 300 s: the two are far apart, so the test fails only
#: if the scan really waited for the gap and cannot fail because a loaded machine started the worker slowly (that took 9-21 s under load).
WAKE_GAP_SECONDS = 3600
DEADLINE_SECONDS = 300


def _json(*args: str) -> dict:
    result = runner.invoke(app, ["--json", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["data"]


def test_a_scan_through_a_real_worker_returns_when_its_files_are_published_and_is_not_held_by_the_wake_gap(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for i in range(25):
        (tmp_path / f"mod{i}.py").write_text(f"def fn{i}():\n    return {i}\n")
    (tmp_path / "docs.md").write_text("# Notes\n\nCall `fn3` for three.\n")
    db = tmp_path / "_drspec" / "contracts.db"

    # set up in this process (a real file: the worker opens it): no worker yet, the settings it will read, a minute between its passes
    assert runner.invoke(app, ["init", "--no-ide", "--non-interactive", "--no-index"]).exit_code == 0  # pattern-ok: init-for-setup the worker needs a project folder; the objective is the scan
    connection = get_connection(db)  # pattern-ok: duckdb-file the worker process opens this file
    set_setting(connection, "docs.enabled", True)
    set_setting(connection, "worker.wake_min_gap", WAKE_GAP_SECONDS)
    connection.close()

    monkeypatch.delenv("DRSPEC_DISABLE_AUTO_WORKER")  # from here the commands go through a worker, which the first scan starts
    try:
        first = _json("scan")
        # the worker may have indexed some files by itself before the scan looked: those are "unchanged"
        assert first["state"] == "done" and first["files_done"] + first["files_unchanged"] == 26, first
        assert is_worker_running(db) is not None

        second = _json("scan")
        assert second["up_to_date"] is True and second["files_unchanged"] == 26, second
        code, docs = _json("search", "fn3", "--code-only"), _json("search", "fn3", "--docs-only")
        assert code["total_matches"] == 1 and docs["total_matches"] == 1, (code, docs)

        # the worker has just had a pass and may not start the next for WAKE_GAP_SECONDS; a scan that is waited for must not wait it out. The
        # scan runs with a deadline (a scan held by the gap would take WAKE_GAP_SECONDS): reaching it is the failure, no time is measured.
        (tmp_path / "mod0.py").write_text("def fn0():\n    return 'changed'\n")
        outcome: dict = {}
        scan = threading.Thread(target=lambda: outcome.update(result=runner.invoke(app, ["--json", "scan"])), daemon=True)
        scan.start()
        scan.join(DEADLINE_SECONDS)
        assert not scan.is_alive(), "the scan was held back by the minimum gap between the worker's passes"
        data = json.loads(outcome["result"].output)["data"]
        assert data["state"] == "done" and data["files_done"] == 1, data
    finally:
        runner.invoke(app, ["worker", "stop"])
    assert is_worker_running(db) is None
