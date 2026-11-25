import json
from pathlib import Path

from typer.testing import CliRunner

from drspec.cli.app import app

runner = CliRunner()


class TestStatusCommand:
    def test_status_requires_init(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)  # an empty folder: no project
        result = runner.invoke(app, ["--json", "status"])

        assert result.exit_code == 1
        response = json.loads(result.output)
        assert response["success"] is False
        assert response["error"]["code"] == "DB_NOT_INITIALIZED"

    def test_status_empty_project(self, kept_project):
        result = runner.invoke(app, ["--json", "status"])

        assert result.exit_code == 0
        response = json.loads(result.output)
        assert response["success"] is True

        assert "artifacts" in response["data"]
        assert response["data"]["artifacts"]["total"] == 0
        assert "by_status" in response["data"]["artifacts"]

        assert "queue" not in response["data"]
        backlog = response["data"]["backlog"]
        assert backlog["files_pending_rescan"] == 0
        assert backlog["embedding"]["enabled"] is True
        assert backlog["embedding"]["message"] == "embedding up to date"

        assert "contracts" in response["data"]
        assert response["data"]["contracts"]["total"] == 0
        assert "confidence" in response["data"]["contracts"]

        assert "summary" in response["data"]
        assert "items_needing_attention" in response["data"]["summary"]
        assert "completion_rate" in response["data"]["summary"]

    def test_status_after_scan(self, kept_project):
        Path("test.py").write_text('''
def hello():
    return "world"

def add(a, b):
    return a + b
''')
        runner.invoke(app, ["scan", "test.py"])

        result = runner.invoke(app, ["--json", "status"])

        assert result.exit_code == 0
        response = json.loads(result.output)
        assert response["success"] is True

        assert response["data"]["artifacts"]["total"] == 2
        assert response["data"]["artifacts"]["by_status"]["PENDING"] == 2

        assert response["data"]["summary"]["items_needing_attention"] == 2

    def test_status_artifact_statuses(self, kept_project):
        result = runner.invoke(app, ["--json", "status"])

        response = json.loads(result.output)
        by_status = response["data"]["artifacts"]["by_status"]

        assert "PENDING" in by_status
        assert "VERIFIED" in by_status
        assert "NEEDS_REVIEW" in by_status
        assert "STALE" in by_status
        assert "BROKEN" in by_status

    def test_status_contracts_confidence(self, kept_project):
        result = runner.invoke(app, ["--json", "status"])

        response = json.loads(result.output)
        confidence = response["data"]["contracts"]["confidence"]

        assert "average" in confidence
        assert "distribution" in confidence

        dist = confidence["distribution"]
        assert "below_50" in dist
        assert "50_to_70" in dist
        assert "70_to_90" in dist
        assert "above_90" in dist

    def test_status_returns_json(self, kept_project):
        result = runner.invoke(app, ["--json", "status"])

        response = json.loads(result.output)
        assert "success" in response
        assert "data" in response
        assert "error" in response

    def test_status_completion_rate_zero_artifacts(self, kept_project):
        result = runner.invoke(app, ["--json", "status"])

        response = json.loads(result.output)
        assert response["data"]["summary"]["completion_rate"] == 0.0

    def test_status_reports_edge_counts(self, kept_project):
        """Edge counts by confidence tier must be visible via the CLI, not
        just a raw DB query."""

        result = runner.invoke(app, ["--json", "status"])
        response = json.loads(result.output)
        assert response["data"]["edges"] == {
            "calls": {"total": 0, "by_confidence": {"EXTRACTED": 0, "INFERRED": 0, "AMBIGUOUS": 0}},
            "references": {"total": 0, "by_confidence": {"EXTRACTED": 0, "INFERRED": 0, "AMBIGUOUS": 0}},
        }

        Path("a.py").write_text("def foo():\n    return 1\n\n\ndef bar():\n    return foo()\n")
        runner.invoke(app, ["scan", "."])

        result = runner.invoke(app, ["--json", "status"])
        response = json.loads(result.output)
        assert response["data"]["edges"]["calls"]["total"] == 1
        assert response["data"]["edges"]["calls"]["by_confidence"]["EXTRACTED"] == 1
