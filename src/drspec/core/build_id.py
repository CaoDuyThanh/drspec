"""Which build of drspec a process is running.

A background worker outlives the command that started it. After the package is
reinstalled, the worker keeps running the code it loaded, while every new command
runs the new code against the same database. The build identifier is what lets
each side notice that: the worker reports its own, a client compares it with
its own.

It is the package version, the database layout number, and a fingerprint of the
package's source files (names, sizes and modification times), so an editable
install or a rebuilt wheel with an unchanged version number is still told apart
from the code it replaced.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Optional

from drspec import __version__

#: Bumped whenever the layout of the database changes in a way older code cannot
#: read (2: embeddings are variable-length lists; 3: function identifiers carry a hash of
#: the parameters).
LAYOUT_NUMBER = 3

_cached: Optional[str] = None


def _fingerprint() -> str:
    if getattr(sys, "frozen", False):
        return "frozen"  # a bundled executable has no source files; the version is all there is
    package = Path(__file__).resolve().parent.parent
    digest = hashlib.sha1()
    for path in sorted(list(package.rglob("*.py")) + list(package.rglob("*.sql"))):
        try:
            info = path.stat()
        except OSError:
            continue
        digest.update(f"{path.relative_to(package)}:{info.st_size}:{info.st_mtime_ns}\n".encode())
    return digest.hexdigest()[:10]


#: A fixed identifier for tests, which start workers and compare builds: the fingerprint of the source tree changes
#: whenever a file does, so a test that depends on two processes agreeing on it fails while the tree is being edited.
BUILD_OVERRIDE_ENV = "DRSPEC_BUILD_ID"


def build_id() -> str:
    """The identifier of the code this process loaded (computed once, at first use)."""
    global _cached
    if _cached is None:
        _cached = os.environ.get(BUILD_OVERRIDE_ENV) or f"{__version__}+layout{LAYOUT_NUMBER}.{_fingerprint()}"
    return _cached


def installed_build_id() -> str:
    """The identifier of the code on disk now, which differs from `build_id()` once the package was reinstalled or
    edited after this process started (a long-running MCP server keeps the code it started with)."""
    return os.environ.get(BUILD_OVERRIDE_ENV) or f"{__version__}+layout{LAYOUT_NUMBER}.{_fingerprint()}"


def reset_for_tests() -> None:
    global _cached
    _cached = None
