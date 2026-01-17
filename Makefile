# Makefile for drspec project
# Provides version management, development, and testing utilities

VENV?=.venv
PYTHON?=python3

# Default target
.DEFAULT_GOAL := help

.PHONY: help
help:
	@echo "DrSpec Makefile - Available targets:"
	@echo ""
	@echo "Development:"
	@echo "  venv                 - Create virtual environment and install dependencies"
	@echo "  develop-install      - Install package in development mode"
	@echo "  test                 - Run all tests"
	@echo "  lint                 - Run linting checks"
	@echo ""
	@echo "Version Management:"
	@echo "  version-sync         - Sync version across all package files from VERSION"
	@echo "  version-bump-patch   - Bump patch version (0.1.0 -> 0.1.1)"
	@echo "  version-bump-minor   - Bump minor version (0.1.0 -> 0.2.0)"
	@echo "  version-bump-major   - Bump major version (0.1.0 -> 1.0.0)"
	@echo "  downgrade-patch      - Downgrade patch version (0.1.1 -> 0.1.0)"
	@echo "  downgrade-minor      - Downgrade minor version (0.2.0 -> 0.1.0)"
	@echo "  downgrade-major      - Downgrade major version (1.0.0 -> 0.0.0)"
	@echo ""
	@echo "Build & Release:"
	@echo "  build                - Build Python wheel and npm package"
	@echo "  clean                - Remove build artifacts"
	@echo ""

# Development targets
.PHONY: venv
venv:
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install -e .[dev]

.PHONY: develop-install
develop-install: venv
	$(VENV)/bin/pip install -e .[dev]

.PHONY: test
test:
	$(VENV)/bin/pytest tests/ -v

.PHONY: lint
lint:
	$(VENV)/bin/ruff check src/ tests/

.PHONY: lint-fix
lint-fix:
	$(VENV)/bin/ruff check --fix src/ tests/

# Version management targets
.PHONY: version-sync version-bump-patch version-bump-minor version-bump-major
.PHONY: downgrade-major downgrade-minor downgrade-patch _downgrade

version-sync:
	@chmod +x scripts/sync-versions.sh
	@./scripts/sync-versions.sh

version-bump-patch:
	@VERSION=$$(cat VERSION | tr -d '[:space:]'); \
	MAJOR=$$(echo $$VERSION | cut -d. -f1); \
	MINOR=$$(echo $$VERSION | cut -d. -f2); \
	PATCH=$$(echo $$VERSION | cut -d. -f3); \
	NEW_PATCH=$$((PATCH + 1)); \
	NEW_VERSION="$$MAJOR.$$MINOR.$$NEW_PATCH"; \
	echo "$$NEW_VERSION" > VERSION; \
	echo "Bumped version to $$NEW_VERSION"; \
	$(MAKE) version-sync

version-bump-minor:
	@VERSION=$$(cat VERSION | tr -d '[:space:]'); \
	MAJOR=$$(echo $$VERSION | cut -d. -f1); \
	MINOR=$$(echo $$VERSION | cut -d. -f2); \
	NEW_MINOR=$$((MINOR + 1)); \
	NEW_VERSION="$$MAJOR.$$NEW_MINOR.0"; \
	echo "$$NEW_VERSION" > VERSION; \
	echo "Bumped version to $$NEW_VERSION"; \
	$(MAKE) version-sync

version-bump-major:
	@VERSION=$$(cat VERSION | tr -d '[:space:]'); \
	MAJOR=$$(echo $$VERSION | cut -d. -f1); \
	NEW_MAJOR=$$((MAJOR + 1)); \
	NEW_VERSION="$$NEW_MAJOR.0.0"; \
	echo "$$NEW_VERSION" > VERSION; \
	echo "Bumped version to $$NEW_VERSION"; \
	$(MAKE) version-sync

# Downgrade targets
downgrade-major:
	@$(MAKE) _downgrade OP=major

downgrade-minor:
	@$(MAKE) _downgrade OP=minor

downgrade-patch:
	@$(MAKE) _downgrade OP=patch

_downgrade:
	@$(PYTHON) scripts/version_downgrade.py --operation $(OP)
	@$(MAKE) version-sync

# Build targets
.PHONY: build build-python build-npm clean

build: build-python build-npm

build-python:
	$(VENV)/bin/pip install build
	$(VENV)/bin/python -m build

build-npm:
	cd npm && npm ci --ignore-scripts

clean:
	rm -rf dist/ build/ *.egg-info/
	rm -rf npm/node_modules/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
