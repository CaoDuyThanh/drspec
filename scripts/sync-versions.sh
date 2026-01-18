#!/bin/bash
# sync-versions.sh - Synchronize version across all packages from VERSION file

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Read version from VERSION file
VERSION_FILE="$PROJECT_ROOT/VERSION"
if [ ! -f "$VERSION_FILE" ]; then
    echo "Error: VERSION file not found at $VERSION_FILE"
    exit 1
fi

VERSION=$(cat "$VERSION_FILE" | tr -d '[:space:]')
if [ -z "$VERSION" ]; then
    echo "Error: VERSION file is empty"
    exit 1
fi

echo "Syncing all packages to version: $VERSION"
echo "================================================"

# Update npm package.json
echo ""
echo "Updating npm package.json..."
NPM_PACKAGE="$PROJECT_ROOT/npm"
cd "$NPM_PACKAGE"
npm version "$VERSION" --no-git-tag-version --allow-same-version
echo "  npm: $VERSION"
cd "$PROJECT_ROOT"

# Update Python pyproject.toml
echo ""
echo "Updating Python pyproject.toml..."
PYPROJECT="$PROJECT_ROOT/pyproject.toml"
if [ "$(uname)" = "Darwin" ]; then
    # macOS sed requires different syntax
    sed -i '' "s/^version = \".*\"/version = \"$VERSION\"/" "$PYPROJECT"
else
    sed -i "s/^version = \".*\"/version = \"$VERSION\"/" "$PYPROJECT"
fi
echo "  Python: $VERSION"

# Update Python __version__ in __init__.py
echo ""
echo "Updating Python __init__.py..."
INIT_FILE="$PROJECT_ROOT/src/drspec/__init__.py"
if [ -f "$INIT_FILE" ]; then
    if [ "$(uname)" = "Darwin" ]; then
        sed -i '' "s/^__version__ = \".*\"/__version__ = \"$VERSION\"/" "$INIT_FILE"
    else
        sed -i "s/^__version__ = \".*\"/__version__ = \"$VERSION\"/" "$INIT_FILE"
    fi
    echo "  __init__.py: $VERSION"
fi

echo ""
echo "================================================"
echo "✓ All packages synced to version $VERSION"
