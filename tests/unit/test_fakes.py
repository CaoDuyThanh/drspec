"""The fakes themselves: a fake that leaks into the next test, or answers differently from the real thing, fails every test that uses it."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import duckdb
import pytest

from tests.fakes import InProcessPython, KeptDuckDB


def _run(tmp_path, source, stdin=""):
    script = tmp_path / "s.py"
    script.write_text(source)
    return InProcessPython().run([sys.executable, str(script)], input=stdin)


class TestInProcessPython:
    def test_a_script_gets_its_stdin_and_prints_to_stdout(self, tmp_path):
        done = _run(tmp_path, "import sys\nprint(sys.stdin.read().upper())\n", "abc")
        assert (done.returncode, done.stdout.strip()) == (0, "ABC")

    def test_a_script_that_raises_is_a_failed_exit_with_its_traceback(self, tmp_path):
        done = _run(tmp_path, "raise ValueError('boom')\n")
        assert done.returncode == 1 and "ValueError: boom" in done.stderr and done.stdout == ""

    def test_the_exit_code_of_a_script_is_kept(self, tmp_path):
        assert _run(tmp_path, "raise SystemExit(3)\n").returncode == 3

    def test_a_script_leaves_nothing_behind_for_the_next_test(self, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        before = (sys.argv, sys.stdin, list(sys.path), set(sys.modules), os.getcwd())
        _run(
            tmp_path,
            "import sys, os, types\n"
            "sys.argv = ['changed']\nsys.stdin = None\nsys.path.insert(0, 'not-a-folder')\n"
            "sys.modules['leaked_by_the_script'] = types.ModuleType('leaked_by_the_script')\n"
            "import colorsys\n"  # a module imported for the first time by the script
            f"os.chdir({str(elsewhere)!r})\n"  # pattern-ok: os-chdir the text of the script the fake runs, restored by the fake
            "raise RuntimeError('and then it fails')\n",
        )
        after = (sys.argv, sys.stdin, list(sys.path), set(sys.modules), os.getcwd())
        assert after == before
        assert "leaked_by_the_script" not in sys.modules

    def test_it_reports_the_script_as_a_real_process_would_see_it(self, tmp_path):
        done = _run(tmp_path, "import sys, json\nprint(json.dumps({'argv': sys.argv, 'name': __name__}))\n")
        seen = json.loads(done.stdout)
        assert seen["name"] == "__main__" and seen["argv"] == [str(tmp_path / "s.py")]

    def test_it_can_time_out_like_a_script_that_runs_too_long(self, tmp_path):
        fake = InProcessPython()
        fake.times_out = True
        script = tmp_path / "s.py"
        script.write_text("print('never')\n")
        with pytest.raises(subprocess.TimeoutExpired):
            fake.run([sys.executable, str(script)], input="", timeout=0.1)


class TestKeptDuckDB:
    def _kept(self):
        return KeptDuckDB()  # not installed: the tests call `connect` on it, so the real `duckdb.connect` stays in place

    def test_the_same_path_gives_the_same_data_after_a_close(self, tmp_path):
        kept = self._kept()
        first = kept.connect(str(tmp_path / "a.db"))
        first.execute("CREATE TABLE t (x INTEGER)")
        first.execute("INSERT INTO t VALUES (1), (2)")
        first.close()
        assert kept.connect(str(tmp_path / "a.db")).execute("SELECT count(*) FROM t").fetchone() == (2,)

    def test_two_paths_are_two_databases_and_memory_is_always_new(self, tmp_path):
        kept = self._kept()
        kept.connect(str(tmp_path / "a.db")).execute("CREATE TABLE t (x INTEGER)")
        with pytest.raises(duckdb.CatalogException):
            kept.connect(str(tmp_path / "b.db")).execute("SELECT * FROM t")
        kept.connect(":memory:").execute("CREATE TABLE m (x INTEGER)")
        with pytest.raises(duckdb.CatalogException):
            kept.connect(":memory:").execute("SELECT * FROM m")

    def test_closing_rolls_back_a_transaction_nobody_finished_as_a_real_close_does(self, tmp_path):
        kept = self._kept()
        conn = kept.connect(str(tmp_path / "a.db"))
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.begin()
        conn.execute("INSERT INTO t VALUES (1)")
        conn.close()
        assert kept.connect(str(tmp_path / "a.db")).execute("SELECT count(*) FROM t").fetchone() == (0,)

    def test_a_committed_transaction_survives_the_close(self, tmp_path):
        kept = self._kept()
        conn = kept.connect(str(tmp_path / "a.db"))
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.begin()
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        conn.close()
        assert kept.connect(str(tmp_path / "a.db")).execute("SELECT count(*) FROM t").fetchone() == (1,)

    def test_the_database_has_a_marker_file_because_the_commands_check_it_exists(self, tmp_path):
        self._kept().connect(str(tmp_path / "sub" / "a.db"))
        assert (tmp_path / "sub" / "a.db").exists() and (tmp_path / "sub" / "a.db").stat().st_size == 0

    def test_a_read_only_open_of_a_database_never_opened_fails_like_a_real_one(self, tmp_path):
        with pytest.raises(duckdb.IOException):
            self._kept().connect(str(tmp_path / "missing.db"), read_only=True)
        with pytest.raises(duckdb.IOException):  # and the real one says the same
            duckdb.connect(str(tmp_path / "missing.db"), read_only=True)

    def test_it_answers_pragma_database_list_with_the_path_as_a_file_does(self, tmp_path):
        kept = self._kept()
        conn = kept.connect(str(tmp_path / "_drspec" / "contracts.db"))
        assert conn.execute("PRAGMA database_list").fetchall()[0][2] == str((tmp_path / "_drspec" / "contracts.db").resolve())
        assert kept.connect(":memory:").execute("PRAGMA database_list").fetchall()[0][2] is None  # a real memory database has no file

    def test_a_query_still_gets_the_real_rows(self, tmp_path):
        conn = self._kept().connect(str(tmp_path / "x.db"))
        assert conn.execute("SELECT 41 + 1").fetchone() == (42,)


class TestFakeEmbeddingModel:
    def test_a_vector_has_the_model_size_a_unit_length_and_never_changes(self):
        from tests.fakes import FakeEmbeddingModel

        model = FakeEmbeddingModel()
        first = next(model.embed(["def compute_total(order): return sum(order.prices)"])).tolist()
        again = next(model.embed(["def compute_total(order): return sum(order.prices)"])).tolist()
        assert len(first) == 384 and first == again
        assert abs(sum(v * v for v in first) - 1.0) < 1e-9

    def test_texts_that_share_words_are_closer_than_texts_that_do_not(self):
        from tests.fakes import FakeEmbeddingModel

        model = FakeEmbeddingModel()
        query, near, far = (next(model.embed([t])).tolist() for t in ("sum item prices", "return sum(i.price for i in order.items)", "open the socket and listen"))
        dot = lambda a, b: sum(x * y for x, y in zip(a, b))  # noqa: E731
        assert dot(query, near) > dot(query, far)

    def test_installed_it_is_what_embed_text_uses(self, fake_embedder):
        from drspec.core.embedder import embed_text

        assert embed_text("sum item prices") == next(fake_embedder.embed(["sum item prices"])).tolist()
