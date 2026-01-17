# DrSpec

DrSpec (or Doctor Specification) is an AI-powered design-by-contract specification tool for runtime verification.

DrSpec builds a **Semantic Mirror** of your codebase. For every function in your source code, it generates a "Contract" that defines its purpose, invariants, and verification logic.

## Features

- **Incremental Scanning**: Uses tree-sitter for fast, accurate parsing of Python, JavaScript/TypeScript, and C++
- **Contract Generation**: AI-powered extraction of function intent, invariants, and I/O examples
- **Verification Scripts**: Pre-generated Python scripts to verify contracts at runtime
- **Bug-Driven Learning**: Learn from git history to strengthen contracts based on real bug fixes
- **DuckDB Storage**: Serverless, single-file database for all contract metadata

## Installation

```bash
# From PyPI (when published)
pip install drspec

# From source
git clone https://github.com/your-org/drspec.git
cd drspec
pip install -e .
```

## Quick Start

```bash
# Initialize DrSpec in your project
drspec init

# Scan your codebase for functions
drspec scan ./src

# Check project status
drspec status

# View the processing queue
drspec queue peek

# Get next function to process
drspec queue next

# Save a contract for a function
drspec contract save "src/utils.py::calculate_total" \
  --confidence 85 \
  --contract '{"function_signature": "def calculate_total(items: List[float]) -> float", "intent_summary": "Sum all items in list", "invariants": [{"name": "non_negative", "logic": "result >= 0", "criticality": "HIGH", "on_fail": "error"}], "io_examples": []}'

# Get a contract
drspec contract get "src/utils.py::calculate_total"

# List all contracts
drspec contract list
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `drspec init` | Initialize DrSpec in the current project |
| `drspec scan <path>` | Scan source files and extract function signatures |
| `drspec status` | Show project status and queue summary |
| `drspec queue next` | Get next function from queue |
| `drspec queue peek` | Preview queue without removing items |
| `drspec queue prioritize` | Change priority of a queued function |
| `drspec contract get <id>` | Get contract for a function |
| `drspec contract save <id>` | Save a contract with confidence score |
| `drspec contract list` | List all contracts with filters |
| `drspec source get <id>` | Get source code for a function |
| `drspec verify run <id>` | Run verification script for a contract |
| `drspec deps get <id>` | Get function dependencies |
| `drspec learn analyze <range>` | Analyze git commits for bug patterns |
| `drspec learn history` | View learning history |
| `drspec learn stats` | View learning statistics |

## Contract Schema

Contracts are stored as JSON with the following structure:

```json
{
  "function_signature": "def process_data(items: List[Dict]) -> Dict",
  "intent_summary": "Process and aggregate items into summary statistics",
  "invariants": [
    {
      "name": "non_empty_result",
      "logic": "len(result) > 0",
      "criticality": "HIGH",
      "on_fail": "error"
    },
    {
      "name": "preserves_count",
      "logic": "result['count'] == len(items)",
      "criticality": "MEDIUM",
      "on_fail": "warn"
    }
  ],
  "io_examples": [
    {
      "input": {"items": [{"value": 1}, {"value": 2}]},
      "output": {"count": 2, "total": 3}
    }
  ]
}
```

## Bug-Driven Learning

DrSpec can learn from your git history to strengthen contracts:

```bash
# Analyze recent commits for bug fixes
drspec learn analyze HEAD~10..HEAD

# Preview what would be learned (dry-run)
drspec learn analyze HEAD~10..HEAD --dry-run

# Analyze between version tags
drspec learn analyze v1.0.0..v1.1.0

# View what was learned
drspec learn history

# See pattern statistics
drspec learn stats
```

The learning system:
1. Detects bug-fix commits (keywords like "fix", "bug", issue references)
2. Extracts patterns (null checks, bounds validation, error handling)
3. Suggests new invariants for affected functions
4. Boosts confidence for validated contracts

## Output Formats

DrSpec defaults to JSON output for AI agent compatibility:

```bash
# JSON output (default)
drspec status

# Human-readable output
drspec status --pretty

# Disable JSON
drspec status --no-json
```

## Database

DrSpec stores all data in `_drspec/contracts.db` (DuckDB format):

- **artifacts**: Function metadata, signatures, code hashes
- **contracts**: Contract JSON, confidence scores, verification scripts
- **queue**: Processing queue with priorities
- **dependencies**: Function call relationships
- **learning_history**: Bug patterns learned from git

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run with coverage
pytest --cov=drspec

# Build standalone binary
pyinstaller pyinstaller.spec
```

## Architecture

DrSpec implements a multi-agent architecture for contract generation:

1. **Scanner**: Tree-sitter based code parser
2. **Proposer**: Generates initial contract hypotheses
3. **Critic**: Validates contracts against actual code
4. **Judge**: Synthesizes final contract with confidence score
5. **Verifier**: Generates executable verification scripts

See [docs/specification.md](docs/specification.md) for the full design document.

## License

MIT License - see LICENSE file for details.
