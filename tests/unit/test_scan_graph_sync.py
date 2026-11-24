"""Integration tests for the `drspec scan` pipeline (node removal,
embedding, edge diff/promotion), driven through the real CLI rather than
internal functions directly.
"""

import json
from pathlib import Path
from unittest.mock import patch

import duckdb
from typer.testing import CliRunner

from drspec.cli.app import app
from tests.helpers import fid

runner = CliRunner()


class TestScanGraphSync:
    def test_same_file_call_creates_extracted_edge(self, kept_project):
        """Two functions in one file, one calling the other by bare name —
        after a scan, a dependencies row exists with EXTRACTED resolution.
        What only this test shows: the CLI's own `edges_inserted` count and the stored `call_site` text."""
        Path("test.py").write_text(
            "def helper():\n    return 1\n\ndef main():\n    return helper()\n"
        )

        # Scan "." (not the bare filename) so `function_id`s come
        # out relative ("test.py::main"), matching the literals
        # below — a single-file `drspec scan test.py` invocation
        # stores absolute-path function_ids instead, a pre-existing
        # quirk unrelated to this task.
        result = runner.invoke(app, ["--json", "scan", "."])

        assert result.exit_code == 0
        response = json.loads(result.output)
        assert response["data"]["edges_inserted"] == 1

        conn = duckdb.connect("_drspec/contracts.db")
        row = conn.execute(
            "SELECT confidence, call_site FROM dependencies "
                    "WHERE caller_id = ? AND callee_id = ?",
            [fid("test.py", "main"), fid("test.py", "helper")],
        ).fetchone()
        conn.close()

        assert row is not None
        confidence, call_site = row
        assert confidence == "EXTRACTED"
        assert "helper()" in call_site


    def test_removing_a_function_cascades_its_edges(self, kept_project):
        """Scan a file, remove one of its functions, rescan — cascade-delete
        must remove both the artifacts row and the dependent edge row, not
        just the function itself.

        `foo`'s source is kept byte-for-byte identical across both versions
        (still says `bar()`) so its code_hash doesn't change and it's never
        reprocessed as a caller this scan — isolating the cascade-delete
        path from the normal edge-diff path, which would otherwise remove
        the same edge for an unrelated reason."""
        Path("test.py").write_text(
            "def bar():\n    pass\n\ndef foo():\n    return bar()\n"
        )
        runner.invoke(app, ["scan", "."])

        conn = duckdb.connect("_drspec/contracts.db")
        assert (
            conn.execute(
                "SELECT 1 FROM dependencies WHERE caller_id = ? AND callee_id = ?",
                [fid("test.py", "foo"), fid("test.py", "bar")],
            ).fetchone()
            is not None
        )
        conn.close()

        # Remove bar's definition; foo's own text is unchanged.
        Path("test.py").write_text("def foo():\n    return bar()\n")
        result = runner.invoke(app, ["--json", "scan", "."])

        assert result.exit_code == 0
        response = json.loads(result.output)
        assert response["data"]["functions_removed"] == 1
        assert response["data"]["functions_unchanged"] == 1

        conn = duckdb.connect("_drspec/contracts.db")
        assert conn.execute(
            "SELECT 1 FROM artifacts WHERE function_id = ?", [fid("test.py", "bar")]
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM dependencies WHERE callee_id = ?", [fid("test.py", "bar")]
        ).fetchone() is None
        conn.close()

    def test_plain_scan_leaves_seeded_fallback_edge_untouched(self, kept_project):
        """A manually seeded INFERRED row (simulating what a future
        --deep-scan fallback resolution might produce) survives a plain
        scan on that same caller untouched, validated through the real
        pipeline rather than a hand-written diff check.
        The only test that a plain scan does not diff the INFERRED/AMBIGUOUS tiers (the break that makes it diff every tier is caught
        by this test alone)."""
        Path("a.py").write_text("def foo():\n    pass\n")
        Path("b.py").write_text("def other():\n    pass\n")
        runner.invoke(app, ["scan", "."])

        conn = duckdb.connect("_drspec/contracts.db")
        conn.execute(
            "INSERT INTO dependencies (caller_id, callee_id, call_site, edge_hash, embedding, confidence) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
            [fid("a.py", "foo"), fid("b.py", "other"), "other()", "seeded-hash", [0.0] * 384, "INFERRED"],
        )
        conn.close()

        # Change foo's body for an unrelated reason, triggering its
        # own edge re-sync this scan, but WITHOUT --deep-scan.
        Path("a.py").write_text("def foo():\n    return 1\n")
        result = runner.invoke(app, ["scan", "a.py"])

        assert result.exit_code == 0

        conn = duckdb.connect("_drspec/contracts.db")
        row = conn.execute(
            "SELECT confidence FROM dependencies WHERE caller_id = ? AND callee_id = ?",
            [fid("a.py", "foo"), fid("b.py", "other")],
        ).fetchone()
        conn.close()

        assert row is not None and row[0] == "INFERRED"

    def test_deep_scan_flag_actually_widens_the_diff_scope(self, kept_project):
        """`--deep-scan` actually changes the diff's scope: same seeded-edge
        setup as the plain-scan case, but with `--deep-scan` the
        still-unresolved `other()` means the previously-seeded `INFERRED`
        row is now "missing from this run's full-tier diff" and gets
        deleted — the opposite outcome from the same starting state."""
        Path("a.py").write_text("def foo():\n    pass\n")
        Path("b.py").write_text("def other():\n    pass\n")
        runner.invoke(app, ["scan", "."])

        conn = duckdb.connect("_drspec/contracts.db")
        conn.execute(
            "INSERT INTO dependencies (caller_id, callee_id, call_site, edge_hash, embedding, confidence) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
            [fid("a.py", "foo"), fid("b.py", "other"), "other()", "seeded-hash", [0.0] * 384, "INFERRED"],
        )
        conn.close()

        Path("a.py").write_text("def foo():\n    return 1\n")
        result = runner.invoke(app, ["--json", "scan", "--deep-scan", "."])

        assert result.exit_code == 0
        response = json.loads(result.output)
        assert response["data"]["deep_scan"] is True
        assert response["data"]["edges_deleted"] == 1

        conn = duckdb.connect("_drspec/contracts.db")
        row = conn.execute(
            "SELECT 1 FROM dependencies WHERE caller_id = ? AND callee_id = ?",
            [fid("a.py", "foo"), fid("b.py", "other")],
        ).fetchone()
        conn.close()

        assert row is None

    def test_rescanning_unchanged_file_is_a_true_noop(self, kept_project):
        """Rescanning an unchanged file does no embedding recomputation and
        touches no edges — the cheap early-exit path."""
        Path("test.py").write_text("def foo():\n    return 1\n")
        runner.invoke(app, ["scan", "test.py"])

        with patch("drspec.core.graph_sync.embed_text") as mock_embed:
            plain = json.loads(runner.invoke(app, ["--json", "scan", "test.py"]).output)["data"]
            assert plain["up_to_date"] is True  # nothing to read at all
            result = runner.invoke(app, ["--json", "scan", "--full", "test.py"])
            mock_embed.assert_not_called()

        assert result.exit_code == 0
        response = json.loads(result.output)
        assert response["data"]["functions_unchanged"] == 1
        assert response["data"]["functions_new"] == 0
        assert response["data"]["functions_changed"] == 0
        assert response["data"]["edges_inserted"] == 0
        assert response["data"]["edges_updated"] == 0
        assert response["data"]["edges_deleted"] == 0

    def test_end_to_end_scan_populates_embeddings_and_edges_for_real(self, kept_project, fake_embedder):
        """Runs the actual `drspec scan --embed` CLI command (no mocking) to
        confirm artifacts.embedding and a dependencies row are populated for
        real; `--embed` is required since embedding isn't computed by
        default."""
        Path("app.py").write_text(
            "def helper(x):\n    return x + 1\n\ndef main():\n    return helper(1)\n"
        )

        result = runner.invoke(app, ["scan", "--embed", "."])
        assert result.exit_code == 0

        conn = duckdb.connect("_drspec/contracts.db")
        helper_embedding = conn.execute(
            "SELECT (SELECT embedding FROM artifact_embeddings e WHERE e.function_id = artifacts.function_id) FROM artifacts WHERE function_id = ?",
            [fid("app.py", "helper", "x")],
        ).fetchone()[0]
        edge_confidence, edge_embedding, call_site = conn.execute(
            "SELECT confidence, embedding, call_site FROM dependencies "
                    "WHERE caller_id = ? AND callee_id = ?",
            [fid("app.py", "main"), fid("app.py", "helper", "x")],
        ).fetchone()
        conn.close()

        assert helper_embedding is not None
        assert len(helper_embedding) == 384

        assert edge_confidence == "EXTRACTED"
        assert edge_embedding is not None
        assert len(edge_embedding) == 384
        assert "helper(" in call_site

    def test_plain_scan_does_not_embed_by_default(self, kept_project):
        """Without --embed, a new node's embedding stays NULL — embedding
        is opt-in, not computed unconditionally."""
        Path("app.py").write_text("def helper(x):\n    return x + 1\n")

        result = runner.invoke(app, ["--json", "scan", "."])
        assert result.exit_code == 0
        response = json.loads(result.output)
        assert response["data"]["embed"] is False

        conn = duckdb.connect("_drspec/contracts.db")
        embedding = conn.execute(
            "SELECT (SELECT embedding FROM artifact_embeddings e WHERE e.function_id = artifacts.function_id) FROM artifacts WHERE function_id = ?",
            [fid("app.py", "helper", "x")],
        ).fetchone()[0]
        conn.close()

        assert embedding is None

    def test_changed_node_embedding_cleared_when_embed_turned_off(self, kept_project, fake_embedder):
        """A node that was embedded, then changes on a scan without
        --embed, must have its embedding cleared to NULL rather than left
        stale."""
        Path("app.py").write_text("def helper(x):\n    return x + 1\n")
        runner.invoke(app, ["scan", "--embed", "."])

        conn = duckdb.connect("_drspec/contracts.db")
        before = conn.execute(
            "SELECT (SELECT embedding FROM artifact_embeddings e WHERE e.function_id = artifacts.function_id) FROM artifacts WHERE function_id = ?",
            [fid("app.py", "helper", "x")],
        ).fetchone()[0]
        conn.close()
        assert before is not None

        Path("app.py").write_text("def helper(x):\n    return x + 2\n")
        result = runner.invoke(app, ["scan", "."])
        assert result.exit_code == 0

        conn = duckdb.connect("_drspec/contracts.db")
        after = conn.execute(
            "SELECT (SELECT embedding FROM artifact_embeddings e WHERE e.function_id = artifacts.function_id) FROM artifacts WHERE function_id = ?",
            [fid("app.py", "helper", "x")],
        ).fetchone()[0]
        conn.close()
        assert after is None

    def test_bare_call_resolves_to_free_function_not_same_named_method(self, kept_project):
        """A bare call inside a method must resolve to a top-level free
        function of the same name, not implicitly to a same-named method on
        the caller's own class — only C++'s optional `this->` gets that
        implicit-self behavior, never Python/JS."""
        Path("math_utils.py").write_text(
            "def add(a, b):\n"
                    "    return a + b\n"
                    "\n"
                    "class Calculator:\n"
                    "    def __init__(self):\n"
                    "        self.value = 0\n"
                    "\n"
                    "    def add(self, x):\n"
                    "        self.value = add(self.value, x)\n"
                    "        return self.value\n"
        )

        result = runner.invoke(app, ["scan", "."])
        assert result.exit_code == 0

        conn = duckdb.connect("_drspec/contracts.db")
        row = conn.execute(
            "SELECT callee_id, confidence FROM dependencies WHERE caller_id = ?",
            [fid("math_utils.py", "Calculator.add", "x")],
        ).fetchone()
        conn.close()

        assert row == (fid("math_utils.py", "add", "a", "b"), "EXTRACTED")

    def test_property_read_creates_reference_edge_not_a_call_edge(self, kept_project):
        """A `@property` read (`c.value`, no parens) must produce a
        `code_references` row, not a `dependencies` row, and scan stats
        must report it separately."""
        Path("calc.py").write_text(
            "class Calculator:\n"
                    "    def __init__(self):\n"
                    "        self._value = 0\n"
                    "\n"
                    "    @property\n"
                    "    def value(self):\n"
                    "        return self._value\n"
                    "\n"
                    "\n"
                    "def show(c: Calculator):\n"
                    "    print(c.value)\n"
        )

        result = runner.invoke(app, ["--json", "scan", "."])
        assert result.exit_code == 0
        response = json.loads(result.output)
        assert response["data"]["references_inserted"] == 1

        conn = duckdb.connect("_drspec/contracts.db")
        is_property = conn.execute(
            "SELECT is_property FROM artifacts WHERE function_id = ?",
            [fid("calc.py", "Calculator.value")],
        ).fetchone()
        ref_row = conn.execute(
            "SELECT callee_id, confidence FROM code_references WHERE caller_id = ?",
            [fid("calc.py", "show", "Calculator")],
        ).fetchone()
        call_row = conn.execute(
            "SELECT 1 FROM dependencies WHERE caller_id = ? AND callee_id = ?",
            [fid("calc.py", "show", "Calculator"), fid("calc.py", "Calculator.value")],
        ).fetchone()
        conn.close()

        assert is_property == (True,)
        assert ref_row == (fid("calc.py", "Calculator.value"), "EXTRACTED")
        assert call_row is None

    def test_property_read_resolves_as_caller_via_deps_get(self, kept_project):
        """`drspec deps get` on a property must report its reader as a
        caller, tagged `"relation": "references"`, not zero callers."""
        Path("calc.py").write_text(
            "class Calculator:\n"
                    "    @property\n"
                    "    def value(self):\n"
                    "        return 1\n"
                    "\n"
                    "\n"
                    "def show(c: Calculator):\n"
                    "    print(c.value)\n"
        )
        runner.invoke(app, ["scan", "."])

        result = runner.invoke(app, ["--json", "deps", "get", fid("calc.py", "Calculator.value")])
        assert result.exit_code == 0
        response = json.loads(result.output)

        assert response["data"]["summary"]["total_callers"] == 1
        assert response["data"]["callers"][0]["function_id"] == fid("calc.py", "show", "Calculator")
        assert response["data"]["callers"][0]["relation"] == "references"

    def test_force_reresolves_an_unchanged_callers_cross_module_edge(self, kept_project):
        """A caller scanned before its callee's module is scanned is left
        with an unresolved call forever — its own code never changes, so
        the cheap 'unchanged -> skip' early exit never gives resolution a
        second chance, even after the callee becomes scannable. `--force`
        re-attempts resolution for every scanned function regardless of
        code_hash, picking up the edge without `caller.py` itself needing
        to change."""
        Path("caller.py").write_text(
            "from target import helper\n\n\ndef do_work():\n    return helper(1)\n"
        )
        Path("target.py").write_text("def helper(x):\n    return x + 1\n")

        # Scan caller.py alone first — target.py doesn't exist in
        # the DB yet, so the call can't resolve.
        runner.invoke(app, ["scan", "caller.py"])
        conn = duckdb.connect("_drspec/contracts.db")
        assert (
            conn.execute(
                "SELECT 1 FROM dependencies WHERE caller_id = ?", [fid("caller.py", "do_work")]
            ).fetchone()
            is None
        )
        conn.close()

        # Adding target.py automatically repairs the stored caller.
        runner.invoke(app, ["scan", "target.py"])
        result = runner.invoke(app, ["--json", "scan", "caller.py"])
        response = json.loads(result.output)
        assert response["data"]["up_to_date"] is True  # the caller was repaired by scanning target.py, and not read again
        assert response["data"]["edges_inserted"] == 0

        conn = duckdb.connect("_drspec/contracts.db")
        assert (
            conn.execute(
                "SELECT 1 FROM dependencies WHERE caller_id = ?", [fid("caller.py", "do_work")]
            ).fetchone()
            is not None
        )
        conn.close()

        # A stored edge that is wrong or missing for a reason nothing re-reads (here: the row is gone) is only brought back by --force:
        # it re-resolves caller.py's calls against the DB's current state, even though caller.py itself is unchanged.
        conn = duckdb.connect("_drspec/contracts.db")
        conn.execute("DELETE FROM dependencies WHERE caller_id = ?", [fid("caller.py", "do_work")])
        conn.close()
        result = runner.invoke(app, ["--json", "scan", "caller.py"])
        assert json.loads(result.output)["data"]["edges_inserted"] == 0  # a plain scan does not look at an unchanged caller

        result = runner.invoke(app, ["--json", "scan", "--force", "caller.py"])
        response = json.loads(result.output)
        assert response["data"]["functions_unchanged"] == 1
        assert response["data"]["edges_inserted"] == 1

        conn = duckdb.connect("_drspec/contracts.db")
        row = conn.execute(
            "SELECT callee_id, confidence FROM dependencies WHERE caller_id = ?",
            [fid("caller.py", "do_work")],
        ).fetchone()
        conn.close()
        assert row == (fid("target.py", "helper", "x"), "EXTRACTED")

    def test_force_does_not_touch_unchanged_nodes_own_hash_or_embedding(self, kept_project, fake_embedder):
        """`--force` only widens which functions get edge/reference
        resolution re-attempted — it must not treat unchanged functions as
        changed for hashing/embedding purposes."""
        Path("a.py").write_text("def foo():\n    pass\n")
        runner.invoke(app, ["scan", "--embed", "a.py"])

        conn = duckdb.connect("_drspec/contracts.db")
        before = conn.execute(
            "SELECT code_hash, (SELECT embedding FROM artifact_embeddings e WHERE e.function_id = artifacts.function_id) FROM artifacts WHERE function_id = ?",
            [fid("a.py", "foo")],
        ).fetchone()
        conn.close()

        result = runner.invoke(app, ["--json", "scan", "--force", "a.py"])
        response = json.loads(result.output)
        assert response["data"]["functions_unchanged"] == 1
        assert response["data"]["functions_changed"] == 0

        conn = duckdb.connect("_drspec/contracts.db")
        after = conn.execute(
            "SELECT code_hash, (SELECT embedding FROM artifact_embeddings e WHERE e.function_id = artifacts.function_id) FROM artifacts WHERE function_id = ?",
            [fid("a.py", "foo")],
        ).fetchone()
        conn.close()
        assert after[0] == before[0]
        assert after[1] is not None

    def test_deps_plot_html_generates_an_interactive_page(self, kept_project):
        """`drspec deps plot --html` produces a self-contained interactive
        HTML page, verified end-to-end through the real CLI, not just the
        underlying grapher function directly."""
        Path("calc.py").write_text(
            "class Calculator:\n"
                    "    @property\n"
                    "    def value(self):\n"
                    "        return 1\n"
                    "\n"
                    "    def add(self, x):\n"
                    "        return compute_total(x)\n"
                    "\n"
                    "\n"
                    "def compute_total(x):\n"
                    "    return x + 1\n"
                    "\n"
                    "\n"
                    "def show(c: Calculator):\n"
                    "    print(c.value)\n"
                    "    c.add(1)\n"
        )
        runner.invoke(app, ["scan", "."])

        result = runner.invoke(app, ["--json", "deps", "plot", fid("calc.py", "show", "Calculator"), "--html", "--depth", "2"])

        assert result.exit_code == 0
        response = json.loads(result.output)
        assert response["data"]["html"] is True
        assert response["data"]["output_path"].endswith(".html")
        assert response["data"]["nodes"] >= 3

        html_path = Path(response["data"]["output_path"])
        assert html_path.exists()
        content = html_path.read_text()
        assert "<!DOCTYPE html>" in content
        assert fid("calc.py", "show", "Calculator") in content
        assert '"relation": "calls"' in content
        assert '"relation": "references"' in content
        # No PNG written for an --html request.
        assert html_path.with_suffix(".png").exists() is False

    def test_deps_plot_html_output_writes_the_exact_requested_file_name(self, kept_project):
        """`--output` names the file itself, not just its directory: a caller asking for `page.html` must not get `graph_<hash>.html`."""
        Path("calc.py").write_text("def a():\n    return b()\n\n\ndef b():\n    return 1\n")
        runner.invoke(app, ["scan", "."])

        html = runner.invoke(app, ["--json", "deps", "plot", fid("calc.py", "a"), "--html", "-o", "out/page.html"])

        assert html.exit_code == 0, html.output
        assert json.loads(html.output)["data"]["output_path"] == str(Path("out/page.html").resolve())
        assert sorted(p.name for p in Path("out").iterdir()) == ["page.html"]

    def test_package_reexport_import_resolves_real_callers(self, kept_project):
        """End-to-end through the real CLI (not just `project_facts.py`
        directly): a function imported the `from pkg import X` way (through
        `pkg/__init__.py`'s re-export) must show up as a real caller, not
        zero."""
        Path("pkg").mkdir()
        Path("pkg/__init__.py").write_text("from pkg.util import helper\n")
        Path("pkg/util.py").write_text("def helper():\n    return 1\n")
        Path("main.py").write_text("from pkg import helper\n\n\ndef main():\n    return helper()\n")

        result = runner.invoke(app, ["scan", "."])
        assert result.exit_code == 0

        deps_result = runner.invoke(app, ["--json", "deps", "get", fid("pkg/util.py", "helper"), "--direction", "callers"])
        assert deps_result.exit_code == 0
        response = json.loads(deps_result.output)
        assert [c["function_id"] for c in response["data"]["callers"]] == [fid("main.py", "main")]

    def test_deps_unused_lists_a_real_zero_caller_function(self, kept_project):
        Path("a.py").write_text(
            "class Thing:\n"
                    "    def __init__(self):\n"
                    "        pass\n"
                    "\n"
                    "def used():\n"
                    "    return 1\n"
                    "\n"
                    "def caller():\n"
                    "    return used()\n"
                    "\n"
                    "def dead():\n"
                    "    return 2\n"
        )
        runner.invoke(app, ["scan", "."])

        result = runner.invoke(app, ["--json", "deps", "unused"])

        assert result.exit_code == 0
        response = json.loads(result.output)
        function_ids = {f["function_id"] for f in response["data"]["functions"]}
        assert fid("a.py", "dead") in function_ids
        assert fid("a.py", "used") not in function_ids  # has a real caller
        assert fid("a.py", "Thing.__init__") not in function_ids  # dunder, excluded

    def test_deps_unused_distinguishes_test_only_callers_from_truly_zero(self, kept_project):
        """A function called only by its own dedicated test must still
        appear in `deps unused` (not silently excluded), but flagged
        distinctly from a genuinely zero-caller function."""
        Path("a.py").write_text(
            "def normalize_code():\n"
                    "    return 1\n"
                    "\n"
                    "def dead():\n"
                    "    return 2\n"
        )
        Path("tests").mkdir()
        Path("tests/test_a.py").write_text(
            "from a import normalize_code\n"
                    "\n"
                    "\n"
                    "def test_normalize_code():\n"
                    "    assert normalize_code() == 1\n"
        )
        runner.invoke(app, ["scan", "."])

        result = runner.invoke(app, ["--json", "deps", "unused"])

        assert result.exit_code == 0
        response = json.loads(result.output)
        flags = {f["function_id"]: f["only_test_callers"] for f in response["data"]["functions"]}
        assert flags[fid("a.py", "normalize_code")] is True
        assert flags[fid("a.py", "dead")] is False

    def test_possibly_stale_flag_appears_across_search_deps_get_and_unused(self, kept_project):
        """A file flagged in `staleness_queue` (what the background worker
        does on drift) must surface as `possibly_stale: true` everywhere a
        function from that file appears — `search`, `deps get`, and `deps
        unused` alike."""
        from drspec.db.queries import enqueue_staleness

        Path("a.py").write_text("def stale_fn():\n    return 1\n\n\ndef fresh_fn():\n    return 2\n")
        runner.invoke(app, ["scan", "."])

        conn = duckdb.connect(str(Path.cwd() / "_drspec" / "contracts.db"))
        enqueue_staleness(conn, "a.py", "mtime_changed")
        conn.close()

        search_result = runner.invoke(app, ["--json", "search", "fn", "-E"])
        search_data = json.loads(search_result.output)["data"]
        flags = {m["function_name"]: m["possibly_stale"] for m in search_data["matches"]}
        assert flags == {"stale_fn": True, "fresh_fn": True}

        deps_result = runner.invoke(app, ["--json", "deps", "get", fid("a.py", "stale_fn")])
        deps_data = json.loads(deps_result.output)["data"]
        assert deps_data["possibly_stale"] is True

        unused_result = runner.invoke(app, ["--json", "deps", "unused"])
        unused_data = json.loads(unused_result.output)["data"]
        unused_flags = {f["function_name"]: f["possibly_stale"] for f in unused_data["functions"]}
        assert unused_flags == {"stale_fn": True, "fresh_fn": True}
