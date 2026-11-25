"""`drspec learn` prints each result once, through the shared renderer."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.db import get_connection
from drspec.learning.history import (
    LearningEvent,
    init_learning_schema,
    insert_learning_event,
)
from drspec.learning.patterns import PatternType
from tests.fakes import FakeGit

runner = CliRunner()

SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


def _project_with_one_event(initialised_template):
    shutil.copytree(initialised_template, Path.cwd(), dirs_exist_ok=True)  # an initialised project: a copy of the session's `init`
    conn = get_connection(Path("_drspec/contracts.db"))
    init_learning_schema(conn)
    insert_learning_event(
        conn,
        LearningEvent(
            commit_sha=SHA, function_id="a.py::f", pattern_type=PatternType.NULL_CHECK,
            pattern_description="added a null check", invariants_validated=2,
        ),
    )
    conn.close()


class TestLearnHistoryAndStatsPrintOnce:
    """Both commands hand-printed a human block and then also called the
    shared renderer, which prints the same data, so everything appeared twice."""

    def test_history_lists_each_event_once(self, tmp_path, initialised_template):
        tmpdir = str(tmp_path)
        with runner.isolated_filesystem(temp_dir=tmpdir):
            _project_with_one_event(initialised_template)

            result = runner.invoke(app, ["learn", "history"])

            assert result.exit_code == 0, result.output
            assert result.output.count(SHA) == 1
            assert result.output.count("added a null check") == 1

    def test_stats_reports_each_figure_once(self, tmp_path, initialised_template):
        tmpdir = str(tmp_path)
        with runner.isolated_filesystem(temp_dir=tmpdir):
            _project_with_one_event(initialised_template)

            result = runner.invoke(app, ["learn", "stats"])

            assert result.exit_code == 0, result.output
            assert result.output.lower().count("total events") == 1
            assert result.output.lower().count("unique commits") == 1

    def test_json_output_is_unchanged_by_the_cleanup(self, tmp_path, initialised_template):
        tmpdir = str(tmp_path)
        with runner.isolated_filesystem(temp_dir=tmpdir):
            _project_with_one_event(initialised_template)

            history = json.loads(runner.invoke(app, ["--json", "learn", "history"]).output)["data"]
            stats = json.loads(runner.invoke(app, ["--json", "learn", "stats"]).output)["data"]

            assert history["count"] == 1 and history["events"][0]["commit_sha"] == SHA
            assert stats["total_events"] == 1

    def test_an_empty_history_prints_once(self, tmp_path, initialised_template):
        tmpdir = str(tmp_path)
        with runner.isolated_filesystem(temp_dir=tmpdir):
            shutil.copytree(initialised_template, Path.cwd(), dirs_exist_ok=True)  # an initialised project: a copy of the session's `init`

            result = runner.invoke(app, ["learn", "history"])

            assert result.exit_code == 0
            assert result.output.lower().count("events") == 1


class TestLearnAnalyzePrintsOnce:
    def _repo_with_a_bug_fix(self, monkeypatch) -> None:
        """A repository as data (`tests.fakes.FakeGit`): the two commits `learn analyze HEAD~1..HEAD` reads; no git runs."""
        from drspec.cli.commands import learn as learn_module
        from drspec.learning import diff as diff_module

        git = FakeGit().install(monkeypatch, learn_module, diff_module)
        git.commit({"a.py": "def process(x):\n    return x.value\n"}, message="initial")
        git.commit({"a.py": "def process(x):\n    if x is None:\n        return None\n    return x.value\n"}, message="Fix crash when x is None")

    def test_the_summary_appears_once_and_each_result_names_its_commit(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self._repo_with_a_bug_fix(monkeypatch)

        result = runner.invoke(app, ["learn", "analyze", "HEAD~1..HEAD", "--dry-run"])

        assert result.exit_code == 0, result.output
        assert result.output.lower().count("commits analyzed") == 1
        assert result.output.count("Fix crash when x is None") == 1

        data = json.loads(runner.invoke(app, ["--json", "learn", "analyze", "HEAD~1..HEAD", "--dry-run"]).output)["data"]
        assert data["results"][0]["commit_message"] == "Fix crash when x is None"
        assert len(data["results"][0]["commit"]) >= 7
