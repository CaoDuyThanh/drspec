"""The CLI's start-up imports what every command needs and nothing else: not the graph viewer (matplotlib), the contract models
(pydantic), the parsers (tree-sitter), numpy or DuckDB. A command module is imported when its command is looked up."""

from __future__ import annotations

import builtins
import sys

import pytest
import typer
from typer.testing import CliRunner

HEAVY = ("matplotlib", "numpy", "pydantic", "tree_sitter", "tree_sitter_cpp", "tree_sitter_python", "tree_sitter_javascript", "tree_sitter_rust", "duckdb", "pandas")
NOT_AT_START = ("drspec.server", "drspec.visualization", "drspec.contracts", "drspec.parsers", "drspec.core.scanner", "drspec.core.embedder", "drspec.cli.commands.serve", "drspec.cli.commands.contract")

COMMANDS = "scan search serve ack mcp-serve init status config contract source verify deps refs strings learn vision worker trace".split()


@pytest.fixture
def fresh_import():
    """Import `drspec.cli.app` as a new process would, in this one: every `drspec` module is forgotten for the moment (and put back
    after), and the names the `drspec` modules import are recorded."""
    saved = dict(sys.modules)
    for name in [name for name in sys.modules if name == "drspec" or name.startswith("drspec.")]:
        del sys.modules[name]
    imported: dict = {"names": set(), "drspec": set()}
    real_import = builtins.__import__

    def recording(name, globals=None, locals=None, fromlist=(), level=0):
        owner = (globals or {}).get("__name__", "")
        if owner.startswith("drspec") and level == 0:
            imported["names"].add(name.split(".")[0])
        return real_import(name, globals, locals, fromlist, level)

    builtins.__import__ = recording
    try:
        import drspec.cli.app  # noqa: F401

        imported["drspec"] = {name for name in sys.modules if name.startswith("drspec")}
        yield imported
    finally:
        builtins.__import__ = real_import
        for name in [name for name in sys.modules if name == "drspec" or name.startswith("drspec.")]:
            del sys.modules[name]
        sys.modules.update({name: module for name, module in saved.items() if name == "drspec" or name.startswith("drspec.")})


class TestWhatStartingTheCliImports:
    def test_no_heavy_library_is_imported_by_the_drspec_modules_of_the_start_up(self, fresh_import):
        assert sorted(set(fresh_import["names"]) & set(HEAVY)) == []

    def test_the_viewer_the_contract_models_and_the_parsers_are_not_imported(self, fresh_import):
        loaded = fresh_import["drspec"]
        assert [name for name in NOT_AT_START if name in loaded or any(other.startswith(name + ".") for other in loaded)] == []

    def test_no_command_module_is_imported_before_its_command_is_asked_for(self, fresh_import):
        assert [name for name in fresh_import["drspec"] if name.startswith("drspec.cli.commands.")] == []


class TestTheCommandsAreStillThere:
    def test_the_root_lists_every_command_in_the_same_order(self):
        from drspec.cli.app import app

        command = typer.main.get_command(app)
        assert command.list_commands(None) == COMMANDS

    def test_every_command_loads_and_a_direct_command_keeps_its_help(self):
        from drspec.cli.app import _COMMANDS, app

        command = typer.main.get_command(app)
        for name in COMMANDS:
            loaded = command.get_command(None, name)
            assert loaded is not None and loaded.name == name
            if _COMMANDS[name][2] is not None:
                assert loaded.help == _COMMANDS[name][2]

    def test_the_root_help_lists_them_all_and_an_unknown_command_is_an_error(self):
        from drspec.cli.app import app

        runner = CliRunner()
        text = runner.invoke(app, ["--help"], terminal_width=120).output
        assert all(name in text for name in COMMANDS)
        assert runner.invoke(app, ["no-such-command"]).exit_code == 2

    def test_the_version_and_a_command_still_run(self, kept_project):
        from drspec import __version__
        from drspec.cli.app import app

        runner = CliRunner()
        assert runner.invoke(app, ["--version"]).output.strip() == __version__
        assert runner.invoke(app, ["config", "get", "trace.level"]).exit_code == 0

    def test_a_package_attribute_still_gives_the_module(self):
        from drspec.cli import commands
        from drspec.core import Scanner

        assert commands.status.app is not None and Scanner.__name__ == "Scanner"
