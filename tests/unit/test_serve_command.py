"""`run_server` blocks until Ctrl-C, so these tests mock it out to
verify CLI validation/wiring without hanging the test suite; real
server behavior is covered in `test_server.py`.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from drspec.cli.app import app

runner = CliRunner()


class TestServeCommand:
    def test_requires_init(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)  # an empty folder: no project
        result = runner.invoke(app, ["--json", "serve", "a.py::foo"])

        assert result.exit_code == 1
        response = json.loads(result.output)
        assert response["error"]["code"] == "DB_NOT_INITIALIZED"

    def test_rejects_unknown_function(self, kept_project):
        result = runner.invoke(app, ["--json", "serve", "a.py::nonexistent"])

        assert result.exit_code == 1
        response = json.loads(result.output)
        assert response["error"]["code"] == "FUNCTION_NOT_FOUND"

    def test_rejects_invalid_direction(self, kept_project):
        Path("a.py").write_text("def foo():\n    pass\n")
        runner.invoke(app, ["scan", "."])

        result = runner.invoke(app, ["--json", "serve", "a.py::foo", "--direction", "sideways"])

        assert result.exit_code == 1
        response = json.loads(result.output)
        assert response["error"]["code"] == "INVALID_INPUT"

    def test_flags_work_before_and_after_the_positional(self, kept_project):
        """Regression: `serve` must be registered directly on the main
        app (not as a nested Typer sub-app), or a flag after the
        positional function_id silently fails to parse."""
        Path("a.py").write_text("def foo():\n    pass\n")
        runner.invoke(app, ["scan", "."])

        with patch("drspec.cli.commands.serve.run_server") as mock_run:
            before = runner.invoke(app, ["serve", "--depth", "3", "a.py::foo"])
            after = runner.invoke(app, ["serve", "a.py::foo", "--depth", "3"])

        assert before.exit_code == 0
        assert after.exit_code == 0
        assert mock_run.call_count == 2

    def test_calls_run_server_with_validated_arguments(self, kept_project):
        Path("a.py").write_text("def foo():\n    pass\n")
        runner.invoke(app, ["scan", "."])

        with patch("drspec.cli.commands.serve.run_server") as mock_run:
            result = runner.invoke(
                app, ["serve", "a.py::foo", "--depth", "4", "--direction", "callers", "--port", "9123"]
            )

        assert result.exit_code == 0
        mock_run.assert_called_once()
        _, kwargs = mock_run.call_args
        assert kwargs["depth"] == 4
        assert kwargs["direction"] == "callers"
        assert kwargs["port"] == 9123

    def test_surfaces_port_binding_failure_as_a_clean_error(self, kept_project):
        Path("a.py").write_text("def foo():\n    pass\n")
        runner.invoke(app, ["scan", "."])

        with patch(
            "drspec.cli.commands.serve.run_server", side_effect=RuntimeError("Could not bind to 127.0.0.1:1")
        ):
            result = runner.invoke(app, ["--json", "serve", "a.py::foo"])

        assert result.exit_code == 1
        response = json.loads(result.output)
        assert response["success"] is False
        assert "Could not bind" in response["error"]["message"]
