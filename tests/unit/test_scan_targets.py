"""What a scan's target means, as `resolve_targets` turns it into the paths the worker reads: a file, a function id with
or without its hash, a document section, a folder, and a target that does not exist. Files are made in `tmp_path`; no
scan, no database, no process."""

from __future__ import annotations

from pathlib import Path

import pytest

from drspec.cli.commands.scan import project_folder_of, resolve_targets


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("def helper():\n    return 1\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("# Guide\n")
    return tmp_path


def test_a_file_is_its_project_relative_path(root):
    assert resolve_targets(["pkg/a.py"]) == (["pkg/a.py"], None)


def test_a_folder_is_its_project_relative_path(root):
    assert resolve_targets(["pkg"]) == (["pkg"], None)


def test_a_function_id_with_its_hash_keeps_the_hash_after_the_file(root):
    assert resolve_targets(["pkg/a.py::helper#abc123"]) == (["pkg/a.py::helper#abc123"], None)


def test_a_function_id_without_its_hash_keeps_the_name(root):
    assert resolve_targets(["pkg/a.py::helper"]) == (["pkg/a.py::helper"], None)


def test_a_document_section_is_read_as_its_document_part(root):
    assert resolve_targets(["docs/guide.md::guide-section"]) == (["docs/guide.md::guide-section"], None)


def test_a_file_named_from_a_subfolder_is_the_project_file_there(root, monkeypatch):
    (root / "_drspec").mkdir()
    (root / "_drspec" / "contracts.db").write_text("")
    monkeypatch.chdir(root / "pkg")
    assert resolve_targets(["a.py"], project_folder_of(Path.cwd())) == (["pkg/a.py"], None)


def test_a_directory_named_from_a_subfolder_is_the_project_folder_there(root, monkeypatch):
    (root / "_drspec").mkdir()
    (root / "_drspec" / "contracts.db").write_text("")
    monkeypatch.chdir(root / "pkg")
    assert resolve_targets(["."], project_folder_of(Path.cwd())) == (["pkg"], None)


def test_a_function_id_named_from_a_subfolder_is_the_project_file_there(root, monkeypatch):
    (root / "_drspec").mkdir()
    (root / "_drspec" / "contracts.db").write_text("")
    monkeypatch.chdir(root / "pkg")
    assert resolve_targets(["a.py::helper"], project_folder_of(Path.cwd())) == (["pkg/a.py::helper"], None)


def test_the_project_is_the_nearest_folder_with_a_database(root, monkeypatch):
    (root / "_drspec").mkdir()
    (root / "_drspec" / "contracts.db").write_text("")
    assert project_folder_of(root / "pkg") == root
    assert project_folder_of(root) == root


def test_no_database_above_means_the_current_folder(tmp_path):
    assert project_folder_of(tmp_path) == tmp_path


def test_a_target_that_does_not_exist_is_named_and_nothing_is_resolved(root):
    assert resolve_targets(["pkg/a.py", "pkg/missing.py"]) == ([], "pkg/missing.py")


def test_no_targets_resolve_to_nothing(root):
    assert resolve_targets(None) == ([], None)
    assert resolve_targets([]) == ([], None)
