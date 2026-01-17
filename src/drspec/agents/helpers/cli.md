# DrSpec CLI Reference for Agents

This is a shared reference for all DrSpec agents. Read this file before executing CLI commands.

## Getting Help

All commands support `--help` for detailed usage:
```bash
drspec --help              # List all commands
drspec <command> --help    # Command-specific help
drspec <command> <sub> --help  # Subcommand help
```

## JSON Output

All commands output JSON by default. Use `--pretty` as a **global option** (before the command) for formatted output:
```bash
drspec --pretty status         # Correct: global option before command
drspec --pretty queue peek     # Correct
drspec status --pretty         # ERROR: --pretty must come before command
```

---

## Core Commands

### Project Setup

| Command | Description |
|---------|-------------|
| `drspec init` | Initialize DrSpec in current directory |
| `drspec status` | View project statistics and progress |

### Scanning & Queue

> **Note:** Always run `drspec scan` from your project root to ensure consistent
> relative function IDs. Using absolute paths may cause ID mismatches with other commands.

| Command | Description |
|---------|-------------|
| `drspec scan [path]` | Scan source files, populate queue |
| `drspec scan --no-recursive .` | Scan without subdirectories |
| `drspec scan --no-queue .` | Scan without queueing new functions |
| `drspec queue next` | Get next function for processing |
| `drspec queue peek --limit <count>` | Preview queue items (default: 10) |
| `drspec queue list` | List queue items by status |
| `drspec queue prioritize <id> <priority>` | Set item priority (lower = higher) |

### Source Code

| Command | Description |
|---------|-------------|
| `drspec source get <function_id>` | Get function source code with hints |

### Contracts

| Command | Description |
|---------|-------------|
| `drspec contract get <function_id>` | Retrieve contract for a function |
| `drspec contract save <function_id> --confidence <score>` | Save contract (stdin JSON) |
| `drspec contract list` | List all contracts |

### Dependencies

| Command | Description |
|---------|-------------|
| `drspec deps get <function_id>` | Get function callers/callees |
| `drspec deps plot <function_id>` | Generate dependency graph PNG |

Options for `deps plot`:
- `--depth 1-5`: Levels of dependencies (default: 2)
- `--direction callers|callees|both`: Which relationships
- `--output <path>`: Custom output path

### Verification

| Command | Description |
|---------|-------------|
| `drspec verify run <function_id>` | Run verification (stdin test data) |
| `drspec verify run <id> --visualize` | Verify with data visualization |
| `drspec verify run <id> --plot-type <type>` | Specify plot type: auto, line, scatter, bar |

### Vision Findings

| Command | Description |
|---------|-------------|
| `drspec vision save <function_id> --type <type> --significance <level> --description "..."` | Save finding |
| `drspec vision list` | List all findings |
| `drspec vision list --function <id>` | List findings for function |
| `drspec vision list --status NEW` | Filter by status |
| `drspec vision list --significance HIGH` | Filter by significance |
| `drspec vision update <finding_id> --status <status>` | Update finding status |
| `drspec vision update <id> --note "..."` | Add resolution note |

### Learning (Bug-Driven)

| Command | Description |
|---------|-------------|
| `drspec learn analyze <commit-range>` | Analyze git commits for patterns |
| `drspec learn analyze HEAD~10..HEAD --dry-run` | Preview without changes |
| `drspec learn history` | View learning events |
| `drspec learn stats` | View learning statistics |

---

## Reference Data

### Finding Types (for vision save)

| Type | When to Use |
|------|-------------|
| `outlier` | Points far from expected distribution |
| `discontinuity` | Sudden jumps or gaps in data |
| `boundary` | Clustering at min/max values |
| `correlation` | Unexpected relationships between variables |
| `missing_pattern` | Expected pattern not present |

### Significance Levels

| Level | Criteria | Confidence Impact |
|-------|----------|-------------------|
| `HIGH` | Likely causes bugs, security issues, or data corruption | -15 points |
| `MEDIUM` | May cause issues under certain conditions | -8 points |
| `LOW` | Code smell, might warrant investigation | -3 points |

### Finding Statuses

| Status | Meaning |
|--------|---------|
| `NEW` | Unaddressed finding (affects confidence) |
| `ADDRESSED` | Fixed or contract updated |
| `IGNORED` | Determined to be false positive |

### Dependency Graph Colors

| Color | Status |
|-------|--------|
| Green | VERIFIED contracts |
| Yellow | NEEDS_REVIEW |
| Gray | PENDING |
| Orange | STALE |
| Red | BROKEN |

### Queue Item Statuses

| Status | Meaning |
|--------|---------|
| `PENDING` | Ready to be processed |
| `PROCESSING` | Currently being analyzed |
| `COMPLETED` | Successfully processed |
| `FAILED` | Encountered an error (will retry) |

### Invariant Criticality

| Level | When to Use |
|-------|-------------|
| `HIGH` | Violation causes data corruption, security issue, or crash |
| `MEDIUM` | Violation causes incorrect behavior but recoverable |
| `LOW` | Code smell but might not cause immediate issues |

---

## Common Patterns

### Get source, analyze, save contract
```bash
drspec source get "src/foo.py::process"
# ... analyze and generate contract ...
drspec contract save "src/foo.py::process" --confidence 85 << 'EOF'
{"function_signature": "...", "intent_summary": "...", "invariants": [...]}
EOF
```

### Verify with visualization
```bash
echo '{"input": {...}, "output": ...}' | drspec verify run "src/foo.py::process" --visualize
```

### Save vision finding
```bash
drspec vision save "src/foo.py::process" \
  --type outlier \
  --significance HIGH \
  --description "Unexpected spike at x=5" \
  --invariant "x must be <= 4"
```

### Check dependencies before changes
```bash
drspec deps plot "src/foo.py::process" --depth 2 --direction both
```

---

## Technical Notes

- DrSpec uses tree-sitter for fast, accurate parsing
- Supported languages: Python, JavaScript/TypeScript, C++
- Function hashes detect code changes automatically
- The `_drspec/` folder contains all persistent data
- All data is stored in DuckDB database
