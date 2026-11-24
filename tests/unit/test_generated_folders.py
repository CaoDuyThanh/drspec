"""Generated build folders are left out by default: a folder with a marker of a build's output (a CMake build tree, a
catkin devel or install space) is not read, a folder with only the name `devel` or `install` is, and `scan.include`
reads a generated folder anyway."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.core.project_files import generated_kind, list_project_files
from tests.fakes import git_listing

runner = CliRunner()

HEADER = "#pragma once\nvoid util();\n"


def _write(path: Path, text: str = HEADER) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _catkin_space(root: Path, name: str) -> None:
    _write(root / name / "include" / "pkg" / "util.h")
    _write(root / name / ".catkin", "/media/ws/src\n")  # pattern-ok: outside-path the text of a file, never opened
    _write(root / name / "setup.sh", "# setup\n")
    _write(root / name / "_setup_util.py", "# util\n")


def _workspace(root: Path) -> None:
    _write(root / "src" / "pkg" / "include" / "pkg" / "util.h")
    _catkin_space(root, "devel")
    _catkin_space(root, "install")


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    _workspace(tmp_path)
    return tmp_path


class TestTheMarkers:
    def test_a_cmake_build_tree_is_recognised(self, tmp_path: Path):
        (tmp_path / "build").mkdir()
        (tmp_path / "build" / "CMakeCache.txt").write_text("# cache\n")
        assert generated_kind(tmp_path / "build") == "CMake build tree"

    def test_a_catkin_space_is_recognised_by_its_catkin_file(self, tmp_path: Path):
        (tmp_path / "devel").mkdir()
        (tmp_path / "devel" / ".catkin").write_text("/ws/src\n")
        assert generated_kind(tmp_path / "devel") == "catkin"

    def test_a_catkin_space_is_recognised_by_setup_util_and_setup_sh(self, tmp_path: Path):
        (tmp_path / "install").mkdir()
        (tmp_path / "install" / "_setup_util.py").write_text("# util\n")
        assert generated_kind(tmp_path / "install") is None  # setup.sh missing: not enough
        (tmp_path / "install" / "setup.sh").write_text("# setup\n")
        assert generated_kind(tmp_path / "install") == "catkin"

    def test_a_folder_named_devel_or_install_without_a_marker_is_not_generated(self, tmp_path: Path):
        for name in ("devel", "install"):
            (tmp_path / name).mkdir()
            (tmp_path / name / "util.h").write_text(HEADER)
            assert generated_kind(tmp_path / name) is None


class TestTheListing:
    def test_only_the_source_copy_of_a_header_is_indexed(self, workspace: Path):
        result = list_project_files(workspace)
        assert result.files == ["src/pkg/include/pkg/util.h"]

    def test_the_left_out_folders_are_reported_with_their_reason(self, workspace: Path):
        result = list_project_files(workspace)
        assert result.generated == {"devel": "catkin", "install": "catkin"}

    def test_a_cmake_build_tree_is_left_out(self, tmp_path: Path):
        _write(tmp_path / "src" / "a.h")
        _write(tmp_path / "out" / "copy.h")
        (tmp_path / "out" / "CMakeCache.txt").write_text("# cache\n")
        result = list_project_files(tmp_path)
        assert result.files == ["src/a.h"]
        assert result.generated == {"out": "CMake build tree"}

    def test_a_folder_named_install_without_a_marker_is_still_indexed(self, tmp_path: Path):
        _write(tmp_path / "install" / "util.h")
        result = list_project_files(tmp_path)
        assert result.files == ["install/util.h"]
        assert result.generated == {}

    def test_a_generated_folder_in_a_subfolder_is_left_out(self, tmp_path: Path):
        _write(tmp_path / "ws" / "src" / "a.h")
        _catkin_space(tmp_path / "ws", "devel")
        result = list_project_files(tmp_path)
        assert result.files == ["ws/src/a.h"]
        assert result.generated == {"ws/devel": "catkin"}

    def test_a_copy_indexed_before_the_rule_is_judged_as_left_out(self, workspace: Path):
        # a copy an older drspec indexed in devel/ still exists on disk: it must count as left the project, so the
        # scan removes it, and it is not reported as a file the listing missed
        result = list_project_files(workspace)
        assert result.left_the_project(workspace, "devel/include/pkg/util.h")
        assert result.left_the_project(workspace, "install/include/pkg/util.h")
        assert not result.left_the_project(workspace, "src/pkg/include/pkg/util.h")


class TestScanIncludeWins:
    def test_naming_a_generated_folder_reads_it(self, workspace: Path):
        result = list_project_files(workspace, settings={"scan.include": "devel"})
        assert "devel/include/pkg/util.h" in result.files
        assert "install/include/pkg/util.h" not in result.files
        assert "devel" not in result.generated

    def test_naming_a_folder_inside_a_generated_one_reads_that_folder_only(self, workspace: Path):
        result = list_project_files(workspace, settings={"scan.include": "devel/include/pkg"})
        assert "devel/include/pkg/util.h" in result.files
        assert "install/include/pkg/util.h" not in result.files


class TestScanIgnoreStillWorks:
    def test_scan_ignore_still_leaves_out_a_folder(self, workspace: Path):
        _write(workspace / "docs" / "note.h")
        result = list_project_files(workspace, settings={"scan.ignore": "docs"})
        assert "docs/note.h" not in result.files
        assert "src/pkg/include/pkg/util.h" in result.files


class TestInsideGit:
    def test_a_generated_folder_that_git_ignores_is_named_in_the_report_too(self, tmp_path: Path, monkeypatch):
        _workspace(tmp_path)
        ignored = [p.relative_to(tmp_path).as_posix() for folder in ("devel", "install") for p in (tmp_path / folder).rglob("*") if p.is_file()]
        git_listing(monkeypatch, tmp_path, ignored=ignored)  # `.gitignore` has devel/ and install/: git never lists their files as tracked
        result = list_project_files(tmp_path)
        assert result.used_git and result.files == ["src/pkg/include/pkg/util.h"]
        assert result.generated == {"devel": "catkin", "install": "catkin"}


    def test_a_generated_folder_is_left_out_when_git_tracks_it(self, tmp_path: Path, monkeypatch):
        _workspace(tmp_path)
        git_listing(monkeypatch, tmp_path)  # git tracks every file, the generated folders' too: it lists them all
        result = list_project_files(tmp_path)
        assert result.used_git
        assert result.files == ["src/pkg/include/pkg/util.h"]
        assert result.generated == {"devel": "catkin", "install": "catkin"}


class TestTheScanReport:
    @pytest.fixture
    def scanned(self, kept_project):
        _workspace(kept_project)
        return kept_project

    def test_the_scan_text_names_each_left_out_folder_and_how_to_read_it(self, scanned):
        result = runner.invoke(app, ["scan", "."])
        assert result.exit_code == 0, result.output
        line = "generated folders left out: devel (catkin), install (catkin); read one with: drspec config set scan.include <folder>"
        assert line in result.output.split("\n")  # the folders, and the way to read one, on one line
        assert result.output.count("devel (catkin)") == 1  # said once

    def test_the_scan_json_has_the_same_lines(self, scanned):
        data = json.loads(runner.invoke(app, ["--json", "scan", "."]).output)["data"]
        assert data["left_out"] == ["generated folder: devel (catkin)", "generated folder: install (catkin)"]
        assert "scan.include" in data["left_out_note"]

    def test_status_names_them_after_the_scan_in_json_and_in_text(self, scanned):
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        data = json.loads(runner.invoke(app, ["--json", "status"]).output)["data"]
        assert data["generated_folders"]["left_out"] == ["generated folder: devel (catkin)", "generated folder: install (catkin)"]
        assert "scan.include" in data["generated_folders"]["note"]
        text = runner.invoke(app, ["status"]).output  # the default names them on one line, and says how to read one
        assert "Generated folders left out: devel (catkin), install (catkin) (scan.include reads one)" in text
        assert "generated folder: devel (catkin)" in runner.invoke(app, ["status", "--details"]).output  # --details: as it was

    def test_status_says_nothing_before_a_scan_and_when_there_is_no_generated_folder(self, kept_project):
        _write(kept_project / "src" / "a.h")
        assert "generated_folders" not in json.loads(runner.invoke(app, ["--json", "status"]).output)["data"]
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        assert "generated_folders" not in json.loads(runner.invoke(app, ["--json", "status"]).output)["data"]

    def test_status_follows_scan_include_and_ignores_the_scan_of_one_file(self, scanned):
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        assert runner.invoke(app, ["--json", "scan", "src/pkg/include/pkg/util.h"]).exit_code == 0  # a scan of one file lists one file
        assert set(json.loads(runner.invoke(app, ["--json", "status"]).output)["data"]["generated_folders"]["left_out"]) == {
            "generated folder: devel (catkin)", "generated folder: install (catkin)"}
        assert runner.invoke(app, ["config", "set", "scan.include", "devel"]).exit_code == 0
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        left_out = json.loads(runner.invoke(app, ["--json", "status"]).output)["data"]["generated_folders"]["left_out"]
        assert left_out == ["generated folder: install (catkin)"]

    def test_the_summary_says_which_generated_folders_were_left_out(self):
        from drspec.core.scan_service import summarize

        summary = summarize({"queued": 1, "generated_folders": {"devel": "catkin"}}, None, "done")
        assert summary["generated_folders"] == {"devel": "catkin"}
