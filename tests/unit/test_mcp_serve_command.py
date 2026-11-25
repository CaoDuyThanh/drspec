"""Tests for the `drspec mcp-serve` CLI command.

`mcp_server.run` blocks on stdin, so it's mocked here to verify the
command's own db_path resolution without hanging the test suite — the
protocol/tool behavior itself is covered in `test_mcp_server.py`.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from drspec.cli.app import app

runner = CliRunner()


class TestMcpServeCommand:
    def test_defaults_to_drspec_contracts_db_under_cwd(self, tmp_path):
        tmpdir = str(tmp_path)
        with runner.isolated_filesystem(temp_dir=tmpdir):
            with patch("drspec.cli.commands.mcp_serve.mcp_server.run") as mock_run:
                result = runner.invoke(app, ["mcp-serve"])

            assert result.exit_code == 0
            mock_run.assert_called_once()
            (db_path,), _ = mock_run.call_args
            assert db_path == Path.cwd() / "_drspec" / "contracts.db"

    def test_respects_the_global_db_override(self, tmp_path):
        tmpdir = str(tmp_path)
        with runner.isolated_filesystem(temp_dir=tmpdir):
            override = Path("custom") / "contracts.db"
            with patch("drspec.cli.commands.mcp_serve.mcp_server.run") as mock_run:
                result = runner.invoke(app, ["--db", str(override), "mcp-serve"])

            assert result.exit_code == 0
            mock_run.assert_called_once()
            (db_path,), _ = mock_run.call_args
            assert db_path == override

    def test_does_not_require_drspec_init_up_front(self, tmp_path):
        """Unlike `serve`, `mcp-serve` has no positional function_id to
        validate — DB/function existence is checked per tool call inside
        the running server instead, so starting the process itself never
        fails just because the project hasn't been scanned yet."""
        tmpdir = str(tmp_path)
        with runner.isolated_filesystem(temp_dir=tmpdir):
            with patch("drspec.cli.commands.mcp_serve.mcp_server.run") as mock_run:
                result = runner.invoke(app, ["mcp-serve"])

            assert result.exit_code == 0
            mock_run.assert_called_once()
