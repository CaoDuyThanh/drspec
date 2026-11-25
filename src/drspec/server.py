"""Local HTTP server for the live, backend-connected graph viewer
(``drspec serve``).

Design decisions made here:

- **Read-only.** The server never writes to the DB.
- **Routed through the worker, not a direct connection.** This module
  used to open a fresh, held-open-only-per-request *read-only*
  `duckdb.connect()` on the premise that a read-only connection never
  blocks a writer — false for this project's actual DuckDB version, a
  held-open read-only connection blocks a concurrent writer too. The
  worker is now the only process that ever opens the file directly;
  every request here is a `WorkerConnection`
  (`worker_ipc.get_worker_connection`), opened and closed per request
  same as before, just as an IPC client rather than a real connection.
- **Blocks the terminal; Ctrl-C to stop.** No daemonization. Every other
  drspec command to date is a one-shot invocation that exits on its own;
  a background-daemon model is a meaningfully bigger change to this
  project's operating model, not something to drift into by accident.
- **127.0.0.1 only, never 0.0.0.0.** Never exposed beyond the local
  machine.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import duckdb

from drspec.db import get_artifact
from drspec.worker_ipc import WorkerIPCError, get_worker_connection
from drspec.visualization.grapher import (
    graph_to_dict,
    build_dependency_graph,
    layout_graph,
    render_live_graph_html,
)

DEFAULT_PORT = 8787
DEFAULT_HOST = "127.0.0.1"
_VALID_DIRECTIONS = ("callers", "callees", "both")


class _GraphRequestHandler(BaseHTTPRequestHandler):
    """Serves the live viewer page and its `/graph` JSON endpoint.
    Per-request config (`db_path`, initial function/depth/direction)
    lives on `self.server` — set by `run_server` before `serve_forever`
    — not on the handler class, since `HTTPServer` instantiates a fresh
    handler object per request and only ever passes it the server
    instance, not arbitrary extra arguments.
    """

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        pass  # keep the terminal quiet; errors still surface in the JSON response itself

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._serve_index()
        elif parsed.path == "/graph":
            self._serve_graph(parsed.query)
        else:
            self._send_json(404, {"error": f"Not found: {parsed.path}"})

    def _serve_index(self) -> None:
        html_doc = render_live_graph_html(
            self.server.initial_function_id,  # type: ignore[attr-defined]
            self.server.initial_depth,  # type: ignore[attr-defined]
            self.server.initial_direction,  # type: ignore[attr-defined]
        )
        body = html_doc.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_graph(self, query: str) -> None:
        params = parse_qs(query)
        function_id = (params.get("function_id") or [None])[0]
        if not function_id:
            self._send_json(400, {"error": "function_id query parameter is required"})
            return

        try:
            depth = int((params.get("depth") or [str(self.server.initial_depth)])[0])  # type: ignore[attr-defined]
        except ValueError:
            depth = self.server.initial_depth  # type: ignore[attr-defined]
        depth = max(1, min(5, depth))

        direction = (params.get("direction") or ["both"])[0]
        if direction not in _VALID_DIRECTIONS:
            direction = "both"

        db_path = self.server.db_path  # type: ignore[attr-defined]
        try:
            conn = get_worker_connection(db_path, read_only=True)
        except WorkerIPCError as e:
            self._send_json(
                503, {"error": f"Could not reach the drspec worker — try again: {e}"}
            )
            return

        try:
            artifact = get_artifact(conn, function_id)
            if artifact is None:
                self._send_json(404, {"error": f"Function not found: {function_id}"})
                return

            G = build_dependency_graph(conn, function_id, depth, direction)  # type: ignore[arg-type]
            pos = layout_graph(G)
            graph_data = graph_to_dict(G, pos)
            graph_data["title"] = f"Dependencies: {artifact.function_name}"
            self._send_json(200, graph_data)
        except duckdb.Error as e:
            self._send_json(503, {"error": f"Database error — try again: {e}"})
        finally:
            conn.close()

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_server(
    db_path: Path,
    function_id: str,
    depth: int = 2,
    direction: str = "both",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> None:
    """Start the local graph-viewer server. Blocks until Ctrl-C.

    Args:
        db_path: Path to the DuckDB database file.
        function_id: Initial center function ID for the viewer.
        depth: Initial traversal depth.
        direction: Initial traversal direction.
        host: Bind address — always 127.0.0.1 in practice (the CLI layer
            doesn't expose a way to override this).
        port: Port to bind. `0` lets the OS assign a free port.

    Raises:
        RuntimeError: If the port can't be bound (e.g. already in use).
    """
    try:
        httpd = ThreadingHTTPServer((host, port), _GraphRequestHandler)
    except OSError as e:
        raise RuntimeError(
            f"Could not bind to {host}:{port} ({e}). Try a different port with --port, "
            "or --port 0 to let the OS pick one."
        ) from e

    httpd.db_path = db_path  # type: ignore[attr-defined]
    httpd.initial_function_id = function_id  # type: ignore[attr-defined]
    httpd.initial_depth = depth  # type: ignore[attr-defined]
    httpd.initial_direction = direction  # type: ignore[attr-defined]

    url = f"http://{host}:{httpd.server_port}/"
    print(f"drspec serve: {url}")
    print("Open it with VS Code's built-in preview (or any browser). Press Ctrl-C to stop.")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        httpd.server_close()
