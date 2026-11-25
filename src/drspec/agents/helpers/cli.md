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

Commands print labeled plain text by default. Use `--json` as a **global option** (before the command) for strict, machine-parseable JSON:
```bash
drspec --json status         # Correct: global option before command
drspec --json search --status PENDING  # Correct
drspec status --json         # ERROR: --json must come before the command
```

---

## Function IDs

A function's ID is `File::ID`, where `ID` is the qualified name plus `#` and eight hex characters hashing its parameters, for example `src/foo.py::Order.process#3fa91c0e`. Search prints `File:` once per file and `ID:` for each function; join them with `::`. The `#hash` may be left out when only one function in the file has that name (overloads and a property with its setter share a name, and then the command lists the candidates). If a function's parameters change, its ID changes and the scan moves its contract to the new ID, marked stale with a lowered confidence; check it again.

## Core Commands

### Project Setup

| Command | Description |
|---------|-------------|
| `drspec init` | Initialize DrSpec in current directory |
| `drspec status` | View project statistics and progress |
| `drspec trace summary [--since 1h] [--cid <id>]` | Where drspec's own time went (needs `drspec config set trace.level all`) |
| `drspec trace path` / `trace list` / `trace clear [--all]` | Where this project's trace log is (outside the project), every project's log, remove it |

### Scanning & Finding Work

> **Note:** Always run `drspec scan` from your project root to ensure consistent
> relative function IDs. Using absolute paths may cause ID mismatches with other commands.

| Command | Description |
|---------|-------------|
| `drspec scan [target...]` | Ask the worker to read what was added, changed or removed (a path, a function ID or a section ID) and wait; unchanged files are skipped |
| `drspec scan --full` | Re-read and re-resolve everything (`--force` is the old name) |
| `drspec scan --no-wait` | Queue the files and return at once; Ctrl-C cancels a waiting scan |
| `drspec scan --no-recursive .` | Scan without subdirectories |
| `drspec search --status PENDING` | Functions that have no contract yet |
| `drspec search --status STALE` | Functions whose contract is out of date |

### Languages

Python, JavaScript/TypeScript (`.js .jsx .mjs .cjs .ts .tsx`), C++ and Rust (`.rs`) are indexed. Test blocks in
JavaScript (`describe`/`it`) are functions named `suite > case`; a Rust method of a trait implementation is
named with the trait (`Type::<Trait>::method`). A call through an interface or a trait lands on every implementer
(`AMBIGUOUS`). `drspec status` reports, per language, how many calls could not be tied to a function.

### Documents

Markdown files are indexed as sections and linked to the functions they name, with no LLM.

Documents use the same commands as code; results say `Kind: function` or `Kind: section`.

| Command | Description |
|---------|-------------|
| `drspec search --status STALE --docs-only` | Sections whose linked function changed since the section was written, most important first |
| `drspec search <text> --docs-only` | Search document sections (`--code-only` for functions only) |
| `drspec search --undocumented` | Functions no document names |
| `drspec deps get <function_id>` | Also lists the sections that document the function |
| `drspec deps get <section_id>` | The functions one section names, and the state of each link |
| `drspec source get <section_id>` | The text of a section (a document path gives every section) |
| `drspec ack <section_id> [function_id]` | Say a drifted section is still right |

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
