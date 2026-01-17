# DrSpec

AI-powered design-by-contract specification tool for code verification.

## Installation

```bash
npm install -g drspec
```

This will download the pre-built binary for your platform automatically.

## Supported Platforms

- macOS (Intel x64 and Apple Silicon arm64)
- Linux (x64, arm64)
- Windows (x64)

## Quick Start

```bash
# Initialize DrSpec in your project
drspec init

# Scan source files for functions
drspec scan ./src

# Check project status
drspec status

# Get next function from queue for contract generation
drspec queue next

# View a contract
drspec contract get "module.py::function_name"

# Run verification
drspec verify run "module.py::function_name"
```

## Commands

| Command | Description |
|---------|-------------|
| `drspec init` | Initialize DrSpec in current directory |
| `drspec scan <path>` | Scan source files for functions |
| `drspec status` | Show project status summary |
| `drspec queue next` | Get next function for contract generation |
| `drspec queue list` | List all queued functions |
| `drspec contract get <id>` | View a function's contract |
| `drspec contract save <id>` | Save a contract |
| `drspec contract list` | List all contracts |
| `drspec verify run <id>` | Run verification for a function |
| `drspec deps get <id>` | Show function dependencies |
| `drspec learn analyze` | Analyze git history for patterns |

## Documentation

For full documentation, visit: https://github.com/CaoDuyThanh/drspec

## Python Package

DrSpec is also available as a Python package:

```bash
pip install drspec
```

## License

MIT
