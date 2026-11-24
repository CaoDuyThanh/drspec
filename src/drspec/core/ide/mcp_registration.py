"""Telling an AI client about drspec's MCP server.

The native tools (`mcp__drspec__search` and the others) come from `drspec mcp-serve`, which a client starts only when its
configuration names it. `drspec init` writes that entry for each provider it sets up:

| Provider | File | Entry |
|---|---|---|
| `claude-code` | `.mcp.json` | `mcpServers.drspec`, type `stdio` |
| `cursor` | `.cursor/mcp.json` | `mcpServers.drspec` |
| `github-copilot` | `.vscode/mcp.json` | `servers.drspec`, type `stdio` |
| `codex` | `~/.codex/config.toml` | table `[mcp_servers.drspec]` (user-wide, so only with `--codex-global`; otherwise the lines are printed) |

A file that exists is merged, never replaced: other servers stay, an existing `drspec` entry is left as it is, and a file
that is not valid JSON is not touched.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SERVER_NAME = "drspec"

#: Providers with a project file, as `(file, top-level key, whether the entry carries "type": "stdio")`.
JSON_PROVIDERS: Dict[str, Tuple[str, str, bool]] = {
    "claude-code": (".mcp.json", "mcpServers", True),
    "cursor": (".cursor/mcp.json", "mcpServers", False),
    "github-copilot": (".vscode/mcp.json", "servers", True),
}

CLIENT_NAMES = {"claude-code": "Claude Code", "cursor": "Cursor", "github-copilot": "VS Code", "codex": "Codex"}

_CODEX_TABLE = re.compile(r"^\s*\[mcp_servers\.(?:drspec|\"drspec\")\]\s*$", re.MULTILINE)


def codex_config_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


def server_command() -> Tuple[str, List[str]]:
    """How a client starts the server: `drspec mcp-serve` when `drspec` is on the path, otherwise the absolute path of
    the interpreter or executable that is running now (so a virtual environment that is not on the path works)."""
    if shutil.which("drspec"):
        return "drspec", ["mcp-serve"]
    if getattr(sys, "frozen", False):
        return sys.executable, ["mcp-serve"]
    return sys.executable, ["-m", "drspec", "mcp-serve"]


def _entry(typed: bool) -> Dict[str, Any]:
    command, args = server_command()
    entry: Dict[str, Any] = {"type": "stdio"} if typed else {}
    entry.update({"command": command, "args": args})
    return entry


def codex_lines() -> List[str]:
    command, args = server_command()
    return [f"[mcp_servers.{SERVER_NAME}]", f"command = {json.dumps(command)}", f"args = {json.dumps(args)}"]


@dataclass
class Registration:
    provider: str
    state: str
    """`written` (the file was created or the entry added), `present` (an entry was already there and was left alone),
    `invalid` (the file is not valid JSON and was left alone), `printed` (a user-wide file, so the lines are shown, not
    written) or `error`."""
    path: str = ""
    message: str = ""
    lines: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {"state": self.state, "path": self.path, "message": self.message}
        if self.lines:
            data["lines"] = self.lines
        return data


def _register_json(provider: str, project_root: Path) -> Registration:
    relative, key, typed = JSON_PROVIDERS[provider]
    path = project_root / relative
    entry = _entry(typed)
    document: Dict[str, Any] = {}
    existed = path.exists()
    if existed:
        try:
            document = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (ValueError, OSError):
            return Registration(provider, "invalid", str(path), f"{relative} is not valid JSON, so it was left as it is. Add the drspec server to it by hand (see the README).")
        if not isinstance(document, dict) or not isinstance(document.get(key, {}), dict):
            return Registration(provider, "invalid", str(path), f"{relative} does not have the expected layout, so it was left as it is. Add the drspec server to it by hand (see the README).")
    servers = document.setdefault(key, {})
    if SERVER_NAME in servers:
        return Registration(provider, "present", str(path), f"{relative} already has a drspec entry; it was left as it is.")
    servers[SERVER_NAME] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return Registration(provider, "written", str(path), f"drspec MCP server added to {relative}. Restart {CLIENT_NAMES[provider]} and approve the drspec server to get the tools.")


def _register_codex(global_install: bool) -> Registration:
    path = codex_config_path()
    lines = codex_lines()
    if not global_install:
        return Registration(
            "codex", "printed", str(path),
            f"Codex reads MCP servers from {path}, which is outside the project; add these lines to it, or run init with --codex-global. Then restart Codex.",
            lines,
        )
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if _CODEX_TABLE.search(existing):
        return Registration("codex", "present", str(path), f"{path} already has a drspec entry; it was left as it is.")
    path.parent.mkdir(parents=True, exist_ok=True)
    separator = "" if not existing or existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    path.write_text(existing + separator + "\n".join(lines) + "\n", encoding="utf-8")
    return Registration("codex", "written", str(path), f"drspec MCP server added to {path}. Restart Codex and approve the drspec server to get the tools.")


def register(provider: str, project_root: Path, codex_global: bool = False) -> Registration:
    """Write the drspec server entry for one provider (see the module docstring for the rules)."""
    try:
        if provider == "codex":
            return _register_codex(codex_global)
        if provider in JSON_PROVIDERS:
            return _register_json(provider, project_root)
    except OSError as error:
        return Registration(provider, "error", "", f"Could not write the MCP server entry: {error}")
    return Registration(provider, "error", "", f"No MCP configuration is known for {provider}.")


# ---------------------------------------------------------------------------
# Is it registered?
# ---------------------------------------------------------------------------


def _configured_providers(project_root: Path) -> List[str]:
    """The providers `init` set up in this project: those whose launcher folder holds the launcher of one of drspec's
    agents (Codex's may be the user-wide folder)."""
    from drspec.core.ide import IDE_REGISTRY
    from drspec.core.ide.base import AGENT_METADATA
    from drspec.core.ide.codex import CodexSetup

    found = []
    for name in ("claude-code", "cursor", "github-copilot", "codex"):
        setups = [CodexSetup(global_install=False), CodexSetup(global_install=True)] if name == "codex" else [IDE_REGISTRY[name]()]
        for setup in setups:
            folder = setup.get_output_dir(project_root)
            if any((folder / setup.get_filename(agent)).exists() for agent in AGENT_METADATA):
                found.append(name)
                break
    return found


def _entry_for(provider: str, project_root: Path) -> Optional[Dict[str, Any]]:
    """The stored drspec entry of a provider, or `None`. For Codex only `{"command": ...}` is read from the text."""
    if provider == "codex":
        path = codex_config_path()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        match = _CODEX_TABLE.search(text)
        if not match:
            return None
        command = re.search(r'^\s*command\s*=\s*"((?:[^"\\]|\\.)*)"', text[match.end():], re.MULTILINE)
        return {"command": json.loads(f'"{command.group(1)}"') if command else ""}
    relative, key, _typed = JSON_PROVIDERS[provider]
    try:
        document = json.loads((project_root / relative).read_text(encoding="utf-8"))
        entry = document.get(key, {}).get(SERVER_NAME)
    except (OSError, ValueError, AttributeError):
        return None
    return entry if isinstance(entry, dict) else None


def _command_exists(command: str) -> bool:
    return bool(command) and (shutil.which(command) is not None or Path(command).exists())


def registration_status(project_root: Path) -> Dict[str, Any]:
    """For the providers set up in this project: which have the drspec server registered, which do not, and which point
    at a command that cannot be found. Empty when no provider was set up."""
    registered: List[str] = []
    missing: List[str] = []
    broken: List[str] = []
    for provider in _configured_providers(project_root):
        entry = _entry_for(provider, project_root)
        if entry is None:
            missing.append(provider)
        elif not _command_exists(str(entry.get("command", ""))):
            broken.append(provider)
        else:
            registered.append(provider)
    if not (registered or missing or broken):
        return {}
    status: Dict[str, Any] = {"registered": registered, "not_registered": missing, "command_not_found": broken}
    problems = []
    if missing:
        problems.append(f"not registered for {', '.join(missing)} (run 'drspec init --ide <provider>'; codex also needs --codex-global)")
    if broken:
        problems.append(f"registered for {', '.join(broken)} but its command cannot be found")
    if problems:
        status["note"] = "drspec MCP server: " + "; ".join(problems) + "."
    return status
