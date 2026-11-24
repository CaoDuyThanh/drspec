"""Documents as searchable sections, linked to code without a model, and "which documents need an update?"."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.contracts import Contract
from drspec.core import docs as docs_core
from drspec.core.doc_parser import parse_markdown
from drspec.core.settings import SETTINGS
from drspec.db import init_schema

runner = CliRunner()

CODE = {
    "queue.py": "class Queue:\n    def add_item(self, x):\n        return x\n\n\ndef helper(a):\n    return a\n\n\ndef get():\n    return 0\n",
    "m.py": "def dup(x):\n    return x\n\n\ndef unique_name(a, b):\n    return a + b\n",
    "n.py": "def dup(y):\n    return y\n",
    "o.py": "def dup(z):\n    return z\n",
}

GUIDE = """---
title: Guide
tags: [install, queue]
---
Intro text.

# Install

Run `pip install drspec`. **Note**: use a virtual environment.

```bash
# this is a comment, not a heading
pip install drspec
```

## Linux

MUST use the system package manager.

| Step | Meaning |
|------|---------|
| one  | first   |

> [!WARNING]
> Back up the database first.

Underlined Heading
==================

See [the api](docs/api.md).

## Linux

A second section with the same title.
"""

LINKS = """# Links

The full id q.py::helper is queue.py::helper#d9e613a7 here, and `Queue.add_item` is qualified.
The path queue.py and the name `helper` go together. Another `unique_name` alone. The word `get` is common.

```python
result = dup(1)
```
"""


def contract(text: str = "Output is an integer") -> str:
    return Contract.from_dict(dict(
        function_signature="def f():", intent_summary="Return a computed value",
        invariants=[dict(name="rule0", logic=text, criticality="HIGH", on_fail="error")],
    )).to_json()


def run(*args, **kwargs):
    result = runner.invoke(app, ["--json", *args], **kwargs)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["data"]


def docs_search(text):
    """Document sections found by `search --docs-only`, grouped by document."""
    data = run("search", text, "--docs-only")
    grouped = {}
    for match in data["matches"]:
        grouped.setdefault(match["file_path"], []).append(match)
    return {"total": data["total_matches"], "documents": [{"file_path": path, "sections": sections} for path, sections in grouped.items()]}


def docs_stale(*extra):
    """The findings of `search --status STALE --docs-only`, flattened and ranked, grouped by document."""
    data = run("search", "--status", "STALE", "--docs-only", *extra)
    findings = [
        {**finding, "file_path": match["file_path"], "section_id": match["section_id"], "section": match["section"], "line": match["line"]}
        for match in data["matches"] for finding in match["findings"]
    ]
    findings.sort(key=lambda f: (-f["priority"], f["file_path"], f["line"], f["function_id"]))
    grouped = {}
    for finding in findings:
        grouped.setdefault(finding["file_path"], []).append(finding)
    return {"total": len(findings), "documents": [{"file_path": path, "findings": items} for path, items in grouped.items()]}


@pytest.fixture(scope="module")
def _project_template(tmp_path_factory, initialised_template):
    """The code files, an empty `docs` folder, an initialised project (a copy of the session's) and `docs.enabled`, made once for the
    module; each test works on a copy."""
    root = tmp_path_factory.mktemp("template") / "p"
    shutil.copytree(initialised_template, root)
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(root)
        for name, text in CODE.items():
            (root / name).write_text(text)
        (root / "docs").mkdir()
        assert runner.invoke(app, ["config", "set", "docs.enabled", "true"]).exit_code == 0
    return root


@pytest.fixture
def project(_project_template, tmp_path, monkeypatch):
    root = tmp_path / "p"
    shutil.copytree(_project_template, root)  # (keeps the modification times: an unchanged file stays unchanged)
    monkeypatch.chdir(root)
    return root


@pytest.fixture(scope="module")
def _scanned_template(_project_template, tmp_path_factory):
    """The template with `guide.md` and `links.md` written and scanned ONCE; the tests that only read what one scan made use a copy."""
    root = tmp_path_factory.mktemp("scanned") / "p"
    shutil.copytree(_project_template, root)
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(root)
        (root / "docs" / "guide.md").write_text(GUIDE)
        (root / "docs" / "links.md").write_text(LINKS)
        scan()
    return root


@pytest.fixture
def scanned(_scanned_template, tmp_path, monkeypatch):
    root = tmp_path / "p"
    shutil.copytree(_scanned_template, root)
    monkeypatch.chdir(root)
    return root


def db(project: Path):
    return duckdb.connect(str(project / "_drspec" / "contracts.db"), read_only=True)


def scan():
    return run("scan", ".")


def links(project: Path, doc: str = "docs/links.md"):
    connection = db(project)
    try:
        return sorted(
            (r[0].split("#")[0], r[1].split("#")[0], r[2], r[3], r[4])
            for r in connection.execute(
                "SELECT l.section_id, l.function_id, l.kind, l.confidence, l.score FROM doc_links l JOIN doc_sections s ON s.section_id = l.section_id WHERE s.file_path = ?", [doc]
            ).fetchall()
        )
    finally:
        connection.close()


class TestParsing:
    def test_the_tree_has_levels_parents_neighbours_and_line_ranges(self, scanned):
        connection = db(scanned)
        try:
            rows = connection.execute(
                "SELECT title, level, parent_id, prev_id, next_id, start_line, end_line, section_id FROM doc_sections WHERE file_path = 'docs/guide.md' ORDER BY start_line"
            ).fetchall()
            front = connection.execute("SELECT front_matter FROM doc_sections WHERE file_path = 'docs/guide.md' ORDER BY start_line LIMIT 1").fetchone()[0]
        finally:
            connection.close()
        assert [(r[0], r[1]) for r in rows] == [("(top)", 0), ("Install", 1), ("Linux", 2), ("Underlined Heading", 1), ("Linux", 2)]
        top, install, linux1, underlined, linux2 = rows
        assert linux1[2] == install[7] and linux2[2] == underlined[7]
        assert install[3] == top[7] and install[4] == linux1[7] and linux2[4] is None
        assert install[5] == 7 and install[6] == linux1[5] - 1  # a section runs up to the next heading
        assert json.loads(front)["title"] == "Guide"

    def test_comment_lines_in_a_fence_are_not_headings(self):
        titles = [s.title for s in parse_markdown(GUIDE)]
        assert "this is a comment, not a heading" not in titles

    def test_equal_titles_get_different_identifiers_that_survive_a_rescan_and_an_unrelated_edit(self, project):
        path = project / "docs" / "guide.md"
        path.write_text(GUIDE)
        scan()

        def ids():
            connection = db(project)
            try:
                return sorted(r[0] for r in connection.execute("SELECT section_id FROM doc_sections WHERE file_path = 'docs/guide.md'").fetchall())
            finally:
                connection.close()

        first = ids()
        assert len(set(first)) == len(first) == 5
        scan()
        assert ids() == first
        time.sleep(0.05)
        path.write_text(GUIDE.replace("Intro text.", "Intro text, edited."))
        scan()
        assert ids() == first

    def test_important_lines_follow_the_rules(self):
        parsed = parse_markdown(GUIDE)
        install = next(s for s in parsed if s.title == "Install")
        linux = next(s for s in parsed if s.title == "Linux")  # the first of the two
        assert any("MUST" in line for line in linux.important)
        assert any("WARNING" in line.upper() for line in linux.important)
        assert any(line.startswith("table:") for line in linux.important)
        assert "Note" in install.important

    def test_the_settings_exist(self):
        for key in ("docs.enabled", "docs.extensions", "docs.max_file_kb", "docs.keywords_per_section", "docs.max_links_per_section", "docs.link_min_score", "docs.common_name_limit", "docs.auto_ack_on_edit"):
            assert key in SETTINGS


class TestSearch:
    def test_a_search_finds_a_section_by_title_keyword_and_text_grouped_by_document(self, scanned):
        found = docs_search("install")
        assert found["total"] >= 1 and found["documents"][0]["file_path"] == "docs/guide.md"
        assert any(section["section"] == "Install" for section in found["documents"][0]["sections"])
        assert docs_search("virtual environment")["total"] == 1

    def test_the_plain_search_still_returns_functions_only(self, scanned):
        data = run("search", "install", "--code-only")
        assert data["total_matches"] == 0
        assert run("search", "--docs-only", "install")["total_matches"] >= 1


class TestLinks:
    def test_every_tier_gets_its_kind_confidence_and_score(self, scanned):
        found = {(function, kind, confidence, score) for _, function, kind, confidence, score in links(scanned)}
        assert ("queue.py::helper", "id", "EXTRACTED", 1.0) in found
        assert ("queue.py::Queue.add_item", "qualified", "EXTRACTED", 0.95) in found
        assert ("queue.py::helper", "path+name", "EXTRACTED", 0.9) in found or ("queue.py::helper", "id", "EXTRACTED", 1.0) in found
        assert ("m.py::unique_name", "name", "INFERRED", 0.7) in found

    def test_a_name_that_matches_three_functions_makes_three_ambiguous_links_and_a_common_name_none(self, scanned):
        rows = links(scanned)
        dups = [r for r in rows if r[1].endswith("::dup")]
        assert len(dups) == 3 and {(r[2], r[3], r[4]) for r in dups} == {("fence-call", "AMBIGUOUS", round(1 / 3, 3))}
        assert not [r for r in rows if r[1].endswith("::get")]

    def test_a_section_naming_a_file_links_to_the_file(self, scanned):
        connection = db(scanned)
        try:
            assert ("queue.py",) in connection.execute("SELECT file_path FROM doc_file_links").fetchall()
        finally:
            connection.close()

    def test_a_document_written_before_its_function_gets_the_link_when_the_function_appears(self, project):
        (project / "docs" / "later.md").write_text("# Later\n\nCall `brand_new_function` first.\n")
        scan()
        assert links(project, "docs/later.md") == []
        (project / "late.py").write_text("def brand_new_function():\n    return 1\n")
        scan()
        assert [r[1] for r in links(project, "docs/later.md")] == ["late.py::brand_new_function"]

    def test_deps_get_lists_the_sections_that_document_a_function(self, scanned):
        found = run("deps", "get", "queue.py::helper")
        assert found["documented_in"] and found["documented_in"][0]["file_path"] == "docs/links.md"
        assert run("deps", "get", "docs/links.md::/Links/")["links"]
        assert run("source", "get", "queue.py::helper")["documented_in"][0]["file_path"] == "docs/links.md"


@pytest.fixture(scope="module")
def _documented_template(_project_template, tmp_path_factory):
    """The template with `links.md` written and the project scanned ONCE: the drift tests start from it (a copy), as they did from a scan of
    their own; what they change and scan is theirs."""
    root = tmp_path_factory.mktemp("documented") / "p"
    shutil.copytree(_project_template, root)
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(root)
        (root / "docs" / "links.md").write_text("# Links\n\nThe `unique_name` function adds. Also `Queue.add_item`.\n")
        scan()
    return root


class TestDrift:
    @pytest.fixture
    def documented(self, _documented_template, tmp_path, monkeypatch):
        root = tmp_path / "p"
        shutil.copytree(_documented_template, root)  # (a copy of the scanned state: the same files, the same index)
        monkeypatch.chdir(root)
        return root

    def state(self, project, name="m.py::unique_name"):
        connection = db(project)
        try:
            return [
                (r[0], r[1]) for r in connection.execute(
                    "SELECT state, reason FROM doc_links WHERE function_id LIKE ?", [name + "#%"]
                ).fetchall()
            ]
        finally:
            connection.close()

    def test_a_changed_body_drifts_and_a_changed_signature_says_so_with_was_and_now(self, documented):
        (documented / "m.py").write_text(CODE["m.py"].replace("return a + b", "return b + a"))
        scan()
        assert self.state(documented) == [("drifted", "body_changed")]
        # a default value changes the signature text but not the identifier (§44)
        (documented / "m.py").write_text(CODE["m.py"].replace("unique_name(a, b)", "unique_name(a, b=2)"))
        scan()
        assert self.state(documented) == [("drifted", "signature_changed")]
        finding = next(f for d in docs_stale()["documents"] for f in d["findings"] if "unique_name" in f["function_id"])
        assert finding["signature_was"] == "def unique_name(a, b):" and finding["signature_now"] == "def unique_name(a, b=2):"

    def test_a_changed_parameter_list_gives_the_function_a_new_identifier_and_the_link_follows_it(self, documented):
        (documented / "m.py").write_text(CODE["m.py"].replace("unique_name(a, b)", "unique_name(a, b, c)"))
        scan()
        # the function keeps its name and gets a new identifier: the link follows it and says the signature changed
        assert self.state(documented) == [("drifted", "signature_changed")]

    def test_a_removed_function_is_kept_as_removed_and_ranked_first(self, documented):
        (documented / "m.py").write_text(CODE["m.py"].replace("def unique_name(a, b):\n    return a + b\n", "") + "\n")
        (documented / "queue.py").write_text(CODE["queue.py"].replace("return x", "return x + 0", 1))
        scan()
        findings = docs_stale()["documents"][0]["findings"]
        assert findings[0]["state"] == "removed" and findings[0]["function_id"].startswith("m.py::unique_name")
        assert findings[0]["priority"] > findings[-1]["priority"]

    def test_a_renamed_function_with_a_contract_keeps_its_link_under_the_new_identifier(self, documented):
        old = "m.py::unique_name"
        body = "    total = 0\n    for value in (a, b):\n        total += value * 2\n        if total > 100:\n            total -= 1\n    return total\n"
        (documented / "m.py").write_text(CODE["m.py"].replace("    return a + b\n", body))
        scan()
        assert runner.invoke(app, ["contract", "save", old, "--confidence", "80"], input=contract()).exit_code == 0
        (documented / "m.py").write_text((documented / "m.py").read_text().replace("unique_name", "joined_total"))
        scan()
        connection = db(documented)
        try:
            row = connection.execute("SELECT function_id, state, reason, previous_id FROM doc_links WHERE state = 'renamed'").fetchone()
        finally:
            connection.close()
        assert row is not None and row[0].startswith("m.py::joined_total#") and row[3].startswith("m.py::unique_name#")

    def test_ack_clears_a_finding_and_a_later_code_change_drifts_it_again(self, documented):
        (documented / "m.py").write_text(CODE["m.py"].replace("return a + b", "return b + a"))
        scan()
        assert docs_stale()["total"] == 1
        assert run("ack", "docs/links.md::/Links/")["acknowledged"] >= 1
        assert docs_stale()["total"] == 0
        (documented / "m.py").write_text(CODE["m.py"].replace("return a + b", "return a * b"))
        scan()
        assert docs_stale()["total"] == 1

    def test_editing_the_section_clears_its_drifted_links_and_records_the_edit(self, documented):
        (documented / "m.py").write_text(CODE["m.py"].replace("return a + b", "return b + a"))
        scan()
        assert self.state(documented) == [("drifted", "body_changed")]
        time.sleep(0.05)
        (documented / "docs" / "links.md").write_text("# Links\n\nThe `unique_name` function now adds in either order. Also `Queue.add_item`.\n")
        scan()
        assert self.state(documented) == [("ok", "edited_after_code_change")]
        (documented / "m.py").write_text(CODE["m.py"].replace("return a + b", "return a - b"))
        scan()
        assert self.state(documented) == [("drifted", "body_changed")]

    def test_a_scan_says_how_many_sections_may_need_an_update(self, documented):
        (documented / "m.py").write_text(CODE["m.py"].replace("return a + b", "return b + a"))
        data = scan()
        assert data["documentation_sections_to_review"] == 1 and "search --status STALE --docs-only" in data["note"]


class TestRanking:
    def test_removed_above_signature_above_body_and_surer_links_first(self):
        assert docs_core.priority("removed", "EXTRACTED", "id", False) > docs_core.priority("signature_changed", "EXTRACTED", "id", False)
        assert docs_core.priority("signature_changed", "EXTRACTED", "id", False) > docs_core.priority("body_changed", "EXTRACTED", "id", False)
        assert docs_core.priority("body_changed", "EXTRACTED", "id", False) > docs_core.priority("body_changed", "INFERRED", "name", False)
        assert docs_core.priority("body_changed", "INFERRED", "name", False) > docs_core.priority("body_changed", "AMBIGUOUS", "name", False)
        assert docs_core.priority("body_changed", "INFERRED", "fuzzy", False) < docs_core.priority("body_changed", "AMBIGUOUS", "name", False)
        assert docs_core.priority("body_changed", "INFERRED", "name", True) > docs_core.priority("body_changed", "INFERRED", "name", False)

    def test_stale_honours_path_and_groups_by_document(self, project):
        (project / "docs" / "a.md").write_text("# A\n\n`unique_name` here.\n")
        (project / "other.md").write_text("# B\n\n`Queue.add_item` there.\n")
        scan()
        (project / "m.py").write_text(CODE["m.py"].replace("return a + b", "return b + a"))
        (project / "queue.py").write_text(CODE["queue.py"].replace("return x", "return x + 0", 1))
        scan()
        everything = docs_stale()
        assert everything["total"] == 2 and {d["file_path"] for d in everything["documents"]} == {"docs/a.md", "other.md"}
        assert [d["file_path"] for d in docs_stale("--path", "docs/")["documents"]] == ["docs/a.md"]


class TestUndocumented:
    def test_public_functions_no_document_names_are_listed(self, project):
        (project / "docs" / "a.md").write_text("# A\n\n`unique_name` here.\n")
        scan()
        names = {f["function_name"] for f in run("search", "--undocumented", "--limit", "500")["matches"]}
        assert "helper" in names  # no document names it
        assert "unique_name" in names  # a bare name is only an inferred link, not enough to count as documented


class TestTheWorker:
    def test_a_new_document_is_indexed_after_the_code_and_edits_and_deletions_follow(self, project):
        from drspec.staleness_worker import _remediate_queued_files

        database = project / "_drspec" / "contracts.db"
        (project / "docs" / "w.md").write_text("# W\n\nSee `helper`.\n")
        connection = duckdb.connect(str(database))
        try:
            from drspec.core.discovery import discover_new_files

            discover_new_files(connection, project, 1000, 14.0, (".md",))
            reasons = dict((r[0], r[1]) for r in connection.execute("SELECT file_path, reason FROM staleness_queue").fetchall())
            assert reasons["docs/w.md"] == "new_doc" and reasons["queue.py"].startswith("new_file")
            from drspec.db.queries import next_rescan_batch

            ordered = next_rescan_batch(connection, 100, time.time(), project)
            assert ordered.index("docs/w.md") > ordered.index("queue.py")
        finally:
            connection.close()
        connection = duckdb.connect(str(database))
        try:
            _remediate_queued_files(connection, database, ["queue.py", "docs/w.md"])
        finally:
            connection.close()
        assert [r[1] for r in links(project, "docs/w.md")] and docs_search("helper")["total"] == 1
        (project / "docs" / "w.md").unlink()
        connection = duckdb.connect(str(database))
        try:
            _remediate_queued_files(connection, database, ["docs/w.md"])
        finally:
            connection.close()
        assert docs_search("helper")["total"] == 0 and links(project, "docs/w.md") == []


class TestPrivacyAndExistingDatabases:
    def test_a_marker_word_in_a_heading_a_body_and_a_code_block_never_reaches_the_trace_folder(self, project, monkeypatch):
        from drspec.core import tracing

        (project / "docs" / "secret.md").write_text("# zqheading\n\nzqbodyword text `zqspan`\n\n```\nzqcodeword()\n```\n")
        monkeypatch.setenv("DRSPEC_TRACE", "all")
        monkeypatch.setattr("sys.argv", ["drspec", "scan", "."])
        runner.invoke(app, ["scan", "."])
        folder = tracing.default_trace_dir()
        text = "".join(p.read_text(errors="replace") for p in folder.iterdir() if p.is_file())
        assert text and not any(marker in text for marker in ("zqheading", "zqbodyword", "zqspan", "zqcodeword", "secret.md"))

    def test_the_tables_appear_in_an_existing_database_without_a_rebuild(self, tmp_path):
        path = tmp_path / "c.db"
        connection = duckdb.connect(str(path))
        init_schema(connection)
        for table in ("doc_links", "doc_sections", "doc_file_links"):
            connection.execute(f"DROP TABLE {table}")
        init_schema(connection)
        assert connection.execute("SELECT COUNT(*) FROM doc_sections").fetchone() == (0,)
        connection.close()

    def test_status_shows_the_document_counts_and_nothing_without_documents(self, project):
        assert "documents" not in run("status")
        (project / "docs" / "a.md").write_text("# A\n\n`unique_name` here.\n")
        scan()
        status = run("status")["documents"]
        assert status["documents"] == 1 and status["sections"] == 1 and status["links"] >= 1

    def test_documents_can_be_turned_off(self, project):
        runner.invoke(app, ["config", "set", "docs.enabled", "false"])
        (project / "docs" / "a.md").write_text("# A\n\n`unique_name` here.\n")
        scan()
        assert docs_search("unique_name")["total"] == 0


class _Counting:
    """A connection that counts the statements it is given."""

    def __init__(self, inner):
        self.inner, self.statements = inner, 0

    def execute(self, sql, params=None):
        self.statements += 1
        return self.inner.execute(sql, params) if params is not None else self.inner.execute(sql)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class TestScale:
    def test_many_documents_next_to_many_functions_and_a_rename_relinks_only_the_sections_that_name_it(self, tmp_path, monkeypatch):
        """What it proves is that the cost of one document does not grow with what is already stored: the STATEMENTS each ingestion makes are
        the same for the 1st and the 120th document (it was `process_time < 60 s` for 300 documents and 1,200 functions, a real-time bound, and
        `slow`)."""
        monkeypatch.chdir(tmp_path)
        connection = duckdb.connect(":memory:")  # (the statements and the rows are what is checked, not a file)
        init_schema(connection)
        from drspec.core.scanner import Scanner
        from drspec.core.graph_sync import sync_scan_results

        source = "".join(f"def function_number_{i}(x):\n    return x + {i}\n\n\n" for i in range(400))
        (tmp_path / "big.py").write_text(source)
        result = Scanner().scan_file(str(tmp_path / "big.py"), relative_to=tmp_path)
        sync_scan_results(connection, functions=result.functions, scanned_file_paths={"big.py"}, file_imports=result.file_imports)
        settings = {key: setting.default for key, setting in SETTINGS.items()}
        index = docs_core.build_index(connection)
        counting = _Counting(connection)
        per_document = []
        for n in range(120):
            before = counting.statements
            docs_core.ingest_document(counting, f"d{n}.md", f"# Doc {n}\n\nCall `function_number_{n}` and `function_number_{n + 1}`.\n\n```python\nfunction_number_{n + 2}(1)\n```\n" + "filler " * 200, settings, index)
            per_document.append(counting.statements - before)
        assert connection.execute("SELECT COUNT(*) FROM doc_sections").fetchone()[0] == 120
        assert len(set(per_document)) == 1 and per_document[0] < 15, per_document[:3] + per_document[-3:]  # measured: 7 for every one

        counting.statements = 0
        relinked = docs_core.relink_for_names(counting, ["function_number_7"], settings)
        assert relinked == 3  # the three sections that name it, not all 120
        assert counting.statements <= 25  # measured: 21 for the three sections (about 7 each); 120 sections would be about 840
