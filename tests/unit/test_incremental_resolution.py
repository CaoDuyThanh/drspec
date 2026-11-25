"""After a symbol change, only callers the change could affect are re-resolved.

The shortcut is only worth having if it never changes the answer, so every
scenario here runs twice from the same starting point: an ordinary sync, and a
forced one that re-resolves every function. The edge tables must be identical.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import duckdb
import pytest

from tests.helpers import publish as sync_scan_results
from drspec.core.scanner import Scanner
from drspec.db import init_schema
from tests.helpers import fid

CLASS_A = "class K:\n    def run(self):\n        return 1\n"


def _write(root: Path, files: Dict[str, Optional[str]]) -> None:
    for relative, content in files.items():
        path = root / relative
        if content is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def _sync(conn, root: Path, paths: List[str], deleted: List[str] = (), force: bool = False):
    scanner = Scanner()
    functions, imports, scanned = [], {}, set()
    for relative in paths:
        result = scanner.scan_file(str(root / relative), relative_to=root)
        assert result and result.parse_status == "successful", relative
        functions.extend(result.functions)
        imports.update(result.file_imports)
        scanned.add(relative)
    scanned.update(deleted)
    return sync_scan_results(conn, functions=functions, scanned_file_paths=scanned, file_imports=imports, force=force)


def _edges(conn):
    """The edge and artifact tables, read through the connection that wrote them."""
    return {
        "calls": sorted(conn.execute("SELECT caller_id, callee_id, edge_hash, confidence FROM dependencies").fetchall()),
        "references": sorted(conn.execute("SELECT caller_id, callee_id, edge_hash, confidence FROM code_references").fetchall()),
        "artifacts": sorted(conn.execute("SELECT function_id, code_hash, status, is_property FROM artifacts").fetchall()),
    }


SCENARIOS = {
    "a function the caller imports by name appears": (
        {"a.py": "def other():\n    return 0\n", "b.py": "from a import f\n\n\ndef use():\n    return f()\n"},
        {"a.py": "def other():\n    return 0\n\n\ndef f():\n    return 1\n"},
        ["a.py"],
    ),
    "a function the caller uses is removed": (
        {"a.py": "def f():\n    return 1\n\n\ndef keep():\n    return 2\n", "b.py": "from a import f\n\n\ndef use():\n    return f()\n"},
        {"a.py": "def keep():\n    return 2\n"},
        ["a.py"],
    ),
    "a function the caller uses is renamed": (
        {"a.py": "def f():\n    return 1\n", "b.py": "from a import f\n\n\ndef use():\n    return f()\n"},
        {"a.py": "def g():\n    return 1\n"},
        ["a.py"],
    ),
    "an aliased import starts to resolve": (
        {"a.py": "def other():\n    return 0\n", "b.py": "from a import f as g\n\n\ndef use():\n    return g()\n"},
        {"a.py": "def other():\n    return 0\n\n\ndef f():\n    return 1\n"},
        ["a.py"],
    ),
    "a module import starts to resolve": (
        {"a.py": "def other():\n    return 0\n", "b.py": "import a as m\n\n\ndef use():\n    return m.f()\n"},
        {"a.py": "def other():\n    return 0\n\n\ndef f():\n    return 1\n"},
        ["a.py"],
    ),
    "a method the caller needs is added to an annotated class": (
        {"a.py": CLASS_A, "b.py": "from a import K\n\n\ndef use(k: K):\n    return k.extra()\n"},
        {"a.py": CLASS_A + "\n    def extra(self):\n        return 2\n"},
        ["a.py"],
    ),
    "a constructor starts to resolve when the class gains __init__": (
        {"a.py": CLASS_A, "b.py": "from a import K\n\n\ndef use():\n    return K()\n"},
        {"a.py": CLASS_A + "\n    def __init__(self):\n        self.x = 1\n"},
        ["a.py"],
    ),
    "a second file with the same stem makes an import ambiguous": (
        {"one/foo.py": "def bar():\n    return 1\n", "b.py": "from foo import bar\n\n\ndef use():\n    return bar()\n"},
        {"two/foo.py": "def baz():\n    return 2\n"},
        ["two/foo.py"],
    ),
    "a method becomes a property": (
        {
            "a.py": "class K:\n    def v(self):\n        return 1\n",
            "b.py": "from a import K\n\n\ndef use(k: K):\n    return k.v\n",
        },
        {"a.py": "class K:\n    @property\n    def v(self):\n        return 1\n"},
        ["a.py"],
    ),
    "a file is deleted": (
        {"a.py": "def f():\n    return 1\n", "b.py": "from a import f\n\n\ndef use():\n    return f()\n", "c.py": "def lonely():\n    return 3\n"},
        {"a.py": None},
        [],
    ),
    "a new file whose function a caller already calls by bare name": (
        {"b.py": "def use():\n    return helper()\n"},
        {"util.py": "def helper():\n    return 1\n"},
        ["util.py"],
    ),
    "a change nobody depends on": (
        {"a.py": "def f():\n    return 1\n", "b.py": "from a import f\n\n\ndef use():\n    return f()\n"},
        {"a.py": "def f():\n    return 1\n\n\ndef unrelated_new():\n    return 9\n"},
        ["a.py"],
    ),
}


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_the_filtered_sync_leaves_exactly_the_edges_a_full_one_does(name, tmp_path, monkeypatch):
    initial, mutation, changed = SCENARIOS[name]
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    _write(root, initial)

    # Two databases in memory (the rules made of them are what is compared, not files), each synced from the same starting point.
    connections = {}
    for label in ("filtered", "full"):
        conn = duckdb.connect(":memory:")
        init_schema(conn)
        _sync(conn, root, sorted(initial))
        connections[label] = conn
    _write(root, mutation)
    deleted = [path for path, content in mutation.items() if content is None]

    outcomes = {}
    for label, force in (("filtered", False), ("full", True)):
        outcomes[label] = _sync(connections[label], root, changed, deleted, force=force)
    try:
        assert _edges(connections["filtered"]) == _edges(connections["full"])
    finally:
        for conn in connections.values():
            conn.close()
    assert len(outcomes["filtered"].resolution_reasons) <= len(outcomes["full"].resolution_reasons)


def test_an_unrelated_change_does_not_re_resolve_the_rest_of_the_project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    initial = {f"m{i}.py": f"def fn_{i}():\n    return {i}\n" for i in range(30)}
    initial["a.py"] = "def f():\n    return 1\n"
    _write(root, initial)
    conn = duckdb.connect(":memory:")
    init_schema(conn)
    _sync(conn, root, sorted(initial))

    _write(root, {"a.py": "def f():\n    return 1\n\n\ndef brand_new():\n    return 2\n"})
    stats = _sync(conn, root, ["a.py"])
    conn.close()
    assert fid("a.py", "brand_new") in stats.resolution_reasons
    assert not any(function_id.startswith("m") for function_id in stats.resolution_reasons), "unrelated files were re-resolved"


def test_a_caller_that_mentions_a_changed_name_is_re_resolved(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    initial = {"a.py": "def other():\n    return 0\n", "b.py": "from a import f\n\n\ndef use():\n    return f()\n", "c.py": "def far():\n    return 5\n"}
    _write(root, initial)
    conn = duckdb.connect(":memory:")
    init_schema(conn)
    _sync(conn, root, sorted(initial))
    _write(root, {"a.py": "def other():\n    return 0\n\n\ndef f():\n    return 1\n"})
    stats = _sync(conn, root, ["a.py"])
    assert stats.resolution_reasons.get(fid("b.py", "use")) == "symbols_changed"
    assert fid("c.py", "far") not in stats.resolution_reasons
    assert conn.execute("SELECT callee_id FROM dependencies WHERE caller_id = ?", [fid("b.py", "use")]).fetchall() == [(fid("a.py", "f"),)]
    conn.close()
