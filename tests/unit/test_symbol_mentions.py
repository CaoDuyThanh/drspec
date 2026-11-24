"""A change of the project's symbols re-resolves the functions it can touch, and reads only the files that mention a
changed name: the stored result is the one reading every file gives."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest

from drspec.core import graph_sync
from drspec.core.graph_sync import MENTIONS_PREFIX, _may_be_affected, _mentions_of, sync_scan_results
from drspec.core.scanner import Scanner
from drspec.db import init_schema

FILES = {
    "a.py": "def target():\n    return 1\n\n\ndef other():\n    return 2\n",
    "b.py": "from a import target\n\n\ndef uses_target():\n    return target()\n",
    "c.py": "def alone():\n    return helper()\n\n\ndef helper():\n    return 3\n",
    "d.py": "import a\n\n\ndef through_module():\n    return a.target()\n",
    "e.py": "def unrelated_one():\n    return 4\n\n\ndef unrelated_two():\n    return unrelated_one()\n",
    "f.py": "class Thing:\n    def go(self):\n        return self.stop()\n\n    def stop(self):\n        return 5\n",
}


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _scan(root: Path, names):
    scanner = Scanner()
    functions, imports = [], {}
    for name in names:
        result = scanner.scan_file(name, relative_to=root)
        functions += result.functions
        imports.update(result.file_imports)
    return functions, imports, set(names)


def _write(root: Path, files: dict) -> None:
    for name, text in files.items():
        (root / name).write_text(text)


def _published(root: Path, db: str, files: dict):
    _write(root, files)
    connection = duckdb.connect(":memory:")  # (`db` only names the database in the test; what is compared is what SQL holds, not a file)
    init_schema(connection)
    functions, imports, paths = _scan(root, sorted(files))
    sync_scan_results(connection, functions, paths, imports)
    return connection


def _edges(connection):
    return (
        connection.execute("SELECT caller_id, callee_id, confidence FROM dependencies ORDER BY 1, 2, 3").fetchall(),
        connection.execute("SELECT caller_id, callee_id, confidence FROM code_references ORDER BY 1, 2, 3").fetchall(),
    )


def _publish_change(root: Path, connection, names, stats_out=None):
    functions, imports, paths = _scan(root, names)
    stats = sync_scan_results(connection, functions, paths, imports)
    if stats_out is not None:
        stats_out.append(stats)
    return stats


class TestWhatAFileMentions:
    def test_calls_receivers_reads_annotations_and_imports_are_mentioned(self, root):
        _write(root, {"m.py": "from pkg.mod import Thing as T, helper\nimport other.lib as lib\n\n\ndef f(x: Foo.Bar):\n    return x.attr + helper() + lib.run(T)\n"})
        functions, imports, _ = _scan(root, ["m.py"])
        mentions = _mentions_of(functions, graph_sync._imports_dict(imports["m.py"]))
        assert {"helper", "run", "lib", "attr", "x", "Foo", "Bar", "Thing", "T"} <= set(mentions["n"])
        assert {"mod", "lib"} <= set(mentions["s"])

    def test_javascript_is_always_looked_at(self, root):  # (a Rust file names what it calls and re-exports: see test_rust_reach.py)
        _write(root, {"m.js": "export function f() { return g(); }\n"})
        functions, imports, _ = _scan(root, ["m.js"])
        assert _mentions_of(functions, graph_sync._imports_dict(imports["m.js"])) == {"all": True}
        assert _may_be_affected(json.dumps({"all": True}), {"zzz"}, set()) is True

    def test_a_file_with_none_of_the_names_is_not_affected_and_unknown_means_yes(self):
        stored = json.dumps({"n": ["a", "b"], "s": ["s1"]})
        assert _may_be_affected(stored, {"c"}, {"s2"}) is False
        assert _may_be_affected(stored, {"b"}, set()) is True
        assert _may_be_affected(stored, set(), {"s1"}) is True
        assert _may_be_affected(None, {"c"}, set()) is True
        assert _may_be_affected("not json", {"c"}, set()) is True

    def test_the_mentions_are_stored_with_each_file(self, root):
        connection = _published(root, "m.db", FILES)
        keys = {key for (key,) in connection.execute("SELECT key FROM config WHERE starts_with(key, ?)", [MENTIONS_PREFIX]).fetchall()}
        assert keys == {MENTIONS_PREFIX + name for name in FILES}
        connection.close()


class TestTheSameResultWhileReadingFewerFiles:
    CHANGE = {"g.py": "def target():\n    return 'a second one'\n"}  # a second function named like one that others call

    def _step(self, root, db: str, prefilter: bool, drop_mentions: bool = False):
        connection = _published(root, db, FILES)
        if drop_mentions:
            connection.execute("DELETE FROM config WHERE starts_with(key, ?)", [MENTIONS_PREFIX])
        _write(root, self.CHANGE)
        unpacked = []
        real = graph_sync.unpack

        def spy(value, file_path):
            unpacked.append(file_path)
            return real(value, file_path)

        patches = [patch.object(graph_sync, "unpack", spy)]
        if not prefilter:
            patches.append(patch.object(graph_sync, "_may_be_affected", lambda *args: True))
        for item in patches:
            item.start()
        try:
            stats = _publish_change(root, connection, ["g.py"])
        finally:
            for item in patches:
                item.stop()
        return connection, stats, unpacked

    def test_a_new_function_with_a_called_name_gives_the_same_edges(self, root):
        filtered, filtered_stats, read_filtered = self._step(root, "filtered.db", prefilter=True)
        everything, everything_stats, read_all = self._step(root, "everything.db", prefilter=False)
        fresh = _published(root, "fresh.db", {**FILES, **self.CHANGE})
        assert _edges(filtered) == _edges(everything) == _edges(fresh)
        assert filtered_stats.neighbours_re_resolved == everything_stats.neighbours_re_resolved > 0
        # the files that mention `target` are read; the ones that do not are left alone
        assert {"b.py", "d.py"} <= set(read_filtered)
        assert not {"c.py", "e.py", "f.py"} & set(read_filtered)
        assert set(read_all) >= {"c.py", "e.py", "f.py"}
        for connection in (filtered, everything, fresh):
            connection.close()

    def test_a_database_without_mentions_reads_everything_once_and_writes_them(self, root):
        connection, stats, read = self._step(root, "old.db", prefilter=True, drop_mentions=True)
        reference, reference_stats, _ = self._step(root, "reference.db", prefilter=False)
        assert {"c.py", "e.py", "f.py"} <= set(read)  # nothing recorded: every file is looked at
        assert _edges(connection) == _edges(reference)
        assert stats.neighbours_re_resolved == reference_stats.neighbours_re_resolved
        keys = {key for (key,) in connection.execute("SELECT key FROM config WHERE starts_with(key, ?)", [MENTIONS_PREFIX]).fetchall()}
        assert {MENTIONS_PREFIX + name for name in FILES} <= keys
        # and the next change reads only what mentions it
        (root / "h.py").write_text("def target():\n    return 'third'\n")
        read.clear()
        real = graph_sync.unpack
        with patch.object(graph_sync, "unpack", lambda value, path: read.append(path) or real(value, path)):
            _publish_change(root, connection, ["h.py"])
        assert {"b.py", "d.py"} <= set(read) and not {"c.py", "e.py", "f.py"} & set(read)
        connection.close()
        reference.close()

    def test_a_removed_function_is_followed_the_same_way(self, root):
        results = []
        for prefilter in (True, False):
            connection = _published(root, f"rm{prefilter}.db", FILES)
            _write(root, {"a.py": "def other():\n    return 2\n"})  # `target` is gone from a.py
            with patch.object(graph_sync, "_may_be_affected", (lambda *args: True) if not prefilter else _may_be_affected):
                _publish_change(root, connection, ["a.py"])
            results.append(_edges(connection))
            connection.close()
            _write(root, {"a.py": FILES["a.py"]})
        assert results[0] == results[1]

    def test_an_import_stem_that_appears_is_noticed(self, root):
        results = []
        for prefilter in (True, False):
            connection = _published(root, f"st{prefilter}.db", FILES)
            _write(root, {"a_two.py": "def target():\n    return 9\n", "a2.py": "x = 1\n"})
            with patch.object(graph_sync, "_may_be_affected", (lambda *args: True) if not prefilter else _may_be_affected):
                _publish_change(root, connection, ["a_two.py", "a2.py"])
            results.append(_edges(connection))
            connection.close()
            for name in ("a_two.py", "a2.py"):
                (root / name).unlink()
        assert results[0] == results[1]



def _per_file_keys(connection, name):
    return {key for (key,) in connection.execute("SELECT key FROM config WHERE key LIKE ?", [f"%{name}"]).fetchall() if key.startswith("scan:")}


def _sync_as_the_worker(connection, functions, paths, imports, **kwargs):
    """A publish as the worker makes it: the removals it records are carried out when the transaction ends."""
    from drspec.db.staging import publish_transaction

    with publish_transaction(connection) as write_conn:
        return sync_scan_results(write_conn, functions, paths, imports, **kwargs)


class TestADeletedFileLeavesNothingBehind:
    def test_its_record_imports_and_mentions_are_removed_and_the_rest_equals_a_fresh_index(self, root):
        connection = _published(root, "gone.db", FILES)
        assert _per_file_keys(connection, "b.py") == {"scan:file:b.py", "scan:imports:b.py", "scan:imports_sig:b.py", MENTIONS_PREFIX + "b.py"}
        (root / "b.py").unlink()
        _sync_as_the_worker(connection, [], {"b.py"}, {}, deleted_paths={"b.py"})
        assert _per_file_keys(connection, "b.py") == set()
        remaining = {name: text for name, text in FILES.items() if name != "b.py"}
        fresh = _published(root, "fresh.db", remaining)
        def keys(connection):
            return {
                key
                for (key,) in connection.execute(
                    "SELECT key FROM config WHERE key LIKE 'scan:%' OR key LIKE 'artifact:context:%' OR key LIKE 'provenance:%'"
                ).fetchall()
            }
        assert keys(connection) == keys(fresh)
        assert _edges(connection) == _edges(fresh)
        connection.close()
        fresh.close()

    def test_a_file_that_still_exists_with_no_functions_keeps_its_entries(self, root):
        connection = _published(root, "empty.db", FILES)
        _write(root, {"e.py": "# nothing left\n"})
        _sync_as_the_worker(connection, [], {"e.py"}, {})
        assert "scan:file:e.py" in _per_file_keys(connection, "e.py")
        connection.close()

    def test_the_record_of_what_was_seen_drops_the_functions_that_are_gone(self, root):
        from drspec.core.provenance import INPUTS_KEY

        connection = _published(root, "inputs.db", FILES)
        before = set(json.loads(connection.execute("SELECT value FROM config WHERE key = ?", [INPUTS_KEY]).fetchone()[0])["signatures"])
        (root / "e.py").unlink()  # its two functions are called by nothing else
        _sync_as_the_worker(connection, [], {"e.py"}, {}, deleted_paths={"e.py"})
        after = set(json.loads(connection.execute("SELECT value FROM config WHERE key = ?", [INPUTS_KEY]).fetchone()[0])["signatures"])
        live = {row[0] for row in connection.execute("SELECT function_id FROM artifacts").fetchall()}
        assert len(before) > len(after) and after == live
        connection.close()

    def test_the_worker_passes_the_files_that_are_gone(self, root):
        from drspec.staleness_worker import _remediate_queued_files

        connection = _published(root, "w.db", FILES)
        (root / "d.py").unlink()
        db_path = root / "_drspec" / "contracts.db"
        # the worker's own path: a queued file that no longer exists
        db_path.parent.mkdir(exist_ok=True)
        _remediate_queued_files(connection, db_path, ["d.py"])
        assert _per_file_keys(connection, "d.py") == set()
        assert connection.execute("SELECT COUNT(*) FROM artifacts WHERE file_path = 'd.py'").fetchone()[0] == 0
        connection.close()
