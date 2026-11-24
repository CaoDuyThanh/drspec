"""What a publish asks the database: a number of statements that does not grow with the number of functions, and the
same stored result as the one-question-per-row way it replaced."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import pytest

from drspec.core import graph_sync
from drspec.core.graph_sync import _EdgeRows, _function_record, sync_scan_results
from drspec.core.scanner import Scanner
from drspec.db import get_connection, init_schema
from drspec.db.queries import EdgeRecord, ReferenceRecord
from tests.fakes import Counting


def _project(root: Path, files: int):
    for i in range(files):
        (root / f"m{i}.py").write_text(
            f"from m{(i + 1) % files} import f{(i + 1) % files}\n\n\ndef f{i}(x):\n    return f{(i + 1) % files}(x)\n\n\n"
            f"class K{i}:\n    @property\n    def value(self):\n        return 1\n\n    def m(self):\n        return f{i}(self.value)\n"
        )
    scanner = Scanner()
    functions, imports, paths = [], {}, set()
    for i in range(files):
        result = scanner.scan_file(f"m{i}.py", relative_to=root)
        functions += result.functions
        imports.update(result.file_imports)
        paths.add(f"m{i}.py")
    return functions, imports, paths


pytestmark = pytest.mark.component  # the product's real publish (`sync_scan_results`, about 55 ms a call) on an in-memory database: a component, not a unit


@pytest.fixture
def root(tmp_path, monkeypatch, kept_duckdb):
    """A scratch folder that is the working folder. Every `get_connection(root / "x.db")` is a database held in memory (`kept_duckdb`): these tests count
    statements and compare what is stored, which has nothing to do with a file."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _statements(root: Path, files: int, force: bool):
    functions, imports, paths = _project(root, files)
    connection = get_connection(root / f"p{files}.db")
    init_schema(connection)
    counting = Counting(connection)
    sync_scan_results(counting, functions, paths, imports)
    counting.count = 0
    counting.shapes.clear()
    sync_scan_results(counting, functions, paths, imports, force=force)
    connection.close()
    return len(functions), counting.count, counting.shapes


class TestTheNumberOfStatements:
    def test_a_forced_republish_asks_a_few_questions_per_file_and_none_per_function(self, root):
        small_files, large_files = 3, 9  # the slope between two sizes is the proof; the fixed cost cancels, so the sizes can be small
        small_functions, small, _ = _statements(root, small_files, True)
        large_functions, large, shapes = _statements(root, large_files, True)
        per_function = (large - small) / (large_functions - small_functions)
        assert per_function < 2.5, f"{per_function:.1f} statements per function: {shapes.most_common(6)}"
        for shape in shapes:  # none of the old one-per-row questions is asked once per function any more (3 functions per file here)
            assert shapes[shape] <= 2 * large_files, (shape, shapes[shape])

    def test_an_unchanged_republish_writes_no_function(self, root):
        functions, imports, paths = _project(root, 12)
        connection = get_connection(root / "p.db")
        init_schema(connection)
        counting = Counting(connection)
        sync_scan_results(counting, functions, paths, imports)
        counting.shapes.clear()
        sync_scan_results(counting, functions, paths, imports)
        writes = sum(count for shape, count in counting.shapes.items() if shape.startswith(("INSERT INTO artifacts", "UPDATE artifacts")))
        assert writes == 0
        connection.close()


def _stored(connection):
    tables = {
        "artifacts": "SELECT function_id, file_path, function_name, signature, body, code_hash, status, start_line, end_line, parent, is_property FROM artifacts ORDER BY function_id",
        "dependencies": "SELECT caller_id, callee_id, call_site, edge_hash, confidence FROM dependencies ORDER BY caller_id, callee_id",
        "code_references": "SELECT caller_id, callee_id, reference_site, edge_hash, confidence FROM code_references ORDER BY caller_id, callee_id",
        "snapshots": "SELECT key, value FROM config WHERE starts_with(key, 'scan:') ORDER BY key",
        "files": "SELECT file_path, size FROM scanned_files ORDER BY file_path",
    }
    return {name: connection.execute(sql).fetchall() for name, sql in tables.items()}


class TestTheSameResultAsTheOldWay:
    def _run(self, root: Path, name: str, rows: bool):
        functions, imports, paths = _project(root, 15)
        connection = get_connection(root / f"{name}.db")
        init_schema(connection)
        with patch.object(graph_sync, "_EdgeRows", (_EdgeRows if rows else (lambda *a, **k: None))):
            first = sync_scan_results(connection, functions, paths, imports)
            # edit a few functions and the signature of one, then publish again, and once more with force
            edited = []
            for function in functions:
                if function.name in ("f3", "f7"):
                    function = type(function)(**{**asdict(function), "body": function.body.replace("return", "return 0 +"), "calls": function.calls, "property_reads": function.property_reads})
                edited.append(function)
            second = sync_scan_results(connection, edited, paths, imports)
            third = sync_scan_results(connection, edited, paths, imports, force=True)
        result = _stored(connection), [
            {k: v for k, v in vars(stats).items()} for stats in (first, second, third)
        ]
        connection.close()
        return result

    def test_batch_reading_and_the_per_row_queries_store_and_count_the_same(self, root):
        batched = self._run(root, "batched", True)
        per_row = self._run(root, "per_row", False)
        assert batched[0] == per_row[0]
        assert batched[1] == per_row[1]
        assert batched[0]["dependencies"] and batched[0]["code_references"]


class TestThePieces:
    def test_the_fast_function_record_equals_asdict(self, root):
        functions, _imports, _paths = _project(root, 5)
        assert functions and all(_function_record(function) == asdict(function) for function in functions)

    def test_edge_rows_follow_what_the_publish_writes(self, root):
        connection = get_connection(root / "r.db")
        init_schema(connection)
        for function_id in ("a", "b", "c"):
            connection.execute(
                "INSERT INTO artifacts (function_id, file_path, function_name, signature, body, code_hash, language, start_line, end_line) VALUES (?, 'f.py', ?, 's', 'b', 'h', 'python', 1, 2)",
                [function_id, function_id],
            )
        connection.execute("INSERT INTO dependencies (caller_id, callee_id, call_site, edge_hash, confidence) VALUES ('a', 'b', 'b()', 'h1', 'EXTRACTED')")
        rows = _EdgeRows(connection, "dependencies", "call_site", EdgeRecord, ["a", "b"])
        assert [e.caller_id for e in rows.into("b")] == ["a"] and list(rows.out_of("a")) == ["b"] and rows.get("a", "b").edge_hash == "h1"
        rows.rehash("b", [("a", "h2")])
        assert rows.get("a", "b").edge_hash == "h2" and rows.into("b")[0].edge_hash == "h2"
        rows.put("b", "c", "c()", "h3", "INFERRED")
        assert rows.get("b", "c").confidence == "INFERRED" and list(rows.out_of("b")) == ["c"] and rows.into("c") == []  # c is not one of the functions
        rows.forget("a", "b")
        assert rows.get("a", "b") is None and rows.into("b") == [] and rows.out_of("a") == {}
        rows.put("a", "b", "b()", "h4", "EXTRACTED")
        assert [e.edge_hash for e in rows.into("b")] == ["h4"]
        connection.close()

    def test_a_table_the_functions_are_not_in_yields_nothing(self, root):
        connection = get_connection(root / "r2.db")
        init_schema(connection)
        rows = _EdgeRows(connection, "code_references", "reference_site", ReferenceRecord, [])
        assert rows.into("x") == [] and rows.out_of("x") == {} and rows.get("x", "y") is None
        connection.close()


def _cpp_project(root: Path, pairs: int):
    """`pairs` headers declaring a class and sources defining it, so a publish has definitions, aliases and edges."""
    scanner = Scanner()
    functions, imports, paths = [], {}, set()
    for i in range(pairs):
        (root / f"c{i}.h").write_text(f"class C{i} {{\npublic:\n    int get() const;\n    void set(int v);\n    int helper{i}(int x);\n}};\nint free{i}(int x);\nint ghost{i}(int x);\n")
        (root / f"c{i}.cpp").write_text(
            f'#include "c{i}.h"\nint C{i}::get() const {{ return helper{i}(1); }}\nvoid C{i}::set(int v) {{ helper{i}(v); }}\n'
            f"int C{i}::helper{i}(int x) {{ return x + free{i}(x); }}\nint free{i}(int x) {{ return x; }}\n"
        )
        for name in (f"c{i}.h", f"c{i}.cpp"):
            result = scanner.scan_file(name, relative_to=root)
            functions += result.functions
            imports.update(result.file_imports)
            paths.add(name)
    return functions, imports, paths


class TestAFirstPublishAlsoAsksFewStatements:
    def _first(self, root: Path, pairs: int):
        functions, imports, paths = _cpp_project(root, pairs)
        connection = get_connection(root / f"first{pairs}.db")
        init_schema(connection)
        counting = Counting(connection)
        sync_scan_results(counting, functions, paths, imports)
        result = (len(functions), counting.count, counting.shapes, connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0])
        connection.close()
        return result

    def test_python_files(self, root):
        small_functions, small, _ = _statements_first(root, 10)
        large_functions, large, shapes = _statements_first(root, 40)
        # a few statements for each file (the files' own bookkeeping) and none for each function
        assert (large - small) / 30 <= 4, shapes.most_common(5)
        assert large_functions - small_functions == 90 and (large - small) < (large_functions - small_functions) * 1.2

    def test_cpp_files_with_declarations_and_aliases(self, root):
        small_functions, small, _, _ = self._first(root, 6)
        large_functions, large, shapes, artifacts = self._first(root, 24)
        assert artifacts > 0
        assert (large - small) / 18 <= 6, shapes.most_common(6)  # (and nothing like the several statements each function and declaration cost)
        assert (large - small) < (large_functions - small_functions) * 0.6


def _statements_first(root: Path, files: int):
    functions, imports, paths = _project(root, files)
    connection = get_connection(root / f"pf{files}.db")
    init_schema(connection)
    counting = Counting(connection)
    sync_scan_results(counting, functions, paths, imports)
    connection.close()
    return len(functions), counting.count, counting.shapes


class TestDeclarationsKeepTheirMeaning:
    def test_aliases_entries_and_a_republish(self, root):
        functions, imports, paths = _cpp_project(root, 3)
        connection = get_connection(root / "d.db")
        init_schema(connection)
        sync_scan_results(connection, functions, paths, imports)

        def state():
            return (
                connection.execute("SELECT function_id FROM artifacts ORDER BY 1").fetchall(),
                connection.execute("SELECT function_id, definition_id FROM function_declarations ORDER BY 1").fetchall(),
                connection.execute("SELECT caller_id, callee_id FROM dependencies ORDER BY 1, 2").fetchall(),
            )

        first = state()
        names = [row[0] for row in first[0]]
        # a method declared in a header is an alias of its definition: it has no artifact of its own
        assert not any(name.startswith("c0.h::C0::get") for name in names) and any(name.startswith("c0.cpp::C0::get") for name in names)
        aliases = dict(first[1])
        assert any(definition for definition in aliases.values()) and any(definition is None for definition in aliases.values())
        # a declaration nothing defines stays an entry
        assert any(name.startswith("c0.h::ghost0") for name in names)
        sync_scan_results(connection, functions, paths, imports, force=True)
        assert state() == first
        connection.close()

    def test_the_index_finds_what_the_query_finds(self, root):
        from drspec.core.declarations import (
            definition_index,
            find_definition,
            lookup_definition,
        )

        functions, imports, paths = _cpp_project(root, 3)
        connection = get_connection(root / "i.db")
        init_schema(connection)
        sync_scan_results(connection, functions, paths, imports)
        index = definition_index(connection)
        ids = [row[0] for row in connection.execute("SELECT function_id FROM function_declarations").fetchall()]
        assert ids
        for declaration_id in ids + ["nowhere.h::nothing#00000000"]:
            assert lookup_definition(index, declaration_id) == find_definition(connection, declaration_id), declaration_id
        connection.close()


class TestTheBulkWrites:
    def test_insert_rows_takes_a_column_that_is_null_in_every_row(self, root):
        from drspec.db.queries import insert_artifacts

        connection = get_connection(root / "b.db")
        init_schema(connection)
        rows = [(f"f.py::a{i}#0", "f.py", f"a{i}", "def a()", "pass", "h", "python", 1, 2, None, "PENDING", False) for i in range(1500)]
        insert_artifacts(connection, rows)  # more rows than one statement takes
        assert connection.execute("SELECT COUNT(*), COUNT(parent) FROM artifacts").fetchone() == (1500, 0)
        connection.close()

    def test_an_upsert_inserts_updates_and_lets_the_later_row_of_a_pair_win(self, root):
        from drspec.db.queries import insert_artifacts, upsert_edges

        connection = get_connection(root / "e.db")
        init_schema(connection)
        insert_artifacts(connection, [(f"f.py::{n}#0", "f.py", n, "s", "b", "h", "python", 1, 2, None, "PENDING", False) for n in "abc"])
        a, b, c = (f"f.py::{n}#0" for n in "abc")
        upsert_edges(connection, "dependencies", "call_site", [(a, b, "b()", "h1", "EXTRACTED")])
        upsert_edges(connection, "dependencies", "call_site", [(a, b, "b(1)", "h2", "INFERRED"), (a, c, "c()", "h3", "EXTRACTED"), (a, c, "c(2)", "h4", "AMBIGUOUS")])
        rows = connection.execute("SELECT caller_id, callee_id, call_site, edge_hash, confidence FROM dependencies ORDER BY callee_id").fetchall()
        assert rows == [(a, b, "b(1)", "h2", "INFERRED"), (a, c, "c(2)", "h4", "AMBIGUOUS")]
        connection.close()

    def test_nothing_to_write_is_no_statement(self, root):
        from drspec.db.queries import insert_artifacts, upsert_edges

        connection = get_connection(root / "n.db")
        init_schema(connection)
        counting = Counting(connection)
        insert_artifacts(counting, [])
        upsert_edges(counting, "dependencies", "call_site", [])
        assert counting.count == 0
        connection.close()


class TestEdgeRowsWriteTogether:
    def _setup(self, root):
        connection = get_connection(root / "w.db")
        init_schema(connection)
        for function_id in ("a", "b", "c"):
            connection.execute(
                "INSERT INTO artifacts (function_id, file_path, function_name, signature, body, code_hash, language, start_line, end_line) VALUES (?, 'f.py', ?, 's', 'b', 'h', 'python', 1, 2)",
                [function_id, function_id],
            )
        return connection

    def test_nothing_is_written_before_the_flush_and_all_of_it_after(self, root):
        connection = self._setup(root)
        rows = _EdgeRows(connection, "dependencies", "call_site", EdgeRecord, ["a", "b", "c"])
        rows.store("a", "b", "b()", "h1", "EXTRACTED")
        rows.store("a", "c", "c()", "h2", "EXTRACTED")
        assert connection.execute("SELECT COUNT(*) FROM dependencies").fetchone()[0] == 0
        assert rows.get("a", "b").edge_hash == "h1"  # but the publish sees them
        rows.flush()
        assert connection.execute("SELECT COUNT(*) FROM dependencies").fetchone()[0] == 2
        rows.flush()  # nothing left to write
        connection.close()

    def test_a_forgotten_pair_is_never_written(self, root):
        connection = self._setup(root)
        rows = _EdgeRows(connection, "dependencies", "call_site", EdgeRecord, ["a", "b", "c"])
        rows.store("a", "b", "b()", "h1", "EXTRACTED")
        rows.forget("a", "b")
        rows.flush()
        assert connection.execute("SELECT COUNT(*) FROM dependencies").fetchone()[0] == 0
        connection.close()

    def test_a_new_hash_reaches_the_row_that_is_not_in_the_table_yet(self, root):
        connection = self._setup(root)
        rows = _EdgeRows(connection, "dependencies", "call_site", EdgeRecord, ["a", "b", "c"])
        rows.store("a", "b", "b()", "old", "EXTRACTED")
        rows.rehash("b", [("a", "new")])
        rows.flush()
        assert connection.execute("SELECT edge_hash FROM dependencies").fetchone()[0] == "new"
        connection.close()

    def test_the_later_store_of_a_pair_wins(self, root):
        connection = self._setup(root)
        rows = _EdgeRows(connection, "dependencies", "call_site", EdgeRecord, ["a", "b", "c"])
        rows.store("a", "b", "b()", "h1", "EXTRACTED")
        rows.store("a", "b", "b(2)", "h2", "INFERRED")
        rows.flush()
        assert connection.execute("SELECT call_site, edge_hash, confidence FROM dependencies").fetchall() == [("b(2)", "h2", "INFERRED")]
        connection.close()


class TestTheOverloadCache:
    def test_it_answers_as_the_query_does(self, root):
        from drspec.core.graph_sync import _has_overloads

        functions, imports, paths = _project(root, 6)
        connection = get_connection(root / "o.db")
        init_schema(connection)
        sync_scan_results(connection, functions, paths, imports)
        connection.execute(
            "INSERT INTO artifacts (function_id, file_path, function_name, signature, body, code_hash, language, start_line, end_line) "
            "VALUES ('m0.py::f0#other', 'm0.py', 'f0', 's', 'b', 'h', 'python', 1, 2)"
        )
        cache: dict = {}
        ids = [row[0] for row in connection.execute("SELECT function_id FROM artifacts").fetchall()]
        answers = {function_id: _has_overloads(connection, function_id) for function_id in ids}
        assert any(answers.values()) and not all(answers.values())
        assert {function_id: _has_overloads(connection, function_id, cache) for function_id in ids} == answers
        connection.close()


def _reference_contexts(conn):
    """The fingerprints as the one-question-per-root algorithm computed them, kept here to hold the fast one to."""
    import hashlib

    hashes = dict(conn.execute("SELECT function_id, code_hash FROM artifacts").fetchall())
    files = dict(conn.execute("SELECT function_id, file_path FROM artifacts").fetchall())
    imports = {}
    for key, value in conn.execute("SELECT key, value FROM config WHERE starts_with(key, 'scan:imports:')").fetchall():
        imports[key[len("scan:imports:"):]] = json.loads(value)
    edges = {key: [] for key in hashes}
    for table, site in [("dependencies", "call_site"), ("code_references", "reference_site")]:
        for caller, callee, text, confidence in conn.execute(f"SELECT caller_id, callee_id, {site}, confidence FROM {table}").fetchall():
            edges[caller].append((callee, table, text, confidence))
    contexts = {}
    for root in hashes:
        visited, pending = set(), [root]
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            pending.extend(edge[0] for edge in edges[current])
        records = [(node, None if node == root else hashes[node], imports.get(files[node], {}), sorted(edges[node])) for node in sorted(visited)]
        contexts[root] = hashlib.sha256(json.dumps(["dependency-context:v1", records], sort_keys=True).encode("utf-8")).hexdigest()
    return contexts


class TestTheImportsRecord:
    def test_the_written_out_dictionary_is_what_asdict_gave(self, root):
        from dataclasses import asdict

        from drspec.core.graph_sync import _imports_dict
        from drspec.core.scanner import FileImports
        from drspec.parsers.models import RawModuleImport, RawNameImport

        full = FileImports(
            name_imports=[RawNameImport(module_path="a", name="b", alias="c"), RawNameImport(module_path="d", name="e")],
            module_imports=[RawModuleImport(module_path="m", alias=None)],
            includes=["x.h"],
            facts={"uses": [["a", "b"]], "n": {"k": [1, 2]}},
        )
        for imports in (full, FileImports(), FileImports(includes=["y.h"])):
            expected = asdict(imports)
            if not expected.get("facts"):
                expected.pop("facts", None)
            assert _imports_dict(imports) == expected
            assert list(_imports_dict(imports)) == list(expected)  # (the order of the keys is part of what is hashed)
            assert json.dumps(_imports_dict(imports), sort_keys=True) == json.dumps(expected, sort_keys=True)


class TestContexts:
    def _published(self, root: Path, name: str = "x.db"):
        functions, imports, paths = _project(root, 12)  # a ring of calls: every function reaches every other
        connection = get_connection(root / name)
        init_schema(connection)
        sync_scan_results(connection, functions, paths, imports)
        return connection, functions, imports, paths

    def test_the_fingerprints_are_the_ones_the_slow_way_gives(self, root):
        from drspec.core.provenance import compute_contexts

        connection, *_ = self._published(root)
        assert connection.execute("SELECT COUNT(*) FROM dependencies").fetchone()[0] > 0
        assert compute_contexts(connection) == _reference_contexts(connection)
        connection.execute("DELETE FROM config WHERE starts_with(key, 'scan:imports_sig:')")  # a database from before the digests: they are worked out from the imports
        assert compute_contexts(connection) == _reference_contexts(connection)
        connection.close()

    def test_the_fingerprints_with_non_ascii_text_and_a_function_nothing_calls(self, root):
        from drspec.core.provenance import compute_contexts

        (root / "u.py").write_text('def lonely(x):\n    return "héllo ✓"\n\n\ndef caller():\n    return lonely("é")\n')
        result = Scanner().scan_file("u.py", relative_to=root)
        connection = get_connection(root / "u.db")
        init_schema(connection)
        sync_scan_results(connection, result.functions, {"u.py"}, result.file_imports)
        assert compute_contexts(connection) == _reference_contexts(connection)
        connection.close()

    def test_nothing_is_recomputed_when_nothing_it_is_made_of_changed(self, root):
        from drspec.core import provenance

        connection, functions, imports, paths = self._published(root)
        before = _reference_contexts(connection)
        with patch.object(provenance, "_contexts_of", side_effect=AssertionError("recomputed")):
            sync_scan_results(connection, functions, paths, imports, force=True)
        assert provenance.refresh_contexts(connection) == before
        connection.close()

    def test_a_change_to_a_function_is_noticed(self, root):
        from drspec.core import provenance

        connection, functions, imports, paths = self._published(root)
        before = provenance.refresh_contexts(connection)
        edited = [type(f)(**{**asdict(f), "body": f.body.replace("return", "return 0 +"), "calls": f.calls, "property_reads": f.property_reads}) if f.name == "f5" else f for f in functions]
        sync_scan_results(connection, edited, paths, imports)
        after = provenance.refresh_contexts(connection)
        assert after != before and after == _reference_contexts(connection)
        connection.close()

    def _roots_worked_out(self, connection):
        """The roots `refresh_contexts` asks `_contexts_of` for (None when it asks for every function)."""
        from drspec.core import provenance

        asked = []
        real = provenance._contexts_of

        def spy(*args, roots=None):
            asked.append(None if roots is None else set(roots))
            return real(*args, roots=roots)

        with patch.object(provenance, "_contexts_of", spy):
            contexts = provenance.refresh_contexts(connection)
        return contexts, asked

    def test_a_new_function_does_not_make_every_fingerprint_be_worked_out_again(self, root):
        from drspec.core import provenance

        connection, functions, imports, paths = self._published(root)
        provenance.refresh_contexts(connection)
        (root / "extra.py").write_text("def brand_new(x):\n    return x\n")
        result = Scanner().scan_file("extra.py", relative_to=root)
        asked = []
        real = provenance._contexts_of

        def spy(*args, roots=None):
            asked.append(None if roots is None else set(roots))
            return real(*args, roots=roots)

        with patch.object(provenance, "_contexts_of", spy):
            sync_scan_results(connection, functions + result.functions, paths | {"extra.py"}, {**imports, **result.file_imports})
        new_id = result.functions[0].function_id
        assert asked == [{new_id}]  # the new function only: not the twelve that were there
        contexts = provenance.refresh_contexts(connection)
        assert contexts == _reference_contexts(connection) and len(contexts) == len(functions) + 1
        connection.close()

    def test_a_function_without_a_stored_fingerprint_is_worked_out_with_what_reaches_it(self, root):
        connection, functions, *_ = self._published(root)
        from drspec.core import provenance

        provenance.refresh_contexts(connection)
        expected = _reference_contexts(connection)
        connection.execute("DELETE FROM config WHERE key = ?", ["artifact:context:" + next(f.function_id for f in functions if f.name == "f3")])
        contexts, asked = self._roots_worked_out(connection)
        assert contexts == expected
        assert len(asked) == 1 and asked[0] is not None and next(f.function_id for f in functions if f.name == "f3") in asked[0]
        # (in this ring every function reaches every other, so all of them are asked for; in a project where one function
        # is called by few, only those are: the next test)
        connection.close()

    def test_only_what_reaches_a_function_without_a_fingerprint_is_asked_for(self, root):
        from drspec.core import provenance

        (root / "c.py").write_text("def leaf(x):\n    return x\n\n\ndef caller(x):\n    return leaf(x)\n\n\ndef unrelated(x):\n    return x\n")
        result = Scanner().scan_file("c.py", relative_to=root)
        connection = get_connection(root / "c.db")
        init_schema(connection)
        sync_scan_results(connection, result.functions, {"c.py"}, result.file_imports)
        provenance.refresh_contexts(connection)
        expected = _reference_contexts(connection)
        ids = {f.name: f.function_id for f in result.functions}
        connection.execute("DELETE FROM config WHERE key = ?", ["artifact:context:" + ids["leaf"]])
        contexts, asked = self._roots_worked_out(connection)
        assert contexts == expected
        assert asked == [{ids["leaf"], ids["caller"]}]  # not `unrelated`
        connection.close()

    def test_a_missing_stored_context_is_filled_in(self, root):
        from drspec.core import provenance

        connection, *_ = self._published(root)
        expected = _reference_contexts(connection)
        connection.execute("DELETE FROM config WHERE starts_with(key, 'artifact:context:')")
        assert provenance.refresh_contexts(connection) == expected
        assert connection.execute("SELECT COUNT(*) FROM config WHERE starts_with(key, 'artifact:context:')").fetchone()[0] == len(expected)
        connection.close()
