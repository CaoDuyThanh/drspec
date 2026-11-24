"""`drspec init` tells each provider's client about drspec's MCP server, so the native tools exist; `status` says when a
configured provider has no entry; a running MCP server says when it is out of date."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from drspec import mcp_server
from drspec.cli.app import app
from drspec.core.ide.mcp_registration import register, registration_status, server_command

pytestmark = pytest.mark.usefixtures("fixed_launcher")  # `drspec` is looked up on the PATH: the answer is fixed, not the machine's

runner = CliRunner()


@pytest.fixture
def home(tmp_path, monkeypatch):
    folder = tmp_path / "home"
    folder.mkdir()
    monkeypatch.setenv("HOME", str(folder))
    return folder


@pytest.fixture
def project(tmp_path, home, monkeypatch):
    folder = tmp_path / "project"
    folder.mkdir()
    monkeypatch.chdir(folder)
    return folder


def _init(project: Path, *arguments: str):
    return runner.invoke(app, ["--json", "init", "--project-root", str(project), "--non-interactive", *arguments])


def _entry(path: Path, key: str) -> dict:
    return json.loads(path.read_text())[key]["drspec"]


class TestInitWritesTheEntry:
    def test_claude_code_gets_a_project_mcp_json(self, project):
        result = _init(project, "--ide", "claude-code")
        assert result.exit_code == 0, result.output
        entry = _entry(project / ".mcp.json", "mcpServers")
        assert entry["type"] == "stdio" and entry["args"][-1] == "mcp-serve"
        data = json.loads(result.output)["data"]
        assert data["ide_integrations"]["claude-code"]["mcp_server"]["state"] == "written"
        assert any("Restart Claude Code and approve the drspec server" in note for note in data["mcp_server"])

    def test_cursor_and_copilot_use_their_own_file_and_key(self, project):
        assert _init(project, "--ide", "cursor", "--ide", "github-copilot").exit_code == 0
        assert "type" not in _entry(project / ".cursor" / "mcp.json", "mcpServers")
        assert _entry(project / ".vscode" / "mcp.json", "servers")["type"] == "stdio"

    def test_codex_without_codex_global_prints_the_lines_and_writes_no_file(self, project, home):
        result = _init(project, "--ide", "codex")
        data = json.loads(result.output)["data"]
        registration = data["ide_integrations"]["codex"]["mcp_server"]
        assert registration["state"] == "printed" and registration["lines"][0] == "[mcp_servers.drspec]"
        assert not (home / ".codex" / "config.toml").exists()
        assert any("[mcp_servers.drspec]" in note for note in data["mcp_server"])

    def test_codex_global_adds_a_table_and_keeps_what_was_there(self, project, home, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: str(project / "bin" / name))  # (`drspec` is on the PATH: the entry names it, not the interpreter)
        config = home / ".codex" / "config.toml"
        config.parent.mkdir()
        config.write_text('model = "o4"\n\n[mcp_servers.other]\ncommand = "x"\n')
        assert _init(project, "--ide", "codex", "--codex-global").exit_code == 0
        text = config.read_text()
        assert 'model = "o4"' in text and "[mcp_servers.other]" in text and "[mcp_servers.drspec]" in text and 'args = ["mcp-serve"]' in text
        assert _init(project, "--ide", "codex", "--codex-global").exit_code == 0
        assert config.read_text() == text  # again: nothing changes

    def test_codex_global_creates_the_file_when_there_is_none(self, project, home):
        assert _init(project, "--ide", "codex", "--codex-global").exit_code == 0
        assert (home / ".codex" / "config.toml").read_text().startswith("[mcp_servers.drspec]")

    def test_no_ide_and_no_choice_write_nothing(self, project):
        assert _init(project, "--no-ide").exit_code == 0
        assert not (project / ".mcp.json").exists()
        other = project.parent / "other"
        other.mkdir()
        assert _init(other).exit_code == 0  # non-interactive without --ide: no provider chosen
        assert not (other / ".mcp.json").exists()


class TestAnExistingFile:
    def test_other_servers_stay_and_drspec_is_added(self, project):
        (project / ".mcp.json").write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "note": 1}))
        assert _init(project, "--ide", "claude-code").exit_code == 0
        document = json.loads((project / ".mcp.json").read_text())
        assert document["mcpServers"]["other"] == {"command": "x"} and "drspec" in document["mcpServers"] and document["note"] == 1

    def test_an_existing_drspec_entry_is_left_alone_and_a_second_init_changes_nothing(self, project):
        custom = {"mcpServers": {"drspec": {"command": "/my/drspec", "args": ["mcp-serve", "--db", "x"]}}}
        (project / ".mcp.json").write_text(json.dumps(custom))
        result = _init(project, "--ide", "claude-code")
        assert json.loads((project / ".mcp.json").read_text()) == custom
        assert json.loads(result.output)["data"]["ide_integrations"]["claude-code"]["mcp_server"]["state"] == "present"
        other = project.parent / "again"
        other.mkdir()
        _init(other, "--ide", "claude-code")
        before = (other / ".mcp.json").read_bytes()
        _init(other, "--ide", "claude-code")
        assert (other / ".mcp.json").read_bytes() == before

    def test_a_file_that_is_not_json_is_not_touched(self, project):
        (project / ".vscode").mkdir()
        original = '{\n  // VS Code allows comments here\n  "servers": {}\n}\n'
        (project / ".vscode" / "mcp.json").write_text(original)
        result = _init(project, "--ide", "github-copilot")
        assert (project / ".vscode" / "mcp.json").read_text() == original
        registration = json.loads(result.output)["data"]["ide_integrations"]["github-copilot"]["mcp_server"]
        assert registration["state"] == "invalid" and "by hand" in registration["message"]

    def test_a_file_with_the_wrong_layout_is_not_touched(self, project):
        (project / ".mcp.json").write_text('{"mcpServers": []}')
        assert register("claude-code", project).state == "invalid"
        assert (project / ".mcp.json").read_text() == '{"mcpServers": []}'


class TestTheCommand:
    def test_drspec_on_the_path_is_used_as_it_is(self, monkeypatch, tmp_path):
        monkeypatch.setattr(shutil, "which", lambda name: str(tmp_path / "bin" / "drspec"))
        assert server_command() == ("drspec", ["mcp-serve"])

    def test_without_it_the_running_interpreter_is_named_in_full(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: None)
        assert server_command() == (sys.executable, ["-m", "drspec", "mcp-serve"])

    def test_a_frozen_executable_names_itself(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: None)
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        assert server_command() == (sys.executable, ["mcp-serve"])


class TestStatusSaysWhatIsMissing:
    def test_a_project_with_no_provider_says_nothing(self, project):
        assert _init(project, "--no-ide").exit_code == 0
        assert registration_status(project) == {}
        assert "mcp_server" not in json.loads(runner.invoke(app, ["--json", "status"]).output)["data"]

    def test_registered_then_removed(self, project):
        assert _init(project, "--ide", "claude-code", "--ide", "cursor").exit_code == 0
        status = json.loads(runner.invoke(app, ["--json", "status"]).output)["data"]["mcp_server"]
        assert sorted(status["registered"]) == ["claude-code", "cursor"] and not status["not_registered"] and "note" not in status
        (project / ".cursor" / "mcp.json").unlink()
        status = json.loads(runner.invoke(app, ["--json", "status"]).output)["data"]["mcp_server"]
        assert status["not_registered"] == ["cursor"] and "not registered for cursor" in status["note"]

    def test_an_entry_whose_command_cannot_be_found_is_reported(self, project):
        assert _init(project, "--ide", "claude-code").exit_code == 0
        (project / ".mcp.json").write_text(json.dumps({"mcpServers": {"drspec": {"command": str(project / "nowhere" / "drspec"), "args": ["mcp-serve"]}}}))
        status = registration_status(project)
        assert status["command_not_found"] == ["claude-code"] and "cannot be found" in status["note"]

    def test_codex_counts_when_its_user_file_has_the_table(self, project, home):
        assert _init(project, "--ide", "codex", "--codex-global").exit_code == 0
        assert registration_status(project)["registered"] == ["codex"]

    def test_worker_status_says_it_too(self, project):
        from drspec.staleness_worker import get_worker_status

        assert _init(project, "--ide", "cursor").exit_code == 0
        (project / ".cursor" / "mcp.json").unlink()
        status = get_worker_status(project / "_drspec" / "contracts.db")
        assert status["mcp_server"]["not_registered"] == ["cursor"]


class TestAnOutOfDateServerSaysSo:
    def test_the_first_result_after_a_reinstall_carries_one_notice(self, monkeypatch):
        monkeypatch.setattr(mcp_server, "_upgrade_notice_given", False)
        monkeypatch.setattr(mcp_server, "_started_build", "1.0+layout3.aaaaaaaaaa")
        monkeypatch.setattr("drspec.core.build_id.installed_build_id", lambda: "1.0+layout3.bbbbbbbbbb")
        first = mcp_server._with_upgrade_notice("result")
        assert first.startswith(mcp_server.UPGRADE_NOTICE) and first.endswith("result")
        assert mcp_server._with_upgrade_notice("next") == "next"  # said once

    def test_an_unchanged_install_adds_nothing(self, monkeypatch):
        from drspec.core.build_id import installed_build_id

        monkeypatch.setattr(mcp_server, "_upgrade_notice_given", False)
        monkeypatch.setattr(mcp_server, "_started_build", installed_build_id())
        assert mcp_server._with_upgrade_notice("result") == "result"


class TestTheUpgradeNoticeDoesNotBreakJson:
    """After an upgrade the server says once that it runs the old code. In text it is the first line of the result; in
    `format: "json"` it is a second content block, so that the first block is still exactly the JSON document."""

    @pytest.fixture(scope="class")
    def _server_template(self, tmp_path_factory):
        """A project with one file, initialised and scanned ONCE for this class; each test works on a copy (the tests read it through the
        server and change only the server's notice state, which is set per test)."""
        root = tmp_path_factory.mktemp("server") / "p"
        root.mkdir()
        with pytest.MonkeyPatch.context() as patch:
            patch.chdir(root)
            (root / "a.py").write_text("def alpha():\n    return 1\n")
            from typer.testing import CliRunner

            from drspec.cli.app import app

            assert CliRunner().invoke(app, ["init", "--no-ide", "--non-interactive"]).exit_code == 0
            assert CliRunner().invoke(app, ["--json", "scan", "."]).exit_code == 0
        return root

    @pytest.fixture
    def server(self, _server_template, tmp_path, monkeypatch):
        root = tmp_path / "p"
        shutil.copytree(_server_template, root)  # (a copy of the initialised and scanned project)
        monkeypatch.chdir(root)
        monkeypatch.setattr(mcp_server, "_upgrade_notice_given", False)
        monkeypatch.setattr(mcp_server, "_started_build", "1.0+layout3.aaaaaaaaaa")
        return root / "_drspec" / "contracts.db"

    @staticmethod
    def _call(db_path, fmt, name="search", arguments=None):
        arguments = {"pattern": "alpha", **(arguments or {}), **({"format": fmt} if fmt else {})}
        response = mcp_server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}, db_path
        )
        return response["result"]

    def test_the_first_json_result_after_an_upgrade_parses_and_the_notice_is_a_second_block(self, server, monkeypatch):
        import json

        monkeypatch.setattr("drspec.core.build_id.installed_build_id", lambda: "1.0+layout3.bbbbbbbbbb")  # the build changed after the server started
        first = self._call(server, "json")
        assert json.loads(first["content"][0]["text"])["pattern"] == "alpha"  # (the whole first block is the document)
        assert [block["type"] for block in first["content"]] == ["text", "text"] and first["content"][1]["text"] == mcp_server.UPGRADE_NOTICE
        second = self._call(server, "json")
        assert len(second["content"]) == 1 and json.loads(second["content"][0]["text"])["pattern"] == "alpha"  # said once

    def test_the_text_result_still_starts_with_the_notice_once(self, server, monkeypatch):
        monkeypatch.setattr("drspec.core.build_id.installed_build_id", lambda: "1.0+layout3.bbbbbbbbbb")
        first = self._call(server, None)
        assert len(first["content"]) == 1 and first["content"][0]["text"].startswith(mcp_server.UPGRADE_NOTICE + "\n\n")
        second = self._call(server, None)
        assert len(second["content"]) == 1 and mcp_server.UPGRADE_NOTICE not in second["content"][0]["text"]

    def test_once_is_once_whichever_format_comes_first(self, server, monkeypatch):
        monkeypatch.setattr("drspec.core.build_id.installed_build_id", lambda: "1.0+layout3.bbbbbbbbbb")
        self._call(server, "json")
        later = self._call(server, None)
        assert mcp_server.UPGRADE_NOTICE not in later["content"][0]["text"] and len(later["content"]) == 1

    def test_an_error_in_json_keeps_its_text_and_carries_the_notice_apart(self, server, monkeypatch):
        monkeypatch.setattr("drspec.core.build_id.installed_build_id", lambda: "1.0+layout3.bbbbbbbbbb")
        result = self._call(server, "json", name="source_get", arguments={"function_id": "a.py::missing"})
        assert result["isError"] is True and not result["content"][0]["text"].startswith("[drspec was upgraded")
        assert result["content"][1]["text"] == mcp_server.UPGRADE_NOTICE

    def test_an_unchanged_install_adds_no_block(self, server, monkeypatch):
        from drspec.core.build_id import installed_build_id

        monkeypatch.setattr(mcp_server, "_started_build", installed_build_id())
        assert len(self._call(server, "json")["content"]) == 1 and len(self._call(server, None)["content"]) == 1
