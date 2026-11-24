"""Identifiers that carry the hash of the function's parameters; databases from before are rebuilt."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import List

import duckdb
import pytest

from tests.helpers import publish as sync_scan_results
from tests.helpers import strip_identity_hash as _strip
from drspec.core.scanner import Scanner
from drspec.db import get_connection, init_schema, insert_artifact
from drspec.db.identity_migration import (
    BACKUP_SUFFIX,
    MARKER_KEY,
    ID_COLUMNS,
    backup_path,
    compute_new_ids,
    ensure_identity_layout,
    needs_identity_migration,
)
from drspec.db.migration import MigrationError
from drspec.db.queries import (
    insert_contract,
    insert_reasoning_trace,
    insert_vision_finding,
    set_config,
    upsert_dependency_edge,
)
from drspec.learning.history import LearningEvent, init_learning_schema, insert_learning_event
from tests.helpers import fid

_HASH = re.compile(r"#[0-9a-f]{8}")


def _vector(value: float) -> List[float]:
    return [float(value)] * 384


def _tables(path: Path) -> dict:
    connection = duckdb.connect(str(path), read_only=True)
    try:
        names = [row[0] for row in connection.execute("SELECT table_name FROM duckdb_tables()").fetchall()]
        return {name: sorted(map(repr, connection.execute(f"SELECT * FROM {name}").fetchall())) for name in names}
    finally:
        connection.close()


def _old_database(path: Path) -> None:
    """A database as it was before: identifiers without a hash, a row in every table that holds one."""
    connection = get_connection(path)
    init_schema(connection)
    connection.execute("BEGIN TRANSACTION")  # one commit for the rows below, not one per statement (each commit waits for the disk)
    bodies = {
        "a": ("def a(x: int, y=2):\n    return b(x)", "python"),
        "b": ("def b(n):\n    return n", "python"),
        "K.run": ("def run(self, q: str) -> None:\n        pass", "python"),
    }
    for name, (body, language) in bodies.items():
        short = name.split(".")[-1]
        insert_artifact(
            connection, function_id=f"x.py::{name}", file_path="x.py", function_name=short, signature=body.splitlines()[0],
            body=body, code_hash=f"h{short}", language=language, start_line=1, end_line=2,
            parent="K" if "." in name else None, embedding=_vector(1) if name == "a" else None,
        )
    upsert_dependency_edge(connection, "x.py::a", "x.py::b", "b(x)", "e1", _vector(2), "EXTRACTED")
    connection.execute(
        "INSERT INTO code_references (caller_id, callee_id, reference_site, edge_hash, embedding, confidence) VALUES ('x.py::a', 'x.py::K.run', 'k.run', 'r1', NULL, 'EXTRACTED')"
    )
    insert_contract(connection, "x.py::a", json.dumps({"intent": "adds"}), 0.8)
    insert_reasoning_trace(connection, "x.py::a", "proposer", "{}")
    insert_vision_finding(connection, "x.py::a", "outlier", "HIGH", "odd")
    init_learning_schema(connection)
    insert_learning_event(connection, LearningEvent(commit_sha="abc123", function_id="x.py::a", pattern_description="p"))
    connection.execute("INSERT INTO usage_stats (kind, item_id, related_id, score) VALUES ('node', 'x.py::a', '', 3.5)")
    connection.execute("INSERT INTO usage_stats (kind, item_id, related_id, score) VALUES ('edge', 'x.py::a', 'x.py::b', 1.5)")
    set_config(connection, "embedding:" + json.dumps(["dependencies", "x.py::a", "x.py::b"]), json.dumps(["model", "e1"]))
    set_config(connection, "embedding:" + json.dumps(["artifacts", "x.py::a"]), json.dumps(["model", "ha"]))
    set_config(connection, "contract:source:x.py::a", "ha")
    set_config(connection, "artifact:context:x.py::a", "ctx")
    set_config(connection, "artifact:stale_reason:x.py::b", "IMPORTS_CHANGED")
    set_config(connection, "verification:x.py::a", json.dumps({"ok": True}))
    set_config(connection, "worker:embedding_state", json.dumps({"state": "idle"}))
    connection.execute("COMMIT")
    connection.execute("CHECKPOINT")
    connection.close()


@pytest.fixture(scope="module")
def _old_template(tmp_path_factory):
    """The old database, built once (about a second of DuckDB statements and a checkpoint); each test copies the file."""
    path = tmp_path_factory.mktemp("old") / "contracts.db"
    _old_database(path)
    return path


@pytest.fixture
def old(tmp_path, _old_template):
    path = tmp_path / "_drspec" / "contracts.db"
    path.parent.mkdir()
    shutil.copyfile(_old_template, path)
    return path


@pytest.fixture(scope="module")
def _current_template(tmp_path_factory):
    """A database of the current layout with one artifact, built once for the module (the schema is the slow part); each test copies it."""
    path = tmp_path_factory.mktemp("current") / "contracts.db"
    connection = get_connection(path)
    init_schema(connection)
    insert_artifact(
        connection, function_id=fid("x.py", "a"), file_path="x.py", function_name="a", signature="def a():",
        body="def a(): pass", code_hash="h", language="python", start_line=1, end_line=2,
    )
    connection.close()
    return path


NEW_A = fid("x.py", "a", "int", "y")
NEW_B = fid("x.py", "b", "n")
NEW_RUN = fid("x.py", "K.run", "str")


class TestRebuildingAnOldDatabase:
    def test_the_fixture_really_has_identifiers_without_a_hash(self, old):
        connection = duckdb.connect(str(old), read_only=True)
        try:
            assert needs_identity_migration(connection)
        finally:
            connection.close()

    def test_every_table_and_key_that_holds_an_identifier_is_rewritten(self, old):
        before = _tables(old)
        assert ensure_identity_layout(old) == "migrated"
        connection = duckdb.connect(str(old), read_only=True)
        try:
            ids = {row[0] for row in connection.execute("SELECT function_id FROM artifacts").fetchall()}
            assert ids == {NEW_A, NEW_B, NEW_RUN}
            assert connection.execute("SELECT caller_id, callee_id FROM dependencies").fetchall() == [(NEW_A, NEW_B)]
            assert connection.execute("SELECT caller_id, callee_id FROM code_references").fetchall() == [(NEW_A, NEW_RUN)]
            for table in ("contracts", "reasoning_traces", "vision_findings", "learning_history"):
                assert connection.execute(f"SELECT function_id FROM {table}").fetchall() == [(NEW_A,)], table
            assert sorted(connection.execute("SELECT kind, item_id, related_id FROM usage_stats").fetchall()) == sorted(
                [("node", NEW_A, ""), ("edge", NEW_A, NEW_B)]
            )
            keys = {row[0] for row in connection.execute("SELECT key FROM config").fetchall()}
            assert "contract:source:" + NEW_A in keys and "artifact:context:" + NEW_A in keys and "verification:" + NEW_A in keys
            assert "artifact:stale_reason:" + NEW_B in keys
            assert "embedding:" + json.dumps(["dependencies", NEW_A, NEW_B]) in keys
            assert "embedding:" + json.dumps(["artifacts", NEW_A]) in keys
            assert "worker:embedding_state" in keys and MARKER_KEY in keys
            assert not [key for key in keys if "x.py::a" in key and not _HASH.search(key)]
        finally:
            connection.close()
        # nothing else changed: every other row is byte for byte what it was
        after = _tables(old)
        for table in ("scanned_files", "staleness_queue", "rescan_backoff"):
            assert after[table] == before[table]

    def test_row_counts_vectors_and_sequences_are_kept(self, old):
        connection = duckdb.connect(str(old), read_only=True)
        counts = {name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in (*ID_COLUMNS, "usage_stats", "config")}
        connection.close()
        ensure_identity_layout(old)
        connection = duckdb.connect(str(old))
        try:
            after = {name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in counts}
            assert after == {**counts, "config": counts["config"] + 1}  # the marker
            assert connection.execute("SELECT len(embedding) FROM artifact_embeddings WHERE function_id = ?", [NEW_A]).fetchone() == (384,)
            assert connection.execute("SELECT len(embedding) FROM dependencies").fetchone() == (384,)
            insert_reasoning_trace(connection, NEW_A, "critic", "{}")
            assert connection.execute("SELECT COUNT(*) FROM reasoning_traces").fetchone() == (2,)  # the sequence went on
        finally:
            connection.close()

    def test_the_old_file_is_kept_until_the_next_start_and_a_second_call_changes_nothing(self, old):
        original = old.read_bytes()
        assert ensure_identity_layout(old) == "migrated"
        assert backup_path(old).read_bytes() == original and str(backup_path(old)).endswith(BACKUP_SUFFIX)
        migrated = _tables(old)
        assert ensure_identity_layout(old) == "current"
        assert not backup_path(old).exists()
        assert _tables(old) == migrated

    def test_an_interrupted_rebuild_leaves_the_old_file_untouched(self, old, monkeypatch):
        original = old.read_bytes()

        def fail(*_args, **_kwargs):
            raise OSError("disk gone")

        monkeypatch.setattr("drspec.db.migration.os.replace", fail)
        with pytest.raises(MigrationError, match="not changed"):
            ensure_identity_layout(old)
        assert old.read_bytes() == original
        assert not [p for p in old.parent.iterdir() if p.name.startswith(".drspec-scan-")]

    def test_a_current_database_is_left_alone_and_marked(self, tmp_path, _current_template):
        path = tmp_path / "_drspec" / "contracts.db"
        path.parent.mkdir()
        shutil.copyfile(_current_template, path)
        assert ensure_identity_layout(path) == "current"
        connection = duckdb.connect(str(path), read_only=True)
        try:
            assert connection.execute("SELECT function_id FROM artifacts").fetchall() == [(fid("x.py", "a"),)]
            assert connection.execute("SELECT value FROM config WHERE key = ?", [MARKER_KEY]).fetchone() == ("v1",)
        finally:
            connection.close()
        assert not backup_path(path).exists()

    def test_a_missing_file_is_absent(self, tmp_path):
        assert ensure_identity_layout(tmp_path / "nothing.db") == "absent"


class TestTheNewIdentifiers:
    def test_they_equal_what_a_fresh_parse_gives(self, tmp_path):
        source = (
            "import os\n\n\ndef top(a, b: int = 1, *rest, **extra):\n    return a\n\n\n"
            "class K:\n    @property\n    def v(self):\n        return 1\n\n"
            "    @staticmethod\n    def s(x: 'List[int]'):\n        pass\n\n"
            "    async def run(self, a: dict[str, int], *, flag=False):\n        pass\n"
        )
        (tmp_path / "m.py").write_text(source)
        result = Scanner().scan_file(str(tmp_path / "m.py"), relative_to=tmp_path)
        rows = []
        for f in result.functions:
            old_id = f.function_id.rsplit("#", 1)[0]
            rows.append((old_id, f.name, f.language, f.body))
        mapping, _tokens, unread = compute_new_ids(rows)
        assert unread == []
        assert set(mapping.values()) == {f.function_id for f in result.functions}

    @pytest.mark.parametrize(
        "language,name,body,expected",
        [
            ("javascript", "helper", "function helper(a, b = 2, ...rest) { return a; }", ["a", "b", "...rest"]),
            ("javascript", "arrow", "const arrow = (x, {y, z}) => x;", ["x", "{y,z}"]),
            ("javascript", "method", "method(a, b) { return a; }", ["a", "b"]),
            ("cpp", "add", "int add(int a, double b) { return a; }", ["int", "double"]),
            ("cpp", "mul", "int mul(const std::string &s, char* p) const;", ["const std::string&", "char*", "const"]),
        ],
    )
    def test_other_languages_read_back_from_their_stored_text(self, language, name, body, expected):
        mapping, tokens, unread = compute_new_ids([(f"f.x::{name}", name, language, body)])
        assert unread == [] and tokens[f"f.x::{name}"] == expected

    def test_a_body_that_cannot_be_read_back_gets_the_hash_of_no_parameters(self):
        mapping, tokens, unread = compute_new_ids([("f.py::g", "g", "python", "not python at all ((")])
        assert unread == ["f.py::g"] and mapping["f.py::g"] == fid("f.py", "g")

    def test_a_cpp_overload_pair_keeps_one_identifier_each(self):
        rows = [
            ("c.cpp::add", "add", "cpp", "int add(int a, int b) { return a + b; }"),
            ("c.cpp::sub", "sub", "cpp", "int sub(int a) { return a; }"),
        ]
        mapping, _, _ = compute_new_ids(rows)
        assert mapping["c.cpp::add"] != mapping["c.cpp::sub"]


class TestAgainstARealScan:
    @pytest.mark.slow
    def test_migrating_gives_the_database_a_scan_gives(self, tmp_path, monkeypatch):
        root = tmp_path / "project"
        root.mkdir()
        monkeypatch.chdir(root)
        (root / "a.py").write_text("from b import helper\n\n\nclass K:\n    @property\n    def v(self):\n        return 1\n\n    def run(self, x: int):\n        return helper(x) + self.v\n")
        (root / "b.py").write_text("def helper(n):\n    return n\n\n\ndef unused():\n    return 0\n")
        scanned = root / "scanned.db"
        connection = get_connection(scanned)
        init_schema(connection)
        scanner = Scanner()
        functions, imports = [], {}
        for path in ("a.py", "b.py"):
            result = scanner.scan_file(str(root / path), relative_to=root)
            functions.extend(result.functions)
            imports.update(result.file_imports)
        sync_scan_results(connection, functions=functions, scanned_file_paths={"a.py", "b.py"}, file_imports=imports)
        insert_contract(connection, fid("a.py", "K.run", "int"), json.dumps({"intent": "x"}), 0.9)
        connection.execute("CHECKPOINT")
        connection.close()
        assert _edge_rows(scanned)["dependencies"], "the scan should have found the call"

        old = tmp_path / "old.db"
        _strip(scanned, old)
        connection = duckdb.connect(str(old), read_only=True)
        assert needs_identity_migration(connection)
        connection.close()

        assert ensure_identity_layout(old) == "migrated"
        assert _edge_rows(old) == _edge_rows(scanned)
        migrated, expected = _tables(old), _tables(scanned)
        for table in ("artifacts", "contracts", "dependencies", "code_references"):
            assert migrated[table] == expected[table], table
        assert _config(old) == {**_config(scanned), MARKER_KEY: "v1"}


def _edge_rows(path: Path) -> dict:
    connection = duckdb.connect(str(path), read_only=True)
    try:
        return {
            "dependencies": sorted(connection.execute("SELECT caller_id, callee_id, edge_hash, confidence FROM dependencies").fetchall()),
            "code_references": sorted(connection.execute("SELECT caller_id, callee_id, edge_hash, confidence FROM code_references").fetchall()),
        }
    finally:
        connection.close()


def _config(path: Path) -> dict:
    connection = duckdb.connect(str(path), read_only=True)
    try:
        return dict(connection.execute("SELECT key, value FROM config").fetchall())
    finally:
        connection.close()


class TestWhereItRuns:
    def test_a_worker_start_rebuilds_an_old_database(self, old, monkeypatch):
        from drspec.staleness_worker import run_worker

        monkeypatch.chdir(old.parent.parent)
        monkeypatch.setattr("drspec.staleness_worker._wait_for_wake", lambda event, seconds: False)  # (the loop's idle waits are not slept)
        from drspec.embedding_supervisor import EmbeddingSupervisor

        # (the worker would start its embedding process for the old database's backlog: a process that is not what is tested here)
        monkeypatch.setattr("drspec.staleness_worker.EmbeddingSupervisor", lambda: EmbeddingSupervisor(spawn=lambda db_path, threads=1: type("P", (), {"poll": lambda self: None})()))
        run_worker(old, idle_timeout=0.2, poll_interval=0.1)
        connection = duckdb.connect(str(old), read_only=True)
        try:
            assert {r[0] for r in connection.execute("SELECT function_id FROM artifacts").fetchall()} == {NEW_A, NEW_B, NEW_RUN}
        finally:
            connection.close()

    def test_a_worker_refuses_to_start_when_the_rebuild_fails(self, old, monkeypatch):
        from drspec.staleness_worker import read_startup_error, run_worker

        monkeypatch.chdir(old.parent.parent)

        def fail(*_args, **_kwargs):
            raise OSError("disk gone")

        monkeypatch.setattr("drspec.db.migration.os.replace", fail)
        with pytest.raises(MigrationError):
            run_worker(old, idle_timeout=0.2, poll_interval=0.1)
        assert "not changed" in read_startup_error(old)
        connection = duckdb.connect(str(old), read_only=True)
        try:
            assert {r[0] for r in connection.execute("SELECT function_id FROM artifacts").fetchall()} == {"x.py::a", "x.py::b", "x.py::K.run"}
        finally:
            connection.close()

    def test_init_on_an_existing_project_rebuilds_it(self, old, tmp_path, monkeypatch):
        from typer.testing import CliRunner

        from drspec.cli.app import app

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        database = old  # (the existing project: a `_drspec` folder holding the old database)
        result = runner.invoke(app, ["--json", "init", "--project-root", str(tmp_path), "--no-ide", "--non-interactive"])
        assert result.exit_code == 0, result.output
        connection = duckdb.connect(str(database), read_only=True)
        try:
            assert NEW_A in {r[0] for r in connection.execute("SELECT function_id FROM artifacts").fetchall()}
        finally:
            connection.close()
