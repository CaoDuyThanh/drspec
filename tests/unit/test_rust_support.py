"""Rust: names, module-aware call edges, and what the parser leaves out on purpose."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.core.hasher import normalize_code
from drspec.core.rust_modules import child_file, discover_crates, parse_manifest
from drspec.db import init_schema
from drspec.parsers import RustParser

runner = CliRunner()

FILES = {
 "Cargo.toml": "[package]\nname = \"demo\"\nversion = \"0.1.0\"\n\n[dependencies]\n",
 "src/lib.rs": "pub mod net;\nmod util {\n    pub fn helper(x: i32) -> i32 { x + 1 }\n    pub mod deep {\n        pub fn bottom() -> i32 { super::helper(1) }\n    }\n}\n\npub use crate::net::server::Server;\nuse crate::util::{helper, deep::bottom as low};\n\npub trait Shape {\n    fn area(&self) -> f64;\n    fn describe(&self) -> String { format!(\"area {}\", self.area()) }\n}\n\npub struct Circle { pub r: f64 }\nimpl Shape for Circle {\n    fn area(&self) -> f64 { 3.0 * self.r * self.r }\n}\n\npub struct Square { pub s: f64 }\nimpl Shape for Square {\n    fn area(&self) -> f64 { self.s * self.s }\n}\n\npub fn total<T: Shape>(items: &[T], first: &T) -> f64 {\n    first.area() + helper(1) as f64\n}\n\npub fn boxed(s: Box<dyn Shape>) -> String { s.describe() }\n\npub fn add(a: i32, b: i32) -> i32 { a + b }\n\nmacro_rules! twice { ($x:expr) => { $x * 2 }; }\n\npub async fn fetch() -> i32 { low() + add(1, 2) }\n\npub fn uses_macro() -> i32 { twice!(add(1, 1)) }\n\npub fn make() -> f64 {\n    let c = Circle { r: 1.0 };\n    let s: Square = Square { s: 2.0 };\n    c.area() + s.area() + unknown_thing.go()\n}\n\n#[cfg(test)]\nmod tests {\n    use super::*;\n    #[test]\n    fn adds() { assert_eq!(add(1, 2), 3); }\n    #[test]\n    fn made() { assert!(make() > 0.0); }\n}\n",
 "src/main.rs": "use demo::net::server::Server;\nuse demo::{add, Circle, Shape};\n\nfn main() {\n    let s = Server::new();\n    println!(\"{}\", s.start());\n    let c = Circle { r: 1.0 };\n    let n = add(1, 2);\n    c.describe();\n}\n",
 "src/net/mod.rs": "pub mod server;\npub fn connect() -> i32 { server::Server::new().start() }\n",
 "src/net/server.rs": "use std::fmt;\nuse crate::add;\nuse super::connect as dial;\n\npub struct Server { port: u16 }\n\nimpl Server {\n    pub fn new() -> Self { Server { port: 80 } }\n    pub fn start(&self) -> i32 { self.bind() + add(1, 2) + Self::default_port() }\n    fn bind(&self) -> i32 { 1 }\n    fn default_port() -> i32 { 80 }\n    pub fn fmt(&self) -> i32 { 0 }\n    pub fn redial(&self) -> i32 { dial() }\n}\n\nimpl fmt::Display for Server {\n    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result { write!(f, \"{}\", self.port) }\n}\n"
}


def _fingerprint(root: Path) -> dict:
    """The number of rows of every table of the project's database."""
    connection = duckdb.connect(str(root / "_drspec" / "contracts.db"))  # pattern-ok: duckdb-file this module fixture is patched with KeptDuckDB: the path is an in-memory database
    tables = [row[0] for row in connection.execute("SHOW TABLES").fetchall()]
    return {table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tables}


@pytest.fixture(scope="module")
def _scanned_crate(tmp_path_factory):
    """The fixture crate, scanned ONCE for the module (a scan costs about 85 ms and 13 tests only READ what it made): in a folder with an
    in-memory project database kept for the module (`KeptDuckDB`, patched by a context that puts everything back when the module ends)."""
    from tests.fakes import KeptDuckDB

    root = tmp_path_factory.mktemp("crate")
    with pytest.MonkeyPatch.context() as patch_in_module:
        KeptDuckDB().install(patch_in_module)
        patch_in_module.chdir(root)
        (root / "_drspec").mkdir()
        connection = duckdb.connect(str(root / "_drspec" / "contracts.db"))  # pattern-ok: duckdb-file this module fixture is patched with KeptDuckDB: the path is an in-memory database
        init_schema(connection)
        connection.close()
        for name, text in FILES.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        before = _fingerprint(root)
        yield root
        # the 13 tests that share this database only READ it: a test that wrote (a scan, a save, an edit) would make the others depend on the
        # order they run in, so the rows of every table are counted again at the end
        assert _fingerprint(root) == before, "a test of this module wrote to the shared project database"


@pytest.fixture
def project(_scanned_crate, monkeypatch):
    """The scanned crate, as the working folder; a test of this class only reads it."""
    monkeypatch.chdir(_scanned_crate)
    return _scanned_crate


def _db(project: Path):
    return duckdb.connect(str(project / "_drspec" / "contracts.db"), read_only=True)  # pattern-ok: duckdb-file this module fixture is patched with KeptDuckDB: the path is an in-memory database


def _short(function_id: str) -> str:
    return function_id.split("#")[0]


def _edges(project: Path):
    connection = _db(project)
    try:
        return sorted((_short(a), _short(b), c) for a, b, c in connection.execute("SELECT caller_id, callee_id, confidence FROM dependencies").fetchall())
    finally:
        connection.close()


def _callees(project: Path, caller: str):
    return {(b, c) for a, b, c in _edges(project) if a == caller}


def _functions(source: str):
    return {f.qualified_name: f for f in RustParser().parse(source, file_path="x.rs").functions}


class TestNames:
    def test_free_methods_trait_methods_and_default_methods(self):
        names = set(_functions(
            "fn free() {}\nstruct S;\nimpl S { fn m(&self) {} fn assoc() {} }\nimpl fmt::Display for S { fn fmt(&self, f: &mut Formatter) -> Result { Ok(()) } }\n"
            "trait T { fn declared(&self); fn defaulted(&self) {} }\nmod inner { pub fn deep() {} mod more { pub fn deeper() {} } }\n"
        ))
        assert names == {"free", "S::m", "S::assoc", "S::<Display>::fmt", "T::defaulted", "inner::deep", "inner::more::deeper"}

    def test_an_inherent_method_and_a_trait_method_of_one_name_never_collide(self):
        functions = _functions("struct S;\nimpl S { fn fmt(&self) {} }\nimpl Display for S { fn fmt(&self) {} }\n")
        assert set(functions) == {"S::fmt", "S::<Display>::fmt"}

    def test_self_is_not_a_parameter_and_types_are_the_tokens(self):
        functions = _functions("impl S { fn a(&self, x: &mut Vec<u8>, y: i32) {} fn b(self) {} fn c(&mut self) {} }\n")
        assert functions["S::a"].parameter_tokens == ["&mut Vec<u8>", "i32"]
        assert functions["S::b"].parameter_tokens == [] and functions["S::c"].parameter_tokens == []

    def test_async_const_unsafe_extern_and_nested_functions(self):
        functions = _functions('async fn a() {}\nconst fn b() {}\nunsafe fn c() {}\nextern "C" fn d() {}\nfn outer() { fn inner() {} }\n')
        assert set(functions) == {"a", "b", "c", "d", "outer", "outer::inner"}
        assert functions["a"].is_async and not functions["b"].is_async

    def test_closures_are_not_functions_and_their_calls_belong_to_the_function_around_them(self):
        functions = _functions("fn f() { let g = |x| helper(x); g(1); }\n")
        assert set(functions) == {"f"}
        assert {c.name for c in functions["f"].calls} == {"helper", "g"}

    def test_macro_rules_is_a_function_named_with_a_bang(self):
        assert set(_functions("macro_rules! twice { ($x:expr) => { $x * 2 }; }\n")) == {"twice!"}

    def test_cfg_variants_of_one_function_are_one_entry(self):
        functions = RustParser().parse('#[cfg(unix)]\nfn plat() -> i32 { 1 }\n#[cfg(windows)]\nfn plat() -> i32 { 2 }\n', file_path="x.rs").functions
        assert [f.qualified_name for f in functions] == ["plat"]

    def test_test_functions_are_marked(self):
        functions = _functions("fn real() {}\n#[test]\nfn alone() {}\n#[cfg(test)]\nmod tests { #[test] fn in_mod() {} fn helper() {} }\n")
        assert "test" in functions["alone"].decorators and "test" in functions["tests::in_mod"].decorators and "test" in functions["tests::helper"].decorators
        assert "test" not in functions["real"].decorators


class TestCallsInsideMacros:
    def test_standard_macros_are_not_edges_but_their_arguments_are_code(self):
        calls = _functions("fn f() { assert_eq!(add(1, 2), 3); println!(\"{}\", x.len()); let v = vec![make(), make()]; }\n")["f"].calls
        assert {(c.receiver, c.name) for c in calls} == {(None, "add"), ("x", "len"), (None, "make")}
        assert not [c for c in calls if c.kind == "macro"]

    def test_a_user_macro_is_a_call_to_it_and_its_arguments_are_code(self):
        calls = _functions("fn f() { my_macro!(inner(1)); }\n")["f"].calls
        assert {(c.name, c.kind) for c in calls} == {("my_macro!", "macro"), ("inner", "call")}

    def test_nested_macros_are_read(self):
        calls = _functions("fn f() { assert!(matches!(g(1), Some(_))); }\n")["f"].calls
        assert "g" in {c.name for c in calls}


class TestTypes:
    def test_references_and_smart_pointers_are_looked_through(self):
        types = _functions("fn f(a: &Foo, b: &mut Bar, c: Box<Baz>, d: Option<Qux>, e: Rc<RefCell<Quux>>, g: i32) {}\n")["f"].declared_param_types
        assert types == {"a": "Foo", "b": "Bar", "c": "Baz", "d": "Qux", "e": "Quux"}

    def test_impl_trait_dyn_trait_and_generic_bounds_are_the_trait(self):
        functions = _functions("fn f<T: Shape + Clone>(a: T, b: impl Draw, c: &dyn Paint) {}\nfn g<T>(a: T) where T: Walk {}\n")
        assert functions["f"].declared_param_types == {"a": "Shape", "b": "Draw", "c": "Paint"}
        assert functions["g"].declared_param_types == {"a": "Walk"}

    def test_locals_are_typed_by_annotation_constructor_call_and_struct_literal(self):
        types = _functions("fn f() { let a: Foo = make(); let b = Bar::new(); let c = Baz { x: 1 }; let d = Qux::new()?; let mut e = Quux::default(); }\n")["f"].declared_param_types
        assert types == {"a": "Foo", "b": "Bar", "c": "Baz", "d": "Qux", "e": "Quux"}


class TestFacts:
    def test_use_declarations_groups_aliases_globs_and_reexports(self):
        facts = RustParser().parse("use a::{b, c as d, e::*};\npub use crate::x::Y as Z;\nuse std::fmt;\nuse a::{self};\n", file_path="x.rs").facts
        uses = {(u["path"], u["alias"], u["pub"]) for u in facts["uses"]}
        assert uses == {("a::b", "b", False), ("a::c", "d", False), ("a::e", "*", False), ("crate::x::Y", "Z", True), ("std::fmt", "fmt", False), ("a", "a", False)}

    def test_modules_impls_and_traits_are_recorded(self):
        facts = RustParser().parse("mod a;\n#[path = \"other.rs\"]\nmod b;\nmod c { mod d {} }\nstruct S;\nimpl T for S { fn f(&self) {} }\ntrait T { fn f(&self); }\n", file_path="x.rs").facts
        assert {(m["name"], m["mod"], m["inline"], m["path"]) for m in facts["mods"]} == {("a", "", False, None), ("b", "", False, "other.rs"), ("c", "", True, None), ("d", "c", True, None)}
        assert facts["impls"][0]["trait"] == "T" and facts["impls"][0]["methods"] == ["S::<T>::f"]
        assert facts["traits"] == [{"mod": "", "name": "T", "methods": ["f"]}]


class TestCallsAcrossFiles:
    def test_the_edges_of_the_fixture_crate(self, project):
        edges = _edges(project)
        assert len(edges) >= 20
        assert not [e for e in edges if e[0] == e[1]]

    def test_a_use_alias_a_group_and_a_reexport_resolve(self, project):
        assert ("src/lib.rs::util::deep::bottom", "EXTRACTED") in _callees(project, "src/lib.rs::fetch")  # use ... as low
        assert ("src/lib.rs::util::helper", "EXTRACTED") in _callees(project, "src/lib.rs::total")  # use crate::util::{helper, ..}

    def test_crate_super_and_self_paths(self, project):
        assert ("src/lib.rs::util::helper", "EXTRACTED") in _callees(project, "src/lib.rs::util::deep::bottom")  # super::helper
        assert ("src/net/server.rs::Server::default_port", "EXTRACTED") in _callees(project, "src/net/server.rs::Server::start")  # Self::
        assert ("src/lib.rs::add", "EXTRACTED") in _callees(project, "src/net/server.rs::Server::start")  # use crate::add

    def test_super_import_and_module_files_from_mod_declarations(self, project):
        assert ("src/net/mod.rs::connect", "EXTRACTED") in _callees(project, "src/net/server.rs::Server::redial")  # use super::connect as dial
        assert ("src/net/server.rs::Server::new", "EXTRACTED") in _callees(project, "src/net/mod.rs::connect")  # server::Server::new through `mod server;`

    def test_a_binary_reaches_the_library_by_the_package_name(self, project):
        callees = _callees(project, "src/main.rs::main")
        assert ("src/lib.rs::add", "EXTRACTED") in callees and ("src/net/server.rs::Server::new", "EXTRACTED") in callees

    def test_methods_on_a_typed_value_are_inferred(self, project):
        assert ("src/net/server.rs::Server::start", "INFERRED") in _callees(project, "src/main.rs::main")  # let s = Server::new()
        assert ("src/lib.rs::Circle::<Shape>::area", "INFERRED") in _callees(project, "src/lib.rs::make")

    def test_self_in_an_impl_goes_to_the_same_type(self, project):
        assert ("src/net/server.rs::Server::bind", "EXTRACTED") in _callees(project, "src/net/server.rs::Server::start")

    def test_a_call_through_a_trait_bound_goes_to_every_implementer_as_ambiguous(self, project):
        callees = _callees(project, "src/lib.rs::total")
        assert ("src/lib.rs::Circle::<Shape>::area", "AMBIGUOUS") in callees and ("src/lib.rs::Square::<Shape>::area", "AMBIGUOUS") in callees

    def test_a_default_method_is_reached_through_a_trait_object_and_calls_the_implementers(self, project):
        assert ("src/lib.rs::Shape::describe", "INFERRED") in _callees(project, "src/lib.rs::boxed")
        assert ("src/lib.rs::Circle::<Shape>::area", "AMBIGUOUS") in _callees(project, "src/lib.rs::Shape::describe")

    def test_user_macros_and_their_arguments(self, project):
        callees = _callees(project, "src/lib.rs::uses_macro")
        assert ("src/lib.rs::twice!", "EXTRACTED") in callees and ("src/lib.rs::add", "EXTRACTED") in callees

    def test_tests_in_a_cfg_test_module_see_the_parent_with_a_glob(self, project):
        assert ("src/lib.rs::add", "EXTRACTED") in _callees(project, "src/lib.rs::tests::adds")  # inside assert_eq!, through use super::*

    def test_tests_are_not_dead_code_candidates(self, project):
        names = json.dumps(json.loads(runner.invoke(app, ["--json", "deps", "unused"]).output)["data"])
        assert "tests::adds" not in names and "tests::made" not in names

    def test_an_unknown_receiver_stays_unresolved_and_is_counted(self, project):
        data = json.loads(runner.invoke(app, ["--json", "status"]).output)["data"]["rust"]
        assert data["files"] == 4 and data["unresolved_reasons"].get("unknown receiver", 0) >= 1
        assert data["unresolved_share"] < 35


class TestModules:
    def test_a_manifest_gives_the_crate_and_its_roots(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "my-app"\n\n[lib]\npath = "src/engine.rs"\n\n[[bin]]\nname = "tool"\npath = "src/tool.rs"\n')
        crates = discover_crates(tmp_path, {"src/engine.rs", "src/tool.rs", "src/main.rs"})
        assert [(c.name, c.roots) for c in crates] == [("my_app", ["src/engine.rs", "src/tool.rs", "src/main.rs"])]

    def test_workspace_members_are_crates(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/*"]\n')
        for name in ("a", "b"):
            (tmp_path / "crates" / name / "src").mkdir(parents=True)
            (tmp_path / "crates" / name / "Cargo.toml").write_text(f'[package]\nname = "{name}"\n')
        crates = discover_crates(tmp_path, {"crates/a/src/lib.rs", "crates/b/src/lib.rs"})
        assert sorted((c.name, c.roots[0]) for c in crates) == [("a", "crates/a/src/lib.rs"), ("b", "crates/b/src/lib.rs")]

    def test_without_a_manifest_the_usual_roots_are_found(self):
        crates = discover_crates(None, {"proj/src/lib.rs", "proj/src/main.rs", "proj/src/util.rs"})
        assert [(c.name, c.roots) for c in crates] == [("proj", ["proj/src/lib.rs", "proj/src/main.rs"])]

    def test_mod_declarations_find_name_rs_and_name_mod_rs_and_explicit_paths(self):
        known = {"src/lib.rs", "src/a.rs", "src/b/mod.rs", "src/a/inner.rs", "src/odd.rs"}
        assert child_file("src/lib.rs", [], "a", None, True, known) == "src/a.rs"
        assert child_file("src/lib.rs", [], "b", None, True, known) == "src/b/mod.rs"
        assert child_file("src/a.rs", [], "inner", None, False, known) == "src/a/inner.rs"  # a non-mod file's children live in a folder
        assert child_file("src/lib.rs", [], "x", "odd.rs", True, known) == "src/odd.rs"
        assert child_file("src/lib.rs", [], "missing", None, True, known) is None

    def test_manifest_parser_reads_sections_arrays_and_comments(self):
        manifest = parse_manifest('[package]\nname = "x" # the name\n[workspace]\nmembers = [\n  "a", # one\n  "b",\n]\n')
        assert manifest["package"]["name"] == "x" and manifest["workspace"]["members"] == ["a", "b"]


class TestHashing:
    def test_comments_do_not_change_the_normalised_text(self):
        plain = "fn f() { let x = 1;\n x\n }"
        commented = "fn f() { /* outer /* nested */ still */ let x = 1; // trailing\n x /// doc\n }"
        assert normalize_code(plain, "rust") == normalize_code(commented, "rust")

    def test_a_comment_marker_inside_a_string_is_text(self):
        assert normalize_code('fn f() { "a // b" }', "rust") != normalize_code('fn f() { "a" }', "rust")
        assert "r#\"x // y\"#" in normalize_code('fn f() { r#"x // y"# }', "rust")

    def test_lifetimes_are_not_character_literals(self):
        assert "'a" in normalize_code("fn f<'a>(x: &'a str) -> &'a str { x } // note", "rust")


class TestPartialFiles:
    def test_a_file_with_a_broken_function_keeps_the_readable_ones(self, kept_project):
        (kept_project / "ok.rs").write_text("fn good() -> i32 { 1 }\nfn broken( { \nfn also_good() -> i32 { 2 }\n")
        data = json.loads(runner.invoke(app, ["--json", "scan", "."]).output)["data"]
        assert data["functions_found"] >= 1
        names = {row[0] for row in _db(kept_project).execute("SELECT function_name FROM artifacts").fetchall()}
        assert "good" in names


class TestProjectWithoutRust:
    def test_no_rust_block(self, kept_project):
        (kept_project / "a.py").write_text("def f():\n    return 1\n")
        runner.invoke(app, ["--json", "scan", "."])
        assert "rust" not in json.loads(runner.invoke(app, ["--json", "status"]).output)["data"]
