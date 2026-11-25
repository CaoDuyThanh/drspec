"""The disk-work counters (`disk_counts.py`): what they count on a fake connection, and what the report prints. Report only."""

from __future__ import annotations

import os

import pytest

from tests.tools import disk_counts

pytest_plugins = ["pytester"]


class FakeConnection:
    """What a DuckDB connection answers: it records the statements it is given."""

    def __init__(self):
        self.statements = []
        self.closed = False

    def execute(self, sql, *args):
        self.statements.append(sql)
        return self

    executemany = execute

    def commit(self):
        self.statements.append("<commit>")

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _clean():
    disk_counts.reset()
    yield
    disk_counts.reset()


def _counted(on_disk: bool) -> tuple:
    fake = FakeConnection()
    return fake, disk_counts.CountedConnection(fake, on_disk)


def test_a_write_outside_a_transaction_is_a_commit_each_and_a_transaction_is_one():
    fake, connection = _counted(True)
    connection.execute("INSERT INTO t VALUES (1)")
    connection.execute("update t set a = 1")
    assert disk_counts.snapshot()["commits"] == 2
    connection.execute("BEGIN")
    for _ in range(50):
        connection.execute("INSERT INTO t VALUES (2)")
    assert disk_counts.snapshot()["commits"] == 2  # (nothing is flushed until the end of the transaction)
    connection.execute("COMMIT")
    assert disk_counts.snapshot()["commits"] == 3
    assert len(fake.statements) == 54  # (the statements went through to the real connection)


def test_reads_are_free_and_a_checkpoint_and_a_close_are_counted():
    _, connection = _counted(True)
    connection.execute("SELECT 1")
    connection.execute("  checkpoint ")
    connection.close()
    assert disk_counts.snapshot() == {"commits": 0, "checkpoints": 1, "closes": 1, "fsyncs": 0, "connects": 0}


def test_a_rolled_back_transaction_is_not_a_commit_and_the_next_write_is():
    _, connection = _counted(True)
    connection.execute("BEGIN")
    connection.execute("DELETE FROM t")
    connection.execute("ROLLBACK")
    assert disk_counts.snapshot()["commits"] == 0
    connection.execute("DELETE FROM t")
    assert disk_counts.snapshot()["commits"] == 1


def test_an_in_memory_database_costs_the_disk_nothing():
    _, connection = _counted(False)
    connection.execute("INSERT INTO t VALUES (1)")
    connection.execute("CHECKPOINT")
    connection.close()
    assert disk_counts.snapshot() == {"commits": 0, "checkpoints": 0, "closes": 0, "fsyncs": 0, "connects": 0}


def test_connect_and_fsync_are_counted_while_installed_and_restored_after(tmp_path):
    import duckdb

    real_connect, real_fsync = duckdb.connect, os.fsync
    disk_counts.install()
    try:
        memory = duckdb.connect(":memory:")
        memory.close()
        on_disk = duckdb.connect(str(tmp_path / "x.db"))
        on_disk.execute("CREATE TABLE t (a INT)")
        on_disk.close()
        with open(tmp_path / "f", "wb") as stream:
            os.fsync(stream.fileno())
        assert disk_counts.snapshot() == {"commits": 1, "checkpoints": 0, "closes": 1, "fsyncs": 1, "connects": 1}
    finally:
        disk_counts.uninstall()
    assert duckdb.connect is real_connect and os.fsync is real_fsync


def test_the_report_prints_the_counts_the_busiest_tests_and_the_load(pytester, monkeypatch):
    monkeypatch.setenv("DRSPEC_TIER_REPORT", "1")
    pytester.makepyfile(
        test_inner="""
import duckdb
import pytest


@pytest.mark.component
def test_writes_a_file(tmp_path):
    connection = duckdb.connect(str(tmp_path / "x.db"))
    connection.execute("CREATE TABLE t (a INT)")
    connection.execute("INSERT INTO t VALUES (1)")
    connection.execute("CHECKPOINT")
    connection.close()


@pytest.mark.unit
def test_writes_nothing():
    connection = duckdb.connect(":memory:")
    connection.execute("CREATE TABLE t (a INT)")
    connection.close()
"""
    )
    result = pytester.runpytest_inprocess("-p", "tests.tools.tier_summary", "-q")
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(
        [
            "*disk work (counts of requests*: commits 2, checkpoints 1, closes 1, fsyncs *, connects 1*",
            "*commits=2 checkpoints=1 closes=1 *connects=1  *test_writes_a_file*",
            "*load average (1, 5, 15 min) at start / end: *",
        ]
    )
    assert "test_writes_nothing" not in result.stdout.str().split("disk work")[1].split("over their tier budget")[0]
