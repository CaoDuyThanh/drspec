#!/usr/bin/env python3
"""Version downgrade utility for drspec project.

Handles semantic version downgrades with proper cascading:
- Patch: 1.2.3 -> 1.2.2
- Minor: 1.2.0 -> 1.1.0 (cascades from patch)
- Major: 1.0.0 -> 0.0.0 (cascades from minor)

Cannot downgrade below 0.0.0.
"""
import argparse
import sys
from pathlib import Path


VERSION_FILE = Path("VERSION")


def downgrade_major(major: int, minor: int, patch: int):
    """Downgrade major version.

    Args:
        major: Current major version
        minor: Current minor version
        patch: Current patch version

    Returns:
        Tuple of (new_major, new_minor, new_patch)

    Raises:
        ValueError: If already at 0.x.x
    """
    if major > 0:
        return (major - 1, 0, 0)
    else:
        # Cannot downgrade from 0.x.x
        raise ValueError(
            f"Cannot downgrade major from {major}.{minor}.{patch}: "
            f"already at 0.{minor}.{patch}"
        )


def downgrade_minor(major: int, minor: int, patch: int):
    """Downgrade minor version.

    Args:
        major: Current major version
        minor: Current minor version
        patch: Current patch version

    Returns:
        Tuple of (new_major, new_minor, new_patch)

    Raises:
        ValueError: If already at x.0.0 and cannot cascade
    """
    if minor > 0:
        return (major, minor - 1, 0)
    elif major > 0:
        # Cascade: decrement major when minor is 0
        return (major - 1, 0, 0)
    else:
        # At 0.0.X, cannot downgrade minor further
        raise ValueError(
            f"Cannot downgrade minor from {major}.{minor}.{patch}: "
            f"already at 0.0.{patch}"
        )


def downgrade_patch(major: int, minor: int, patch: int):
    """Downgrade patch version.

    Args:
        major: Current major version
        minor: Current minor version
        patch: Current patch version

    Returns:
        Tuple of (new_major, new_minor, new_patch)

    Raises:
        ValueError: If already at x.y.0 and cannot cascade
    """
    if patch > 0:
        return (major, minor, patch - 1)
    elif minor > 0:
        # Cascade: decrement minor when patch is 0
        return (major, minor - 1, 0)
    else:
        # At X.0.0, cannot downgrade patch further
        raise ValueError(
            f"Cannot downgrade patch from {major}.{minor}.{patch}: "
            f"already at {major}.0.0"
        )


def parse_version(version_str: str):
    """Parse semantic version string into components.

    Args:
        version_str: Version string in X.Y.Z format

    Returns:
        Tuple of (major, minor, patch) as integers

    Raises:
        ValueError: If version format is invalid
    """
    parts = version_str.strip().split(".")
    if len(parts) != 3:
        raise ValueError("Invalid version format: expected X.Y.Z")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        raise ValueError("Invalid version components: must be integers")


def format_version(major: int, minor: int, patch: int) -> str:
    """Format version components into semantic version string.

    Args:
        major: Major version number
        minor: Minor version number
        patch: Patch version number

    Returns:
        Formatted version string (e.g., "1.2.3")
    """
    return f"{major}.{minor}.{patch}"


def read_current_version() -> str:
    """Read current version from VERSION file.

    Returns:
        Current version string

    Raises:
        FileNotFoundError: If VERSION file doesn't exist
    """
    if not VERSION_FILE.exists():
        raise FileNotFoundError("VERSION file not found")
    return VERSION_FILE.read_text().strip()


def write_version(new_version: str):
    """Write new version to VERSION file.

    Args:
        new_version: New version string to write
    """
    VERSION_FILE.write_text(new_version + "\n")


def validate_version_bounds(major: int, minor: int, patch: int) -> bool:
    """Validate that version components are non-negative.

    Args:
        major: Major version number
        minor: Minor version number
        patch: Patch version number

    Returns:
        True if all components are >= 0, False otherwise
    """
    if major < 0 or minor < 0 or patch < 0:
        print("ERROR: Cannot downgrade below 0.0.0")
        return False
    return True


def main():
    """Main entry point for version downgrade utility."""
    parser = argparse.ArgumentParser(
        description="Downgrade semantic version for drspec project"
    )
    parser.add_argument(
        "--operation",
        choices=["major", "minor", "patch"],
        required=True,
        help="Which component to downgrade",
    )
    args = parser.parse_args()

    try:
        current = read_current_version()
        major, minor, patch = parse_version(current)
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    try:
        if args.operation == "major":
            n_major, n_minor, n_patch = downgrade_major(major, minor, patch)
        elif args.operation == "minor":
            n_major, n_minor, n_patch = downgrade_minor(major, minor, patch)
        else:
            n_major, n_minor, n_patch = downgrade_patch(major, minor, patch)
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    if not validate_version_bounds(n_major, n_minor, n_patch):
        print(f"Current version: {current}")
        print("Downgrade requested would go below 0.0.0")
        sys.exit(1)

    new_version = format_version(n_major, n_minor, n_patch)

    try:
        write_version(new_version)
    except Exception as e:
        print(f"ERROR: Failed to write VERSION: {e}")
        sys.exit(1)

    print(f"✓ Downgraded from v{current} to v{new_version}")
    sys.exit(0)


if __name__ == "__main__":
    main()
