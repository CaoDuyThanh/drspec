"""Tests for CLI command structure and subcommands."""

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from drspec.cli.app import app

runner = CliRunner()


@pytest.fixture(scope="module")
def _initialised_project(tmp_path_factory, initialised_template):
    """An initialised project: a copy of the session's (an `init` is 250 ms and every test below ran one)."""
    root = tmp_path_factory.mktemp("initialised") / "p"
    shutil.copytree(initialised_template, root)
    return root


@pytest.fixture(autouse=True)
def project_dir(_initialised_project, tmp_path, monkeypatch):
    """Run every test in its own copy of an initialized temp project. These tests resolve the project through `Path.cwd()`; against the
    checkout they would open its real `_drspec` database, which a live worker may hold."""
    root = tmp_path / "p"
    shutil.copytree(_initialised_project, root)
    monkeypatch.chdir(root)
    return root


class TestGlobalFlags:
    def test_json_flag_default_enabled(self):
        result = runner.invoke(app, ["init", "--project-root", str(Path.cwd())])
        assert result.exit_code == 0
        # default output format is plain text, not JSON
        assert "{" not in result.stdout

    def test_json_flag_forces_json(self):
        result = runner.invoke(app, ["--json", "init", "--project-root", str(Path.cwd())])
        assert result.exit_code == 0
        assert "{" in result.stdout
        json.loads(result.stdout)

    def test_no_json_flag(self):
        result = runner.invoke(app, ["--no-json", "init"])
        assert result.exit_code == 0

    def test_db_path_flag(self, tmp_path):
        result = runner.invoke(app, ["--db", str(tmp_path / "test.db"), "init"])
        assert result.exit_code == 0


class TestCommandGroups:
    def test_init_command_exists(self):
        result = runner.invoke(app, ["init", "--project-root", str(Path.cwd()), "--help"])
        assert result.exit_code == 0
        assert "Initialize" in result.stdout

    def test_scan_command_exists(self):
        result = runner.invoke(app, ["scan", "--help"])
        assert result.exit_code == 0
        assert "scan" in result.stdout.lower()

    def test_status_command_exists(self):
        result = runner.invoke(app, ["status", "--help"])
        assert result.exit_code == 0
        assert "status" in result.stdout.lower()

    def test_the_contract_queue_command_is_gone(self):
        result = runner.invoke(app, ["queue", "--help"])
        assert result.exit_code != 0

    def test_contract_command_exists(self):
        result = runner.invoke(app, ["contract", "--help"])
        assert result.exit_code == 0
        assert "contract" in result.stdout.lower()

    def test_source_command_exists(self):
        result = runner.invoke(app, ["source", "--help"])
        assert result.exit_code == 0
        assert "source" in result.stdout.lower()

    def test_verify_command_exists(self):
        result = runner.invoke(app, ["verify", "--help"])
        assert result.exit_code == 0
        assert "verify" in result.stdout.lower()

    def test_deps_command_exists(self):
        result = runner.invoke(app, ["deps", "--help"])
        assert result.exit_code == 0
        assert "deps" in result.stdout.lower()


class TestContractSubcommands:
    def test_contract_get_help(self):
        result = runner.invoke(app, ["contract", "get", "--help"])
        assert result.exit_code == 0
        assert "function_id" in result.stdout.lower()

    def test_contract_get_requires_init(self, tmp_path):
        tmpdir = str(tmp_path)
        with runner.isolated_filesystem(temp_dir=tmpdir):
            result = runner.invoke(app, ["contract", "get", "test.py::foo"])
            assert result.exit_code == 1
            assert "DB_NOT_INITIALIZED" in result.stdout

    def test_contract_save_help(self):
        result = runner.invoke(app, ["contract", "save", "--help"])
        assert result.exit_code == 0
        assert "confidence" in result.stdout.lower()

    def test_contract_save_requires_confidence(self):
        result = runner.invoke(app, ["contract", "save", "test.py::foo"])
        # exit code 2 indicates missing required option
        assert result.exit_code == 2
        assert "confidence" in result.output.lower()

    def test_contract_list(self):
        result = runner.invoke(app, ["contract", "list"])
        assert result.exit_code == 0

    def test_contract_list_with_status_filter(self):
        result = runner.invoke(app, ["contract", "list", "--status", "VERIFIED"])
        assert result.exit_code == 0


class TestSourceSubcommands:
    def test_source_get(self, tmp_path):
        tmpdir = str(tmp_path)
        with runner.isolated_filesystem(temp_dir=tmpdir):
            result = runner.invoke(app, ["--json", "source", "get", "test.py::foo"])
            assert result.exit_code == 1
            response = json.loads(result.output)
            assert response["error"]["code"] == "DB_NOT_INITIALIZED"


class TestVerifySubcommands:
    def test_verify_run_requires_contract(self):
        result = runner.invoke(app, ["--json", "verify", "run", "test.py::foo"], input="{}")
        assert result.exit_code == 1
        response = json.loads(result.output)
        # either DB_NOT_INITIALIZED or CONTRACT_NOT_FOUND
        assert response["success"] is False

    def test_verify_script_requires_contract(self):
        result = runner.invoke(app, ["--json", "verify", "script", "test.py::foo"])
        assert result.exit_code == 1
        response = json.loads(result.output)
        assert response["success"] is False


class TestDepsSubcommands:
    def test_deps_get(self):
        result = runner.invoke(app, ["deps", "get", "test.py::foo"])
        assert result.exit_code == 1

    def test_deps_get_with_depth(self):
        result = runner.invoke(app, ["deps", "get", "--depth", "3", "test.py::foo"])
        assert result.exit_code == 1


class TestHelpText:
    def test_main_help_shows_all_commands(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0

        expected_commands = ["init", "scan", "status", "contract", "source", "verify", "deps"]
        for cmd in expected_commands:
            assert cmd in result.stdout, f"Command '{cmd}' not found in help"

    def test_command_descriptions_present(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0

        assert "AI-powered" in result.stdout or "contract" in result.stdout.lower()


class TestExitCodes:
    def test_success_exit_code(self):
        result = runner.invoke(app, ["init", "--project-root", str(Path.cwd())])
        assert result.exit_code == 0

    def test_version_exit_code(self):
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0

    def test_invalid_command_exit_code(self):
        result = runner.invoke(app, ["nonexistent-command"])
        assert result.exit_code != 0
