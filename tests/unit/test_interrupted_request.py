"""A Ctrl-C that lands while the client waits for the reply of a request must not leave that reply to be read as the answer
to the next one. During a publish the worker is busy and most of the client's time is spent waiting for the reply of a
`scan_progress`, so a Ctrl-C there made the cancel "answer" with a progress record: the cancel looked as if it had come in time,
and the result said "3054 not read" for a batch that was then published whole.

The Ctrl-C is put where it landed: `_ctrl_c_on_reads` raises `KeyboardInterrupt` in the client's wait for a reply (what the
interpreter does at the Ctrl-C), and the worker is in this process on a pair of in-memory sockets (`tests.fakes`): no signal, no timer,
no sleep, no process. The last test is the same Ctrl-C against a real server that holds a batch in its publish and a real publish.
"""

from __future__ import annotations

import threading

import duckdb
import pytest

from drspec import staleness_worker, worker_ipc
from drspec.core import scan_service
from drspec.db import init_schema
from drspec.staleness_worker import _remediate_queued_files
from drspec.worker_ipc import (
    WorkerConnection,
    WorkerIPCServer,
    _read_line,
    _send_message,
)
from tests.fakes import fake_socket_pair


class _Server(threading.Thread):
    """A worker that answers each request with its number."""

    def __init__(self, sock) -> None:
        super().__init__(daemon=True)
        self.sock, self.seen = sock, []

    def run(self) -> None:
        buffer = bytearray()
        number = 0
        while True:
            line = _read_line(self.sock, buffer)
            if line is None:
                return
            number += 1
            self.seen.append(line)
            _send_message(self.sock, {"ok": True, "result": {"answer_to": number}})


@pytest.fixture
def pair():
    client_sock, server_sock = fake_socket_pair()
    yield client_sock, server_sock
    client_sock.close()
    server_sock.close()


def _ctrl_c_on_reads(monkeypatch, connection, *numbers: int) -> None:
    """A Ctrl-C (a `KeyboardInterrupt` in the main thread) at the `numbers`-th wait for a reply on `connection` (1 is the first): the
    request is already sent, its reply is still to come. Only this connection's reads are touched (the server's own are not)."""
    real = worker_ipc._read_line
    waits = []

    def read_line(sock, buffer):
        if sock is connection._sock:
            waits.append(1)
            if len(waits) in numbers:
                raise KeyboardInterrupt
        return real(sock, buffer)

    monkeypatch.setattr(worker_ipc, "_read_line", read_line)


@pytest.mark.unit  # in-memory sockets and a thread: no process, no real socket, no sleep
class TestTheNextRequestGetsItsOwnReply:
    def test_a_reply_left_by_an_interrupted_request_is_not_read_as_the_next_answer(self, pair, monkeypatch):
        client_sock, server_sock = pair
        _Server(server_sock).start()
        connection = WorkerConnection(client_sock)
        _ctrl_c_on_reads(monkeypatch, connection, 1)
        with pytest.raises(KeyboardInterrupt):
            connection.scan_progress("s1")  # the reply of this one is still to come
        assert connection.scan_cancel("s1") == {"answer_to": 2}  # (it read 1 and dropped it: before, this was `{"answer_to": 1}`)
        assert connection.scan_progress("s1") == {"answer_to": 3}

    def test_a_second_interrupt_while_dropping_the_reply_keeps_the_count(self, pair, monkeypatch):
        client_sock, server_sock = pair
        _Server(server_sock).start()
        connection = WorkerConnection(client_sock)
        _ctrl_c_on_reads(monkeypatch, connection, 1, 2)  # the second is the wait for the first reply to go by, in the next request
        with pytest.raises(KeyboardInterrupt):
            connection.scan_progress("s1")
        with pytest.raises(KeyboardInterrupt):
            connection.scan_cancel("s1")
        # the cancel was never sent (it was interrupted while the first reply was being dropped): the next request is the second one
        assert connection.scan_detach("s1") == {"answer_to": 2}
        assert connection._unanswered == 0


    def test_requests_that_were_answered_leave_nothing_to_drop(self, pair):
        client_sock, server_sock = pair
        _Server(server_sock).start()
        connection = WorkerConnection(client_sock)
        assert [connection.scan_progress("s")["answer_to"] for _ in range(3)] == [1, 2, 3] and connection._unanswered == 0

    def test_a_lost_connection_is_an_error_not_a_wait(self, pair):
        client_sock, server_sock = pair
        connection = WorkerConnection(client_sock)
        server_sock.close()
        with pytest.raises(worker_ipc.WorkerIPCError):
            connection.scan_progress("s")
        assert connection._unanswered == 0


class _AlreadyStarted:
    """The client of a scan that was queued by the test (`scan_start` answers what it got), the rest as it is."""

    def __init__(self, inner, started) -> None:
        self.inner, self.started = inner, started

    def scan_start(self, **spec):
        return self.started

    def __getattr__(self, name):
        return getattr(self.inner, name)


@pytest.mark.component  # a real publish of a batch of files, on the worker's side of a pair of in-memory sockets, into an in-memory database
class TestACtrlCInAProgressRequestDuringThePublish:
    FILES, FUNCTIONS = 12, 8

    def test_it_says_the_batch_is_being_finished_and_the_batch_is_published_whole(self, tmp_path, monkeypatch, serve, fake_time):
        monkeypatch.chdir(tmp_path)
        for index in range(self.FILES):
            (tmp_path / f"mod{index}.py").write_text("\n".join(f"def fn{index}_{n}(x):\n    return x + {n}\n" for n in range(self.FUNCTIONS)))
        db_path = tmp_path / "_drspec" / "contracts.db"  # (only its folder is looked at, to find the project; the database is in memory)
        db_path.parent.mkdir()
        conn = duckdb.connect(":memory:")
        init_schema(conn)
        spec = {"scan_id": scan_service.new_scan_id(), "targets": [], "recursive": True, "full": False, "deep": False,
                "embed": False, "code_only": False, "docs_only": False, "wait": True}
        started = scan_service.start_scan(conn, db_path, spec)
        files = [row[0] for row in conn.execute("SELECT file_path FROM scan_requests").fetchall()]
        inside, release = threading.Event(), threading.Event()
        real_begin = staleness_worker.begin_publish

        def begin_publish_and_hold(requests, file_paths):
            left_out = real_begin(requests, file_paths)  # the real one: from here the batch is "publishing" and a cancel is too late
            inside.set()
            release.wait(30)
            return left_out

        monkeypatch.setattr(staleness_worker, "begin_publish", begin_publish_and_hold)
        monkeypatch.setattr(scan_service, "time", fake_time)  # (the wait for a cancel is the fake clock's)
        served = serve(WorkerIPCServer(conn, db_path, remediate_fn=_remediate_queued_files))
        try:
            batch, client = served.client(), served.client()
            worker_thread = threading.Thread(target=lambda: batch.remediate(files))
            worker_thread.start()
            assert inside.wait(30), "the batch never reached its publish"
            _ctrl_c_on_reads(monkeypatch, client, 2)  # the first progress is answered; the Ctrl-C comes while the reply of the second is awaited
            seen = []
            summary = scan_service.run_scan(_AlreadyStarted(client, started), spec, on_progress=seen.append, poll_seconds=0.05)
            assert (seen[0]["batch"]["phase"], seen[0]["batch"]["files_total"]) == ("publishing", self.FILES)  # what the user saw before the Ctrl-C (the polls of the wait for the cancel come after it)
            assert summary["state"] == "cancelled"
            # the cancel came inside the publish: that batch is finished, not undone, and its files are not "not read"
            assert summary["batch_in_flight"] is True and summary["files_in_flight"] == self.FILES and summary["files_cancelled"] == 0, summary
            assert "still being finished, not undone" in summary["message"] and "not read" not in summary["message"], summary
        finally:
            release.set()
        worker_thread.join(30)
        assert not worker_thread.is_alive()
        # and it is published whole
        assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == self.FILES * self.FUNCTIONS
        conn.close()
