"""Tests for version and package structure."""

from pathlib import Path


def test_version_exists():
    """Test that __version__ is defined."""
    from drspec import __version__

    assert __version__ is not None
    assert isinstance(__version__, str)


def test_version_format():
    """Test that version follows semver format."""
    from drspec import __version__

    parts = __version__.split(".")
    assert len(parts) == 3, "Version should have 3 parts (major.minor.patch)"

    for part in parts:
        assert part.isdigit(), f"Version part '{part}' should be numeric"


def test_version_matches_version_file():
    """Test that __version__ matches VERSION file (single source of truth)."""
    from drspec import __version__

    # Read from VERSION file which is the single source of truth
    version_file = Path(__file__).parent.parent.parent / "VERSION"
    assert version_file.exists(), "VERSION file not found at project root"

    expected_version = version_file.read_text().strip()
    assert __version__ == expected_version, (
        f"__version__ ({__version__}) should match VERSION file ({expected_version}). "
        "Use 'make version-sync' to synchronize all version files."
    )
