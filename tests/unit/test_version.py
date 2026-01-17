"""Tests for version and package structure."""



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


def test_version_value():
    """Test initial version value."""
    from drspec import __version__

    assert __version__ == "0.1.0"
