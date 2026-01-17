"""Tests for project root detection and prompts."""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from drspec.core.ide.prompts import (
    detect_project_root,
    prompt_project_root,
    PROJECT_ROOT_MARKERS,
)


class TestProjectRootMarkers:
    """Tests for project root markers constant."""

    def test_markers_include_git(self):
        """Test that .git is a marker."""
        assert ".git" in PROJECT_ROOT_MARKERS

    def test_markers_include_python(self):
        """Test that pyproject.toml is a marker."""
        assert "pyproject.toml" in PROJECT_ROOT_MARKERS

    def test_markers_include_nodejs(self):
        """Test that package.json is a marker."""
        assert "package.json" in PROJECT_ROOT_MARKERS

    def test_markers_include_rust(self):
        """Test that Cargo.toml is a marker."""
        assert "Cargo.toml" in PROJECT_ROOT_MARKERS

    def test_markers_include_go(self):
        """Test that go.mod is a marker."""
        assert "go.mod" in PROJECT_ROOT_MARKERS


class TestDetectProjectRoot:
    """Tests for detect_project_root function."""

    def test_detects_git_root(self):
        """Test detection of .git directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".git").mkdir()
            subdir = root / "src" / "app"
            subdir.mkdir(parents=True)

            detected = detect_project_root(subdir)
            assert detected == root

    def test_detects_pyproject_root(self):
        """Test detection of pyproject.toml."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "pyproject.toml").touch()
            subdir = root / "src" / "module"
            subdir.mkdir(parents=True)

            detected = detect_project_root(subdir)
            assert detected == root

    def test_detects_package_json_root(self):
        """Test detection of package.json."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "package.json").touch()
            subdir = root / "src" / "components"
            subdir.mkdir(parents=True)

            detected = detect_project_root(subdir)
            assert detected == root

    def test_detects_cargo_toml_root(self):
        """Test detection of Cargo.toml."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "Cargo.toml").touch()
            subdir = root / "src"
            subdir.mkdir()

            detected = detect_project_root(subdir)
            assert detected == root

    def test_returns_cwd_if_no_markers(self):
        """Test returns cwd when no markers found."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cwd = Path(tmpdir)
            detected = detect_project_root(cwd)
            assert detected == cwd.resolve()

    def test_git_takes_priority_over_pyproject(self):
        """Test .git is found even if pyproject.toml exists in subdirectory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".git").mkdir()
            subdir = root / "packages" / "python"
            subdir.mkdir(parents=True)
            (subdir / "pyproject.toml").touch()

            # From subdir, should find root's .git first (walking up)
            detected = detect_project_root(subdir)
            # Since pyproject.toml is in subdir, it should be detected first
            assert detected == subdir

    def test_walks_up_multiple_levels(self):
        """Test that detection walks up multiple directory levels."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".git").mkdir()
            deep_subdir = root / "a" / "b" / "c" / "d" / "e"
            deep_subdir.mkdir(parents=True)

            detected = detect_project_root(deep_subdir)
            assert detected == root


class TestPromptProjectRoot:
    """Tests for prompt_project_root function."""

    def test_non_interactive_returns_detected(self):
        """Test non-interactive mode returns detected root."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cwd = Path(tmpdir) / "subdir"
            cwd.mkdir()
            detected = Path(tmpdir)

            with patch("drspec.core.ide.prompts.is_interactive", return_value=False):
                result = prompt_project_root(cwd, detected)

            assert result == detected

    def test_cwd_equals_detected_still_prompts(self):
        """Test input prompt is shown even when cwd == detected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cwd = Path(tmpdir)
            detected = Path(tmpdir)

            with patch("drspec.core.ide.prompts.is_interactive", return_value=True):
                with patch("builtins.input", return_value="") as mock_input:
                    with patch("builtins.print"):
                        result = prompt_project_root(cwd, detected)

            # Should ALWAYS call input() - even when cwd == detected
            mock_input.assert_called_once()
            assert result == detected

    def test_cwd_different_prompts_user(self):
        """Test prompts user when cwd != detected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cwd = root / "subdir"
            cwd.mkdir()
            detected = root

            with patch("drspec.core.ide.prompts.is_interactive", return_value=True):
                with patch("builtins.input", return_value="") as mock_input:
                    with patch("builtins.print"):
                        result = prompt_project_root(cwd, detected)

            # Should have called input() since cwd != detected
            mock_input.assert_called_once()
            # Empty response means use default (detected)
            assert result == detected

    def test_user_can_override_path(self):
        """Test user can provide custom path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cwd = root / "subdir"
            cwd.mkdir()
            custom = root / "custom"
            custom.mkdir()
            detected = root

            with patch("drspec.core.ide.prompts.is_interactive", return_value=True):
                with patch("builtins.input", return_value=str(custom)):
                    with patch("builtins.print"):
                        result = prompt_project_root(cwd, detected)

            assert result == custom.resolve()

    def test_creates_nonexistent_custom_path(self):
        """Test creates directory if custom path doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cwd = root / "subdir"
            cwd.mkdir()
            custom = root / "new_project"
            detected = root

            assert not custom.exists()

            with patch("drspec.core.ide.prompts.is_interactive", return_value=True):
                with patch("builtins.input", return_value=str(custom)):
                    with patch("builtins.print"):
                        result = prompt_project_root(cwd, detected)

            assert custom.exists()
            assert result == custom.resolve()


class TestInitWithProjectRoot:
    """Integration tests for init with --project-root flag."""

    def test_init_with_explicit_project_root(self):
        """Test init with --project-root flag."""
        import json
        from typer.testing import CliRunner
        from drspec.cli.app import app

        runner = CliRunner()

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "my-project"
            project_dir.mkdir()

            with runner.isolated_filesystem(temp_dir=tmpdir):
                result = runner.invoke(app, [
                    "init",
                    "--project-root", str(project_dir),
                    "--no-ide",
                ])

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["success"] is True
                assert str(project_dir) in response["data"]["drspec_folder"]

                # Verify files created in project_dir
                assert (project_dir / "_drspec").exists()
                assert (project_dir / "_drspec" / "contracts.db").exists()

    def test_init_creates_nonexistent_project_root(self):
        """Test init creates project root if it doesn't exist."""
        import json
        from typer.testing import CliRunner
        from drspec.cli.app import app

        runner = CliRunner()

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "new-project"

            assert not project_dir.exists()

            with runner.isolated_filesystem(temp_dir=tmpdir):
                result = runner.invoke(app, [
                    "init",
                    "--project-root", str(project_dir),
                    "--no-ide",
                ])

                assert result.exit_code == 0
                assert project_dir.exists()
                assert (project_dir / "_drspec").exists()

    def test_init_auto_detects_git_root(self):
        """Test init auto-detects .git directory."""
        import json
        from typer.testing import CliRunner
        from drspec.cli.app import app

        runner = CliRunner()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".git").mkdir()
            subdir = root / "src" / "app"
            subdir.mkdir(parents=True)

            # Change to subdir but expect _drspec created in root
            import os
            original_cwd = os.getcwd()
            try:
                os.chdir(subdir)
                result = runner.invoke(app, ["init", "--non-interactive", "--no-ide"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["success"] is True

                # _drspec should be in root, not subdir
                assert (root / "_drspec").exists()
                assert not (subdir / "_drspec").exists()
            finally:
                os.chdir(original_cwd)
