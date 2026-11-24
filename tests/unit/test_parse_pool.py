"""Parsing on several cores: the pool turns paths into the same parse results one process would give, in the same order;
how many processes a scan or the background drain uses; a dead parser process does not stop a batch."""

from __future__ import annotations

import json
import os
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest

from drspec.core import parse_pool
from drspec.core.parse_pool import ParsePool, parser_count
from drspec.core.scanner import Scanner
from drspec.db import init_schema
from drspec.db.queries import enqueue_staleness_many


def _files(root: Path, count: int) -> list[str]:
    names = []
    for i in range(count):
        name = f"mod{i}.py"
        (root / name).write_text(f"from mod{(i + 1) % count} import fn{(i + 1) % count}\n\n\ndef fn{i}(x):\n    return fn{(i + 1) % count}(x) + {i}\n\n\nclass K{i}:\n    def m(self):\n        return fn{i}(1)\n")
        names.append(name)
    return names


class _InThisProcess:
    """The pool's executor without processes: `map` runs the parser function here, as each parser process would (what is under test is the pool's
    ordering, its error results and its dropping, not the operating system's processes)."""

    def map(self, function, paths, chunksize):
        return [function(path) for path in paths]

    def shutdown(self, *args, **kwargs):
        pass


class TestThePool:
    @pytest.fixture(autouse=True)
    def _a_parser_in_this_process(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(parse_pool, "_SCANNER", Scanner())

    def test_it_gives_the_results_one_process_would_in_the_order_given(self, tmp_path):
        names = _files(tmp_path, 14) + ["missing.py"]
        pool = ParsePool(3)
        pool._executor = _InThisProcess()
        outcomes = pool.parse(names)
        serial = [Scanner().scan_file(name, relative_to=Path.cwd()) for name in names]
        assert len(outcomes) == len(names)
        for (result, error), expected in zip(outcomes, serial):
            assert error is None and result is not None
            assert [(f.function_id, f.body) for f in result.functions] == [(f.function_id, f.body) for f in expected.functions]
            assert result.parse_status == expected.parse_status and [e.message for e in result.errors] == [e.message for e in expected.errors]
            assert {k: v.facts for k, v in result.file_imports.items()} == {k: v.facts for k, v in expected.file_imports.items()}

    def test_a_file_that_cannot_be_read_comes_back_as_a_result_with_its_error(self):
        pool = ParsePool(2)
        pool._executor = _InThisProcess()
        (result, error), = pool.parse(["nothing_here.py"])
        assert error is None and result.errors and result.parse_status not in ("successful", "partial")

    def test_cpu_time_is_counted_for_the_workers_limit(self):
        """What each parser process reports as its CPU is added up for the worker's limit (a fake executor answers, 0.25 s a file: nothing is
        measured, so the sum is the same on every machine)."""

        class Executor:
            def map(self, function, paths, chunksize):
                return [(None, None, 0.25) for _ in paths]

            def shutdown(self, *args, **kwargs):
                pass

        pool = ParsePool(2)
        pool._executor = Executor()
        try:
            assert [outcome for outcome in pool.parse([f"f{i}.py" for i in range(8)])] == [(None, None)] * 8
            assert pool.cpu_seconds == 2.0  # pattern-ok: real-time-assert the sum of the fake executor's numbers
            pool.parse(["g.py"])
            assert pool.cpu_seconds == 2.25  # and it keeps adding  # pattern-ok: real-time-assert the same sum, nothing measured
        finally:
            pool.close()

    def test_a_dead_parser_process_is_reported_and_the_pool_is_dropped(self):
        class Dead(_InThisProcess):
            def map(self, function, paths, chunksize):
                raise BrokenProcessPool("a parser died")

        pool = ParsePool(2)
        pool._executor = Dead()
        with pytest.raises(BrokenProcessPool):
            pool.parse(["mod0.py", "mod1.py"])
        assert pool._executor is None


class TestHowManyProcesses:
    def test_a_waiting_scan_takes_every_core_but_one(self, monkeypatch):
        monkeypatch.setattr(os, "cpu_count", lambda: 12)
        assert parser_count({"scan.workers": 0}, True, None, None) == 11

    def test_the_setting_overrides_it(self, monkeypatch):
        monkeypatch.setattr(os, "cpu_count", lambda: 12)
        assert parser_count({"scan.workers": 3}, True, None, None) == 3
        assert parser_count({"scan.workers": 20}, True, None, None) == 20

    def test_the_background_drain_takes_its_share_of_the_machine(self, monkeypatch):
        monkeypatch.setattr(os, "cpu_count", lambda: 12)
        assert parser_count({"worker.cpu_percent": 20}, False, None, None) == 2
        assert parser_count({"worker.cpu_percent": 100}, False, None, None) == 12

    def test_a_busy_machine_gets_one(self, monkeypatch):
        monkeypatch.setattr(os, "cpu_count", lambda: 12)
        assert parser_count({"worker.cpu_percent": 50}, False, "machine busy", None) == 1

    def test_free_memory_holds_the_number_down(self, monkeypatch):
        monkeypatch.setattr(os, "cpu_count", lambda: 12)
        settings = {"scan.workers": 0, "embedding.min_available_memory_mb": 1500}
        assert parser_count(settings, True, None, 1500 + 3 * parse_pool.MEMORY_PER_PARSER_MB) == 3
        assert parser_count(settings, True, None, 100) == 1  # never fewer than one

    def test_one_core_means_one_process(self, monkeypatch):
        monkeypatch.setattr(os, "cpu_count", lambda: 1)
        assert parser_count({"scan.workers": 0}, True, None, None) == 1


class TestSayingHowManyAndWhy:
    def test_the_plan_names_what_held_the_number_down(self, monkeypatch):
        from drspec.core.parse_pool import parser_plan

        monkeypatch.setattr(os, "cpu_count", lambda: 12)
        busy = {"worker.cpu_percent": 20}
        assert parser_plan(busy, False, None, None) == (2, None)
        assert parser_plan(busy, False, "machine busy", None) == (1, "machine busy")
        assert parser_plan({"scan.workers": 0, "embedding.min_available_memory_mb": 1500}, True, None, 100) == (1, "low memory")
        assert parser_plan({"scan.workers": 0}, True, None, None) == (11, None)

    def test_one_that_would_have_been_one_anyway_is_not_blamed_on_the_machine(self, monkeypatch):
        from drspec.core.parse_pool import parser_plan

        monkeypatch.setattr(os, "cpu_count", lambda: 2)
        assert parser_plan({"worker.cpu_percent": 20}, False, "machine busy", None) == (1, None)

    def test_the_report_reads_the_record_while_it_is_fresh(self, tmp_path):
        import time

        from drspec.core.parse_pool import PARSERS_KEY, parsers_report
        from drspec.db import init_schema

        connection = duckdb.connect(":memory:")
        init_schema(connection)
        assert parsers_report(connection) is None  # nothing recorded
        now = time.time()
        connection.execute("INSERT INTO config (key, value) VALUES (?, ?)", [PARSERS_KEY, json.dumps({"count": 1, "held_down_by": "machine busy", "at": now})])
        assert parsers_report(connection, now + 5) == {"count": 1, "held_down_by": "machine busy", "line": "parsers: 1 (machine busy)"}
        assert parsers_report(connection, now + 3600) is None  # a count from an hour ago says nothing about now
        connection.execute("UPDATE config SET value = ? WHERE key = ?", [json.dumps({"count": 2, "held_down_by": None, "at": now}), PARSERS_KEY])
        assert parsers_report(connection, now)["line"] == "parsers: 2"
        connection.close()

    def test_the_worker_records_it_when_it_changes_and_not_otherwise(self, tmp_path, monkeypatch):
        from drspec import staleness_worker
        from drspec.core import parse_pool
        from drspec.db import init_schema

        connection = duckdb.connect(":memory:")
        init_schema(connection)
        monkeypatch.delenv(staleness_worker._DISABLE_ENV_VAR, raising=False)
        monkeypatch.setattr(os, "cpu_count", lambda: 12)
        monkeypatch.setattr(staleness_worker, "_last_parsers", None)
        monkeypatch.setattr(staleness_worker, "pause_reason", lambda settings: "machine busy")
        monkeypatch.setattr(staleness_worker, "available_memory_mb", lambda: None)
        monkeypatch.setattr(staleness_worker, "get_parse_pool", lambda n: None)
        monkeypatch.setattr(staleness_worker, "scanning_now", lambda conn: False)
        settings = {"worker.cpu_percent": 20, "scan.workers": 0, "embedding.min_available_memory_mb": 1500}
        writes = []
        real = staleness_worker.set_config
        monkeypatch.setattr(staleness_worker, "set_config", lambda conn, key, value: (writes.append(value), real(conn, key, value)))
        staleness_worker._parser_pool(connection, settings, [f"f{i}.py" for i in range(10)])
        staleness_worker._parser_pool(connection, settings, [f"f{i}.py" for i in range(10)])
        assert len(writes) == 1  # the same answer again is not written again
        assert parse_pool.parsers_report(connection)["line"] == "parsers: 1 (machine busy)"
        monkeypatch.setattr(staleness_worker, "pause_reason", lambda settings: None)
        staleness_worker._parser_pool(connection, settings, [f"f{i}.py" for i in range(10)])
        assert len(writes) == 2 and parse_pool.parsers_report(connection)["line"] == "parsers: 2"
        connection.close()


class TestABatchSurvivesAPoolThatFails:
    def test_a_pool_that_dies_leaves_the_batch_parsed_in_the_worker(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        names = _files(tmp_path, 10)
        connection = duckdb.connect(":memory:")
        init_schema(connection)

        class Dying:
            workers = 2

            def parse(self, paths):
                raise BrokenProcessPool("a parser died")

        from drspec import staleness_worker

        with patch.object(staleness_worker, "_parser_pool", return_value=Dying()):
            done = staleness_worker._remediate_queued_files(connection, tmp_path / "p.db", names)
        assert done == 10 and connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 20  # fn + K.m per file
        connection.close()

    def test_a_small_batch_and_the_test_mode_use_no_pool(self, tmp_path, monkeypatch):
        from drspec import staleness_worker

        connection = duckdb.connect(":memory:")
        init_schema(connection)
        assert staleness_worker._parser_pool(connection, {}, ["a.py"]) is None  # too few files to be worth it
        assert staleness_worker._parser_pool(connection, {}, [f"f{i}.py" for i in range(50)]) is None  # DRSPEC_DISABLE_AUTO_WORKER is set
        connection.close()

    def test_the_pool_is_used_for_a_big_batch_when_there_is_a_worker(self, tmp_path, monkeypatch):
        from drspec import staleness_worker

        monkeypatch.delenv("DRSPEC_DISABLE_AUTO_WORKER", raising=False)
        monkeypatch.setattr(os, "cpu_count", lambda: 8)
        monkeypatch.setattr(staleness_worker, "available_memory_mb", lambda: 16000.0)  # the real one reads /proc/meminfo  # pattern-ok: real-time-assert patches the memory reader with a fixed number; nothing is measured
        connection = duckdb.connect(":memory:")  # only the queue rows are read: no file
        init_schema(connection)
        enqueue_staleness_many(connection, [("a.py", "scan")])
        try:
            pool = staleness_worker._parser_pool(connection, {"scan.workers": 0, "embedding.min_available_memory_mb": 0}, [f"f{i}.py" for i in range(50)])
            assert pool is not None and pool.workers in range(2, 8)
        finally:
            parse_pool.close_pool()
            connection.close()
