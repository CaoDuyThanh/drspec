"""JavaScript and TypeScript at the level of Python: module-aware call edges, indexed object literals and test blocks."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.core.js_modules import JsConfig, load_config, resolve_module
from drspec.parsers.javascript_parser import JavaScriptParser

runner = CliRunner()

FILES = {
 "package.json": "{ \"name\": \"fixture\", \"main\": \"src/index.js\" }\n",
 "src/app.js": "import main, { add, Calc } from './math.js';\nimport * as m from './math.js';\nimport { plus } from './index.js';\nimport { legacy as old } from './math';\n\nexport function run() {\n  add(1, 2);\n  m.mul(2, 3);\n  main();\n  plus(1, 2);\n  const c = new Calc(1);\n  c.twice(2);\n  Calc.of(3);\n  return old();\n}\n",
 "src/index.js": "export * from './math.js';\nexport { add as plus } from './math.js';\n",
 "src/lib/util.ts": "export interface Shape { area(): number; }\nexport class Circle implements Shape {\n  constructor(private r: number) {}\n  area(): number { return 3 * this.r * this.r; }\n  static unit(): Circle { return new Circle(1); }\n}\nexport class Square implements Shape {\n  area(): number { return 1; }\n}\nexport function identity<T>(x: T): T { return x; }\nexport namespace Util {\n  export function helper(): number { return 1; }\n}\nnamespace Other {\n  export function helper(): number { return 2; }\n}\nclass Base { greet() { return 1; } }\nclass Derived extends Base {\n  constructor() { super(); }\n  hello() { return super.greet(); }\n}\n",
 "src/math.js": "export function add(a, b) { return a + b; }\nexport function mul(a, b) { return a * b; }\nexport default function main() { return add(1, 2); }\nexport class Calc {\n  constructor(v) { this.v = v; }\n  twice(x) { return mul(x, 2); }\n  static of(b) { return new Calc(b); }\n  get value() { return this.v; }\n  set value(x) { this.v = x; }\n  handler = () => this.twice(1);\n  #priv() { return 1; }\n}\nexport function legacy() { return 1; }\nconst obj = { method() { return add(1, 1); }, arrow: () => mul(1, 1), fn: function () { return 3; } };\nexport { obj };\n",
 "src/old.js": "const { legacy } = require('./math');\nconst math = require('./math');\nfunction useLegacy() { return legacy(); }\nfunction viaNamespace() { return math.add(1, 2); }\nmodule.exports = { useLegacy };\n",
 "src/ui/Button.tsx": "import { add } from '../math';\nexport function Button(props: { label: string }) {\n  return <button>{add(1, 2)}</button>;\n}\n",
 "src/ui/Card.tsx": "import { Button } from './Button';\nimport { Shape, identity, Util } from '@lib/util';\nexport function Card() { return <div><Button label=\"x\" /></div>; }\nexport function useShape(s: Shape) { Util.helper(); identity(1); return s.area(); }\n",
 "test/app.test.js": "import { run } from '../src/app.js';\ndescribe('app', () => {\n  it('runs', () => { run(); });\n  test('also', function () { run(); });\n});\n",
 "tsconfig.json": "{ \"compilerOptions\": { \"baseUrl\": \".\", \"paths\": { \"@lib/*\": [\"src/lib/*\"] } } }\n"
}


@pytest.fixture(scope="module")
def _initialised(tmp_path_factory, initialised_template):
    """An empty project after `init`: a copy of the session's initialised project (the projects below start from a copy of it)."""
    root = tmp_path_factory.mktemp("initialised") / "p"
    shutil.copytree(initialised_template, root)
    return root


def _copy_of(template: Path, destination: Path, monkeypatch) -> Path:
    """A copy of a project made once (`copytree` keeps the modification times, so an unchanged file stays unchanged), as the working folder."""
    root = destination / "p"
    shutil.copytree(template, root)
    monkeypatch.chdir(root)
    return root


def _scanned(template: Path, files: dict, destination: Path) -> Path:
    """`files` written into a copy of an initialised project and scanned: a project made once for a module."""
    root = destination / "p"
    shutil.copytree(template, root)
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(root)
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
    return root


@pytest.fixture(scope="module")
def _project_template(_initialised, tmp_path_factory):
    return _scanned(_initialised, FILES, tmp_path_factory.mktemp("js"))


@pytest.fixture(scope="module")
def _python_only_template(_initialised, tmp_path_factory):
    return _scanned(_initialised, {"a.py": "def f():\n    return 1\n"}, tmp_path_factory.mktemp("py"))


@pytest.fixture
def project(_project_template, tmp_path, monkeypatch):
    return _copy_of(_project_template, tmp_path, monkeypatch)


@pytest.fixture
def python_only(_python_only_template, tmp_path, monkeypatch):
    return _copy_of(_python_only_template, tmp_path, monkeypatch)


def _db(project: Path):
    return duckdb.connect(str(project / "_drspec" / "contracts.db"), read_only=True)


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


def _parse(source: str, dialect: str = "javascript"):
    return JavaScriptParser(dialect).parse(source, file_path="x.js")


class TestCallsAcrossFiles:
    def test_the_project_goes_from_five_edges_to_many(self, project):
        assert len(_edges(project)) >= 18

    def test_run_reaches_what_it_imports_by_every_kind_of_import(self, project):
        callees = {b for b, _ in _callees(project, "src/app.js::run")}
        assert callees == {
            "src/math.js::add",  # named import, and `plus` through the barrel re-export
            "src/math.js::mul",  # namespace import
            "src/math.js::main",  # default import
            "src/math.js::Calc.constructor",  # new Calc(...)
            "src/math.js::Calc.twice",  # a variable created with new
            "src/math.js::Calc.of",  # a static method
            "src/math.js::legacy",  # an aliased import without the extension
        }

    def test_a_call_through_a_reexport_alias_is_the_original_function(self, project):
        sites = [row[0] for row in _fetch(project, "SELECT call_site FROM dependencies WHERE caller_id LIKE 'src/app.js::run#%' AND callee_id LIKE 'src/math.js::add#%'")]
        assert "plus(1, 2)" in sites[0]

    def test_commonjs_require_resolves(self, project):
        assert ("src/math.js::legacy", "EXTRACTED") in _callees(project, "src/old.js::useLegacy")
        assert ("src/math.js::add", "EXTRACTED") in _callees(project, "src/old.js::viaNamespace")

    def test_a_jsx_component_is_a_call_and_a_dom_element_is_not(self, project):
        assert _callees(project, "src/ui/Card.tsx::Card") == {("src/ui/Button.tsx::Button", "EXTRACTED")}

    def test_a_path_alias_from_tsconfig_resolves(self, project):
        assert ("src/lib/util.ts::identity", "EXTRACTED") in _callees(project, "src/ui/Card.tsx::useShape")

    def test_a_typescript_namespace_keeps_its_qualifier(self, project):
        connection = _db(project)
        try:
            names = {_short(row[0]).split("::", 1)[1] for row in connection.execute("SELECT function_id FROM artifacts WHERE file_path = 'src/lib/util.ts'").fetchall()}
        finally:
            connection.close()
        assert {"Util.helper", "Other.helper"} <= names
        assert ("src/lib/util.ts::Util.helper", "EXTRACTED") in _callees(project, "src/ui/Card.tsx::useShape")

    def test_an_interface_receiver_resolves_to_every_implementer_as_ambiguous(self, project):
        callees = _callees(project, "src/ui/Card.tsx::useShape")
        assert ("src/lib/util.ts::Circle.area", "AMBIGUOUS") in callees and ("src/lib/util.ts::Square.area", "AMBIGUOUS") in callees

    def test_super_goes_to_the_parent_class(self, project):
        assert ("src/lib/util.ts::Base.greet", "EXTRACTED") in _callees(project, "src/lib/util.ts::Derived.hello")

    def test_a_bare_call_never_means_a_sibling_method(self, tmp_path):
        source = "class A {\n  helper() { return 1; }\n  run() { return helper(); }\n}\nfunction other() { return 1; }\n"
        result = _parse(source)
        assert [c.name for c in next(f for f in result.functions if f.qualified_name == "A.run").calls] == ["helper"]


class TestIndexing:
    def test_object_literal_methods_and_arrow_properties_are_functions(self, project):
        connection = _db(project)
        try:
            names = {_short(row[0]).split("::", 1)[1] for row in connection.execute("SELECT function_id FROM artifacts WHERE file_path = 'src/math.js'").fetchall()}
        finally:
            connection.close()
        assert {"obj.method", "obj.arrow", "obj.fn"} <= names

    def test_export_default_object_methods_are_named_default(self):
        names = [f.qualified_name for f in _parse("export default { handle() { return 1; }, other: () => 2 };\n").functions]
        assert names == ["default.handle", "default.other"]

    def test_test_blocks_are_functions_named_by_their_titles(self, project):
        connection = _db(project)
        try:
            names = {_short(row[0]).split("::", 1)[1] for row in connection.execute("SELECT function_id FROM artifacts WHERE file_path = 'test/app.test.js'").fetchall()}
        finally:
            connection.close()
        assert names == {"app", "app > runs", "app > also"}
        assert ("src/app.js::run", "EXTRACTED") in _callees(project, "test/app.test.js::app > runs")

    def test_a_title_cannot_break_the_identifier(self):
        names = [f.qualified_name for f in _parse("describe('a::b #1', () => { it('x', () => {}); });\n").functions]
        assert names == ["a:b _1", "a:b _1 > x"]

    def test_a_function_called_describe_without_a_callback_is_not_a_test_block(self):
        assert _parse("describe('only a title');\nfunction f() {}\n").functions[0].qualified_name == "f"

    def test_tests_are_not_dead_code_candidates(self, project):
        out = json.loads(runner.invoke(app, ["--json", "deps", "unused"]).output)["data"]
        names = json.dumps(out)
        assert "obj.fn" in names  # a function nobody calls is still listed
        assert "app > runs" not in names and "app.test.js" not in names


class TestModuleFacts:
    def test_default_named_aliased_namespace_and_require_bindings(self):
        facts = _parse("import d, { a as b, c } from './x';\nimport * as ns from './y';\nconst { q, r: s } = require('./z');\nconst w = require('./w');\n").facts
        by_local = {b["local"]: (b["module"], b["name"]) for b in facts["bindings"]}
        assert by_local == {"d": ("./x", "default"), "b": ("./x", "a"), "c": ("./x", "c"), "ns": ("./y", "*"), "q": ("./z", "q"), "s": ("./z", "r"), "w": ("./w", "*")}

    def test_exports_and_reexports(self):
        facts = _parse("export function f() {}\nexport default class K {}\nexport { f as g };\nexport * from './a';\nexport { h as i } from './b';\nmodule.exports = { f };\n").facts
        assert facts["exports"]["f"] == "f" and facts["exports"]["g"] == "f" and facts["exports"]["default"] in ("K", "f")
        assert {"module": "./a", "name": "*", "alias": None} in facts["reexports"]
        assert {"module": "./b", "name": "h", "alias": "i"} in facts["reexports"]

    def test_exports_assigned_functions_are_exported_functions(self):
        result = _parse("exports.run = function () { return 1; };\nmodule.exports.go = () => 2;\n")
        assert [f.qualified_name for f in result.functions] == ["run", "go"]
        assert result.facts["exports"] == {"run": "run", "go": "go"}

    def test_dynamic_import_with_a_literal_binds_the_module(self):
        facts = _parse("async function f() { const m = await import('./lazy'); return m.x(); }\n").facts
        assert {"local": "m", "module": "./lazy", "name": "*", "kind": "dynamic"} in facts["bindings"]

    def test_typed_parameters_and_new_expressions_give_declared_types(self):
        result = _parse("function f(s: Shape, n: number) { const c = new Circle(1); const d: Square = make(); }\n", "typescript")
        assert result.functions[0].declared_param_types == {"s": "Shape", "c": "Circle", "d": "Square"}  # a primitive is not a class

    def test_classes_record_their_parents_and_interfaces(self):
        classes = {c["name"]: c for c in _parse("interface I {}\nabstract class A implements I {}\nclass B extends A {}\n", "typescript").facts["classes"]}
        assert classes["I"]["interface"] and classes["A"]["implements"] == ["I"] and classes["B"]["extends"] == "A"


class TestModuleResolution:
    KNOWN = {"src/math.ts", "src/lib/index.tsx", "src/util.js", "src/lib/a.ts", "pkg/src/index.ts", "pkg/src/deep/x.ts"}

    def test_relative_paths_with_extensions_and_index_files(self):
        known = self.KNOWN
        assert resolve_module("src/app.ts", "./math", known) == "src/math.ts"
        assert resolve_module("src/app.ts", "./lib", known) == "src/lib/index.tsx"
        assert resolve_module("src/app.ts", "./math.js", known) == "src/math.ts"  # ESM spelling of a TypeScript file
        assert resolve_module("src/lib/a.ts", "../util", known) == "src/util.js"

    def test_a_package_from_node_modules_is_not_guessed(self):
        assert resolve_module("src/app.ts", "react", self.KNOWN) is None
        assert resolve_module("src/app.ts", "./missing", self.KNOWN) is None

    def test_tsconfig_paths_and_base_url(self, tmp_path):
        (tmp_path / "tsconfig.json").write_text('{\n // comment\n "compilerOptions": { "baseUrl": "src", "paths": { "@lib/*": ["lib/*"], }, },\n}\n')
        config = load_config(tmp_path)
        assert resolve_module("x.ts", "@lib/a", self.KNOWN, config) == "src/lib/a.ts"
        assert resolve_module("x.ts", "math", self.KNOWN, config) == "src/math.ts"

    def test_workspace_packages_resolve_by_name(self, tmp_path):
        (tmp_path / "package.json").write_text('{"name": "root", "workspaces": ["pkg"]}')
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "package.json").write_text('{"name": "@acme/pkg", "main": "src/index.ts"}')
        config = load_config(tmp_path)
        assert resolve_module("x.ts", "@acme/pkg", self.KNOWN, config) == "pkg/src/index.ts"
        assert resolve_module("x.ts", "@acme/pkg/deep/x", self.KNOWN, config) == "pkg/src/deep/x.ts"

    def test_an_unreadable_configuration_means_no_setting(self, tmp_path):
        (tmp_path / "tsconfig.json").write_text("{ not json")
        assert load_config(tmp_path) == JsConfig()


class TestResolverRobustness:
    def test_a_cycle_of_reexports_ends(self, _initialised, tmp_path, monkeypatch):
        root = _scanned(_initialised, {
            "a.js": "export * from './b.js';\nexport function fa() { return fb(); }\nimport { fb } from './b.js';\n",
            "b.js": "export * from './a.js';\nexport function fb() { return 1; }\n",
            "c.js": "import { nothing } from './a.js';\nexport function use() { return nothing(); }\n",
        }, tmp_path)
        assert ("a.js::fa", "b.js::fb", "EXTRACTED") in _edges(root)
        assert not [e for e in _edges(root) if e[0] == "c.js::use"]

    def test_a_change_of_exports_re_resolves_a_file_that_was_not_scanned(self, project):
        index = project / "src" / "index.js"
        assert ("src/app.js::run", "src/math.js::add", "EXTRACTED") in _edges(project)
        index.write_text("export * from './math.js';\n")  # `plus` is gone
        assert runner.invoke(app, ["--json", "scan", "src/index.js"]).exit_code == 0
        sites = [row[0] for row in _fetch(project, "SELECT call_site FROM dependencies WHERE caller_id LIKE 'src/app.js::run#%' AND callee_id LIKE 'src/math.js::add#%'")]
        assert sites and "plus(1, 2)" not in sites[0]


class TestStatus:
    def test_the_language_block_counts_calls_and_says_why_some_are_unresolved(self, project):
        data = json.loads(runner.invoke(app, ["--json", "status"]).output)["data"]["javascript"]
        assert data["files"] == 8 and data["calls"] > 0
        assert data["unresolved_share"] < 25
        assert "unresolved_reasons" in data

    def test_a_project_without_javascript_has_no_block(self, python_only):
        data = json.loads(runner.invoke(app, ["--json", "status"]).output)["data"]
        assert "javascript" not in data and "rust" not in data


def _fetch(project: Path, query: str):
    connection = _db(project)
    try:
        return connection.execute(query).fetchall()
    finally:
        connection.close()


class TestFilesReadByAnOlderParser:
    def test_files_read_before_the_module_facts_existed_are_queued_once(self, project):
        from drspec.core.facts_version import requeue_for_new_facts
        from drspec.db.queries import list_staleness_queue, set_config

        connection = duckdb.connect(str(project / "_drspec" / "contracts.db"))
        try:
            connection.execute("DELETE FROM config WHERE starts_with(key, 'scan:facts:')")
            assert requeue_for_new_facts(connection) == 8
            assert len(list_staleness_queue(connection)) >= 8
            connection.execute("DELETE FROM staleness_queue")
            assert requeue_for_new_facts(connection) == 0  # the marker is set: not again
            assert list_staleness_queue(connection) == []
            set_config(connection, "scan:facts:javascript", "0")
            assert requeue_for_new_facts(connection) == 8
        finally:
            connection.close()

    def test_a_project_without_javascript_only_gets_the_marker(self, python_only):
        from drspec.core.facts_version import requeue_for_new_facts

        connection = duckdb.connect(str(python_only / "_drspec" / "contracts.db"))
        try:
            connection.execute("DELETE FROM config WHERE starts_with(key, 'scan:facts:')")
            assert requeue_for_new_facts(connection) == 0
        finally:
            connection.close()
