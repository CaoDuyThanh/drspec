# CHANGELOG
All notable changes to this project will be documented in this file.
- **Added** - new features
- **Changed** - updates to existing functionality
- **Deprecated** - features soon to be removed
- **Removed** - features removed in this release
- **Fixed** - bug fixes

## v0.1.0

**Added**
- Initial CLI framework using Typer with command groups (init, scan, status, queue, contract, source, deps, learn, verify, vision)
- Project initialization command (`drspec init`) that creates `_drspec/` folder, database, and agent templates
- Code scanning with tree-sitter parsers for Python, JavaScript/TypeScript, and C++
- DuckDB database storage with schema for contracts, artifacts, queue, dependencies, and reasoning traces
- Contract management commands (save, get, list) with confidence scoring
- Queue system for processing functions with peek/next operations
- JSON output formatter with `--json` and `--pretty` flags for agent compatibility
- PyInstaller binary setup for standalone executable distribution
- Status command for project overview
- Source code extraction and artifact management
- Runtime verification framework
- Learning system for bug-driven contract improvement
- Vision analysis for visual debugging and anomaly detection
- Dependency analysis and visualization
- Comprehensive test suite covering CLI, parsers, database, and core functionality
