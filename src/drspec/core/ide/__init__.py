"""IDE integration module for DrSpec launcher generation."""

from __future__ import annotations

from typing import Dict, Type

from drspec.core.ide.base import BaseIdeSetup, AGENT_METADATA
from drspec.core.ide.cursor import CursorSetup
from drspec.core.ide.claude_code import ClaudeCodeSetup
from drspec.core.ide.github_copilot import GitHubCopilotSetup
from drspec.core.ide.codex import CodexSetup

# Registry of available IDE setups
IDE_REGISTRY: Dict[str, Type[BaseIdeSetup]] = {
    "cursor": CursorSetup,
    "claude-code": ClaudeCodeSetup,
    "github-copilot": GitHubCopilotSetup,
    "codex": CodexSetup,
}

__all__ = [
    "BaseIdeSetup",
    "AGENT_METADATA",
    "CursorSetup",
    "ClaudeCodeSetup",
    "GitHubCopilotSetup",
    "CodexSetup",
    "IDE_REGISTRY",
]
