"""`refs get`, `deps get` and a function search say when a file their answer comes from has changed since the last scan (edited, deleted or a
new file beside them): one line in `note` and a `changed_since_scan` field, from the stored size and mtime against the disk, nothing opened.
In memory: the check on a database with a few rows and files in a tmp folder; the commands on one small project scanned once for the module."""

from __future__ import annotations

import contextlib
import json
import os

import duckdb
import pytest
from typer.testing import CliRunner

from drspec import mcp_server
from drspec.cli.app import app
from drspec.core import compact_format
from drspec.core.changed_notice import add_changed_notice, changed_note, changed_since_scan
from drspec.db import init_schema

runner = CliRunner()

CTX_CPP = """struct Ctx { double counter = 0.0; };
void up(Ctx& c) { c.counter += 1.0; }
void down(Ctx& c) { c.counter -= 1.0; }
"""
CALLER_CPP = "#include \"ctx.cpp\"\nvoid both(Ctx& c) { up(c); down(c); }\n"


# -- the check itself: a database with rows and files in a tmp folder --------------------------------------------------------------------


@pytest.fixture
def conn():
    connection = duckdb.connect(":memory:")
    init_schema(connection)
    yield connection
    connection.close()


def _scanned(connection, root, *names):
    """Files written to `root` and recorded as scanned with their real size and mtime."""
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"void {name.split('.')[0].split('/')[-1]}() {{}}\n")
        stat = path.stat()
        connection.execute("INSERT INTO scanned_files (file_path, mtime, size) VALUES (?, ?, ?)", [name, stat.st_mtime, stat.st_size])


class TestWhatChangedSinceTheScan:
    def test_nothing_changed_says_nothing(self, conn, tmp_path):
        _scanned(conn, tmp_path, "a.cpp", "src/b.cpp")
        changes = changed_since_scan(conn, tmp_path, ["a.cpp", "src/b.cpp"])
        assert changes == {"edited": [], "deleted": [], "added": [], "being_read": []} and changed_note(changes) is None
        data = {"x": 1}
        assert add_changed_notice(conn, data, tmp_path, ["a.cpp"]) == {"x": 1}

    def test_an_edited_file_by_size(self, conn, tmp_path):
        _scanned(conn, tmp_path, "a.cpp", "b.cpp")
        (tmp_path / "a.cpp").write_text("void a() { more(); }\n")
        assert changed_since_scan(conn, tmp_path, ["a.cpp", "b.cpp"])["edited"] == ["a.cpp"]

    def test_an_edited_file_by_time_alone(self, conn, tmp_path):
        _scanned(conn, tmp_path, "a.cpp")
        stat = (tmp_path / "a.cpp").stat()
        os.utime(tmp_path / "a.cpp", ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
        assert changed_since_scan(conn, tmp_path, ["a.cpp"])["edited"] == ["a.cpp"]

    def test_a_deleted_file(self, conn, tmp_path):
        _scanned(conn, tmp_path, "a.cpp", "b.cpp")
        (tmp_path / "b.cpp").unlink()
        changes = changed_since_scan(conn, tmp_path, ["a.cpp", "b.cpp"])
        assert changes["deleted"] == ["b.cpp"] and changes["edited"] == []

    def test_a_new_file_beside_the_files_of_the_answer(self, conn, tmp_path):
        _scanned(conn, tmp_path, "src/a.cpp", "src/b.cpp")
        (tmp_path / "src" / "late.cpp").write_text("void late() {}\n")
        (tmp_path / "src" / "notes.txt").write_text("not code\n")
        (tmp_path / "other").mkdir()
        (tmp_path / "other" / "far.cpp").write_text("void far() {}\n")  # (a folder none of the answer's files is in: not looked at)
        assert changed_since_scan(conn, tmp_path, ["src/a.cpp"])["added"] == ["src/late.cpp"]

    def test_a_new_file_the_project_ignores_is_not_new(self, conn, tmp_path):
        _scanned(conn, tmp_path, "a.cpp")
        (tmp_path / "__pycache__").mkdir()  # (a folder drspec never reads: no file inside is a candidate)
        (tmp_path / "__pycache__" / "x.py").write_text("x = 1\n")
        assert changed_since_scan(conn, tmp_path, ["a.cpp"])["added"] == []

    def test_a_file_without_a_snapshot_that_was_asked_about_is_left_out(self, conn, tmp_path):
        assert changed_since_scan(conn, tmp_path, ["never_scanned.cpp"], look_for_new=False) == {"edited": [], "deleted": [], "added": [], "being_read": []}

    def test_the_line_names_each_change_and_stops_at_five(self, conn, tmp_path):
        names = [f"f{i}.cpp" for i in range(7)]
        _scanned(conn, tmp_path, *names)
        for name in names:
            (tmp_path / name).write_text("changed\n" * 3)
        (tmp_path / "f6.cpp").unlink()
        note = changed_note(changed_since_scan(conn, tmp_path, names, look_for_new=False))
        assert note.startswith("7 indexed file(s) changed since the last scan (f0.cpp, f1.cpp, f2.cpp, f3.cpp, f4.cpp, ...): ")
        assert note.endswith("results may be outdated; drspec scan <paths> updates them.")

    def test_the_line_marks_a_deleted_and_a_new_file(self):
        note = changed_note({"edited": ["a.cpp"], "deleted": ["b.cpp"], "added": ["c.cpp"]})
        assert "3 indexed file(s) changed since the last scan (a.cpp, b.cpp (deleted), c.cpp (new))" in note

    def test_the_notice_joins_a_note_that_is_there_and_keeps_the_rest(self, conn, tmp_path):
        _scanned(conn, tmp_path, "a.cpp")
        (tmp_path / "a.cpp").write_text("void a() { changed(); }\n")
        data = add_changed_notice(conn, {"note": "Index incomplete: 1 of 9 files are not scanned yet.", "matches": []}, tmp_path, ["a.cpp"])
        assert data["note"].startswith("Index incomplete: 1 of 9 files are not scanned yet. 1 indexed file(s) changed since the last scan (a.cpp)")
        assert data["changed_since_scan"] == {"count": 1, "edited": ["a.cpp"]} and data["matches"] == []

    def test_no_root_or_a_failure_adds_nothing_and_never_raises(self, conn, tmp_path):
        assert add_changed_notice(conn, {"x": 1}, None, ["a.cpp"]) == {"x": 1}
        broken = type("Broken", (), {"execute": lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))})()
        assert add_changed_notice(broken, {"x": 1}, tmp_path, ["a.cpp"]) == {"x": 1}


# -- the commands: one small project, scanned once -------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    from tests.fakes import KeptDuckDB

    folder = tmp_path_factory.mktemp("changed")
    with pytest.MonkeyPatch.context() as patch:
        KeptDuckDB().install(patch)
        (folder / "_drspec").mkdir()
        (folder / "ctx.cpp").write_text(CTX_CPP)
        (folder / "caller.cpp").write_text(CALLER_CPP)
        connection = duckdb.connect(str(folder / "_drspec" / "contracts.db"))  # pattern-ok: duckdb-file kept in memory by `KeptDuckDB`
        init_schema(connection)
        connection.close()
        patch.chdir(folder)
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        yield folder


@contextlib.contextmanager
def _edited(path):
    """The file changed in size while the block runs, and exactly as it was after (content and mtime)."""
    content, stat = path.read_bytes(), path.stat()
    path.write_bytes(content + b"void late(Ctx& c) { c.counter -= 1.0; }\n")
    try:
        yield
    finally:
        path.write_bytes(content)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))


def _json(*args):
    result = runner.invoke(app, ["--json", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["data"]


def _function_id(name):
    return next(m["function_id"] for m in _json("search", name)["matches"] if m["function_name"] == name)


class TestTheCommandsSayWhenAFileChanged:
    def test_refs_get_has_no_note_while_nothing_changed(self, project):
        data = _json("refs", "get", "Ctx::counter")
        assert "changed_since_scan" not in data and data["total_accesses"] == 2

    def test_refs_get_right_after_an_edit_says_which_file(self, project):
        with _edited(project / "ctx.cpp"):
            data = _json("refs", "get", "counter")
            text = runner.invoke(app, ["refs", "get", "counter"]).output
        assert data["changed_since_scan"] == {"count": 1, "edited": ["ctx.cpp"]} and data["total_accesses"] == 2  # (the answer itself is as before)
        assert "1 indexed file(s) changed since the last scan (ctx.cpp): results may be outdated; drspec scan <paths> updates them." in data["note"]
        assert "Changed Since Scan:" in text and "ctx.cpp" in text
        assert "changed_since_scan" not in _json("refs", "get", "counter")  # (restored: the line is gone)

    def test_refs_get_names_a_deleted_file(self, project):
        path = project / "ctx.cpp"
        content, stat = path.read_bytes(), path.stat()
        path.unlink()
        try:
            data = _json("refs", "get", "Ctx::counter")
        finally:
            path.write_bytes(content)
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        assert data["changed_since_scan"]["deleted"] == ["ctx.cpp"] and "ctx.cpp (deleted)" in data["note"]

    def test_refs_get_names_a_new_file_beside_the_files_it_comes_from(self, project):
        new = project / "fresh.cpp"
        new.write_text("void fresh(Ctx& c) { c.counter = 5.0; }\n")
        try:
            data = _json("refs", "get", "Ctx::counter")
        finally:
            new.unlink()
        assert data["changed_since_scan"]["added"] == ["fresh.cpp"] and "fresh.cpp (new)" in data["note"] and data["total_accesses"] == 2

    def test_deps_get_says_so_for_the_file_of_a_caller(self, project):
        function_id = _function_id("up")
        assert "changed_since_scan" not in _json("deps", "get", function_id, "--direction", "callers")
        with _edited(project / "caller.cpp"):
            data = _json("deps", "get", function_id, "--direction", "callers")
        assert data["changed_since_scan"] == {"count": 1, "edited": ["caller.cpp"]} and "caller.cpp" in data["note"]
        assert [caller["function_name"] for caller in data["callers"]] == ["both"]

    def test_a_function_search_says_so_for_the_files_of_its_matches(self, project):
        assert "changed_since_scan" not in _json("search", "up")
        with _edited(project / "ctx.cpp"):
            data = _json("search", "up")
            unrelated = _json("search", "both")
        assert data["changed_since_scan"]["edited"] == ["ctx.cpp"] and "ctx.cpp" in data["note"]
        assert "changed_since_scan" not in unrelated  # (its match is in caller.cpp, which did not change)

    def test_the_mcp_tools_say_so_too_and_the_compact_form_keeps_it(self, project):
        db_path = project / "_drspec" / "contracts.db"
        function_id = _function_id("up")
        with _edited(project / "ctx.cpp"):
            refs, failed_refs = mcp_server._tool_refs_get(db_path, {"symbol": "Ctx::counter"})
            deps, failed_deps = mcp_server._tool_deps_get(db_path, {"function_id": function_id})
            found, failed_search = mcp_server._tool_search(db_path, {"pattern": "up"})
        assert not (failed_refs or failed_deps or failed_search)
        for answer in (refs, deps, found):
            assert answer["changed_since_scan"]["edited"] == ["ctx.cpp"] and "ctx.cpp" in answer["note"]
            encoded = compact_format.encode(answer)
            assert "changed since the last scan (ctx.cpp)" in encoded and "changed_since_scan" in encoded
        clean, _failed = mcp_server._tool_refs_get(db_path, {"symbol": "Ctx::counter"})
        assert "changed_since_scan" not in clean and "note" not in clean
