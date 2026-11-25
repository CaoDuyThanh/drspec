"""Rust names found through modules that glob each other (a crate's prelude re-exporting the modules that use the
prelude): every lookup in the cycle gives the same answer however the cycle is entered, the answer is the same on
every scan, and a name that is nowhere is still unresolved."""

from __future__ import annotations

import json

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.db import init_schema

runner = CliRunner()

FILES = {
    "Cargo.toml": '[package]\nname = "cyc"\nversion = "0.1.0"\n',
    "src/lib.rs": (
        "mod a;\nmod b;\nmod c;\nmod d;\n"
        "pub mod prelude { pub use crate::a::*; pub use crate::b::*; }\n"
        "use prelude::*;\n"
        "pub fn top() { fa(); fb(); fc(); }\n"
        "pub fn top_two() { fd(); fa(); }\n"
        "pub fn lost() { nowhere(); }\n"
    ),
    "src/a.rs": "pub use crate::b::*;\npub fn fa() { fb(); }\npub fn needs_ghost() { ghost(); }\n",
    "src/b.rs": "pub use crate::a::*;\npub fn fb() { fa(); }\npub fn fc() {}\n",
    "src/c.rs": "pub use crate::d::*;\npub use crate::a::*;\npub fn from_c() { fd(); fb(); }\n",
    "src/d.rs": "pub use crate::c::*;\npub fn fd() { from_c(); fa(); }\n",
}


def _write_crate(root, names):
    """The crate's files, in the order given."""
    for name in names:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(FILES[name])


@pytest.fixture
def project(kept_project):
    """The crate, not yet scanned, in a project folder with an empty in-memory project database (`kept_project`)."""
    _write_crate(kept_project, FILES)
    return kept_project


def _edges(project):
    connection = duckdb.connect(str(project / "_drspec" / "contracts.db"), read_only=True)
    try:
        return sorted((a.split("#")[0], b.split("#")[0], c) for a, b, c in connection.execute("SELECT caller_id, callee_id, confidence FROM dependencies").fetchall())
    finally:
        connection.close()


def _scan(*args):
    result = runner.invoke(app, ["--json", "scan", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["data"]


EXPECTED = sorted([
    ("src/lib.rs::top", "src/a.rs::fa", "EXTRACTED"),
    ("src/lib.rs::top", "src/b.rs::fb", "EXTRACTED"),
    ("src/lib.rs::top", "src/b.rs::fc", "EXTRACTED"),
    ("src/lib.rs::top_two", "src/a.rs::fa", "EXTRACTED"),
    ("src/a.rs::fa", "src/b.rs::fb", "EXTRACTED"),
    ("src/b.rs::fb", "src/a.rs::fa", "EXTRACTED"),
    ("src/c.rs::from_c", "src/d.rs::fd", "EXTRACTED"),
    ("src/c.rs::from_c", "src/b.rs::fb", "EXTRACTED"),
    ("src/d.rs::fd", "src/c.rs::from_c", "EXTRACTED"),
    ("src/d.rs::fd", "src/a.rs::fa", "EXTRACTED"),
])


class TestGlobCycles:
    def test_a_name_is_found_through_modules_that_glob_each_other(self, project):
        _scan(".")
        # (`top_two` does not reach `fd`: the prelude re-exports a and b, not d)
        assert _edges(project) == EXPECTED

    def test_a_name_nothing_defines_stays_unresolved(self, project):
        data = _scan(".")
        edges = _edges(project)
        assert not [e for e in edges if e[0] in ("src/lib.rs::lost", "src/a.rs::needs_ghost")]
        assert data["state"] == "done"

    def test_a_second_scan_gives_the_same_edges(self, project):
        _scan(".")
        first = _edges(project)
        _scan(".", "--full")
        assert _edges(project) == first

    def test_the_answer_does_not_depend_on_which_file_is_read_first(self, project, tmp_path_factory, monkeypatch):
        _scan(".")
        first = _edges(project)
        other = tmp_path_factory.mktemp("order")  # a second project: its own in-memory database (the fake keeps one per path)
        monkeypatch.chdir(other)
        (other / "_drspec").mkdir()
        connection = duckdb.connect(str(other / "_drspec" / "contracts.db"))
        init_schema(connection)
        connection.close()
        _write_crate(other, reversed(list(FILES)))
        _scan("src/d.rs", "src/c.rs", "src/b.rs", "src/a.rs", "src/lib.rs", "Cargo.toml")
        assert _edges(other) == first
