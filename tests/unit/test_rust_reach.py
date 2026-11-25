"""A change in Rust code re-resolves the functions it can reach, and the result is the one a fresh index of the same tree gives.

Each scenario is a small crate and a list of edits. After every edit the files that changed are published on top of the
earlier index (what a scan does), a second index is built from the whole tree, and the two are compared: functions, call
and reference edges and every config row. The rule is `drspec.core.rust_reach`: tokens (names) a change touches, grown through
`pub use` and traits; a function none of whose calls holds a token keeps its stored edges.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest

from drspec.core import graph_sync, rust_reach
from drspec.core.scanner import Scanner
from drspec.db import init_schema
from tests.helpers import publish

LIB = "src/lib.rs"

# name -> (the crate, [(what the edit is, {file: new text, or None to delete it}, edges that must be there after it)])
SCENARIOS = {
    "the five steps of the reviewer's check": (
        {"src/main.rs": "mod later;\nmod util;\nfn main() {\n    later::go();\n    util::other();\n    util::added();\n}\n", "src/util.rs": "pub fn other() {\n}\n"},
        [
            ("add src/later.rs", {"src/later.rs": "pub fn go() {\n}\n"}, [("main", "go")]),
            ("add util::added", {"src/util.rs": "pub fn other() {\n}\npub fn added() {\n}\n"}, [("main", "go"), ("main", "added")]),
            ("body only in util::other", {"src/util.rs": "pub fn other() {\n    added();\n}\npub fn added() {\n}\n"}, [("main", "added"), ("other", "added")]),
            ("delete src/later.rs", {"src/later.rs": None}, [("main", "added"), ("other", "added")]),
            ("re-add later.rs with go renamed", {"src/later.rs": "pub fn go2() {\n}\n"}, [("main", "added")]),
        ],
    ),
    "a function added that a call in another file names": (
        {LIB: "mod util;\nmod app;\n", "src/util.rs": "pub fn other() {\n}\n", "src/app.rs": "use crate::util::added;\n\npub fn run() {\n    added();\n    crate::util::later();\n}\n"},
        [
            ("added is defined", {"src/util.rs": "pub fn other() {\n}\npub fn added() {\n}\n"}, [("run", "added")]),
            ("later is defined", {"src/util.rs": "pub fn other() {\n}\npub fn added() {\n}\npub fn later() {\n}\n"}, [("run", "added"), ("run", "later")]),
            ("added goes away", {"src/util.rs": "pub fn other() {\n}\npub fn later() {\n}\n"}, [("run", "later")]),
        ],
    ),
    "a pub use re-export is added, renamed and removed": (
        {LIB: "mod a;\nmod b;\npub use a::f2 as g2;\n", "src/a.rs": "pub fn f() {\n}\n", "src/b.rs": "pub fn use_it() {\n    crate::g();\n    crate::g2();\n}\n"},
        [
            ("f2 is defined behind an existing alias", {"src/a.rs": "pub fn f() {\n}\npub fn f2() {\n}\n"}, [("use_it", "f2")]),
            ("the alias g is added", {LIB: "mod a;\nmod b;\npub use a::f2 as g2;\npub use a::f as g;\n"}, [("use_it", "f"), ("use_it", "f2")]),
            ("the alias g is renamed", {LIB: "mod a;\nmod b;\npub use a::f2 as g2;\npub use a::f as h;\n"}, [("use_it", "f2")]),
            ("the call follows the new name", {"src/b.rs": "pub fn use_it() {\n    crate::h();\n    crate::g2();\n}\n"}, [("use_it", "f"), ("use_it", "f2")]),
            ("the alias is removed", {LIB: "mod a;\nmod b;\npub use a::f2 as g2;\n"}, [("use_it", "f2")]),
        ],
    ),
    "a re-export of a re-export": (
        {LIB: "mod a;\nmod mid;\nmod b;\n", "src/a.rs": "pub fn f() {\n}\n", "src/mid.rs": "pub use crate::a::f as g;\n", "src/b.rs": "pub fn use_it() {\n    crate::mid::g();\n}\n"},
        [
            ("f is renamed in a", {"src/a.rs": "pub fn f2() {\n}\n"}, []),
            ("f is back", {"src/a.rs": "pub fn f() {\n}\n"}, [("use_it", "f")]),
        ],
    ),
    "a mod declaration brings an existing file into the tree": (
        {LIB: "mod app;\nfn helper() {\n}\n", "src/app.rs": "pub fn run() {\n    crate::x::go();\n}\n", "src/x.rs": "pub fn go() {\n    super::helper();\n}\n"},
        [
            ("mod x is declared", {LIB: "mod app;\nmod x;\nfn helper() {\n}\n"}, [("run", "go"), ("go", "helper")]),
            ("mod x is dropped again", {LIB: "mod app;\nfn helper() {\n}\n"}, []),
        ],
    ),
    "a trait implemented in a new file": (
        {
            LIB: "mod shape;\nmod app;\nmod circle;\n",
            "src/shape.rs": "pub trait Shape {\n    fn area(&self) -> i32;\n    fn name(&self) -> i32 {\n        0\n    }\n}\n",
            "src/app.rs": "use crate::shape::Shape;\n\npub fn total(s: &dyn Shape) -> i32 {\n    s.area() + s.name()\n}\n",
        },
        [
            ("circle.rs implements Shape", {"src/circle.rs": "use crate::shape::Shape;\n\npub struct Circle;\n\nimpl Shape for Circle {\n    fn area(&self) -> i32 {\n        1\n    }\n}\n"}, [("total", "area"), ("total", "name")]),
            ("it overrides name too", {"src/circle.rs": "use crate::shape::Shape;\n\npub struct Circle;\n\nimpl Shape for Circle {\n    fn area(&self) -> i32 {\n        1\n    }\n    fn name(&self) -> i32 {\n        2\n    }\n}\n"}, [("total", "area"), ("total", "name")]),
            ("circle.rs is deleted", {"src/circle.rs": None}, [("total", "name")]),
        ],
    ),
    "a glob import": (
        {
            LIB: "mod util;\nmod app;\nmod extra;\n",
            "src/util.rs": "pub fn one() {\n}\n",
            "src/extra.rs": "pub fn three() {\n}\n",
            "src/app.rs": "use crate::util::*;\n\npub fn run() {\n    one();\n    two();\n    three();\n}\n",
        },
        [
            ("two is defined behind the glob", {"src/util.rs": "pub fn one() {\n}\npub fn two() {\n}\n"}, [("run", "one"), ("run", "two")]),
            ("util re-exports a glob", {"src/util.rs": "pub use crate::extra::*;\npub fn one() {\n}\npub fn two() {\n}\n"}, [("run", "one"), ("run", "two"), ("run", "three")]),
            ("the glob is private again", {"src/util.rs": "use crate::extra::*;\npub fn one() {\n}\npub fn two() {\n}\n"}, [("run", "one"), ("run", "two")]),
        ],
    ),
    "a macro_rules! change": (
        {LIB: "mod mac;\nmod mac2;\nmod app;\n", "src/mac.rs": "macro_rules! twice {\n    ($x:expr) => { $x * 2 };\n}\n", "src/app.rs": "pub fn run() -> i32 {\n    twice!(1)\n}\n"},
        [
            ("the macro is removed", {"src/mac.rs": "pub fn nothing() {\n}\n"}, []),
            ("another file defines it", {"src/mac2.rs": "macro_rules! twice {\n    ($x:expr) => { $x + $x };\n}\n"}, [("run", "twice!")]),
            ("the first file defines it again", {"src/mac.rs": "macro_rules! twice {\n    ($x:expr) => { $x * 2 };\n}\n"}, [("run", "twice!")]),
        ],
    ),
    "a method added to an impl": (
        {LIB: "mod t;\nmod app;\n", "src/t.rs": "pub struct W;\n\nimpl W {\n    pub fn new() -> W {\n        W\n    }\n}\n", "src/app.rs": "use crate::t::W;\n\npub fn run() {\n    let w = W::new();\n    w.go();\n}\n"},
        [
            ("go is added to the impl", {"src/t.rs": "pub struct W;\n\nimpl W {\n    pub fn new() -> W {\n        W\n    }\n    pub fn go(&self) {\n    }\n}\n"}, [("run", "new"), ("run", "go")]),
            ("a second impl block holds go", {"src/t.rs": "pub struct W;\n\nimpl W {\n    pub fn new() -> W {\n        W\n    }\n}\n\nimpl W {\n    pub fn go(&self) {\n    }\n}\n"}, [("run", "new"), ("run", "go")]),
            ("go is removed", {"src/t.rs": "pub struct W;\n\nimpl W {\n    pub fn new() -> W {\n        W\n    }\n}\n"}, [("run", "new")]),
        ],
    ),
    "a file is deleted and added again": (
        {LIB: "mod util;\nmod app;\n", "src/util.rs": "pub fn helper() {\n}\n", "src/app.rs": "use crate::util::helper;\n\npub fn run() {\n    helper();\n}\n"},
        [
            ("util.rs is deleted", {"src/util.rs": None}, []),
            ("util.rs is back", {"src/util.rs": "pub fn helper() {\n}\n"}, [("run", "helper")]),
            ("util.rs is back with another function", {"src/util.rs": "pub fn helper2() {\n}\n"}, []),
        ],
    ),
}


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write(root: Path, files: dict) -> None:
    for name, text in files.items():
        path = root / name
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _publish(connection, root: Path, names, deleted=()):
    scanner = Scanner()
    functions, imports = [], {}
    for name in sorted(names):
        if name in deleted:
            continue
        result = scanner.scan_file(name, relative_to=root)
        functions += result.functions
        imports.update(result.file_imports)
    return publish(connection, functions=functions, scanned_file_paths=set(names), file_imports=imports, deleted_paths=set(deleted))


def _new_connection():
    connection = duckdb.connect(":memory:")
    init_schema(connection)
    return connection


def _fresh(root: Path):
    connection = _new_connection()
    _publish(connection, root, sorted(path.relative_to(root).as_posix() for path in root.rglob("*.rs")))
    return connection


_VOLATILE = ("scan:run", "worker", "time", "_at", "scan:request", "parser:requeued")


def _state(connection) -> dict:
    return {
        "functions": connection.execute("SELECT function_id, file_path, function_name, signature, code_hash, start_line, end_line, parent, status FROM artifacts ORDER BY 1").fetchall(),
        "calls": connection.execute("SELECT caller_id, callee_id, call_site, edge_hash, confidence FROM dependencies ORDER BY 1, 2, 3").fetchall(),
        "references": connection.execute("SELECT caller_id, callee_id, reference_site, edge_hash, confidence FROM code_references ORDER BY 1, 2, 3").fetchall(),
        "config": [row for row in connection.execute("SELECT key, value FROM config ORDER BY 1").fetchall() if not any(word in row[0] for word in _VOLATILE)],
    }


def _short(function_id: str) -> str:
    return function_id.split("#")[0].split("::", 1)[-1].rsplit("::", 1)[-1]


def _edges(connection) -> set:
    return {(_short(caller), _short(callee)) for caller, callee in connection.execute("SELECT caller_id, callee_id FROM dependencies").fetchall()}


def _differences(incremental: dict, fresh: dict) -> list:
    found = []
    for kind, rows in incremental.items():
        if rows != fresh[kind]:
            only_incremental = [row for row in rows if row not in fresh[kind]]
            only_fresh = [row for row in fresh[kind] if row not in rows]
            found.append(f"{kind}: only in the incremental index {[str(row)[:120] for row in only_incremental[:2]]}, only in the fresh one {[str(row)[:120] for row in only_fresh[:2]]}")
    return found


def _each_edit(root: Path, crate: dict, steps: list):
    """Publish the crate, then each edit on top of it; yields `(label, differences, edges after it, expected edges)`."""
    _write(root, crate)
    connection = _new_connection()
    _publish(connection, root, sorted(crate))
    for label, files, expected in steps:
        _write(root, files)
        _publish(connection, root, list(files), deleted={name for name, text in files.items() if text is None})
        fresh = _fresh(root)
        yield label, _differences(_state(connection), _state(fresh)), _edges(connection), set(expected)
        fresh.close()
    connection.close()


@pytest.mark.component  # a few small files in tmp_path, read by the real Rust parser, two in-memory databases per edit
class TestTheSameAsAFreshIndex:
    @pytest.mark.parametrize("name", list(SCENARIOS))
    def test_every_edit_gives_the_index_of_the_whole_tree(self, root, name):
        crate, steps = SCENARIOS[name]
        for label, differences, edges, expected in _each_edit(root, crate, steps):
            assert not differences, f"{name} / {label}: {differences}"
            assert expected <= edges, f"{name} / {label}: edges missing {expected - edges}"

    def test_a_rule_that_finds_nothing_to_look_at_loses_an_edge_and_the_check_sees_it(self, root):
        """The check is only worth having if it can fail: with a rule that answers 'nothing is affected' for every change, a
        step no longer equals the fresh index (and the edge a caller should have is missing)."""
        crate, steps = SCENARIOS["a function added that a call in another file names"]
        with patch.object(rust_reach, "reach", lambda *args, **kwargs: set()):
            results = list(_each_edit(root, crate, steps))
        assert any(differences for _, differences, _, _ in results)
        assert any(not expected <= edges for _, _, edges, expected in results)


@pytest.mark.unit  # facts and names only
class TestWhatAChangeTouches:
    def test_a_method_added_to_an_impl_names_the_method_and_not_the_type(self):
        old = {"impls": [{"mod": "", "type": "W", "type_path": "W", "trait": None, "methods": ["W::new"]}]}
        new = {"impls": [{"mod": "", "type": "W", "type_path": "W", "trait": None, "methods": ["W::new", "W::go"]}]}
        assert rust_reach.fact_tokens(old, new) == {"go"}

    def test_an_impl_that_appears_names_its_type_its_trait_and_its_methods(self):
        new = {"impls": [{"mod": "", "type": "Circle", "type_path": "Circle", "trait": "Shape", "methods": ["Circle::<Shape>::area"]}]}
        assert rust_reach.fact_tokens({}, new) == {"area", "Circle", "Shape"}

    def test_a_use_names_each_segment_and_its_alias(self):
        new = {"uses": [{"mod": "", "path": "a::f", "alias": "g", "pub": True}]}
        assert rust_reach.fact_tokens({}, new) == {"a", "f", "g"}

    @pytest.mark.parametrize(
        "old, new",
        [
            ({}, {"mods": [{"name": "x", "mod": "", "inline": False, "path": None}]}),
            ({"mods": [{"name": "x", "mod": "", "inline": False, "path": None}]}, {}),
            ({}, {"uses": [{"mod": "", "path": "crate::extra", "alias": "*", "pub": True}]}),
        ],
    )
    def test_what_moves_a_file_or_exposes_a_whole_module_cannot_be_bounded(self, old, new):
        assert rust_reach.fact_tokens(old, new) is None

    def test_a_private_glob_touches_only_what_it_names(self):
        assert rust_reach.fact_tokens({}, {"uses": [{"mod": "", "path": "crate::extra", "alias": "*", "pub": False}]}) == {"crate", "extra"}

    def test_a_pub_use_makes_its_alias_a_token_whenever_its_target_is(self):
        stored = {"src/lib.rs": json.dumps({"r": 1, "n": [], "s": [], "u": [["f", "g"]]}), "src/other.rs": json.dumps({"r": 1, "n": [], "s": [], "u": [["g", "h"]]})}
        assert rust_reach.reach({}, [], [], stored, {"f"}) == {"f", "g", "h"}

    def test_a_trait_that_gains_an_implementer_brings_the_names_of_its_methods(self):
        stored = {"src/shape.rs": json.dumps({"r": 1, "n": [], "s": [], "t": {"Shape": ["area", "name"]}})}
        assert rust_reach.reach({}, [], [], stored, {"Shape"}) == {"Shape", "area", "name"}

    def test_a_rust_file_is_looked_at_only_when_it_mentions_a_token_and_always_when_nothing_can_be_told(self):
        stored = json.dumps({"r": 1, "n": ["go"], "s": []})
        assert graph_sync._may_be_affected(stored, set(), set(), {"go"}) is True
        assert graph_sync._may_be_affected(stored, set(), set(), {"other"}) is False
        assert graph_sync._may_be_affected(stored, set(), set(), None) is True
        assert graph_sync._may_be_affected(None, set(), set(), {"other"}) is True  # nothing stored: looked at

    def test_a_file_with_no_function_still_records_what_it_re_exports(self):
        facts = {"uses": [{"mod": "", "path": "a::f", "alias": "g", "pub": True}], "mods": [{"name": "a", "mod": "", "inline": False, "path": None}], "types": [], "impls": [], "traits": []}
        mentioned = graph_sync._mentions_of([], {"facts": facts})
        assert mentioned["r"] == 1 and mentioned["u"] == [["f", "g"]] and {"a", "f", "g"} <= set(mentioned["n"])

    def test_a_file_that_appears_names_its_module(self):
        assert rust_reach.reach({}, ["src/circle.rs", "src/net/mod.rs"], [], {}, set()) == {"circle", "net"}

    def test_a_crate_root_that_appears_or_goes_cannot_be_bounded(self):
        assert rust_reach.reach({}, ["src/lib.rs"], [], {}, set()) is None
        assert rust_reach.reach({}, [], ["crates/x/src/main.rs"], {}, set()) is None
        assert rust_reach.reach({"src/a.rs": (None, {})}, [], [], {}, set()) is None  # earlier facts not known

    def test_a_function_is_asked_about_its_calls_its_receivers_its_types_and_its_type(self):
        class Call:
            def __init__(self, name, receiver=None):
                self.name, self.receiver = name, receiver

        class Function:
            def __init__(self, calls=(), types=None, parent=None):
                self.calls, self.property_reads, self.declared_param_types, self.parent = list(calls), [], types or {}, parent

        assert rust_reach.function_may_resolve_differently(Function([Call("go")]), {"go"})
        assert rust_reach.function_may_resolve_differently(Function([Call("new", "crate::t::W")]), {"W"})
        assert rust_reach.function_may_resolve_differently(Function(types={"w": "Box<W>"}), {"W"})
        assert rust_reach.function_may_resolve_differently(Function(parent="W::<Shape>"), {"Shape"})
        assert not rust_reach.function_may_resolve_differently(Function([Call("other", "x")], {"y": "Z"}, "Q"), {"go", "W"})
        assert rust_reach.function_may_resolve_differently(Function(), None)
