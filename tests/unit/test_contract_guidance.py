"""The guidance that tells a user's assistant when to run the contract workflow in a sub-agent, and how much per run: written into the
skill that `init --ide claude-code` installs and into the MCP server's instructions, from the numbers of one module."""

from __future__ import annotations

import re

import pytest

from drspec.cli.commands.init import setup_ide_integrations
from drspec.core import contract_guidance
from drspec.mcp_server import handle_message


def _numbers(text: str) -> dict:
    """What the text says about the cap: functions per run, after the user says yes, per sub-agent."""
    return {
        "run": int(re.search(r"at most (\d+) functions per run", text, re.IGNORECASE).group(1)),
        "consent": int(re.search(r"at most (\d+) after\s+the user says\s+yes", text, re.IGNORECASE).group(1)),
        "group": int(re.search(r"a group of at most\s+(\d+)", text, re.IGNORECASE).group(1)),
    }


class TestTheText:
    def test_it_says_when_when_not_how_and_how_much(self):
        text = contract_guidance.guidance_section()
        assert "PENDING or STALE" in text and "callers" in text and "why or how a function behaves" in text and "scan result lists changed functions" in text
        assert all(word in text for word in ("VERIFIED", "tests", "one-line wrappers", "generated files", "not about those functions"))
        assert "proposer, then the critic" in text and "contract id" in text and "not the transcript" in text and "whole function bodies" in text
        assert "how many PENDING and STALE functions remain" in text

    def test_it_is_a_short_section(self):
        assert len(contract_guidance.guidance_section().strip().splitlines()) <= 40

    def test_the_numbers_of_the_text_are_the_constants(self):
        expected = {"run": contract_guidance.MAX_FUNCTIONS_PER_RUN, "consent": contract_guidance.MAX_FUNCTIONS_WITH_CONSENT, "group": contract_guidance.MAX_FUNCTIONS_PER_SUB_AGENT}
        assert (expected["run"], expected["consent"]) == (5, 20)
        assert _numbers(contract_guidance.guidance_section()) == expected
        assert _numbers(contract_guidance.mcp_instructions()) == expected

    def test_a_changed_constant_changes_every_text(self, monkeypatch):
        monkeypatch.setattr(contract_guidance, "MAX_FUNCTIONS_PER_RUN", 7)
        monkeypatch.setattr(contract_guidance, "MAX_FUNCTIONS_WITH_CONSENT", 31)
        for text in (contract_guidance.guidance_section(), contract_guidance.mcp_instructions(), contract_guidance.skill_file()):
            flat = text.replace("\n", " ").lower()
            assert "at most 7 functions per run" in flat and "at most 31 after" in flat


@pytest.mark.usefixtures("fixed_launcher")  # `init` looks the `drspec` launcher up on the PATH
class TestWhatInitInstalls:
    def test_init_for_claude_code_writes_the_skill_with_the_section(self, tmp_path):
        result = setup_ide_integrations(tmp_path, ["claude-code"])
        path = tmp_path / ".claude" / "skills" / "drspec-contracts" / "SKILL.md"
        assert result["claude-code"]["skill"] == ".claude/skills/drspec-contracts/SKILL.md"
        content = path.read_text()
        assert content.startswith("---\nname: drspec-contracts\n") and content == contract_guidance.skill_file()
        assert content.count(contract_guidance.BEGIN_MARK) == 1 and "PENDING or STALE" in content

    def test_a_second_init_updates_the_section_and_does_not_repeat_it(self, tmp_path):
        setup_ide_integrations(tmp_path, ["claude-code"])
        path = tmp_path / ".claude" / "skills" / "drspec-contracts" / "SKILL.md"
        path.write_text("---\nname: drspec-contracts\n---\n\nan older text of the guidance\n")  # (what an earlier release wrote)
        setup_ide_integrations(tmp_path, ["claude-code"])
        setup_ide_integrations(tmp_path, ["claude-code"])
        content = path.read_text()
        assert content == contract_guidance.skill_file() and "an older text" not in content
        assert content.count(contract_guidance.BEGIN_MARK) == 1 and content.count("## Contracts: when to run the workflow") == 1

    def test_the_launchers_are_still_written_and_other_ides_get_no_skill(self, tmp_path):
        result = setup_ide_integrations(tmp_path, ["claude-code", "cursor"])
        assert result["claude-code"]["files_created"] == 6 and "skill" not in result["cursor"]
        assert not (tmp_path / ".cursor" / "skills").exists()


class TestTheMcpServerSaysItToo:
    def test_initialize_carries_the_instructions(self):
        response = handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}, None)
        assert response["result"]["instructions"] == contract_guidance.mcp_instructions()
        assert response["result"]["capabilities"] == {"tools": {}} and response["result"]["serverInfo"]["name"] == "drspec"
