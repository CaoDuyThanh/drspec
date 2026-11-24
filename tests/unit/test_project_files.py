"""Which files of a project are read: what git tracks or does not ignore, narrowed by two settings; the built-in
rules outside git; and the change check every scan and the worker share."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from drspec.core.changes import detect_changes
from drspec.core.discovery import discover_new_files
from drspec.core.project_files import folders_of, list_project_files
from drspec.db import get_connection, init_schema
from drspec.db.queries import list_staleness_queue, upsert_scanned_file
from tests.fakes import git_listing

DOCS = (".md", ".markdown")
IGNORED = {"_bmad/notes.md", "_bmad/helper.py", "generated/api.py"}  # what the project's `.gitignore` (`_bmad/`, `generated/`) hides


def _write(root: Path, files: dict) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A project inside git, as the listing git would give for it (`tests.fakes.git_listing`): no `git` runs, and the files
    `.gitignore` names are the ones in IGNORED. Nothing was ever added or committed, as in a project just started."""
    root = tmp_path / "project"
    root.mkdir()
    _write(
        root,
        {
            "src/app.py": "def run():\n    pass\n",
            "src/util.ts": "export function f() {}\n",
            "README.md": "# Readme\n",
            "docs/guide.md": "# Guide\n",
            "docs/archive/old.md": "# Old\n",
            "_bmad/notes.md": "# Notes\n",
            "_bmad/helper.py": "def helper():\n    pass\n",
            "generated/api.py": "def api():\n    pass\n",
            ".gitignore": "_bmad/\ngenerated/\n",
        },
    )
    git_listing(monkeypatch, root, ignored=IGNORED)
    return root


class TestAProjectInsideGit:
    def test_files_git_ignores_are_left_out_and_counted(self, repo):
        listing = list_project_files(repo, document_suffixes=DOCS)
        assert listing.used_git
        assert listing.files == ["README.md", "docs/archive/old.md", "docs/guide.md", "src/app.py", "src/util.ts"]
        assert listing.skipped_by_gitignore == 3  # two in _bmad, one in generated
        assert "_bmad/notes.md" in listing.ignored

    def test_a_nested_ignore_file_counts_too(self, repo, monkeypatch):
        _write(repo, {"src/.gitignore": "util.ts\n"})
        git_listing(monkeypatch, repo, ignored=IGNORED | {"src/util.ts"})  # what git prints once src/.gitignore names it
        assert "src/util.ts" not in list_project_files(repo, document_suffixes=DOCS).files

    def test_a_file_git_does_not_track_yet_is_still_read(self, repo):
        assert "src/app.py" in list_project_files(repo).files  # nothing was ever added or committed

    def test_the_built_in_ignores_still_apply(self, repo):
        _write(repo, {"node_modules/pkg/index.js": "function x() {}\n", "build/out.py": "def o():\n    pass\n"})
        files = list_project_files(repo, document_suffixes=DOCS).files
        assert not [f for f in files if f.startswith(("node_modules", "build"))]

    def test_scan_include_reads_a_folder_git_ignores(self, repo):
        listing = list_project_files(repo, document_suffixes=DOCS, settings={"scan.include": "generated"})
        assert "generated/api.py" in listing.files
        assert listing.skipped_by_gitignore == 2  # the _bmad files only

    def test_scan_include_reaches_a_folder_the_built_in_rules_would_skip(self, repo):
        _write(repo, {"build/gen/code.py": "def gen():\n    pass\n"})  # tracked: git lists it, the built-in rules skip `build`
        assert "build/gen/code.py" not in list_project_files(repo).files
        assert "build/gen/code.py" in list_project_files(repo, settings={"scan.include": "build/gen"}).files

    def test_scan_ignore_skips_a_folder_git_tracks(self, repo):
        listing = list_project_files(repo, document_suffixes=DOCS, settings={"scan.ignore": "docs/archive, nothing/here"})
        assert "docs/archive/old.md" not in listing.files and "docs/guide.md" in listing.files
        assert "docs/archive/old.md" in listing.ignored

    def test_drspecs_own_folder_is_never_listed(self, repo):
        _write(repo, {"_drspec/agents/prompt.md": "# Prompt\n", "_drspec/x.py": "def x():\n    pass\n"})
        assert not [f for f in list_project_files(repo, document_suffixes=DOCS).files if f.startswith("_drspec")]

    def test_a_deleted_tracked_file_is_not_listed(self, repo, monkeypatch):
        (repo / "src" / "app.py").unlink()
        git_listing(monkeypatch, repo, ignored=IGNORED, also_listed=["src/app.py"])  # git still lists what it tracks
        assert "src/app.py" not in list_project_files(repo).files

    def test_paths_are_sorted_folder_by_folder_as_the_discovery_cursor_counts(self, repo):
        _write(repo, {"a/b.py": "def b():\n    pass\n", "a.py": "def a():\n    pass\n"})
        files = list_project_files(repo).files
        assert files.index("a/b.py") < files.index("a.py")  # ('a', 'b.py') sorts before ('a.py',)


class TestAProjectOutsideGit:
    def test_the_built_in_rules_alone_apply(self, tmp_path, no_git):
        root = tmp_path / "plain"
        _write(root, {"a.py": "def a():\n    pass\n", ".gitignore": "a.py\n", "node_modules/x.js": "function x() {}\n", "notes.md": "# n\n"})
        listing = list_project_files(root, document_suffixes=DOCS)
        assert not listing.used_git
        assert listing.files == ["a.py", "notes.md"]  # .gitignore means nothing without git
        assert listing.skipped_by_gitignore == 0


class TestTheSettingsParse:
    def test_folders_are_cleaned(self):
        assert folders_of(" docs/archive/ , ./generated,, ") == ("docs/archive", "generated")
        assert folders_of(None) == ()


@pytest.fixture
def database(tmp_path):
    connection = get_connection(tmp_path / "p.db")
    init_schema(connection)
    yield connection
    connection.close()


class TestTheChangeCheck:
    def _snapshot(self, connection, root, names):
        for name in names:
            stat = os.stat(root / name)
            upsert_scanned_file(connection, name, stat.st_mtime, stat.st_size)

    def test_added_changed_removed_and_unchanged(self, repo, database):
        self._snapshot(database, repo, ["src/app.py", "src/util.ts", "README.md"])
        upsert_scanned_file(database, "src/deleted.py", 1.0, 5)
        (repo / "src" / "app.py").write_text("def run():\n    return 1\n")  # size changed
        changes = detect_changes(database, repo, {"docs.enabled": True, "docs.extensions": "md,markdown"})
        assert changes.changed == ["src/app.py"]
        assert sorted(changes.added) == ["docs/archive/old.md", "docs/guide.md"]
        assert changes.removed == ["src/deleted.py"]
        assert changes.unchanged == 2

    def test_a_file_that_became_ignored_counts_as_removed(self, repo, database):
        self._snapshot(database, repo, ["src/app.py", "_bmad/helper.py"])
        changes = detect_changes(database, repo, {"docs.enabled": True, "docs.extensions": "md,markdown"})
        assert changes.removed == ["_bmad/helper.py"]

    def test_full_reports_every_known_file_as_changed(self, repo, database):
        self._snapshot(database, repo, ["src/app.py"])
        changes = detect_changes(database, repo, {"docs.enabled": False}, full=True)
        assert changes.changed == ["src/app.py"] and changes.unchanged == 0

    def test_a_scope_narrows_the_answer(self, repo, database):
        changes = detect_changes(database, repo, {"docs.enabled": True, "docs.extensions": "md"}, in_scope=lambda path: path.startswith("docs/"))
        assert sorted(changes.added) == ["docs/archive/old.md", "docs/guide.md"]

    def test_documents_switched_off_are_not_reported_as_removed(self, repo, database):
        self._snapshot(database, repo, ["README.md"])
        changes = detect_changes(database, repo, {"docs.enabled": False, "docs.extensions": "md,markdown"})
        assert changes.removed == []


class TestDiscoveryUsesTheSameList:
    def test_new_files_are_found_through_git_and_ignored_ones_are_not(self, repo, database):
        found = discover_new_files(database, repo, 1000, 14.0, DOCS)
        queued = set(list_staleness_queue(database))
        assert found["queued"] == 5 and "_bmad/helper.py" not in queued and "src/app.py" in queued
