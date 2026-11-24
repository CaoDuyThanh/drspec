"""CLI command modules for DrSpec.

A module is imported when it is first used (`from drspec.cli.commands import scan`, or its command running), not when the
package is: the CLI's start-up does not need them all (see `drspec.cli.app`)."""

from __future__ import annotations

import importlib

__all__ = [
    "config",
    "contract",
    "deps",
    "refs",
    "init",
    "learn",
    "mcp_serve",
    "scan",
    "search",
    "serve",
    "source",
    "status",
    "ack",
    "trace",
    "verify",
    "vision",
    "worker",
]


def __getattr__(name: str):
    if name in __all__:
        return importlib.import_module(f"drspec.cli.commands.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
