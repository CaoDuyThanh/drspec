"""Tests for PyInstaller configuration and binary build."""

from pathlib import Path



def test_pyinstaller_spec_exists():
    """Test that pyinstaller.spec exists at project root."""
    project_root = Path(__file__).parent.parent.parent
    spec_file = project_root / "pyinstaller.spec"

    assert spec_file.exists(), "pyinstaller.spec not found at project root"


def test_pyinstaller_spec_has_required_config():
    """Test that spec file contains required configuration."""
    project_root = Path(__file__).parent.parent.parent
    spec_file = project_root / "pyinstaller.spec"

    content = spec_file.read_text()

    # Check for onefile mode (EXE with a.binaries, a.zipfiles, a.datas)
    assert "EXE(" in content, "Should define EXE"
    assert "a.binaries" in content, "Should include binaries"
    assert "a.datas" in content, "Should include data files"

    # Check for hidden imports
    assert "hiddenimports" in content, "Should define hidden imports"
    assert "typer" in content, "Should include typer"
    assert "pydantic" in content, "Should include pydantic"
    assert "duckdb" in content, "Should include duckdb"

    # Check entry point
    assert "__main__.py" in content, "Should use __main__.py as entry"


def test_project_structure_for_pyinstaller():
    """Test that project structure supports PyInstaller build."""
    project_root = Path(__file__).parent.parent.parent

    # Required files
    assert (project_root / "src" / "drspec" / "__main__.py").exists()
    assert (project_root / "src" / "drspec" / "__init__.py").exists()
    assert (project_root / "pyproject.toml").exists()

    # Agent templates directory should exist inside drspec package (for bundling)
    assert (project_root / "src" / "drspec" / "agents").exists()
