from pathlib import Path


def test_pyinstaller_spec_exists():
    project_root = Path(__file__).parent.parent.parent
    spec_file = project_root / "pyinstaller.spec"

    assert spec_file.exists(), "pyinstaller.spec not found at project root"


def test_pyinstaller_spec_has_required_config():
    project_root = Path(__file__).parent.parent.parent
    spec_file = project_root / "pyinstaller.spec"

    content = spec_file.read_text()

    assert "EXE(" in content, "Should define EXE"
    assert "a.binaries" in content, "Should include binaries"
    assert "a.datas" in content, "Should include data files"

    assert "hiddenimports" in content, "Should define hidden imports"
    assert "typer" in content, "Should include typer"
    assert "pydantic" in content, "Should include pydantic"
    assert "duckdb" in content, "Should include duckdb"

    assert "__main__.py" in content, "Should use __main__.py as entry"


def test_project_structure_for_pyinstaller():
    project_root = Path(__file__).parent.parent.parent

    assert (project_root / "src" / "drspec" / "__main__.py").exists()
    assert (project_root / "src" / "drspec" / "__init__.py").exists()
    assert (project_root / "pyproject.toml").exists()

    # agent templates must be inside the package (not just the repo) to be bundled
    assert (project_root / "src" / "drspec" / "agents").exists()
