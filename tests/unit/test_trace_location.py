"""The trace log lives outside the project, one file per project, and holds no project data.

The projects are in memory (`kept_duckdb`: one DuckDB kept between the commands, no file) and the command tree is built once
(`cached_cli_command`); the project folder, its files and the trace folder are real, since where the log is written is the objective. The
worker is `run_worker` in this process, with its server on a real socket (integration): no process is started.
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.core import tracing
from drspec.core.settings import set_setting
from drspec.core.trace_report import clear_trace, trace_summary, trace_tail
from drspec.db import get_connection, init_schema

runner = CliRunner()

pytestmark = pytest.mark.usefixtures("kept_duckdb", "cached_cli_command")


def _invoke(monkeypatch, args):
    monkeypatch.setattr("sys.argv", ["drspec", *args])
    return runner.invoke(app, args)


def _project(root: Path, monkeypatch, files=None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, source in (files or {"a.py": "def f():\n    return 1\n"}).items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(source)
    monkeypatch.chdir(root)
    (root / "_drspec").mkdir()
    connection = get_connection(root / "_drspec" / "contracts.db")  # (kept in memory: what `init` makes of the schema, without `init`)
    init_schema(connection)
    connection.close()
    assert _invoke(monkeypatch, ["scan", "."]).exit_code == 0
    return root


def _folder() -> Path:
    return tracing.default_trace_dir()


class TestWhereTheLogIs:
    @pytest.mark.component
    def test_nothing_is_written_under_the_project_and_the_log_is_private(self, tmp_path, monkeypatch):
        root = _project(tmp_path / "p", monkeypatch)
        assert _invoke(monkeypatch, ["--trace", "search", "f"]).exit_code == 0
        assert not [p for p in (root / "_drspec").iterdir() if p.name.startswith("trace.log")]
        log = tracing.log_file_for(root / "_drspec" / "contracts.db")
        assert log.exists() and log.parent == _folder()
        assert stat.S_IMODE(log.stat().st_mode) == 0o600 and stat.S_IMODE(_folder().stat().st_mode) == 0o700

    @pytest.mark.unit
    def test_the_name_is_a_hash_of_the_database_path(self, tmp_path):
        a, b = tmp_path / "a" / "contracts.db", tmp_path / "b" / "contracts.db"
        assert tracing.log_file_for(a).name != tracing.log_file_for(b).name
        assert tracing.log_file_for(a) == tracing.log_file_for(a)
        assert len(tracing.log_file_for(a).stem) == 16 and str(tmp_path) not in tracing.log_file_for(a).name

    @pytest.mark.unit
    def test_the_default_folder_follows_the_temporary_directory_and_the_user(self, monkeypatch, tmp_path):
        monkeypatch.delenv(tracing.DIR_ENV_VAR)
        monkeypatch.setattr(tracing.tempfile, "gettempdir", lambda: str(tmp_path))
        assert tracing.default_trace_dir() == tmp_path / ("drspec-trace-" + tracing.user_id())

    @pytest.mark.component
    def test_the_environment_variable_moves_the_log(self, tmp_path, monkeypatch):
        elsewhere = tmp_path / "elsewhere"
        monkeypatch.setenv(tracing.DIR_ENV_VAR, str(elsewhere))
        root = _project(tmp_path / "p", monkeypatch)
        assert _invoke(monkeypatch, ["--trace", "search", "f"]).exit_code == 0
        assert tracing.log_file_for(root / "_drspec" / "contracts.db").parent == elsewhere
        assert list(elsewhere.glob("*.log"))

    @pytest.mark.component
    def test_an_unwritable_folder_leaves_tracing_off_and_the_command_working(self, tmp_path, monkeypatch):
        blocker = tmp_path / "blocker"
        blocker.write_text("not a folder")
        monkeypatch.setenv(tracing.DIR_ENV_VAR, str(blocker / "logs"))
        _project(tmp_path / "p", monkeypatch)
        result = _invoke(monkeypatch, ["--trace", "search", "f"])
        assert result.exit_code == 0 and not blocker.is_dir()

    @pytest.mark.unit
    def test_a_symbolic_link_in_place_of_the_folder_is_refused_with_one_line(self, tmp_path, monkeypatch, capsys):
        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "link"
        link.symlink_to(target)
        monkeypatch.setenv(tracing.DIR_ENV_VAR, str(link))
        tracing.bind(tmp_path / "contracts.db")
        tracing.configure("all", 500, 20)
        for _ in range(3):
            tracing.emit("request", 1.0, op="execute")
        assert not list(target.iterdir())
        assert [line for line in capsys.readouterr().err.splitlines() if "tracing is off" in line and "not a plain directory" in line]

    @pytest.mark.unit
    def test_a_folder_loose_enough_for_others_is_tightened(self, tmp_path, monkeypatch):
        folder = tmp_path / "logs"
        folder.mkdir(mode=0o777)
        os.chmod(folder, 0o777)
        monkeypatch.setenv(tracing.DIR_ENV_VAR, str(folder))
        assert tracing.ensure_trace_dir() == folder and stat.S_IMODE(folder.stat().st_mode) == 0o700


class TestOneFilePerProject:
    @pytest.mark.component
    def test_two_projects_never_share_a_log(self, tmp_path, monkeypatch):
        first = _project(tmp_path / "one", monkeypatch)
        _invoke(monkeypatch, ["--trace", "search", "f"])
        second = _project(tmp_path / "two", monkeypatch)
        _invoke(monkeypatch, ["--trace", "status"])
        _invoke(monkeypatch, ["--trace", "status"])
        db1, db2 = first / "_drspec" / "contracts.db", second / "_drspec" / "contracts.db"
        names1 = {e["label"] for e in trace_tail(db1, 100)["events"] if e["event"] == "command"}
        names2 = [e["label"] for e in trace_tail(db2, 100)["events"] if e["event"] == "command"]
        assert names1 == {"search"} and names2 == ["status", "status"]
        assert trace_summary(db1)["events_read"] == 1 and trace_summary(db2)["events_read"] == 2
        assert len(list(_folder().glob("*.log"))) == 2

    @pytest.mark.component
    def test_list_shows_every_log_without_a_path_and_clear_touches_only_this_project(self, tmp_path, monkeypatch):
        first = _project(tmp_path / "one", monkeypatch)
        _invoke(monkeypatch, ["--trace", "search", "f"])
        second = _project(tmp_path / "two", monkeypatch)
        _invoke(monkeypatch, ["--trace", "status"])
        listing = json.loads(_invoke(monkeypatch, ["--json", "trace", "list"]).output)["data"]
        assert len(listing["logs"]) == 2 and all(set(row) == {"name", "size_bytes", "last_event"} for row in listing["logs"])
        assert str(tmp_path) not in json.dumps(listing["logs"])

        monkeypatch.chdir(second)
        assert _invoke(monkeypatch, ["--json", "trace", "clear"]).exit_code == 0
        assert trace_summary(second / "_drspec" / "contracts.db")["events_read"] == 0
        assert trace_summary(first / "_drspec" / "contracts.db")["events_read"] == 1

        assert _invoke(monkeypatch, ["--json", "trace", "clear", "--all"]).exit_code == 0
        assert trace_summary(first / "_drspec" / "contracts.db")["events_read"] == 0

    @pytest.mark.component
    def test_path_prints_where_this_project_logs(self, tmp_path, monkeypatch):
        root = _project(tmp_path / "p", monkeypatch)
        data = json.loads(_invoke(monkeypatch, ["--json", "trace", "path"]).output)["data"]
        assert data["path"] == str(tracing.log_file_for(root / "_drspec" / "contracts.db")) and data["exists"] is False


class TestKeepingItSmall:
    def _log(self, name: str, size: int, age_days: float) -> Path:
        _folder().mkdir(parents=True, exist_ok=True)
        path = _folder() / (name + ".log")
        path.write_bytes(b"x" * size)
        when = time.time() - age_days * 86400
        os.utime(path, (when, when))
        return path

    @pytest.mark.unit
    def test_a_log_not_written_for_days_is_removed(self):
        old, fresh = self._log("a" * 16, 10, 9), self._log("b" * 16, 10, 1)
        assert tracing.prune_logs(7, 200) == 1
        assert not old.exists() and fresh.exists()

    @pytest.mark.unit
    def test_over_the_total_cap_the_oldest_go_first_and_the_one_in_use_stays(self):
        oldest, middle, newest = self._log("a" * 16, 600_000, 3), self._log("b" * 16, 600_000, 2), self._log("c" * 16, 600_000, 1)
        assert tracing.prune_logs(7, 1.5, in_use=oldest) == 1  # 1.8 MB is over 1.5 MB; the oldest is in use, so the next oldest goes
        assert oldest.exists() and not middle.exists() and newest.exists()

    @pytest.mark.integration
    @pytest.mark.socket  # the worker's loop with its server on a real socket is the objective  # pattern-ok: slow-socket-model-marker run_worker serves on a real socket: the only way to start the worker's loop
    def test_the_worker_applies_the_configured_numbers_at_its_start(self, tmp_path, monkeypatch):
        from drspec.staleness_worker import run_worker

        root = _project(tmp_path / "p", monkeypatch)
        db = root / "_drspec" / "contracts.db"
        connection = get_connection(db)
        set_setting(connection, "trace.keep_days", 1)  # (the default is 7: a log of two days is kept unless this number is the one used)
        connection.close()
        two_days, hours = self._log("d" * 16, 10, 2), self._log("e" * 16, 10, 0.1)
        run_worker(db, idle_timeout=0.1, poll_interval=0.05)
        assert not two_days.exists() and hours.exists()


class TestTheOldPlace:
    @pytest.mark.component
    def test_a_log_left_in_the_project_is_read_as_legacy_until_cleared(self, tmp_path, monkeypatch):
        root = _project(tmp_path / "p", monkeypatch)
        db = root / "_drspec" / "contracts.db"
        (root / "_drspec" / "trace.log").write_text(json.dumps({"t": "2099-01-01T00:00:00.000+00:00", "event": "pass", "ms": 5.0}) + "\n")
        (root / "_drspec" / "trace.log.1").write_text("x\n")
        events = trace_tail(db)["events"]
        assert [e.get("legacy") for e in events] == [True]
        result = clear_trace(db)
        assert result["legacy_removed_bytes"] > 0
        assert not (root / "_drspec" / "trace.log").exists() and not (root / "_drspec" / "trace.log.1").exists()

    @pytest.mark.component
    def test_the_trace_is_never_written_there_again(self, tmp_path, monkeypatch):
        root = _project(tmp_path / "p", monkeypatch)
        _invoke(monkeypatch, ["--trace", "search", "f"])
        assert not (root / "_drspec" / "trace.log").exists()


class TestNoProjectData:
    MARKERS = ("zqmarkerdir", "zqmarkerfile", "zqmarkerfunc", "zqmarkerbody", "zqmarkerpattern", "zqmarkercontract")

    def _run_everything(self, tmp_path, monkeypatch):
        files = {
            "zqmarkerdir/zqmarkerfile.py": "def zqmarkerfunc(zqmarkerparam):\n    return 'zqmarkerbody'\n\n\ndef caller():\n    return zqmarkerfunc(1)\n",
        }
        root = _project(tmp_path / "proj", monkeypatch, files)
        monkeypatch.setenv("DRSPEC_TRACE", "all")
        function_id = "zqmarkerdir/zqmarkerfile.py::zqmarkerfunc"
        for args in (
            ["scan", "."],
            ["search", "zqmarkerpattern"], ["search", "zqmarkerfunc"], ["search", "-E", "zqmarker.*pattern"], ["search", "zqmarkerfunc", "--lines"],
            ["source", "get", function_id], ["deps", "get", function_id], ["status"],
        ):
            _invoke(monkeypatch, args)
        from drspec.contracts import Contract

        contract = Contract.from_dict(dict(
            function_signature="def zqmarkerfunc():", intent_summary="zqmarkercontract",
            invariants=[dict(name="rule0", logic="Output is positive", criticality="HIGH", on_fail="error")],
        )).to_json()
        monkeypatch.setattr("sys.argv", ["drspec", "contract", "save", function_id])
        runner.invoke(app, ["contract", "save", function_id, "--confidence", "80"], input=contract)
        _invoke(monkeypatch, ["contract", "get", function_id])
        return root

    @pytest.mark.component
    def test_no_marker_and_no_project_path_reaches_the_log_folder(self, tmp_path, monkeypatch):
        root = self._run_everything(tmp_path, monkeypatch)
        text = "".join(path.read_text(errors="replace") for path in _folder().iterdir() if path.is_file())
        assert text, "tracing wrote nothing"
        for marker in self.MARKERS:
            assert marker not in text, marker
        assert str(root) not in text and str(tmp_path) not in text

    @pytest.mark.component
    def test_a_re_scan_and_a_search_no_longer_carry_a_file_or_a_pattern(self, tmp_path, monkeypatch):
        root = self._run_everything(tmp_path, monkeypatch)
        events = list(trace_tail(root / "_drspec" / "contracts.db", 500)["events"])
        assert events and all("file" not in e and "pattern" not in e and "first" not in e for e in events)

    @pytest.mark.unit
    def test_a_field_that_is_not_on_the_list_is_dropped_and_counted(self, tmp_path):
        tracing.bind(tmp_path / "contracts.db")
        tracing.configure("all", 500, 20)
        tracing.emit("request", 1.0, op="execute", file="src/secret.py", pattern="zq", rows=3, kind="/etc/passwd", args=["--ok", "value"])  # pattern-ok: outside-path a value that tracing must not keep, never opened
        (line,) = [json.loads(entry) for entry in tracing.log_file_for(tmp_path / "contracts.db").read_text().splitlines()]
        assert line["rows"] == 3 and line["op"] == "execute" and line["dropped_fields"] == 4
        assert not {"file", "pattern", "kind", "args"} & set(line)

    @pytest.mark.unit
    def test_text_is_accepted_only_as_a_short_code(self, tmp_path):
        tracing.bind(tmp_path / "contracts.db")
        tracing.configure("all", 500, 20)
        tracing.emit("embed", 1.0, kind="chunk", reason="a sentence that quotes src/x.py", status="ok")
        (line,) = [json.loads(entry) for entry in tracing.log_file_for(tmp_path / "contracts.db").read_text().splitlines()]
        assert line["kind"] == "chunk" and line["status"] == "ok" and "reason" not in line and line["dropped_fields"] == 1


@pytest.mark.integration
@pytest.mark.socket  # the worker's loop with its server on a real socket is the objective  # pattern-ok: slow-socket-model-marker run_worker serves on a real socket: the only way to start the worker's loop
class TestTheWorker:
    def test_a_worker_with_tracing_on_writes_only_to_the_log_folder_and_no_marker_leaks(self, tmp_path, monkeypatch):
        from drspec.staleness_worker import run_worker

        root = tmp_path / "proj"
        (root / "zqmarkerdir").mkdir(parents=True)
        (root / "zqmarkerdir" / "zqmarkerfile.py").write_text("def zqmarkerfunc():\n    return 1\n")
        (root / "_drspec").mkdir()
        db = root / "_drspec" / "contracts.db"
        connection = get_connection(db)  # (kept in memory)
        init_schema(connection)
        set_setting(connection, "trace.level", "all")
        connection.close()
        monkeypatch.chdir(root)

        run_worker(db, idle_timeout=0.1, poll_interval=0.05)  # its first passes find the file nobody scanned and read it: the worker's own events

        log = tracing.log_file_for(db)
        assert log.exists() and log.parent == _folder()
        text = log.read_text()
        assert '"proc":"worker"' in text and "zqmarker" not in text and str(root) not in text and str(tmp_path) not in text
        assert '"event":"rescan"' in text and '"kind":"parse"' in text, "the worker did not trace the file it read"
        connection = get_connection(db)
        try:
            assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1, "the worker never read the file"
        finally:
            connection.close()
        assert not [p for p in (root / "_drspec").iterdir() if p.name.startswith("trace.log")]
