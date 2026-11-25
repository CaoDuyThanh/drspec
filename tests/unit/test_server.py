"""The live graph viewer's HTTP handler (`drspec.server._GraphRequestHandler`): what each URL answers.

The handler is driven in this process, with no socket (`tests.fakes.FakeHttpServer` gives it the bytes of a GET request and returns what it wrote),
over an in-memory project database. Two tests keep a real operating-system socket because it is their objective: one real HTTP round trip through
the standard library's `ThreadingHTTPServer` with our handler, and the failure to bind a port that is taken (`run_server`).
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from drspec import server as server_module
from drspec.db import get_connection, init_schema, insert_artifact
from drspec.server import DEFAULT_HOST, _GraphRequestHandler
from tests.fakes import FakeHttpServer
from tests.helpers import insert_dependency


def _add(conn, name: str, line: int, status: str = "PENDING") -> None:
    insert_artifact(
        conn,
        function_id=f"src/app.py::{name}",
        file_path="src/app.py",
        function_name=name,
        signature=f"def {name}():",
        body=f"def {name}(): pass",
        code_hash=f"hash_{name}",
        language="python",
        start_line=line,
        end_line=line + 1,
        status=status,
    )


@pytest.fixture
def db_path(tmp_path, kept_duckdb):
    """A project database (one in-memory database kept between the requests) with `main` calling `helper`."""
    path = tmp_path / "_drspec" / "contracts.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(path)
    init_schema(conn)
    _add(conn, "main", 1)
    _add(conn, "helper", 4, status="VERIFIED")
    insert_dependency(conn, "src/app.py::main", "src/app.py::helper")
    conn.close()
    return path


@pytest.fixture
def viewer(db_path):
    return FakeHttpServer(_GraphRequestHandler, db_path=db_path, initial_function_id="src/app.py::main", initial_depth=2, initial_direction="both")


class TestServeIndex:
    def test_index_returns_html(self, viewer):
        answer = viewer.get("/")

        assert answer.status == 200
        assert answer.headers["content-type"].startswith("text/html")
        body = answer.body.decode("utf-8")
        assert "<!DOCTYPE html>" in body
        assert "src/app.py::main" in body  # initial function_id baked into the page


class TestServeGraphEndpoint:
    def test_returns_real_graph_data(self, viewer):
        answer = viewer.get("/graph?function_id=src/app.py::main&depth=2&direction=both")
        body = answer.json()

        assert answer.status == 200
        assert body["title"] == "Dependencies: main"
        assert {n["id"] for n in body["nodes"]} == {"src/app.py::main", "src/app.py::helper"}
        assert len(body["edges"]) == 1
        assert body["edges"][0]["source"] == "src/app.py::main"
        assert body["edges"][0]["target"] == "src/app.py::helper"

    def test_missing_function_id_is_400(self, viewer):
        answer = viewer.get("/graph")

        assert answer.status == 400
        assert "function_id" in answer.json()["error"]

    def test_unknown_function_is_404(self, viewer):
        answer = viewer.get("/graph?function_id=nope.py::nothing")

        assert answer.status == 404
        assert "nope.py::nothing" in answer.json()["error"]

    def test_invalid_direction_falls_back_to_both(self, viewer):
        """Never guessed into an error for a bad query param — falls
        back to the safe default rather than 400ing on something a user
        might type by hand in the URL bar."""
        answer = viewer.get("/graph?function_id=src/app.py::main&direction=sideways")

        assert answer.status == 200
        assert "src/app.py::helper" in {n["id"] for n in answer.json()["nodes"]}

    def test_depth_is_clamped_to_valid_range_or_the_initial_one(self, viewer, depth="999"):
        answer = viewer.get("/graph?function_id=src/app.py::main&depth=999")

        assert answer.status == 200
        assert {n["id"] for n in answer.json()["nodes"]} == {"src/app.py::main", "src/app.py::helper"}

    def test_each_request_opens_its_own_connection_and_releases_it(self, viewer, monkeypatch):
        """Each request must open and fully release its own connection, so back-to-back requests never interfere with each other."""
        opened, closed = [], []
        real_get = server_module.get_worker_connection

        def tracked(*args, **kwargs):
            connection = real_get(*args, **kwargs)
            opened.append(connection)
            real_close = connection.close
            connection.close = lambda: (closed.append(connection), real_close())[1]
            return connection

        monkeypatch.setattr(server_module, "get_worker_connection", tracked)

        for _ in range(5):
            answer = viewer.get("/graph?function_id=src/app.py::main")
            assert answer.status == 200 and len(answer.json()["nodes"]) == 2

        assert len(opened) == 5 and len(closed) == 5 and all(c is o for c, o in zip(closed, opened))

    def test_a_write_between_two_requests_is_seen_by_the_second(self, viewer, db_path):
        """The server never caches graph data: what is stored after the first request is in the answer to the next."""
        assert len(viewer.get("/graph?function_id=src/app.py::main").json()["nodes"]) == 2

        conn = get_connection(db_path)
        _add(conn, "extra", 7)
        insert_dependency(conn, "src/app.py::main", "src/app.py::extra")
        conn.close()

        assert "src/app.py::extra" in {n["id"] for n in viewer.get("/graph?function_id=src/app.py::main").json()["nodes"]}


@pytest.mark.socket  # the objective is the real thing: our handler behind the standard library's server, on a real localhost socket  # pattern-ok: slow-socket-model-marker a real localhost socket is the objective of this test class
class TestARealServer:
    def test_a_real_http_request_gets_the_graph(self, db_path):
        httpd = ThreadingHTTPServer((DEFAULT_HOST, 0), _GraphRequestHandler)
        httpd.db_path = db_path
        httpd.initial_function_id = "src/app.py::main"
        httpd.initial_depth = 2
        httpd.initial_direction = "both"
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f"http://{DEFAULT_HOST}:{httpd.server_port}/graph?function_id=src/app.py::main", timeout=30) as response:
                status, body = response.status, json.loads(response.read())
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=30)

        assert status == 200
        assert {n["id"] for n in body["nodes"]} == {"src/app.py::main", "src/app.py::helper"}


@pytest.mark.socket  # a port that is really taken is the objective  # pattern-ok: slow-socket-model-marker a real localhost socket is the objective of this test class
class TestRunServerPortBinding:
    def test_raises_runtime_error_on_port_already_in_use(self, db_path):
        from drspec.server import run_server

        occupying = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        occupying.bind((DEFAULT_HOST, 0))
        occupying.listen(1)
        taken_port = occupying.getsockname()[1]

        try:
            with pytest.raises(RuntimeError, match="Could not bind"):
                run_server(db_path, "src/app.py::main", port=taken_port)
        finally:
            occupying.close()
