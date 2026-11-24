"""The per-file record a publish keeps (`scan:file:<path>` in `config`): what a later resolution needs, and no second copy of
what `artifacts` holds. An older database is converted once, and everything resolves as before."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest

from drspec.core import graph_sync
from drspec.core.file_records import unpack
from drspec.core.graph_sync import (
    RECORD_FORM,
    RECORD_FORM_KEY,
    _function_record,
    convert_records,
    sync_scan_results,
)
from drspec.core.scanner import Scanner
from drspec.db import init_schema
from drspec.db.queries import get_config
from tests.fakes import Counting

pytestmark = pytest.mark.component  # the real parser and a real publish of two small files into an in-memory database, 0.06-0.2 s each


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


A = "def helper(x):\n    return x + 1\n\n\ndef other(x):\n    return x\n"
B = "from a import helper\n\n\ndef use(y):\n    return helper(y)\n\n\nclass K:\n    def m(self, z):\n        return use(z)\n"


def _scan(root: Path, names):
    scanner = Scanner()
    functions, imports = [], {}
    for name in names:
        result = scanner.scan_file(name, relative_to=root)
        functions += result.functions
        imports.update(result.file_imports)
    return functions, imports


def _publish(connection, root, names, written=(), **kwargs):
    functions, imports = _scan(root, names)
    return sync_scan_results(connection, functions, set(written or names), imports, **kwargs)


def _records(connection) -> dict:
    return {key[len("scan:file:"):]: unpack(value, key[len("scan:file:"):]) for key, value in connection.execute("SELECT key, value FROM config WHERE starts_with(key, 'scan:file:')").fetchall()}


def _imports(connection) -> dict:
    return {key[len("scan:imports:"):]: json.loads(value) for key, value in connection.execute("SELECT key, value FROM config WHERE starts_with(key, 'scan:imports:')").fetchall()}


def _edges(connection):
    return connection.execute("SELECT caller_id, callee_id, confidence FROM dependencies ORDER BY 1, 2").fetchall()


def _project(root: Path):
    (root / "a.py").write_text(A)
    (root / "b.py").write_text(B)
    connection = duckdb.connect(":memory:")  # the records are rows of `config`: read back through SQL, so no file
    init_schema(connection)
    _publish(connection, root, ["a.py", "b.py"])
    return connection


def _fingerprint(connection) -> list:
    return [connection.execute("SELECT key, value FROM config ORDER BY key").fetchall(), _edges(connection),
            connection.execute("SELECT function_id, code_hash FROM artifacts ORDER BY 1").fetchall()]


@pytest.fixture(scope="module")
def _built(tmp_path_factory):
    """The project of `_project`, built ONCE for the tests that only read it, and checked at the end of the module to be as it was built."""
    folder = tmp_path_factory.mktemp("records")
    with pytest.MonkeyPatch.context() as patch_in_module:  # a module-scoped fixture has no `monkeypatch`; this puts the folder back
        patch_in_module.chdir(folder)
        connection = _project(folder)
    before = _fingerprint(connection)
    yield folder, connection
    assert _fingerprint(connection) == before, "a test changed the project that the read-only tests of the module share"
    connection.close()


@pytest.fixture
def built(_built, monkeypatch):
    """`(folder, connection)` of the shared project, with the folder as the working folder (the scanner reads the files by relative name)."""
    folder, connection = _built
    monkeypatch.chdir(folder)
    return folder, connection


def _to_the_old_form(connection, root: Path, names=("a.py", "b.py")) -> None:
    """Rewrite the records of `names` the way earlier versions kept them: every field, the body, and the symbols hash in each."""
    symbols = get_config(connection, "scan:symbols")
    current = _records(connection)
    for name in names:
        functions, imports = _scan(root, [name])
        record = {"imports_hash": current[name]["imports_hash"], "symbols_hash": symbols, "imports": graph_sync._imports_dict(imports[name]), "functions": [_function_record(f) for f in functions]}
        connection.execute("UPDATE config SET value = ? WHERE key = ?", [json.dumps(record), "scan:file:" + name])
        connection.execute("DELETE FROM config WHERE key = ?", ["scan:imports:" + name])
    connection.execute("DELETE FROM config WHERE key = ?", [RECORD_FORM_KEY])


class TestWhatTheRecordKeeps:
    def test_no_second_copy_of_the_text(self, built):
        _root, connection = built
        records = _records(connection)
        assert sorted(records) == ["a.py", "b.py"]
        for path, record in records.items():
            for function in record["functions"]:
                assert not {"body", "signature", "start_line", "end_line", "file_path", "language", "qualified_name"} & set(function), function
            assert "symbols_hash" not in record

    def test_it_keeps_what_a_resolution_needs(self, built):
        _root, connection = built
        record = _records(connection)["b.py"]
        by_name = {function["name"]: function for function in record["functions"]}
        assert by_name["use"]["calls"] and by_name["use"]["calls"][0]["name"] == "helper"
        assert by_name["m"]["is_method"] is True and by_name["m"]["parent"] == "K"
        assert record["language"] == "python" and "imports" not in record
        assert _imports(connection)["b.py"]["name_imports"]  # the imports are an entry of the file's own
        stored = connection.execute("SELECT function_id, code_hash FROM artifacts").fetchall()
        assert {(f["function_id"], f["code_hash"]) for r in _records(connection).values() for f in r["functions"]} == set(stored)
        assert sorted(function_id.split("#")[0] for function_id, _hash in stored) == ["a.py::helper", "a.py::other", "b.py::K.m", "b.py::use"]  # every function, once

    def test_a_default_is_left_out(self, built):
        _root, connection = built
        function = next(f for f in _records(connection)["a.py"]["functions"] if f["name"] == "other")
        assert set(function) <= {"function_id", "name", "code_hash", "calls", "parameter_tokens", "symbol_reads", "first_param_name", "declared_param_types"}
        assert "decorators" not in function and "is_method" not in function and "is_async" not in function

    def test_the_records_are_much_smaller_than_the_full_form(self, built):
        root, connection = built
        functions, _imports = _scan(root, ["a.py", "b.py"])
        full = sum(len(json.dumps(_function_record(f))) for f in functions)
        slim = sum(len(json.dumps(f)) for r in _records(connection).values() for f in r["functions"])
        assert slim < full * 0.55, (slim, full)

    def test_the_form_is_recorded(self, built):
        _root, connection = built
        assert get_config(connection, RECORD_FORM_KEY) == RECORD_FORM


class TestThePackedRecord:
    def test_a_record_is_stored_packed_and_reads_back_the_same(self, built):
        from drspec.core.file_records import is_packed, pack

        _root, connection = built
        stored = dict(connection.execute("SELECT key, value FROM config WHERE starts_with(key, 'scan:file:')").fetchall())
        assert stored and all(is_packed(value) for value in stored.values())
        for key, value in stored.items():
            path = key[len("scan:file:"):]
            record = unpack(value, path)
            assert unpack(pack(record, path), path) == record
            assert all(f["function_id"].startswith(path + "::") for f in record["functions"])

    def test_every_shape_of_call_and_read_survives(self):
        from drspec.core.file_records import pack

        record = {
            "imports_hash": "h", "language": "python",
            "functions": [{
                "function_id": "m.py::f#1234abcd", "name": "f", "code_hash": "c" * 64,
                "calls": [{"receiver": None, "name": "g", "text": "g(1)", "kind": "call"}, {"receiver": "o", "name": "m", "text": "o.m()", "kind": "method"}, {"name": "odd", "extra": 1}],
                "property_reads": [{"receiver": "o", "name": "a", "text": "o.a"}, {"receiver": "o", "name": "b", "text": "o.b  # padded"}, {"x": 1}],
                "symbol_reads": [{"name": "K", "text": "K"}, {"name": "L", "text": "L.attr"}, {"y": 2}],
            }, {"function_id": "elsewhere.py::g#1", "name": "g", "code_hash": "d"}],
        }
        assert unpack(pack(record, "m.py"), "m.py") == record

    def test_the_plain_form_of_an_older_database_reads_the_same(self):
        record = {"language": "python", "functions": [{"function_id": "a.py::f#1", "name": "f", "code_hash": "c"}]}
        assert unpack(json.dumps(record), "a.py") == record

    def test_it_is_a_fraction_of_the_text_it_replaces(self, root):
        from drspec.core.file_records import pack

        source = Path(graph_sync.__file__).read_text()
        (root / "big.py").write_text(source)
        result = Scanner().scan_file("big.py", relative_to=root)
        record = {"imports_hash": "h", "language": "python", "functions": [graph_sync._stored_function(f) for f in result.functions]}
        plain, packed = len(json.dumps(record)), len(pack(record, "big.py"))
        assert result.functions and packed < plain / 4, (packed, plain)

    def test_a_database_of_the_form_before_is_packed_once_and_resolves_the_same(self, root):
        connection = _project(root)
        packed, edges = _records(connection), _edges(connection)
        for key, value in connection.execute("SELECT key, value FROM config WHERE starts_with(key, 'scan:file:')").fetchall():
            connection.execute("UPDATE config SET value = ? WHERE key = ?", [json.dumps(unpack(value, key[len("scan:file:"):])), key])
        connection.execute("UPDATE config SET value = '3' WHERE key = ?", [RECORD_FORM_KEY])
        assert convert_records(connection) == 2
        assert _records(connection) == packed
        assert all(value.startswith("z1:") for (value,) in connection.execute("SELECT value FROM config WHERE starts_with(key, 'scan:file:')").fetchall())
        _publish(connection, root, ["a.py", "b.py"], written=["a.py"])
        assert _edges(connection) == edges
        connection.close()


class TestAnOlderDatabase:
    def test_it_is_converted_once(self, root):
        connection = _project(root)
        current, current_imports = _records(connection), _imports(connection)
        _to_the_old_form(connection, root)
        assert any("body" in f for r in _records(connection).values() for f in r["functions"])
        assert convert_records(connection) == 6  # two records, and the imports of two files with their digests
        assert _records(connection) == current and _imports(connection) == current_imports
        assert get_config(connection, RECORD_FORM_KEY) == RECORD_FORM
        assert convert_records(connection) == 0  # nothing left to do
        connection.close()

    def test_a_publish_converts_it_first_and_a_second_one_changes_no_record(self, root):
        connection = _project(root)
        before, before_imports = _records(connection), _imports(connection)
        _to_the_old_form(connection, root)
        _publish(connection, root, ["a.py", "b.py"], written=["a.py"])
        assert _records(connection) == before and _imports(connection) == before_imports
        with patch.object(graph_sync, "set_config_many", wraps=graph_sync.set_config_many) as spy:
            _publish(connection, root, ["a.py", "b.py"], written=["a.py"])
        assert not [row for call in spy.call_args_list for row in call.args[1] if row[0].startswith("scan:file:") and row[0] != "scan:file:a.py"]
        connection.close()

    def test_a_partly_converted_database_is_not_trusted(self, root):
        connection = _project(root)
        current = _records(connection)
        _to_the_old_form(connection, root, names=("a.py",))  # a.py is in the old form, b.py is already packed, and the form key is missing
        assert any("body" in f for f in _records(connection)["a.py"]["functions"])
        assert convert_records(connection) == 3  # a.py's record and its imports with their digest: whatever is still old is found and converted, the packed one is left
        assert _records(connection) == current
        connection.close()


class TestResolvingAfterARename:
    """`use` is in a file that did not change; `helper` is renamed in the one that did. The caller is found again from
    its record, the stale edge goes, and the counts are the ones the older form gave."""

    def _run(self, root, old_form: bool):
        connection = _project(root)
        assert any(callee.endswith("helper#" + callee.rsplit("#", 1)[1]) or "::helper#" in callee for _caller, callee, _c in _edges(connection))
        if old_form:
            _to_the_old_form(connection, root)
        (root / "a.py").write_text(A.replace("def helper(", "def helper2("))
        stats = _publish(connection, root, ["a.py", "b.py"], written=["a.py"])
        result = (_edges(connection), stats.neighbours_re_resolved, stats.edges_deleted, stats.edges_inserted, dict(stats.resolution_reasons))
        connection.close()
        return result

    def test_the_same_edges_and_counts_in_either_form(self, root, tmp_path_factory, monkeypatch):
        new_form = self._run(root, False)
        other = tmp_path_factory.mktemp("old")
        monkeypatch.chdir(other)
        old_form = self._run(other, True)
        assert new_form[1] >= 1 and new_form[2] >= 1
        assert [(c.split("::", 1)[1], d.split("::", 1)[1]) for c, d, _ in new_form[0]] == [(c.split("::", 1)[1], d.split("::", 1)[1]) for c, d, _ in old_form[0]]
        assert new_form[1:4] == old_form[1:4]
        assert {k.split("::", 1)[1]: v for k, v in new_form[4].items()} == {k.split("::", 1)[1]: v for k, v in old_form[4].items()}

    def test_adding_a_function_rewrites_only_the_records_of_the_scanned_files(self, root):
        connection = _project(root)
        (root / "a.py").write_text(A + "\n\ndef added():\n    return 3\n")
        with patch.object(graph_sync, "set_config_many", wraps=graph_sync.set_config_many) as spy:
            _publish(connection, root, ["a.py", "b.py"], written=["a.py"])
        written = {row[0] for call in spy.call_args_list for row in call.args[1] if row[0].startswith("scan:file:")}
        assert written == {"scan:file:a.py"}
        connection.close()


class TestAPublishOfOneFileDoesNotReadEveryRecord:
    def test_no_statement_asks_for_all_the_records(self, root):
        connection = _project(root)
        counting = Counting(connection)
        functions, imports = _scan(root, ["a.py"])
        sync_scan_results(counting, functions, {"a.py"}, imports, force=True)
        asked = [shape for shape in counting.shapes if "scan:file:" in shape and "starts_with" in shape]
        assert not asked, asked
        connection.close()
