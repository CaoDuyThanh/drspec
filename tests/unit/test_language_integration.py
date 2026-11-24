"""What the rest of drspec does for every language: carry-over on a rename, the identity migration, document links,
lookups with candidates, and the packaging that carries the grammars."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.core.scanner import Scanner
from drspec.db import get_connection, init_schema
from drspec.db.identity_migration import ensure_identity_layout
from drspec.db.queries import get_contract, insert_contract
from tests.helpers import publish as sync_scan_results
from tests.helpers import strip_identity_hash as _strip
from tests.unit import test_js_support, test_rust_support

runner = CliRunner()
ROOT = Path(__file__).parent.parent.parent

RUST_BODY = "pub fn compute_total(items: &[i32], rate: i32) -> i32 {\n    let mut total = 0;\n    for item in items {\n        total += item * rate;\n        if total > 1000 {\n            total -= 10;\n        }\n    }\n    total\n}\n"
JS_BODY = "export function computeTotal(items, rate) {\n  let total = 0;\n  for (const item of items) {\n    total += item.price * rate;\n    if (total > 1000) {\n      total -= 10;\n    }\n  }\n  return total;\n}\n"


@pytest.fixture
def database(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    conn = duckdb.connect(":memory:")  # only what the publish stores is read back: no file
    init_schema(conn)
    yield root, conn
    conn.close()


def _sync(conn, root: Path, paths):
    scanner = Scanner()
    functions, imports = [], {}
    for relative in paths:
        result = scanner.scan_file(str(root / relative), relative_to=root)
        assert result and result.parse_status == "successful", relative
        functions.extend(result.functions)
        imports.update(result.file_imports)
    return sync_scan_results(conn, functions=functions, scanned_file_paths=set(paths), file_imports=imports)


@pytest.mark.parametrize(
    "file, body, old, new",
    [("a.rs", RUST_BODY, "compute_total", "sum_prices"), ("a.ts", JS_BODY, "computeTotal", "sumPrices")],
)
@pytest.mark.component
class TestCarryOverOnARename:
    def test_the_contract_follows_a_renamed_function(self, database, file, body, old, new):
        root, conn = database
        (root / file).write_text(body)
        _sync(conn, root, [file])
        old_id = conn.execute("SELECT function_id FROM artifacts").fetchone()[0]
        insert_contract(conn, old_id, json.dumps({"summary": "totals", "invariants": []}), 0.9)

        (root / file).write_text(body.replace(old, new))
        stats = _sync(conn, root, [file])

        (new_id,) = [row[0] for row in conn.execute("SELECT function_id FROM artifacts").fetchall()]
        assert new_id != old_id and stats.carried_over == {new_id: old_id}
        assert json.loads(get_contract(conn, new_id)["contract_json"])["summary"] == "totals"


@pytest.mark.component
class TestSameNameInOneFile:
    def test_a_rust_inherent_and_trait_method_of_one_name_are_two_functions(self, database):
        root, conn = database
        (root / "a.rs").write_text("struct S;\nimpl S { fn fmt(&self) {} }\nimpl Display for S { fn fmt(&self) {} }\nfn lone() {}\n")
        _sync(conn, root, ["a.rs"])
        names = {row[0].split("#")[0] for row in conn.execute("SELECT function_id FROM artifacts").fetchall()}
        assert names == {"a.rs::S::fmt", "a.rs::S::<Display>::fmt", "a.rs::lone"}


@pytest.mark.component
class TestIdentityMigration:
    #: The migration parses every stored function body again with the parser of ITS language (the parameters, hence the hash, come from there), so
    #: the languages are part of what is proved. One project holds the fixtures of both (their files do not collide): one scan, one old-layout
    #: file, one migration; what is compared is the identifiers of each language by itself, so a break in one parser names it.
    LANGUAGES = {"javascript and typescript": (".js", ".ts", ".tsx"), "rust": (".rs",)}

    def test_a_database_from_before_the_hash_gets_the_identifiers_a_fresh_parse_gives(self, tmp_path, monkeypatch):
        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.chdir(project)
        for module in (test_js_support, test_rust_support):
            for name, text in module.FILES.items():
                (project / name).parent.mkdir(parents=True, exist_ok=True)
                (project / name).write_text(text)
        database = project / "_drspec" / "contracts.db"  # the objective is the migration of a real FILE, which the next lines make from this scan
        database.parent.mkdir()
        made = get_connection(database)  # pattern-ok: duckdb-file the migration works on a real file
        init_schema(made)
        made.close()
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        connection = duckdb.connect(str(database), read_only=True)  # pattern-ok: duckdb-file a real file
        try:
            expected = sorted(row[0] for row in connection.execute("SELECT function_id FROM artifacts").fetchall())
        finally:
            connection.close()
        copy = tmp_path / "copy.db"
        shutil.copy(database, copy)
        old = tmp_path / "old" / "contracts.db"
        old.parent.mkdir()
        _strip(copy, old)
        assert ensure_identity_layout(old) == "migrated"
        migrated = duckdb.connect(str(old), read_only=True)  # pattern-ok: duckdb-file a real database FILE: the migration works on a file
        try:
            got = sorted(row[0] for row in migrated.execute("SELECT function_id FROM artifacts").fetchall())
        finally:
            migrated.close()

        for language, suffixes in self.LANGUAGES.items():
            want = [i for i in expected if i.split("::")[0].endswith(suffixes)]
            have = [i for i in got if i.split("::")[0].endswith(suffixes)]
            assert want, f"the fixtures have no {language} function"
            assert have == want, language


@pytest.mark.component
class TestDocumentLinks:
    def test_a_section_naming_a_rust_function_and_a_typescript_function_links_to_both(self, kept_project):
        tmp_path = kept_project
        (tmp_path / "lib.rs").write_text("pub fn compute_total(a: i32) -> i32 { a }\n")
        (tmp_path / "util.ts").write_text("export function computeTotal(a: number) { return a; }\n")
        (tmp_path / "README.md").write_text("# Totals\n\nCall `compute_total` in lib.rs and `computeTotal` in util.ts.\n")
        assert runner.invoke(app, ["config", "set", "docs.enabled", "true"]).exit_code == 0
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        for identifier in ("lib.rs::compute_total", "util.ts::computeTotal"):
            data = json.loads(runner.invoke(app, ["--json", "deps", "get", identifier]).output)["data"]
            assert "Totals" in json.dumps(data["documented_in"])


@pytest.mark.unit
class TestPackaging:
    def test_the_grammars_are_dependencies_and_hidden_imports(self):
        pyproject = (ROOT / "pyproject.toml").read_text()
        spec = (ROOT / "pyinstaller.spec").read_text()
        for grammar in ("javascript", "typescript", "cpp", "rust"):
            assert f"tree-sitter-{grammar}" in pyproject
            assert f"tree_sitter_{grammar}" in spec

    def test_the_rust_pin_is_the_same_family_as_the_other_grammars(self):
        pyproject = (ROOT / "pyproject.toml").read_text()
        assert "tree-sitter-rust>=0.21.2,<0.22.0" in pyproject

    def test_the_scanner_reads_rs_and_ignores_target(self):
        scanner = Scanner()
        assert scanner.detect_language("src/lib.rs") == "rust"
        assert "target" in scanner.ignore_patterns and "Cargo.lock" in scanner.ignore_patterns
