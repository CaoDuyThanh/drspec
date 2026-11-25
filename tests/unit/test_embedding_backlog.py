"""The embedding backlog, the usage scores, the stored vectors, the status message, the embedding process and its supervisor. The database is in
memory; the model, the clock, the sleeps and the spawned process are fakes."""

import json

import duckdb
import pytest

from drspec.core import embedder, embedding_runner
from drspec.core.embedding_backlog import (
    Item,
    embedding_status,
    fetch_texts,
    find_backlog,
    meta_key,
    store_embeddings,
)
from drspec.core.settings import read_all_settings, set_setting
from drspec.core.usage import dependency_items, record_usage
from drspec.db import init_schema, insert_artifact
from drspec.db.queries import add_usage_pending, fold_usage_pending, top_usage_items
from drspec.embedding_supervisor import EmbeddingSupervisor
from tests.helpers import insert_dependency

pytestmark = pytest.mark.unit  # in-memory database, fake model, fake clock, fake process

DAY = 86400.0
VECTOR = [0.5] * 384


@pytest.fixture
def conn():
    connection = duckdb.connect(":memory:")  # only SQL on the project tables is checked: no file
    init_schema(connection)
    yield connection
    connection.close()


def _function(conn, name, body=None):
    insert_artifact(
        conn, function_id=f"a.py::{name}", file_path="a.py", function_name=name, signature=f"def {name}():",
        body=body or f"def {name}(): pass", code_hash=f"hash-{name}", language="python", start_line=1, end_line=2,
    )
    return Item("artifacts", (f"a.py::{name}",), f"hash-{name}")


def _backlog(conn, now=1000.0, top_n=10):
    return find_backlog(conn, top_n, 7.0, now)


def _use(conn, name, weight=1.0, as_of=1000.0):
    add_usage_pending(conn, [("node", f"a.py::{name}", "", weight)])
    fold_usage_pending(conn, as_of, 7.0)


class TestWhatIsOwed:
    def test_an_unused_item_that_was_never_embedded_is_not_owed(self, conn):
        _function(conn, "f")
        assert _backlog(conn) == []

    def test_a_used_item_without_a_vector_is_owed(self, conn):
        item = _function(conn, "f")
        _use(conn, "f")
        assert _backlog(conn) == [item]

    def test_an_up_to_date_vector_is_not_owed_again(self, conn):
        item = _function(conn, "f")
        _use(conn, "f")
        store_embeddings(conn, [(item, VECTOR)])
        assert _backlog(conn) == []

    def test_only_the_top_n_by_usage_are_owed(self, conn):
        items = [_function(conn, name) for name in "abc"]
        _use(conn, "a", 1)
        _use(conn, "b", 5)
        _use(conn, "c", 3)
        assert _backlog(conn, top_n=2) == [items[1], items[2]]

    def test_an_item_embedded_before_is_re_embedded_after_its_vector_was_cleared(self, conn):
        item = _function(conn, "f")
        store_embeddings(conn, [(item, VECTOR)])
        conn.execute("DELETE FROM artifact_embeddings")  # what a re-scan does
        assert _backlog(conn) == [item]  # owed with no usage at all

    def test_a_changed_source_is_owed_again(self, conn):
        item = _function(conn, "f")
        store_embeddings(conn, [(item, VECTOR)])
        conn.execute("UPDATE artifacts SET code_hash = 'new'")
        assert [i.source_hash for i in _backlog(conn)] == ["new"]

    def test_a_model_change_requeues_everything_that_had_a_vector(self, conn, monkeypatch):
        item = _function(conn, "f")
        _function(conn, "never")
        store_embeddings(conn, [(item, VECTOR)])
        monkeypatch.setattr("drspec.core.embedding_backlog.MODEL_VERSION", "other-model")
        assert _backlog(conn) == [item]

    def test_leaving_the_top_n_keeps_the_vector_and_owes_nothing(self, conn):
        a, b = _function(conn, "a"), _function(conn, "b")
        _use(conn, "a", 1)
        store_embeddings(conn, [(a, VECTOR)])
        _use(conn, "b", 50)
        assert _backlog(conn, top_n=1) == [b]
        assert conn.execute("SELECT EXISTS (SELECT 1 FROM artifact_embeddings WHERE function_id = 'a.py::a')").fetchone()[0]

    def test_edges_and_references_are_owed_too(self, conn):
        _function(conn, "f")
        _function(conn, "g")
        insert_dependency(conn, "a.py::f", "a.py::g")
        conn.execute("UPDATE dependencies SET call_site = 'g()', edge_hash = 'e1'")
        add_usage_pending(conn, [("edge", "a.py::f", "a.py::g", 2.0)])
        fold_usage_pending(conn, 1000.0, 7.0)
        assert _backlog(conn) == [Item("dependencies", ("a.py::f", "a.py::g"), "e1")]


class TestUsageScores:
    def test_pending_is_folded_into_a_decayed_score(self, conn):
        _use(conn, "f", 8.0, as_of=0.0)
        add_usage_pending(conn, [("node", "a.py::f", "", 1.0)])
        fold_usage_pending(conn, 7 * DAY, 7.0)  # one half-life later
        (_kind, _id, _rel, score), = top_usage_items(conn, 5, 7 * DAY, 7.0)
        assert score == pytest.approx(8.0 * 0.5 + 1.0)

    def test_a_ranking_read_decays_scores_that_were_not_touched(self, conn):
        _use(conn, "old", 10.0, as_of=0.0)
        _use(conn, "new", 4.0, as_of=14 * DAY)
        ranked = [row[1] for row in top_usage_items(conn, 5, 14 * DAY, 7.0)]
        assert ranked == ["a.py::new", "a.py::old"]  # 10 decays to 2.5, below 4

    def test_recording_is_best_effort_and_skipped_when_read_only(self, conn):
        assert record_usage(conn, [("node", "a.py::f", "")]) == 1

        class ReadOnly:
            read_only = True

            def execute(self, *args, **kwargs):
                raise AssertionError("a read-only connection must not be written to")

        assert record_usage(ReadOnly(), [("node", "a.py::g", "")]) == 0

        class Broken:
            def execute(self, *args, **kwargs):
                raise RuntimeError("worker gone")

        assert record_usage(Broken(), [("node", "a.py::f", "")]) == 0

    def test_a_lookup_weighs_more_than_a_search_hit(self, conn):
        record_usage(conn, [("node", "a.py::s", "")])
        record_usage(conn, [("node", "a.py::l", "")], "usage.lookup_weight")
        fold_usage_pending(conn, 0.0, 7.0)
        scores = {row[1]: row[3] for row in top_usage_items(conn, 5, 0.0, 7.0)}
        assert set(scores) == {"a.py::l", "a.py::s"}
        assert scores["a.py::l"] == 5 * scores["a.py::s"]

    def test_a_dependency_lookup_records_the_function_its_neighbours_and_the_direct_edges(self):
        callees = [{"function_id": "b", "depth": 1, "relation": "calls"}, {"function_id": "c", "depth": 2, "relation": "calls"}]
        callers = [{"function_id": "d", "depth": 1, "relation": "references"}]
        items = dependency_items("a", callees, callers)
        assert ("edge", "a", "b") in items and ("reference", "d", "a") in items
        assert ("edge", "a", "c") not in items
        assert {i[1] for i in items if i[0] == "node"} == {"a", "b", "c", "d"}


class TestStoring:
    def test_a_vector_is_stored_with_the_model_version(self, conn):
        item = _function(conn, "f")
        assert store_embeddings(conn, [(item, VECTOR)]) == 1
        assert json.loads(conn.execute("SELECT value FROM config WHERE key = ?", [meta_key("artifacts", item.keys)]).fetchone()[0]) \
            == [embedder.MODEL_VERSION, "hash-f"]

    def test_a_vector_for_source_that_changed_meanwhile_is_dropped(self, conn):
        item = _function(conn, "f")
        conn.execute("UPDATE artifacts SET code_hash = 'newer'")
        assert store_embeddings(conn, [(item, VECTOR)]) == 0
        assert conn.execute("SELECT NOT EXISTS (SELECT 1 FROM artifact_embeddings)").fetchone()[0]

    def test_texts_are_fetched_only_for_unchanged_items(self, conn):
        keep, move = _function(conn, "keep", "body-keep"), _function(conn, "move")
        conn.execute("UPDATE artifacts SET code_hash = 'x' WHERE function_name = 'move'")
        assert fetch_texts(conn, [keep, move]) == [(keep, "body-keep")]

    def test_storing_an_artifact_vector_keeps_edges_that_point_at_it(self, conn):
        _function(conn, "f")
        g = _function(conn, "g")
        insert_dependency(conn, "a.py::f", "a.py::g")
        store_embeddings(conn, [(g, VECTOR)])
        assert conn.execute("SELECT COUNT(*) FROM dependencies").fetchone()[0] == 1


class TestStatusMessage:
    def _status(self, conn):
        return embedding_status(conn, read_all_settings(conn), 1000.0)

    def test_reports_counts_and_says_so_when_only_a_share_is_embedded(self, conn):
        item = _function(conn, "f")
        _function(conn, "g")
        store_embeddings(conn, [(item, VECTOR)])
        status = self._status(conn)
        assert (status["embedded"], status["total"], status["backlog"], status["top_n"]) == (1, 2, 0, 2000)
        # (nothing is owed because only the functions in use are candidates: that is not "up to date")
        assert status["message"] == "1 of 2 embedded (top_n = 2000): the others are not found by meaning"

    def test_is_up_to_date_only_when_everything_is_embedded(self, conn):
        first, second = _function(conn, "f"), _function(conn, "g")
        store_embeddings(conn, [(first, VECTOR), (second, VECTOR)])
        status = self._status(conn)
        assert (status["embedded"], status["total"], status["backlog"]) == (2, 2, 0)
        assert status["message"] == "embedding up to date"

    def test_says_when_it_is_waiting_on_the_model_download(self, conn):
        embedding_runner.write_state(conn, "model_unavailable", "offline")
        assert self._status(conn)["message"] == "embedding waiting: model download failed"

    def test_says_when_it_is_downloading(self, conn):
        embedding_runner.write_state(conn, "downloading")
        assert self._status(conn)["message"] == "embedding: downloading the model"

    def test_says_when_it_is_off(self, conn):
        set_setting(conn, "embedding.enabled", False)
        assert self._status(conn)["message"] == "embedding off"


class _Direct:
    """A worker connection stand-in over a plain connection."""

    def __init__(self, conn):
        self._conn = conn
        self.stored = []  # how many vectors each call to `store_embeddings` carried

    def execute(self, *args, **kwargs):
        return self._conn.execute(*args, **kwargs)

    def store_embeddings(self, results):
        self.stored.append(len(results))
        return store_embeddings(self._conn, results)


class TestEmbeddingProcess:
    @pytest.fixture(autouse=True)
    def fake_model(self, monkeypatch):
        self.loaded_with = []
        monkeypatch.setattr(embedder, "load_model", lambda threads: self.loaded_with.append(threads))
        monkeypatch.setattr(embedder, "embed_text", lambda text: VECTOR)
        monkeypatch.setattr(embedder, "model_is_cached", lambda: True)
        monkeypatch.setattr(embedding_runner.os, "nice", lambda n: 0)
        monkeypatch.setattr(embedding_runner, "pause_reason", lambda settings: None)

    def _run(self, conn, sleep=lambda s: None):
        self.direct = _Direct(conn)
        return embedding_runner.run_embedding_process(self.direct, sleep=sleep)

    def test_works_through_a_backlog_in_chunks_and_exits(self, conn, monkeypatch):
        monkeypatch.setattr(embedding_runner, "CHUNK_SIZE", 1)  # the product's chunk is 20: two chunks need 2 items, not 21 (a stored vector costs 16 ms)
        for i in range(2):
            _function(conn, f"f{i}")
        add_usage_pending(conn, [("node", f"a.py::f{i}", "", 1.0) for i in range(2)])
        fold_usage_pending(conn, 1000.0, 7.0)
        assert self._run(conn) == 0
        assert conn.execute("SELECT COUNT(*) FROM artifact_embeddings").fetchone()[0] == 2
        assert self.direct.stored == [1, 1]  # one chunk of one item each, not one chunk of two
        assert self.loaded_with == [1]


    def test_does_not_load_the_model_when_nothing_is_owed(self, conn):
        _function(conn, "f")
        assert self._run(conn) == 0
        assert self.loaded_with == []

    def test_does_nothing_when_embedding_is_off(self, conn):
        _function(conn, "f")
        _use(conn, "f")
        set_setting(conn, "embedding.enabled", False)
        assert self._run(conn) == 0
        assert self.loaded_with == []

    def test_a_model_that_cannot_load_is_reported_not_raised(self, conn, monkeypatch):
        _function(conn, "f")
        _use(conn, "f")

        def fail(threads):
            raise OSError("offline")

        monkeypatch.setattr(embedder, "load_model", fail)
        assert self._run(conn) == embedding_runner.EXIT_MODEL_UNAVAILABLE
        assert json.loads(conn.execute("SELECT value FROM config WHERE key = 'worker:embedding_state'").fetchone()[0])["state"] \
            == "model_unavailable"

    def test_the_duty_cycle_sleeps_in_proportion_to_work(self, conn, monkeypatch):
        _function(conn, "f")
        _use(conn, "f")
        set_setting(conn, "embedding.cpu_percent", 25)
        ticks = iter([0.0, 2.0])
        sleeps = []
        embedding_runner.run_embedding_process(_Direct(conn), sleep=sleeps.append, clock=lambda: next(ticks))
        assert sleeps == [pytest.approx(6.0)]  # 2s of work at 25% => 6s rest

    def test_pauses_while_the_machine_is_busy_then_continues(self, conn, monkeypatch):
        _function(conn, "f")
        _use(conn, "f")
        reasons = iter(["machine busy", "low memory", None])
        monkeypatch.setattr(embedding_runner, "pause_reason", lambda settings: next(reasons))
        sleeps = []
        assert self._run(conn, sleep=sleeps.append) == 0
        assert sleeps[:2] == [embedding_runner.PAUSE_SECONDS] * 2
        assert conn.execute("SELECT COUNT(*) FROM artifact_embeddings").fetchone()[0] == 1

    def test_pause_reasons_come_from_load_and_memory(self, monkeypatch):
        monkeypatch.undo()
        settings = {"embedding.max_load_per_core": 1.0, "embedding.min_available_memory_mb": 1500}
        monkeypatch.setattr(embedding_runner, "load_per_core", lambda: 2.0)
        monkeypatch.setattr(embedding_runner, "available_memory_mb", lambda: 9000.0)
        assert embedding_runner.pause_reason(settings) == "machine busy"
        monkeypatch.setattr(embedding_runner, "load_per_core", lambda: 0.2)
        monkeypatch.setattr(embedding_runner, "available_memory_mb", lambda: 500.0)
        assert embedding_runner.pause_reason(settings) == "low memory"
        monkeypatch.setattr(embedding_runner, "available_memory_mb", lambda: 9000.0)
        assert embedding_runner.pause_reason(settings) is None


class _Process:
    def __init__(self):
        self.code = None

    def poll(self):
        return self.code

    def terminate(self):
        self.code = -15

    def wait(self, timeout=None):
        return self.code


class TestSupervisor:
    @pytest.fixture
    def supervisor(self):
        self.spawned = []

        def spawn(db_path, threads=1):
            process = _Process()
            self.spawned.append(process)
            return process

        return EmbeddingSupervisor(spawn=spawn)

    def _tick(self, supervisor, conn, now):
        supervisor.tick(conn, "unused.db", read_all_settings(conn), now)

    def test_nothing_is_started_without_a_backlog(self, supervisor, conn):
        _function(conn, "f")
        self._tick(supervisor, conn, 1000.0)
        assert self.spawned == []

    def test_usage_is_folded_and_one_process_starts(self, supervisor, conn):
        _function(conn, "f")
        add_usage_pending(conn, [("node", "a.py::f", "", 1.0)])
        self._tick(supervisor, conn, 1000.0)
        self._tick(supervisor, conn, 1001.0)  # still running: no second one
        assert len(self.spawned) == 1
        assert conn.execute("SELECT SUM(pending) FROM usage_stats").fetchone()[0] == 0

    def test_embedding_off_starts_nothing(self, supervisor, conn):
        _function(conn, "f")
        _use(conn, "f")
        set_setting(conn, "embedding.enabled", False)
        self._tick(supervisor, conn, 1000.0)
        assert self.spawned == []

    def test_a_failing_process_is_retried_after_a_growing_wait(self, supervisor, conn):
        _function(conn, "f")
        _use(conn, "f")
        self._tick(supervisor, conn, 1000.0)
        self.spawned[-1].code = embedding_runner.EXIT_MODEL_UNAVAILABLE
        self._tick(supervisor, conn, 1010.0)
        assert len(self.spawned) == 1  # first wait is 60s
        self._tick(supervisor, conn, 1071.0)
        assert len(self.spawned) == 2
        self.spawned[-1].code = 1
        self._tick(supervisor, conn, 1080.0)
        self._tick(supervisor, conn, 1080.0 + 119.0)
        assert len(self.spawned) == 2  # second wait is 120s
        self._tick(supervisor, conn, 1080.0 + 121.0)
        assert len(self.spawned) == 3
        assert json.loads(conn.execute("SELECT value FROM config WHERE key = 'worker:embedding_state'").fetchone()[0])["state"] == "error"

    def test_a_process_that_finishes_without_progress_is_not_respawned_every_pass(self, supervisor, conn):
        _function(conn, "f")
        _use(conn, "f")
        self._tick(supervisor, conn, 1000.0)
        self.spawned[-1].code = 0  # exited cleanly, yet the item is still owed
        self._tick(supervisor, conn, 1010.0)
        self._tick(supervisor, conn, 1020.0)
        assert len(self.spawned) == 1

    def test_stop_ends_a_running_process(self, supervisor, conn):
        _function(conn, "f")
        _use(conn, "f")
        self._tick(supervisor, conn, 1000.0)
        supervisor.stop()
        assert self.spawned[0].code == -15
