"""Tests for the hand-rolled MCP stdio server.

Drives `handle_message` directly (the dict-in/dict-out core, independent
of the actual stdio framing) for the bulk of the coverage, plus a few
`run()` tests over `io.StringIO` to cover the newline-delimited framing
itself. The *real* MCP-client integration (an actual client connecting,
listing these tools, and calling them) isn't something a unit test can
reproduce, since it requires a genuine external MCP client process.

Every handler reaches the database through ONE function, `mcp_server.get_worker_connection`, so that is where the tests
plug in: `db_path` puts an in-memory project database (with two functions and one call) behind it, and `conn` is that
database for a test that wants to seed more rows. A test that never reaches the database uses `no_db`. The real thing is kept
only where it is the objective: the `init` that creates a project, the `scan` that lands an artifact, the status of a
database file, the `.gitignore`-free listing of a real folder.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from drspec import mcp_server
from drspec.core import compact_format
from drspec.db import (
    enqueue_staleness,
    get_connection,
    init_schema,
    insert_artifact,
    insert_contract,
)
from drspec.db.queries import insert_vision_finding
from drspec.mcp_server import handle_message, run
from tests.fakes import FakeGit
from tests.helpers import fid, file_hash, folder_listing, insert_dependency, table_counts

pytestmark = pytest.mark.usefixtures("fixed_build")  # every tool call asks for the build id, which hashes the package otherwise


def _seed(conn) -> None:
    insert_artifact(
        conn,
        function_id=fid("src/app.py", "main"),
        file_path="src/app.py",
        function_name="main",
        signature="def main():",
        body="def main(): return helper()",
        code_hash="hash_main",
        language="python",
        start_line=1,
        end_line=2,
        status="PENDING",
    )
    insert_artifact(
        conn,
        function_id=fid("src/app.py", "helper"),
        file_path="src/app.py",
        function_name="helper",
        signature="def helper():",
        body="def helper(): pass",
        code_hash="hash_helper",
        language="python",
        start_line=4,
        end_line=5,
        status="VERIFIED",
    )
    insert_dependency(conn, fid("src/app.py", "main"), fid("src/app.py", "helper"))


class _Keep:
    """The project's one in-memory connection, handed out for every tool call; closing it does nothing."""

    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql, params=None):
        return self._connection.execute(sql, params or [])

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._connection, name)


@pytest.fixture
def conn():
    """The project database: in memory, with the schema, two functions in `src/app.py` and the call between them."""
    connection = duckdb.connect(":memory:")
    init_schema(connection)
    _seed(connection)
    yield connection
    connection.close()


@pytest.fixture
def db_path(tmp_path, conn, monkeypatch):
    """The path the tools are given, with `conn` behind it. Nothing is created at it (see `placed_db` for the tools that check
    that the file exists)."""
    monkeypatch.setattr(mcp_server, "get_worker_connection", lambda p, **kwargs: _Keep(conn))
    return tmp_path / "_drspec" / "contracts.db"


@pytest.fixture
def placed_db(db_path):
    """`db_path` with an empty placeholder file at it: `scan` and `learn_analyze` check that the database file exists."""
    db_path.parent.mkdir()
    db_path.touch()
    return db_path


@pytest.fixture
def no_db():
    """A database path for a test that never reaches the database (nothing is created)."""
    return Path("nowhere") / "_drspec" / "contracts.db"


@pytest.fixture(scope="module")
def initialized_template(tmp_path_factory):
    """A project with an initialized database file and nothing in it, made once for the module: for the few tests that need
    a real file (they copy it)."""
    root = tmp_path_factory.mktemp("initialized")
    path = root / "_drspec" / "contracts.db"
    path.parent.mkdir()
    connection = get_connection(path)  # pattern-ok: duckdb-file builds the template FILE that tests copy
    init_schema(connection)
    connection.close()
    before = file_hash(path)
    yield root
    assert file_hash(path) == before, "a test wrote into the template the module copies"


@pytest.fixture
def real_db(initialized_template, tmp_path):
    """A copy of the initialized project in `tmp_path`: the database file, ready to be opened."""
    shutil.copytree(initialized_template, tmp_path, dirs_exist_ok=True)
    return tmp_path / "_drspec" / "contracts.db"


def _call(db_path: Path, name: str, arguments: dict) -> dict:
    response = handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
        db_path,
    )
    return response["result"]


def _call_text(db_path: Path, name: str, arguments: dict) -> tuple[dict, bool]:
    """Calls a tool and parses its result as JSON — `format: "json"` is
    passed by default here since the tool's own default is labeled
    plain text, not JSON, and every existing caller of this helper was
    written against the old always-JSON behavior. A test about the
    plain-text default itself calls `_call` directly instead."""
    arguments = {**arguments, "format": arguments.get("format", "json")}
    result = _call(db_path, name, arguments)
    if result["isError"]:
        return result["content"][0]["text"], result["isError"]
    return json.loads(result["content"][0]["text"]), result["isError"]


class TestLifecycle:
    def test_initialize_echoes_requested_protocol_version(self, no_db):
        response = handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            no_db,
        )
        assert response["result"]["protocolVersion"] == "2025-06-18"
        assert response["result"]["capabilities"] == {"tools": {}}
        assert response["result"]["serverInfo"]["name"] == "drspec"

    def test_initialize_instructions_carry_the_legend_of_the_compact_format(self, no_db):
        response = handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, no_db)
        instructions = response["result"]["instructions"]
        assert compact_format.legend() in instructions  # built from the code of the encoder, not a copy
        assert "~B" in instructions and "THE FUNCTION ID" in instructions and "name#hash" in instructions

    def test_initialize_falls_back_without_a_requested_version(self, no_db):
        response = handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, no_db)
        assert response["result"]["protocolVersion"]

    def test_initialized_notification_gets_no_response(self, no_db):
        assert handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}, no_db) is None

    def test_ping_returns_empty_result(self, no_db):
        response = handle_message({"jsonrpc": "2.0", "id": 9, "method": "ping"}, no_db)
        assert response == {"jsonrpc": "2.0", "id": 9, "result": {}}

    def test_unknown_notification_gets_no_response(self, no_db):
        assert handle_message({"jsonrpc": "2.0", "method": "notifications/something_else"}, no_db) is None

    def test_unknown_method_with_id_is_a_protocol_error(self, no_db):
        response = handle_message({"jsonrpc": "2.0", "id": 5, "method": "nope"}, no_db)
        assert response["error"]["code"] == -32601


class TestToolsList:
    def test_lists_all_tools_with_schemas(self, no_db):
        response = handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, no_db)
        tools_by_name = {t["name"]: t for t in response["result"]["tools"]}
        # Every drspec command gets a tool, except what structurally
        # can't be a request/response tool call (`worker run`,
        # `serve`, `mcp-serve`).
        assert set(tools_by_name) == {
            "search", "deps_get", "refs_get", "strings_get", "unused",
            "status", "trace_summary",
            "contract_get", "contract_list", "source_get", "verify_script",
            "learn_history", "learn_stats", "vision_list",
            "scan", "init", "deps_plot",
            "contract_save", "verify_run", "learn_analyze",
            "vision_save", "vision_update", "worker_stop", "worker_status",
            "config_get", "config_set",
            "ack",
        }
        for tool in tools_by_name.values():
            assert tool["inputSchema"]["type"] == "object"
        assert "pattern" in tools_by_name["search"]["inputSchema"]["properties"]
        assert "function_id" in tools_by_name["deps_get"]["inputSchema"]["properties"]
        assert "path_prefix" in tools_by_name["unused"]["inputSchema"]["properties"]


class TestToolsCallDispatch:
    def test_unknown_tool_is_a_protocol_error(self, no_db):
        response = handle_message(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "nope", "arguments": {}}},
            no_db,
        )
        assert response["error"]["code"] == -32602
        assert "nope" in response["error"]["message"]

    def test_tool_internal_exception_becomes_a_tool_error_not_a_crash(self, no_db, monkeypatch):
        import drspec.mcp_server as mcp_server_module

        def _boom(_no_db, _arguments):
            raise RuntimeError("boom")

        monkeypatch.setitem(mcp_server_module._TOOL_HANDLERS, "search", _boom)
        response = handle_message(
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "search", "arguments": {"pattern": "x"}}},
            no_db,
        )
        assert response["result"]["isError"] is True
        assert "boom" in response["result"]["content"][0]["text"]


class TestSearchTool:
    def test_literal_substring_match(self, db_path):
        data, is_error = _call_text(db_path, "search", {"pattern": "helper"})
        assert is_error is False
        ids = {m["function_id"] for m in data["matches"]}
        assert ids == {fid("src/app.py", "main"), fid("src/app.py", "helper")}  # "helper" appears in main's body too

    def test_regex_match(self, db_path):
        data, is_error = _call_text(db_path, "search", {"pattern": "^helper$", "regex": True})
        assert is_error is False
        assert [m["function_id"] for m in data["matches"]] == [fid("src/app.py", "helper")]

    def test_possibly_stale_flag_reflects_the_staleness_queue(self, db_path, conn):
        """Reads whatever the (simulated, here) worker already
        flagged, not a live recheck."""
        enqueue_staleness(conn, "src/app.py", "mtime_changed")

        data, is_error = _call_text(db_path, "search", {"pattern": "helper"})
        assert is_error is False
        assert all(m["possibly_stale"] for m in data["matches"])

    def test_missing_pattern_is_a_tool_error(self, db_path):
        result = _call(db_path, "search", {})
        assert result["isError"] is True
        assert "pattern" in result["content"][0]["text"]

    def test_invalid_status_is_a_tool_error(self, db_path):
        result = _call(db_path, "search", {"pattern": "helper", "status": "NOT_A_STATUS"})
        assert result["isError"] is True
        assert "NOT_A_STATUS" in result["content"][0]["text"]

    def test_semantic_without_any_embeddings_explains_why_empty(self, db_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "embed_text", lambda text: [0.0] * 384)  # (the model itself is not what is asked about)
        result = _call(db_path, "search", {"pattern": "does something", "semantic": True})
        assert result["isError"] is True
        assert "--embed" in result["content"][0]["text"]

    def test_db_not_initialized_is_a_tool_error_not_a_crash(self, tmp_path):
        missing_db = tmp_path / "_drspec" / "contracts.db"
        result = _call(missing_db, "search", {"pattern": "anything"})
        assert result["isError"] is True
        assert "drspec init" in result["content"][0]["text"]

    def _write_app_file(self, db_path, monkeypatch, text="def main():\n    return helper()\n\n\ndef helper():\n    pass\n"):
        monkeypatch.chdir(db_path.parent.parent)
        (db_path.parent.parent / "src").mkdir(exist_ok=True)
        (db_path.parent.parent / "src" / "app.py").write_text(text)

    def test_lines_returns_flat_live_hits_labelled_by_function(self, db_path, monkeypatch):
        self._write_app_file(db_path, monkeypatch)

        data, is_error = _call_text(db_path, "search", {"pattern": "helper", "lines": True})

        assert is_error is False
        assert data["hits"] == [
            {"file_path": "src/app.py", "line": 2, "text": "    return helper()", "function_id": fid("src/app.py", "main")},
            {"file_path": "src/app.py", "line": 5, "text": "def helper():", "function_id": fid("src/app.py", "helper")},
        ]
        assert "matches" not in data

    def test_lines_reaches_a_line_no_function_body_contains(self, db_path, monkeypatch):
        self._write_app_file(
            db_path, monkeypatch, "def main():\n    return helper()\n\n\ndef helper():\n    pass\n\nLIMIT = 5\n",
        )

        by_function, _ = _call_text(db_path, "search", {"pattern": "LIMIT"})
        by_line, _ = _call_text(db_path, "search", {"pattern": "LIMIT", "lines": True})

        assert by_function["matches"] == []
        assert [(h["line"], h["function_id"]) for h in by_line["hits"]] == [(8, None)]

    def test_context_flags_merge_into_blocks(self, db_path, monkeypatch):
        self._write_app_file(db_path, monkeypatch)

        data, is_error = _call_text(db_path, "search", {"pattern": "helper", "context": 1})

        assert is_error is False
        # Lines 2 and 5's ±1 windows ([1,3] and [4,6]) touch, so they
        # merge into one block, as grep does.
        (block,) = data["blocks"]
        assert block["matched_lines"] == [2, 5]
        assert (block["start_line"], block["end_line"]) == (1, 6)
        assert block["functions"] == [fid("src/app.py", "main"), fid("src/app.py", "helper")]

    def test_lines_limit_truncates_and_says_so(self, db_path, monkeypatch):
        self._write_app_file(db_path, monkeypatch)

        data, _ = _call_text(db_path, "search", {"pattern": "helper", "lines": True, "limit": 1})

        assert data["total_hits"] == 1
        assert data["truncated"] is True

    def test_lines_rejects_function_level_filters(self, db_path):
        for extra in ({"invert_match": True}, {"status": "PENDING"}):
            result = _call(db_path, "search", {"pattern": "helper", "lines": True, **extra})
            assert result["isError"] is True

    def test_lines_with_an_invalid_regex_is_a_tool_error(self, db_path, monkeypatch):
        self._write_app_file(db_path, monkeypatch)

        result = _call(db_path, "search", {"pattern": "(unclosed", "regex": True, "lines": True})

        assert result["isError"] is True
        assert "Invalid regex" in result["content"][0]["text"]

    def test_lines_combined_with_semantic_is_a_tool_error(self, db_path):
        result = _call(db_path, "search", {"pattern": "does something", "semantic": True, "lines": True})

        assert result["isError"] is True


class TestSearchFlagParity:
    """`invert_match`/`count`/`files_with_matches` — full flag parity
    with `drspec search`."""

    def test_count_returns_the_total_only_no_match_detail(self, db_path):
        data, is_error = _call_text(db_path, "search", {"pattern": "helper", "count": True})
        assert is_error is False
        assert data == {"pattern": "helper", "count": 2, "files_searched": 1}  # both main (calls it) and helper (its own signature)

    def test_files_with_matches_returns_distinct_paths_only(self, db_path):
        data, is_error = _call_text(db_path, "search", {"pattern": "helper", "files_with_matches": True})
        assert is_error is False
        assert data == {"pattern": "helper", "files": ["src/app.py"], "file_count": 1, "files_searched": 1}

    def test_invert_match_shows_the_non_matching_function(self, db_path):
        """"pass" only appears in `helper`'s body — inverted, only `main` qualifies."""
        data, is_error = _call_text(db_path, "search", {"pattern": "pass", "invert_match": True})
        assert is_error is False
        assert [m["function_id"] for m in data["matches"]] == [fid("src/app.py", "main")]


class TestDepsGetTool:
    def test_callees_direction(self, db_path):
        data, is_error = _call_text(db_path, "deps_get", {"function_id": fid("src/app.py", "main"), "direction": "callees"})
        assert is_error is False
        assert data["callers"] == []
        assert [c["function_id"] for c in data["callees"]] == [fid("src/app.py", "helper")]

    def test_possibly_stale_flag_on_the_root_function_and_its_callees(self, db_path, conn):
        enqueue_staleness(conn, "src/app.py", "mtime_changed")

        data, is_error = _call_text(db_path, "deps_get", {"function_id": fid("src/app.py", "main"), "direction": "callees"})
        assert is_error is False
        assert all(c["possibly_stale"] for c in data["callees"])

    def test_callers_direction(self, db_path):
        data, is_error = _call_text(db_path, "deps_get", {"function_id": fid("src/app.py", "helper"), "direction": "callers"})
        assert is_error is False
        assert data["callees"] == []
        assert [c["function_id"] for c in data["callers"]] == [fid("src/app.py", "main")]

    def test_unknown_function_suggests_similar_names(self, db_path):
        result = _call(db_path, "deps_get", {"function_id": fid("src/app.py", "helper_typo")})
        assert result["isError"] is True
        assert fid("src/app.py", "helper") in result["content"][0]["text"]

    def test_invalid_direction_is_a_tool_error(self, db_path):
        result = _call(db_path, "deps_get", {"function_id": fid("src/app.py", "main"), "direction": "sideways"})
        assert result["isError"] is True

    def test_depth_is_clamped_not_rejected(self, db_path):
        result = _call(db_path, "deps_get", {"function_id": fid("src/app.py", "main"), "depth": 999})
        assert result["isError"] is False

    def test_missing_function_id_is_a_tool_error(self, db_path):
        result = _call(db_path, "deps_get", {})
        assert result["isError"] is True


class TestUnusedTool:
    def test_lists_only_the_real_zero_caller_function(self, db_path):
        """Fixture: `main` calls `helper`, so `helper` has a real caller
        and must be excluded; `main` itself has none and must appear."""
        data, is_error = _call_text(db_path, "unused", {})
        assert is_error is False
        assert [f["function_id"] for f in data["functions"]] == [fid("src/app.py", "main")]
        assert data["total_count"] == 1
        assert data["functions"][0]["only_test_callers"] is False

    def test_only_test_callers_flag_distinguishes_from_zero_callers(self, db_path, conn):
        """A function called only from a test file must still appear,
        flagged distinctly from `main`'s genuinely zero-caller case."""
        insert_artifact(
            conn, function_id=fid("tests/test_app.py", "test_main"), file_path="tests/test_app.py",
            function_name="test_main", signature="def test_main():", body="pass",
            code_hash="hash_test_main", language="python", start_line=1, end_line=2,
        )
        insert_dependency(conn, fid("tests/test_app.py", "test_main"), fid("src/app.py", "main"))

        data, is_error = _call_text(db_path, "unused", {})
        assert is_error is False
        flags = {f["function_id"]: f["only_test_callers"] for f in data["functions"]}
        assert flags[fid("src/app.py", "main")] is True

    def test_path_prefix_filters(self, db_path):
        data, is_error = _call_text(db_path, "unused", {"path_prefix": "no/such/path"})
        assert is_error is False
        assert data["functions"] == []
        assert data["total_count"] == 0

    def test_possibly_stale_flag(self, db_path, conn):
        enqueue_staleness(conn, "src/app.py", "mtime_changed")

        data, is_error = _call_text(db_path, "unused", {})
        assert is_error is False
        assert all(f["possibly_stale"] for f in data["functions"])

    def test_limit_is_respected(self, db_path):
        data, is_error = _call_text(db_path, "unused", {"limit": 1})
        assert is_error is False
        assert len(data["functions"]) <= 1

    def test_db_not_initialized_is_a_tool_error_not_a_crash(self, tmp_path):
        missing_db = tmp_path / "_drspec" / "contracts.db"
        result = _call(missing_db, "unused", {})
        assert result["isError"] is True
        assert "drspec init" in result["content"][0]["text"]


class TestStatusTool:
    def test_reports_artifact_backlog_and_edge_counts(self, db_path):
        data, is_error = _call_text(db_path, "status", {})
        assert is_error is False
        assert data["artifacts"]["total"] == 2
        assert data["artifacts"]["by_status"]["VERIFIED"] == 1
        assert data["edges"]["calls"]["total"] == 1
        assert "queue" not in data
        assert data["backlog"]["files_pending_rescan"] == 0
        assert data["backlog"]["embedding"]["embedded"] == 0
        assert data["backlog"]["embedding"]["total"] == 3  # two functions and one edge


class TestContractQueueIsGone:
    @pytest.mark.parametrize("name", ["queue_peek", "queue_get", "queue_next", "queue_prioritize"])
    def test_the_queue_tools_no_longer_exist(self, no_db, name):
        response = handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, no_db)
        assert name not in {t["name"] for t in response["result"]["tools"]}
        called = handle_message(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": name, "arguments": {}}}, no_db
        )
        assert "error" in called


class TestContractTools:
    def test_get_and_list(self, db_path, conn):
        insert_contract(
            conn, fid("src/app.py", "main"),
            json.dumps({"function_signature": "def main():", "intent_summary": "runs the app", "invariants": []}),
            confidence_score=0.8,
        )

        get_data, is_error = _call_text(db_path, "contract_get", {"function_id": fid("src/app.py", "main")})
        assert is_error is False
        assert get_data["confidence"]["base"] == 80

        list_data, is_error = _call_text(db_path, "contract_list", {})
        assert is_error is False
        assert list_data["total"] == 1
        assert list_data["contracts"][0]["function_id"] == fid("src/app.py", "main")

    def test_get_missing_contract_suggests_similar(self, db_path):
        result = _call(db_path, "contract_get", {"function_id": fid("src/app.py", "mian")})
        assert result["isError"] is True
        assert "main" in result["content"][0]["text"]


class TestSourceGetTool:
    def test_returns_stored_source(self, db_path):
        data, is_error = _call_text(db_path, "source_get", {"function_id": fid("src/app.py", "helper")})
        assert is_error is False
        assert data["body"] == "def helper(): pass"
        assert data["start_line"] == 4
        assert data["possibly_stale"] is False

    def test_flags_source_from_a_file_the_worker_has_queued(self, db_path, conn):
        enqueue_staleness(conn, "src/app.py", "mtime_changed")

        data, is_error = _call_text(db_path, "source_get", {"function_id": fid("src/app.py", "helper")})
        assert is_error is False
        assert data["possibly_stale"] is True

    def test_missing_function_is_a_tool_error(self, db_path):
        result = _call(db_path, "source_get", {"function_id": fid("no/such.py", "fn")})
        assert result["isError"] is True


class TestVerifyScriptTool:
    def test_generates_a_script_when_none_is_stored(self, db_path, conn):
        insert_contract(
            conn, fid("src/app.py", "main"),
            json.dumps({
                "function_signature": "def main():",
                "intent_summary": "runs the app",
                "invariants": [{"name": "returns_none", "logic": "result is None", "criticality": "LOW", "on_fail": "warn"}],
            }),
        )

        data, is_error = _call_text(db_path, "verify_script", {"function_id": fid("src/app.py", "main")})
        assert is_error is False
        assert fid("src/app.py", "main") in data["script"]

    def test_missing_contract_is_a_tool_error(self, db_path):
        result = _call(db_path, "verify_script", {"function_id": fid("src/app.py", "main")})
        assert result["isError"] is True


class TestLearnTools:
    def test_history_and_stats_on_an_empty_history(self, db_path):
        history_data, is_error = _call_text(db_path, "learn_history", {})
        assert is_error is False
        assert history_data["events"] == []

        stats_data, is_error = _call_text(db_path, "learn_stats", {})
        assert is_error is False
        assert stats_data["total_events"] == 0


class TestVisionListTool:
    def test_lists_saved_findings(self, db_path, conn):
        insert_vision_finding(
            conn, fid("src/app.py", "main"), finding_type="outlier", significance="HIGH", description="spike",
        )

        data, is_error = _call_text(db_path, "vision_list", {})
        assert is_error is False
        assert data["total"] == 1
        assert data["findings"][0]["function_id"] == fid("src/app.py", "main")

    def test_filters_by_function_id(self, db_path, conn):
        insert_vision_finding(
            conn, fid("src/app.py", "main"), finding_type="outlier", significance="HIGH", description="spike",
        )

        data, is_error = _call_text(db_path, "vision_list", {"function_id": fid("src/app.py", "helper")})
        assert is_error is False
        assert data["total"] == 0


class TestScanTool:
    def test_scanning_a_real_file_lands_a_new_artifact_in_the_db(self, real_db, monkeypatch):
        # a real file and a real database file: the objective is that a scan through the tool lands an artifact
        db_path = real_db
        monkeypatch.chdir(db_path.parent.parent)
        (db_path.parent.parent / "newmod.py").write_text("def brand_new():\n    return 42\n")

        data, is_error = _call_text(db_path, "scan", {"path": "newmod.py", "embed": False})
        assert is_error is False
        assert data["files_scanned"] == 1
        assert data["functions_found"] == 1
        assert data["functions_new"] == 1

        source_data, is_error = _call_text(db_path, "source_get", {"function_id": fid("newmod.py", "brand_new")})
        assert is_error is False
        assert source_data["function_name"] == "brand_new"

    def test_a_large_scan_result_is_passed_through_as_the_scan_service_bounded_it(self, placed_db, monkeypatch):
        db_path = placed_db
        """The bounding itself (`summarize_by_reason`, `bounded_errors`) is `core/scan_service.py`'s and is tested through a real
        scan in `test_scan_command.py` (`test_per_function_maps_become_counts_plus_a_few_examples`,
        `test_errors_are_capped_but_counted`); here the tool must return what the service gave, unchanged and not as an error."""
        bounded = {
            "state": "done", "files_scanned": 65, "resolution_reasons": {"total": 40, "examples": ["x"] * 20},
            "error_count": 25, "errors": [{"file": "bad.py"}] * 20, "errors_omitted": 5,
        }
        monkeypatch.setattr("drspec.core.scan_service.run_scan", lambda connection, spec, max_wait=None: dict(bounded))
        monkeypatch.chdir(db_path.parent.parent)

        data, is_error = _call_text(db_path, "scan", {"path": ".", "embed": False})

        assert is_error is False
        assert {key: data[key] for key in bounded} == bounded

    def test_nonexistent_path_is_a_tool_error(self, db_path, tmp_path):
        result = _call(db_path, "scan", {"path": str(tmp_path / "no" / "such" / "path")})
        assert result["isError"] is True
        assert "does not exist" in result["content"][0]["text"]

    def test_db_not_initialized_is_a_tool_error_not_a_crash(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        missing_db = tmp_path / "_drspec" / "contracts.db"
        result = _call(missing_db, "scan", {"path": "."})
        assert result["isError"] is True
        assert "drspec init" in result["content"][0]["text"]


@pytest.fixture(scope="module")
def seeded_project(tmp_path_factory):
    """An initialized project (confidence threshold 70) with one function and one vision finding in it, made once for the module
    by one real `init` call: each `TestInitTool` test copies it and makes only the call it is about."""
    root = tmp_path_factory.mktemp("seeded")
    _call(root / "unused.db", "init", {"project_root": str(root), "confidence_threshold": 70})
    connection = get_connection(root / "_drspec" / "contracts.db")  # pattern-ok: duckdb-file builds the template FILE that tests copy
    insert_artifact(
        connection, function_id=fid("a.py", "f"), file_path="a.py", function_name="f", signature="def f():",
        body="pass", code_hash="h", language="python", start_line=1, end_line=2, status="PENDING",
    )
    insert_vision_finding(connection, fid("a.py", "f"), finding_type="outlier", significance="HIGH", description="spike")
    connection.close()
    before = (table_counts(root / "_drspec" / "contracts.db"), folder_listing(root))
    yield root
    assert (table_counts(root / "_drspec" / "contracts.db"), folder_listing(root)) == before, "a test wrote into the template the module copies"


class TestInitTool:
    """Real files are the objective here (the project folder `init` makes), so each test works in a real folder; the ones that
    start from an initialized project copy the one made once above (the folder with its database and agent templates)."""

    @pytest.fixture
    def project(self, seeded_project, tmp_path):
        shutil.copytree(seeded_project, tmp_path, dirs_exist_ok=True)
        return tmp_path / "unused.db"  # what the tool is given as `db_path`: it resolves its own path and never touches this

    def _row_counts(self, tmp_path):
        connection = get_connection(tmp_path / "_drspec" / "contracts.db")  # pattern-ok: duckdb-file reads back the file the `init` tool wrote
        counts = {t: connection.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("artifacts", "vision_findings")}
        connection.close()
        return counts

    def test_bootstraps_a_fresh_project(self, tmp_path):
        """`init` resolves its own path and never touches `db_path` (unused here), unlike every other tool."""
        unused_db_path = tmp_path / "unused.db"
        data, is_error = _call_text(unused_db_path, "init", {"project_root": str(tmp_path), "confidence_threshold": 90})
        assert is_error is False
        assert data["already_initialized"] is False
        assert data["confidence_threshold"] == 90

        db_path = tmp_path / "_drspec" / "contracts.db"
        assert db_path.exists()
        connection = get_connection(db_path)  # pattern-ok: duckdb-file reads back the file the `init` tool wrote
        from drspec.contracts.confidence import get_confidence_threshold

        assert get_confidence_threshold(connection) == 90
        connection.close()

    def test_reinit_without_force_is_skipped_not_destroyed(self, project, tmp_path):
        data, is_error = _call_text(project, "init", {"project_root": str(tmp_path)})
        assert is_error is False
        assert data["already_initialized"] is True
        assert data["overwrite_skipped"] is True

    def test_force_on_an_initialized_project_keeps_its_data(self, project, tmp_path):
        """`force` used to rebuild the schema, wiping artifacts, contracts and findings while replying "already initialized".
        The CLI's `init --force` keeps the data; so must this."""
        before = self._row_counts(tmp_path)
        assert before == {"artifacts": 1, "vision_findings": 1}

        data, is_error = _call_text(project, "init", {"project_root": str(tmp_path), "force": True})

        assert is_error is False
        assert data["already_initialized"] is True
        assert self._row_counts(tmp_path) == before

    def test_force_refreshes_the_agent_templates(self, project, tmp_path):
        agents = tmp_path / "_drspec" / "agents"
        removed = sorted(agents.glob("*.md"))[0]
        removed.unlink()

        skipped, _ = _call_text(project, "init", {"project_root": str(tmp_path)})
        assert skipped["overwrite_skipped"] is True
        assert not removed.exists()

        _call_text(project, "init", {"project_root": str(tmp_path), "force": True})
        assert removed.exists()

    def test_reinit_ignores_the_confidence_threshold(self, project, tmp_path):
        from drspec.contracts.confidence import get_confidence_threshold

        _call_text(project, "init", {"project_root": str(tmp_path), "force": True, "confidence_threshold": 50})

        connection = get_connection(tmp_path / "_drspec" / "contracts.db")  # pattern-ok: duckdb-file reads back the file the `init` tool wrote
        assert get_confidence_threshold(connection) == 70
        connection.close()

    def test_a_live_worker_holding_the_database_is_not_opened_directly(self, project, tmp_path, monkeypatch):
        """With a worker running the file is locked; probing or rebuilding it from here would fail on the lock, so an
        initialized project must be left alone."""
        import drspec.cli.commands.init as init_module

        def _locked(*args, **kwargs):
            raise AssertionError("opened the database directly while a worker holds it")

        monkeypatch.setattr(init_module, "is_worker_running", lambda path: 4242)
        monkeypatch.setattr(init_module, "get_connection", _locked)
        monkeypatch.setattr("drspec.mcp_server.get_connection", _locked)

        data, is_error = _call_text(project, "init", {"project_root": str(tmp_path), "force": True})

        assert is_error is False
        assert data["already_initialized"] is True

    def test_a_database_without_drspecs_schema_is_initialized_fresh(self, tmp_path):
        (tmp_path / "_drspec").mkdir()
        other = duckdb.connect(str(tmp_path / "_drspec" / "contracts.db"))  # pattern-ok: duckdb-file a real file with another schema is the objective
        other.execute("CREATE TABLE unrelated (x INTEGER)")
        other.close()

        data, is_error = _call_text(tmp_path / "unused.db", "init", {"project_root": str(tmp_path)})

        assert is_error is False
        assert data["already_initialized"] is False
        assert self._row_counts(tmp_path)["artifacts"] == 0


class TestDepsPlotTool:
    def test_html_plot_writes_a_real_file_with_content(self, db_path, tmp_path):  # (a real text file is the objective)
        output_path = tmp_path / "plots" / "out.html"
        data, is_error = _call_text(
            db_path, "deps_plot",
            {"function_id": fid("src/app.py", "main"), "html": True, "output_path": str(output_path), "depth": 1},
        )
        assert is_error is False
        assert data["nodes"] == 2
        assert data["edges"] == 1
        assert data["center_function"] == "main"

        plot_path = Path(data["output_path"])
        assert plot_path == output_path.resolve()
        assert plot_path.stat().st_size > 0
        assert plot_path.read_text().startswith("<")

    def test_png_plot_passes_the_requested_file_name_to_the_generator_and_returns_its_path(self, db_path, tmp_path, monkeypatch):
        """The generator is replaced by a recorder: the PNG drawing (matplotlib) is no longer tested anywhere, by the person's decision. What this
        proves is the tool's part: it hands the requested name and depth over and reports the path the generator returns. That a generator keeps
        the name it is given is proven for the HTML page by `test_grapher.py::test_output_file_is_used_verbatim_not_replaced_by_a_hashed_name`
        and `test_scan_graph_sync.py::test_deps_plot_html_output_writes_the_exact_requested_file_name`; for the PNG nothing proves it now."""
        from drspec.visualization import grapher

        output_path = tmp_path / "plots" / "my-graph.png"
        asked = {}

        def generator(**kwargs):
            asked.update(kwargs)
            return grapher.GraphResult(path=str(Path(kwargs["output_file"]).resolve()), plot_type="graph", width=10, height=10, data_points=2, nodes=2, edges=1)

        monkeypatch.setattr(grapher, "generate_dependency_graph", generator)
        data, is_error = _call_text(
            db_path, "deps_plot",
            {"function_id": fid("src/app.py", "main"), "output_path": str(output_path), "depth": 1},
        )
        assert is_error is False
        assert asked["output_file"] == str(output_path) and asked["depth"] == 1
        assert Path(data["output_path"]) == output_path.resolve()
        assert (data["nodes"], data["edges"]) == (2, 1)

    def test_unknown_function_suggests_similar_names(self, db_path):
        result = _call(db_path, "deps_plot", {"function_id": fid("src/app.py", "mian")})
        assert result["isError"] is True
        assert fid("src/app.py", "main") in result["content"][0]["text"]


class TestContractSaveTool:
    def test_saves_a_valid_contract(self, db_path):
        contract_json = json.dumps({
            "function_signature": "def main():",
            "intent_summary": "Runs the main application loop",
            "invariants": [{"name": "non_empty", "logic": "Output is not empty", "criticality": "HIGH", "on_fail": "error"}],
        })
        data, is_error = _call_text(
            db_path, "contract_save",
            {"function_id": fid("src/app.py", "main"), "contract_json": contract_json, "confidence": 85},
        )
        assert is_error is False
        assert data["status"] == "NEEDS_REVIEW"

        get_data, is_error = _call_text(db_path, "contract_get", {"function_id": fid("src/app.py", "main")})
        assert is_error is False
        assert get_data["confidence"]["base"] == 85

    def test_schema_validation_failure_surfaces_field_specific_details_not_just_the_generic_message(self, db_path):
        """Must surface `.error.details` (which names the field), not
        just the generic "Contract validation failed" message."""
        invalid_contract = json.dumps({
            "function_signature": "def main():",
            "intent_summary": "Runs the main application loop",
            "invariants": [],  # schema requires at least one
        })
        result = _call(
            db_path, "contract_save",
            {"function_id": fid("src/app.py", "main"), "contract_json": invalid_contract, "confidence": 85},
        )
        assert result["isError"] is True
        assert "invariants" in result["content"][0]["text"]

    def test_missing_artifact_is_a_tool_error(self, db_path):
        contract_json = json.dumps({
            "function_signature": "def f():",
            "intent_summary": "Does a thing reliably",
            "invariants": [{"name": "x", "logic": "always holds true", "criticality": "LOW", "on_fail": "warn"}],
        })
        result = _call(
            db_path, "contract_save",
            {"function_id": fid("no/such.py", "fn"), "contract_json": contract_json, "confidence": 50},
        )
        assert result["isError"] is True


class TestVerifyRunTool:
    """The script's execution is `contracts/executor.py`'s (`test_executor.py` runs real scripts); here the tool is given a
    result and must map it to `passed`/`failed`, record the evidence and set the status."""

    @pytest.fixture(autouse=True)
    def _a_runner_that_judges_by_the_output(self, monkeypatch):
        from drspec.contracts.executor import VerificationResult

        def execute(script, input_data, output_data, timeout=None):
            passed = bool(output_data)  # the contract's one invariant: "Output is not empty"
            return VerificationResult(
                passed=passed, message="ok" if passed else "invariant failed", execution_time=0.0, invariants_checked=1,
                invariants_passed=1 if passed else 0, invariants_failed=0 if passed else 1, status="passed" if passed else "failed",
                results=[{"name": "non_empty", "passed": passed}],
            )

        monkeypatch.setattr(mcp_server, "execute_verification", execute)

    def _insert_contract(self, conn):
        insert_contract(
            conn, fid("src/app.py", "main"),
            json.dumps({
                "function_signature": "def main():",
                "intent_summary": "Runs the main application loop",
                "invariants": [{"name": "non_empty", "logic": "Output is not empty", "criticality": "HIGH", "on_fail": "error"}],
            }),
            confidence_score=0.8,
        )

    def test_passing_verification_marks_the_artifact_verified(self, db_path, conn):
        self._insert_contract(conn)

        data, is_error = _call_text(
            db_path, "verify_run",
            {"function_id": fid("src/app.py", "main"), "input_data": {}, "output_data": [1, 2, 3]},
        )
        assert is_error is False
        assert data["passed"] is True
        assert data["status"] == "passed"

        get_data, is_error = _call_text(db_path, "contract_get", {"function_id": fid("src/app.py", "main")})
        assert is_error is False
        assert get_data["status"] == "VERIFIED"

    def test_failing_verification_marks_the_artifact_needs_review(self, db_path, conn):
        self._insert_contract(conn)

        data, is_error = _call_text(
            db_path, "verify_run",
            {"function_id": fid("src/app.py", "main"), "input_data": {}, "output_data": []},
        )
        assert is_error is False
        assert data["passed"] is False
        assert data["status"] == "failed"
        assert data["invariants"]["failed"] == 1

        get_data, is_error = _call_text(db_path, "contract_get", {"function_id": fid("src/app.py", "main")})
        assert is_error is False
        assert get_data["status"] == "NEEDS_REVIEW"

    def test_missing_contract_is_a_tool_error(self, db_path):
        result = _call(
            db_path, "verify_run",
            {"function_id": fid("src/app.py", "main"), "input_data": {}, "output_data": []},
        )
        assert result["isError"] is True


@pytest.fixture
def git_repo_with_a_bug_fix_commit(monkeypatch):
    """A repository as data (`tests.fakes.FakeGit`: no `git` runs): one ordinary commit and one bug-fix-shaped commit that adds
    a null check, which is enough for `analyze_commit_range`'s own bug-fix and pattern detection to fire for real. It is given
    to the two modules that shell out to git for `learn_analyze`, and its "path" is any string."""
    import drspec.learning.diff as diff_module

    git = FakeGit().install(monkeypatch, mcp_server, diff_module)
    git.commit({"src/app.py": "def process(items):\n    total = 0\n    for item in items:\n        total += item\n    return total\n"}, message="initial")
    git.commit(
        {"src/app.py": (
            "def process(items):\n    if items is None:\n        return 0\n    total = 0\n    for item in items:\n"
            "        total += item\n    return total\n"
        )},
        message="fix: handle None input",
    )
    return "a-repository"


class TestLearnAnalyzeTool:
    def test_dry_run_finds_the_pattern_and_suggests_invariants_without_recording(self, placed_db, git_repo_with_a_bug_fix_commit):
        db_path = placed_db
        data, is_error = _call_text(
            db_path, "learn_analyze",
            {"commit_range": "HEAD~1..HEAD", "repo_path": git_repo_with_a_bug_fix_commit, "dry_run": True},
        )
        assert is_error is False
        assert data["commits_analyzed"] == 1
        assert data["patterns_found"] >= 1
        assert data["results"][0]["function_id"] == fid("src/app.py", "process", "items")
        assert data["results"][0]["new_invariants"]  # the null check was turned into a suggestion
        assert data["results"][0]["commit"]  # each result says which commit it came from
        assert data["results"][0]["commit_message"]

        history_data, is_error = _call_text(db_path, "learn_history", {})
        assert is_error is False
        assert history_data["events"] == []  # dry_run never records

    def test_non_dry_run_with_a_detected_pattern_records_a_learning_event(
        self, placed_db, git_repo_with_a_bug_fix_commit,
    ):
        db_path = placed_db
        data, is_error = _call_text(
            db_path, "learn_analyze",
            {"commit_range": "HEAD~1..HEAD", "repo_path": git_repo_with_a_bug_fix_commit, "dry_run": False},
        )
        assert is_error is False
        assert data["patterns_found"] >= 1

        history_data, is_error = _call_text(db_path, "learn_history", {})
        assert is_error is False
        assert len(history_data["events"]) >= 1
        assert history_data["events"][0]["function_id"] == fid("src/app.py", "process", "items")

    def test_invalid_commit_range_is_a_tool_error(self, db_path):
        result = _call(db_path, "learn_analyze", {"commit_range": "not-a-range"})
        assert result["isError"] is True

    def test_non_git_repo_path_is_a_tool_error(self, db_path, tmp_path, monkeypatch):
        def not_a_repository(command, **kwargs):  # what `git rev-parse` does outside a repository
            raise subprocess.CalledProcessError(128, command)

        monkeypatch.setattr(mcp_server, "subprocess", SimpleNamespace(run=not_a_repository, CalledProcessError=subprocess.CalledProcessError))
        result = _call(db_path, "learn_analyze", {"commit_range": "HEAD~1..HEAD", "repo_path": str(tmp_path)})
        assert result["isError"] is True
        assert "Not a git repository" in result["content"][0]["text"]


class TestVisionSaveAndUpdateTools:
    def test_save_then_update_round_trips_through_vision_list(self, db_path):
        save_data, is_error = _call_text(
            db_path, "vision_save",
            {
                "function_id": fid("src/app.py", "main"), "finding_type": "outlier",
                "significance": "high", "description": "a spike was observed",
            },
        )
        assert is_error is False
        assert save_data["status"] == "NEW"
        finding_id = save_data["finding_id"]

        update_data, is_error = _call_text(
            db_path, "vision_update", {"finding_id": finding_id, "status": "addressed", "note": "confirmed benign"},
        )
        assert is_error is False
        assert update_data["status"] == "ADDRESSED"

        list_data, is_error = _call_text(db_path, "vision_list", {"function_id": fid("src/app.py", "main")})
        assert is_error is False
        assert list_data["findings"][0]["status"] == "ADDRESSED"

    def test_save_missing_required_argument_is_a_tool_error(self, db_path):
        result = _call(db_path, "vision_save", {"function_id": fid("src/app.py", "main"), "finding_type": "outlier"})
        assert result["isError"] is True

    def test_update_of_an_unknown_finding_is_a_tool_error(self, db_path):
        result = _call(db_path, "vision_update", {"finding_id": 99999, "status": "addressed"})
        assert result["isError"] is True
        assert "99999" in result["content"][0]["text"]


class TestWorkerStopTool:
    def test_no_worker_running_is_not_an_error(self, no_db):
        data, is_error = _call_text(no_db, "worker_stop", {})
        assert is_error is False
        assert data == {"stopped": False, "message": "No worker is running."}

    def test_reports_stopped_once_the_worker_has_exited(self, no_db, monkeypatch):
        import drspec.mcp_server as mcp_server_module

        monkeypatch.setattr(mcp_server_module, "stop_worker", lambda _no_db: (4242, True))

        data, is_error = _call_text(no_db, "worker_stop", {})
        assert is_error is False
        assert data == {"stopped": True, "pid": 4242}

    def test_a_worker_that_outlives_the_wait_is_a_tool_error(self, no_db, monkeypatch):
        import drspec.mcp_server as mcp_server_module

        monkeypatch.setattr(mcp_server_module, "stop_worker", lambda _no_db: (4242, False))

        result = _call(no_db, "worker_stop", {})
        assert result["isError"] is True
        assert "4242" in result["content"][0]["text"]

    def test_a_worker_that_cant_be_signaled_is_a_tool_error_not_a_crash(self, no_db, monkeypatch):
        import drspec.mcp_server as mcp_server_module

        def _fail(_no_db):
            raise ProcessLookupError("no such process")

        monkeypatch.setattr(mcp_server_module, "stop_worker", _fail)

        result = _call(no_db, "worker_stop", {})
        assert result["isError"] is True
        assert "Could not stop worker" in result["content"][0]["text"]


class TestConfigTools:
    def test_get_lists_everything_or_one_key(self, db_path):
        everything, is_error = _call_text(db_path, "config_get", {})
        one, _ = _call_text(db_path, "config_get", {"key": "worker.rescan_budget"})

        assert is_error is False
        assert "worker.poll_interval" in {s["key"] for s in everything["settings"]}
        assert (one["value"], one["is_default"]) == (200, True)

    def test_set_then_reset(self, db_path):
        changed, is_error = _call_text(db_path, "config_set", {"key": "worker.rescan_budget", "value": 25})
        assert is_error is False and changed["value"] == 25
        assert _call_text(db_path, "config_get", {"key": "worker.rescan_budget"})[0]["value"] == 25

        restored, _ = _call_text(db_path, "config_set", {"key": "worker.rescan_budget", "reset": True})
        assert (restored["value"], restored["is_default"]) == (200, True)

    def test_a_boolean_can_be_set_as_a_json_boolean(self, db_path):
        data, is_error = _call_text(db_path, "config_set", {"key": "embedding.enabled", "value": False})
        assert is_error is False and data["value"] is False

    def test_errors_are_tool_errors(self, db_path):
        for arguments in (
            {"key": "nope", "value": 1},
            {"key": "worker.poll_interval", "value": 0},
            {"key": "worker.poll_interval"},
            {"value": 1},
        ):
            assert _call(db_path, "config_set", arguments)["isError"] is True, arguments
        assert _call(db_path, "config_get", {"key": "nope"})["isError"] is True


class TestWorkerStatusTool:
    def test_reports_not_running_and_the_queue_size(self, real_db):
        db_path = real_db  # the status reads the database file itself, read-only: a real file is the objective
        connection = get_connection(db_path)  # pattern-ok: duckdb-file the status reads the file itself: a real file is the objective
        enqueue_staleness(connection, "src/app.py", "mtime changed")
        connection.close()

        data, is_error = _call_text(db_path, "worker_status", {})

        assert is_error is False
        assert data["running"] is False and data["pid"] is None
        assert data["staleness_queue_size"] == 1
        assert data["backlog"]["files_pending_rescan"] == 1

    def test_reports_the_pid_of_a_running_worker(self, no_db, monkeypatch):
        # (that the PID file is read, and by whom it is held, is `test_staleness_worker.py::TestSingleton`'s)
        monkeypatch.setattr("drspec.staleness_worker.is_worker_running", lambda path: 4242)

        data, is_error = _call_text(no_db, "worker_status", {})

        assert is_error is False
        assert data["running"] is True
        assert data["pid"] == 4242


class TestRunStdioFraming:
    def test_processes_newline_delimited_requests_and_writes_single_line_responses(self, no_db, monkeypatch):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "search", "arguments": {"pattern": "x"}}},
        ]
        # a result whose text has several lines, to show that none of them reaches the wire as a line of its own
        found = ({"matches": [{"function_id": "a.py::f", "file_path": "a.py"}, {"function_id": "a.py::g", "file_path": "a.py"}]}, False)
        monkeypatch.setitem(mcp_server._TOOL_HANDLERS, "search", lambda path, arguments: found)
        in_stream = io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n")
        out_stream = io.StringIO()

        run(no_db, in_stream=in_stream, out_stream=out_stream)

        lines = [line for line in out_stream.getvalue().split("\n") if line]
        assert len(lines) == 2
        for line in lines:
            assert "\n" not in line  # no embedded newline in a single wire message
            json.loads(line)  # each line is itself one complete, valid JSON message
        assert "\n" in json.loads(lines[1])["result"]["content"][0]["text"]  # (the text inside the message does have lines, JSON-escaped)

    def test_malformed_json_line_gets_a_parse_error_not_a_crash(self, no_db):
        in_stream = io.StringIO("not json\n{\"jsonrpc\": \"2.0\", \"id\": 1, \"method\": \"ping\"}\n")
        out_stream = io.StringIO()

        run(no_db, in_stream=in_stream, out_stream=out_stream)

        lines = [json.loads(line) for line in out_stream.getvalue().split("\n") if line]
        assert lines[0]["error"]["code"] == -32700
        assert lines[1]["result"] == {}

    def test_blank_lines_are_skipped(self, no_db):
        in_stream = io.StringIO("\n\n{\"jsonrpc\": \"2.0\", \"id\": 1, \"method\": \"ping\"}\n\n")
        out_stream = io.StringIO()

        run(no_db, in_stream=in_stream, out_stream=out_stream)

        lines = [line for line in out_stream.getvalue().split("\n") if line]
        assert len(lines) == 1


class TestDefaultOutputFormat:
    """A tool's own default (`format` omitted) is the same
    Graphify-style labeled plain text `cli/output.py` renders for the
    equivalent CLI command — not JSON. `format: "json"` is the
    explicit opt-in that keeps the old always-JSON behavior."""

    def test_status_default_is_labeled_plain_text_a_flat_record(self, db_path):
        result = _call(db_path, "status", {})
        assert result["isError"] is False
        text = result["content"][0]["text"]
        assert "{" not in text
        assert "Artifacts: 2 (pending 1, stale 0, verified 1, broken 0, needs review 0)" in text  # the default: the counts, one line per block
        assert "Total: 2" not in text

        details = _call(db_path, "status", {"details": True})["content"][0]["text"]
        assert "Total: 2" in details  # `details: true` is today's text

        json_result = _call(db_path, "status", {"format": "json"})
        data = json.loads(json_result["content"][0]["text"])
        assert data["artifacts"]["total"] == 2

    def test_search_default_renders_matches_as_labeled_records_a_list(self, db_path):
        result = _call(db_path, "search", {"pattern": "helper"})
        assert result["isError"] is False
        text = result["content"][0]["text"]
        assert "{" not in text
        assert "File: src/app.py" in text
        assert "ID: %s" % fid("src/app.py", "helper").split("::", 1)[1] in text
        assert "ID: %s" % fid("src/app.py", "main").split("::", 1)[1] in text

        json_result = _call(db_path, "search", {"pattern": "helper", "format": "json"})
        data = json.loads(json_result["content"][0]["text"])
        ids = {m["function_id"] for m in data["matches"]}
        assert ids == {fid("src/app.py", "main"), fid("src/app.py", "helper")}

    def test_deps_get_default_renders_an_edge_shaped_graph_with_arrows(self, db_path):
        result = _call(db_path, "deps_get", {"function_id": fid("src/app.py", "main"), "direction": "callees"})
        assert result["isError"] is False
        text = result["content"][0]["text"]
        assert "{" not in text
        assert "ID: src/app.py::main" in text
        assert "File: src/app.py" in text
        assert "--calls--> %s" % fid("src/app.py", "helper").split("::", 1)[1] in text

        json_result = _call(
            db_path, "deps_get", {"function_id": fid("src/app.py", "main"), "direction": "callees", "format": "json"},
        )
        data = json.loads(json_result["content"][0]["text"])
        assert [c["function_id"] for c in data["callees"]] == [fid("src/app.py", "helper")]

    def test_error_text_is_a_plain_message_regardless_of_format(self, db_path):
        """Rendering a single error message as labeled text adds
        nothing — the error path stays a bare string either way."""
        text_result = _call(db_path, "search", {})
        json_result = _call(db_path, "search", {"format": "json"})
        assert text_result["isError"] is True
        assert json_result["isError"] is True
        assert text_result["content"][0]["text"] == json_result["content"][0]["text"]
        assert "pattern" in text_result["content"][0]["text"]


class TestCompactOutputFormat:
    """`format: "compact"` returns the `~B` form of the same result (`core/compact_format.py`); `text` and `json` are the tests above."""

    @pytest.mark.parametrize(
        "tool, arguments",
        [
            ("search", {"pattern": "helper"}),
            ("deps_get", {"function_id": fid("src/app.py", "main"), "direction": "callees"}),
            ("unused", {}),
            ("status", {"details": True}),  # (the status text of the default is the short form, not a `~B` table: see test_status_text)
        ],
        ids=["search", "deps_get", "unused", "status"],
    )
    def test_a_tool_returns_a_compact_result(self, db_path, tool, arguments):
        result = _call(db_path, tool, {**arguments, "format": "compact"})
        assert result["isError"] is False
        assert result["content"][0]["text"].startswith("~B\n")

    def test_functions_are_named_by_name_and_hash_under_the_file_they_are_in(self, db_path):
        text = _call(db_path, "search", {"pattern": "helper", "format": "compact"})["content"][0]["text"]
        lines = text.split("\n")
        assert ">src/" in lines and " app.py" in lines
        assert "  " + fid("src/app.py", "helper").split("::", 1)[1] in [line.split("\t")[0] for line in lines]
        assert "src/app.py::" not in text

    def test_source_get_is_never_compacted(self, db_path):
        arguments = {"function_id": fid("src/app.py", "main")}
        text = _call(db_path, "source_get", arguments)["content"][0]["text"]
        assert _call(db_path, "source_get", {**arguments, "format": "compact"})["content"][0]["text"] == text

    def test_every_tool_offers_the_format(self):
        assert all("compact" in tool["inputSchema"]["properties"]["format"]["enum"] for tool in mcp_server._TOOLS)

    def test_an_error_is_the_same_message_in_every_format(self, db_path):
        assert _call(db_path, "search", {"format": "compact"})["content"][0]["text"] == _call(db_path, "search", {})["content"][0]["text"]
