"""MCP stdio server command — exposes drspec's `search` and `deps_get` as
MCP tools, so an agent reaches for the code graph the same way it
reaches for Bash/Read/Grep. See `drspec.mcp_server` for the protocol
implementation.

Launched by an MCP client (e.g. Claude Code's `.mcp.json`), not run
interactively — it blocks, speaking JSON-RPC over stdin/stdout, until
the client closes stdin.
"""

from __future__ import annotations

from pathlib import Path

import typer

from drspec import mcp_server


def mcp_serve_command(ctx: typer.Context) -> None:
    """Start the MCP stdio server.

    Launched by an MCP client as a subprocess (see the repo's
    `.mcp.json`); blocks until the client closes stdin.
    """
    cli_ctx = ctx.obj or {}
    db_path_override = cli_ctx.get("db_path")
    db_path = db_path_override or (Path.cwd() / "_drspec" / "contracts.db")
    mcp_server.run(db_path)
