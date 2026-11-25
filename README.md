# DrSpec

**DrSpec** (short for **Doctor Specification**) is an AI-powered design-by-contract specification tool for runtime verification.

**DrSpec** builds a **Semantic Mirror** of your codebase. For every function in your source code, it generates a "Contract" that defines its purpose, invariants, and verification logic.

## Features

- **Incremental Scanning**: Uses tree-sitter for fast, accurate parsing of Python, JavaScript/TypeScript, C++, and Rust
- **Contract Generation**: AI-powered extraction of function intent, invariants, and I/O examples
- **Verification Scripts**: Pre-generated Python scripts to verify contracts at runtime
- **Bug-Driven Learning**: Learn from git history to strengthen contracts based on real bug fixes
- **DuckDB Storage**: Serverless, single-file database for all contract metadata

## Installation

Requires Python 3.8–3.12. The pinned local embedding runtime does not support Python 3.13.

```bash
# From PyPI (when published)
pip install drspec

# From source
git clone https://github.com/CaoDuyThanh/drspec.git
cd drspec
pip install -e .
```

## Quick Start

Run commands from the project root. Scanning parses code and updates the graph and marks changed functions for review; it does not call an LLM. A background worker keeps the graph current as files change, finds new files, and embeds the most-used functions at low priority; `drspec status` shows what it still has to do and `drspec config` adjusts it. Agent reasoning runs in your coding assistant using the templates installed by `init`.

`drspec init` also starts the worker, which indexes the project in the background (about 20% of the machine, one parser when the machine is busy), so a project is searchable a little while after `init` with no other command. To index it at full speed and wait, run `drspec init --scan` (or `drspec scan` afterwards); `drspec init --scan --no-wait` queues the whole project and returns at once, and `drspec init --no-index` sets up only. The scan options (`--deep-scan`, `--embed`, `--code-only`, `--docs-only`) belong to `drspec scan`.

This example can be reproduced in an empty directory:

```bash
mkdir drspec-example
cd drspec-example
drspec init --project-root . --no-ide --non-interactive
mkdir src
cat > src/prices.py <<'PYTHON'
def total(items):
    return sum(items)
PYTHON

drspec scan src
drspec search --status PENDING
drspec source get 'src/prices.py::total'
```

To use your assistant for contract creation, initialize with `--ide codex`, `--ide cursor`, `--ide claude-code`, or `--ide github-copilot` instead of `--no-ide`. Ask it to review the source using the installed DrSpec agents and produce contract JSON. For this example, save the following reviewed contract (the same save step the assistant uses):

```bash
cat > total-contract.json <<'JSON'
{
  "function_signature": "def total(items):",
  "intent_summary": "Sum a list of non-negative prices",
  "invariants": [
    {"name": "non_negative_total", "logic": "Output is non-negative", "criticality": "HIGH", "on_fail": "error"}
  ],
  "io_examples": [{"input": {"items": [2, 3]}, "output": 5}]
}
JSON
drspec contract save 'src/prices.py::total' --confidence 85 < total-contract.json
drspec contract get 'src/prices.py::total'
```

Saving records agent confidence separately and leaves the contract in `NEEDS_REVIEW`. Execute a check against supplied input/output:

```bash
printf '%s\n' '{"input":{"items":[2,3]},"output":5}' | drspec verify run 'src/prices.py::total'
# passed: true, status: passed, one rule checked
```

Verification checks the supplied sample; **it does not execute `total`**. A passing sample is evidence for that sample, not proof for every possible input. To reproduce a real violation, change the function and collect its output:

```bash
cat > src/prices.py <<'PYTHON'
def total(items):
    return -sum(items)
PYTHON
python - <<'PYTHON' > sample.json
import json
from src.prices import total
items = [2, 3]
print(json.dumps({"input": {"items": items}, "output": total(items)}))
PYTHON
drspec verify run 'src/prices.py::total' < sample.json
# status: failed; rule non_negative_total failed; exit code 1

drspec scan src
drspec contract get 'src/prices.py::total'
# status: STALE; freshness.current: false; old verification evidence is stale
drspec search --status STALE
# the changed function is listed for contract review
```

Freshness refers to the **latest scan**, so rescan after source or import edits. Function source hashes and dependency-context hashes are separate: import retargeting, changed callee implementations, and transitive call/property dependencies stale previous contracts and verification evidence, recording `IMPORTS_CHANGED` or `DEPENDENCY_CHANGED` as the reason. Legacy contracts without context provenance need review and a fresh save before they can establish current verification. Exact source hashes preserve indentation, comments, and literals; formatting changes within a function may also mark it for review. The first scan after upgrading from normalized hashes deliberately invalidates old hashes once. A repeated scan of identical source does not create new review work. Moving a function without changing its text refreshes line locations. Python source ranges include decorators, so adding, removing, reordering, or changing decorator arguments also invalidates source provenance.

Unsupported natural-language rules are `skipped`, never counted as passed. Each rule reports `passed`, `failed`, `skipped`, or `error`. The aggregate is `error` if any check errors, otherwise `failed` for a violation, `partial` for passed plus skipped checks, `unverified` when no rule was checked, and `passed` only when every rule passes. Partial, unverified, error, failed, and stale-contract verification return exit code 1. Current successful checks can set lifecycle status to `VERIFIED`; `contract get` and `status` show executable evidence separately from agent confidence. Legacy custom scripts without per-rule evidence cannot establish full verification.

The automatic translator accepts a bounded vocabulary such as `Output is positive`, `Output is non-negative`, `Output is not None`, `Output is not empty`, `Output is an integer`, and `All values in output are positive`. Compound or domain-specific rules require manual implementation/review and remain unverified. Type or missing-data exceptions report `error`.

## Search and the live viewer

```bash
drspec search total
drspec search -i -E 'total|price' --path src/
drspec scan src --embed
drspec search --semantic 'sum item prices'
drspec serve 'src/prices.py::total' --depth 2 --port 8765
```

Search results are grouped under the file they are in (`File: src/prices.py   (python, 2 matches)`), and each function is printed as `ID: total#3fa91c0e`. A function's identifier is `File::ID`, that is `src/prices.py::total#3fa91c0e`: the qualified name plus eight characters that hash its parameters, so overloads, and a property and its setter, are separate functions. Any command accepts the identifier without the `#hash` (`src/prices.py::total`) when only one function in that file has that name; otherwise it lists the candidates. Changing a parameter gives the function a new identifier, and the scan moves its contract, traces and history to the new one, marked stale with a lowered confidence; the same happens to a function renamed inside its file. A function moved to another file keeps nothing. Databases from before are rebuilt on the next worker start or `drspec init`.

Text search works offline without model weights. `--embed` downloads the local model on first use, then runs locally. It backfills missing vectors even for unchanged functions and records model/version metadata for node, call, and property-reference vectors. Repeating the scan reuses current vectors; a model version change rebuilds vectors in the requested scope on its next embedding scan. Changed source scanned without `--embed` clears outdated vectors.

The viewer binds to localhost and runs until Ctrl-C. Open the URL printed in the terminal. Use Refresh after scanning from another terminal, or select a node to re-center the graph. `drspec deps plot '<id>' --html` exports a static snapshot.

Generated build output is left out by default. A folder that carries a marker of a build's output is not read: a CMake build tree (`CMakeCache.txt`), or a catkin `devel/` or `install/` space (a `.catkin` file, or `_setup_util.py` with `setup.sh`). A folder only named `devel` or `install` is read as usual. A scan reports the folders it left out (`generated folder: devel (catkin)`), and `drspec status` keeps naming them after the scan (also a folder git ignores). To read one anyway, add it to `scan.include` (`drspec config set scan.include devel`); `scan.include` always wins over this rule, and `scan.ignore` works as before.

`drspec scan` does not read files itself: it asks the background worker for the files that were added, changed or removed (found by size and modification time, nothing is opened to check, so an unchanged project answers "Up to date, nothing to do" in about a second) and waits for them, ahead of anything else the worker has queued. With a target (a file, a directory, a function ID such as `src/a.py::helper`, or a document section) it covers that and re-resolves the call and document links that lead to it, without reading the files they come from. `--full` re-reads and re-publishes everything and re-resolves every call link (`--force` is the old name); `--deep-scan` and `--embed` apply to the files the scan reads, so add `--full` to apply them to files that did not change. `--no-wait` queues the files at background priority and returns; Ctrl-C cancels, keeping the batches already published and dropping the rest from the queue. A scan needs the worker (a sandbox that blocks sockets cannot scan). Files are parsed in parallel processes: a waiting scan uses every core but one (`scan.workers` changes that), the background work uses its `worker.cpu_percent` share of the machine. Each batch of files (about a second of parsing) is published in one transaction, so the index is either the old or the new state of that batch, never half; searches, `status` (including the size of the database file), `deps get` and `source get` are answered from the last published state while a batch is being built, and so is the start of a scan (listing the project and queuing its files). A few small writes made meanwhile do not wait for the batch either: a setting (`drspec config set`), a file noticed as changed, and the usage counts of search results are accepted at once and written, in the order they came, just before the batch ends. So a `drspec config get` made right after a `config set` while a batch is running can still show the old value until the batch ends (seconds; a first scan of a large project, up to a minute), and a worker that is killed, not stopped, in the middle of a batch loses the writes it was holding (a changed file is found again by its next pass; a setting is set again). Any other write, and a request inside an open transaction, still waits for the batch. Markdown documents are read only when `docs.enabled` is on (`drspec config set docs.enabled true`; it is off by default). Which files are read follows git: what it tracks or does not ignore (`scan.ignore` names folders to skip although tracked, `scan.include` folders to read although ignored); without git, the built-in ignore list applies. The summary says how many files `.gitignore` left out. Failed reads/parses retain stored records and report file/line diagnostics; rescan after fixing the source. Import edits and changes to available symbols automatically refresh call resolution, including previously scanned callers.

## CLI Commands

| Command | Description |
|---------|-------------|
| `drspec init` | Initialize DrSpec in the current project |
| `drspec scan [<target>...] [--full] [--embed] [--deep-scan] [--no-wait] [--code-only] [--docs-only] [--verbose]` | Ask the worker for what changed (a path, a function or a document section) and wait; reconcile source, graph, documents and optional embeddings |
| `drspec search <pattern> [-i] [-E] [--semantic] [--code-only] [--docs-only] [--status STALE] [--undocumented]` | Search functions and document sections by text or local vector similarity |
| `drspec serve <id> [--depth 2] [--port 8765]` | Open a live localhost graph viewer |
| `drspec status` | Show project status and the background backlog |
| `drspec config get` / `config set` | View or change worker and embedding settings |
| `drspec trace summary` / `trace tail` / `trace path` / `trace list` / `trace clear [--all]` | Read the optional trace of what drspec did (see below) |
| `drspec ack <id>` | Say a document section is still right although the code it names changed |
| `drspec contract get <id>` | Get contract for a function |
| `drspec contract save <id> --confidence <0-100>` | Save a contract with confidence score |
| `drspec contract list` | List all contracts with filters |
| `drspec source get <id>` | Get source code for a function, or the text of a document section |
| `drspec verify run <id>` | Run verification script for a contract |
| `drspec deps get <id>` | Get function dependencies, or the functions a document section describes |
| `drspec strings get <key>` | Who sets a key of a YAML config file (`avoidance/timeout`, the dot form and a leading `/` are the same key) with its file, line and value, and which code reads it by name (exact string literals; the functions, or the module level), with the line of code; a key set in yaml and never read, and the reverse, are said so; a key ending with `/` lists its sub-keys. Read from the YAML files of the project (`strings.enabled`, `strings.max_file_kb`). a key with a prefix added at run time is reported as a possible prefix site, not a read; `robot/` lists each leaf with its value and reader; unreadable yaml files are counted, and listed with `--problems`. Out of scope: launch files, `rosparam` namespaces and remapping, `.msg`/`.srv`, values that refer to other keys, keys built at run time, JSON/TOML/INI |
| `drspec refs get <Owner::member>` | Who reads and writes a member (a field of a class, C++): every function, grouped by owner class, file and function, with the kind, the line and the line of code (`--kind read\|write`, `--context N`, `--limit N`, `--format json`); a bare name lists every owner; the accesses whose receiver type is unknown are counted and named, so a short list is not read as "nobody uses it"; a file of the class that had parse problems is named too (`parse_problems`: first error line, functions and constructs skipped); `refs get`, `deps get` and a function `search` also say when a file their answer comes from has changed since the last scan (`changed_since_scan`, and a `note`: "results may be outdated; drspec scan <paths> updates them") |
| `drspec learn analyze <range>` | Analyze git commits for bug patterns |
| `drspec learn history` | View learning history |
| `drspec learn stats` | View learning statistics |

## The MCP Server (Native Tools for Your Assistant)

`drspec mcp-serve` is a Model Context Protocol server over stdio. An assistant that starts it gets drspec as native tools (`search`, `deps_get`, `refs_get`, `source_get`, `scan`, `status`, `ack` and the rest) instead of running commands in a shell. A client starts it only when its configuration says so, and `drspec init` writes that for the provider you choose:

| Provider (`--ide`) | File written | Entry |
|---|---|---|
| `claude-code` | `.mcp.json` in the project | `mcpServers.drspec`, type `stdio` |
| `cursor` | `.cursor/mcp.json` | `mcpServers.drspec` |
| `github-copilot` | `.vscode/mcp.json` | `servers.drspec`, type `stdio` |
| `codex` | `~/.codex/config.toml` (outside the project) | table `[mcp_servers.drspec]`; written only with `--codex-global`, otherwise `init` prints the lines to add |

An existing file is merged, never replaced: other servers stay, an existing `drspec` entry is left as it is, and a file that is not valid JSON is not touched (`init` says so). `--no-ide` writes nothing. The command is `drspec mcp-serve`, or the full path of the running Python (`python -m drspec mcp-serve`) when `drspec` is not on the path. After `init`, restart the client and approve the drspec server. `drspec status` (and `worker status`) report `mcp_server` with the providers that have the entry, those that do not, and those whose command cannot be found.

`--ide claude-code` also writes the skill `.claude/skills/drspec-contracts/SKILL.md`, and the MCP server sends the same guidance as its `instructions`: when an assistant should run the contract workflow in a sub-agent (before changing a PENDING or STALE function that has callers, when asked why or how a function behaves, when a scan lists changed functions), how (one sub-agent per function or a group of at most 5, the proposer then the critic, returning the contract id and confidence) and how much (at most 5 functions per run, 20 after you say yes, then it says how many PENDING and STALE functions are left). Nothing starts a sub-agent by itself; a second `init` replaces the skill, it is not repeated.

For any other client, register a stdio server named `drspec` that runs `drspec mcp-serve`, for example `{"mcpServers": {"drspec": {"command": "drspec", "args": ["mcp-serve"]}}}`. A running MCP server keeps the code it started with: after you upgrade drspec, the first tool result says so, and the client has to be restarted.

Every MCP tool takes `format`: `text` (the default, the same labeled text the CLI prints), `json`, or `compact`. A compact result carries the same data in about a third fewer characters (37% over the recorded search, `deps_get` and `unused` results; function search 49%, `deps_get` 58%, `unused` 68%, line search 30%, `refs_get` about 30%): a `~B` first line, the fields that are not defaults, results grouped by directory then file with each function written as `name#hash` (its id is the directory and file of its group, `::`, then that), and signatures clipped at 80 characters (`source_get` has them whole, and its own text is never compacted). The server's `instructions` tell the assistant how to read it, from the encoder's own legend (`core/compact_format.py`). How well an assistant reads it has not been measured on a model yet; `text` stays the default.

## Tracing What drspec Does

When a command or the background worker is slow, an optional trace records where the time went. It is off by default and creates no file while off.

```bash
drspec config set trace.level all     # the worker and every command: off | slow | all
drspec config set trace.slow_ms 500   # at "slow", only events this long or longer
drspec --trace status                 # one command only (--trace-level slow|all|off also works)
DRSPEC_TRACE=all drspec status        # the same, through the environment
drspec trace summary --since 1h       # count, total, median, p95 and max per event, then the slowest events
drspec trace summary --cid 3fa9c1d2   # one command's whole path, including the worker's requests
drspec trace clear
```

Events are JSON lines in a log outside the project, one file per project (`drspec trace path` prints it; `DRSPEC_TRACE_DIR` moves the folder; the files are readable by you only and are removed after `trace.keep_days`, default 7, or beyond `trace.max_total_mb`, default 200). Only numbers, flags and short codes are written, never a path, a pattern, a function or any text of the project. The log is renamed to `.log.1` at `trace.max_mb`, default 20. The events are: `command`, `request` (lock wait and run time of each worker request), `pass`, `rescan`, `embed` and `supervisor`. File contents, function bodies and SQL values are never written; paths are relative to the project. Each command records its peak memory and CPU seconds, and each worker pass, publish and embedding chunk records memory, database size, load and available memory, so waiting can be told from computing. A `trace.*` setting takes effect at once. `trace summary` also reports, for each event that has them, the peak and median memory and the total CPU seconds, and `trace tail` shows them on each event. The MCP tool `trace_summary` returns the same summary.

The embedding model is stored once per user (`~/.cache/drspec/models`; `DRSPEC_MODEL_DIR` overrides), not in every project; a model found in a project's `_drspec/models` is adopted once and `drspec worker status` says the old folder can be deleted.

JavaScript and TypeScript calls are tied across files through `import`, `require`, re-exports (barrel files), `tsconfig.json` `paths` and workspace packages, and `new X()`, `super`, JSX components and typed receivers are edges; Rust calls follow `mod`/`use` paths, `crate::`/`super::`/`Self::`, and the type of a receiver (a call through a trait goes to each implementer as `AMBIGUOUS`). `drspec status` shows a block per language with the share of calls left unresolved and why (`external package`, `unknown receiver`, `macro`, ...).

C++ decorator macros that the parser cannot read (export macros such as `FOO_API` are recognised already) can be listed one per line in `_drspec/cpp_macros.txt`.

After an upgrade, a worker started before it keeps running the old code. `drspec worker status` says so (`outdated`), and the next command that needs the worker replaces it; `drspec worker stop` does the same by hand.

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

DrSpec defaults to Graphify-style labeled plain text, readable by humans and
agents alike:

```bash
# Labeled plain text (default): the counts, one line per block
drspec status
# The same with the lists (partly parsed files, largest tables)
drspec status --details

# Compact JSON, for scripts/automation that need strict machine parsing
drspec --json status
```

## Database

DrSpec stores all data in `_drspec/contracts.db` (DuckDB format):

- **artifacts**: Function metadata, signatures, code hashes
- **contracts**: Contract JSON, confidence scores, verification scripts
- **dependencies**: Function call relationships
- **learning_history**: Bug patterns learned from git

**Upgrading a database written before C had its own label:** run `drspec scan --full` (or delete `_drspec` and scan
again). The scan relabels the `.c` and `.h` functions from `cpp` to `c`; their identifiers, hashes and contracts do not
change. The per-file facts of an older database are packed the same way, once, by the first scan.

A scan that is waited for publishes in batches of up to `worker.scan_budget` files (default 5,000, held in memory until they are published; lower it for a project with hundreds of thousands of functions). The background index keeps its small batches (`worker.rescan_budget`).

## Version Management

- Single source of truth: `VERSION` (kept in sync with Python and npm packages)
- Sync everything after edits: `make version-sync`
- Bump semantic versions: `make version-bump-patch` / `make version-bump-minor` / `make version-bump-major`
- Downgrade safely with cascading logic: `make downgrade-patch` / `make downgrade-minor` / `make downgrade-major`
- Updated files: `VERSION`, `pyproject.toml`, `npm/package.json`, `src/drspec/__init__.py`

```bash
# bump and sync
make version-bump-patch

# downgrade and sync
make downgrade-minor

# just resync from VERSION
make version-sync
```

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Check lint against the explicit, pinned legacy baseline
python scripts/check_lint.py

# Run the offline suite (no model downloads or socket binding)
pytest

# Run with coverage
pytest --cov=drspec

# Check the C and C++ parsers against a real clone of raylib or Dear ImGui (by hand, when a parser changes; not a test)
python scripts/corpus_check_c.py /path/to/raylib
python scripts/corpus_check_cpp.py /path/to/imgui

# Explicit integration lanes
pytest -m model    # initial model download; subsequent inference is local
pytest -m socket   # localhost socket access required

# Build standalone binary
pyinstaller pyinstaller.spec
```

## Architecture

DrSpec supplies agent templates for your coding assistant to perform contract generation:

1. **Scanner**: Tree-sitter based code parser
2. **Proposer**: Generates initial contract hypotheses
3. **Critic**: Validates contracts against actual code
4. **Judge**: Synthesizes final contract with confidence score
5. **Verifier**: DrSpec generates supported executable checks and reports coverage

CI runs the offline suite on Python 3.8 and 3.12, with model/socket integration in a separate job. Ruff 0.17.3 checks an explicit legacy baseline in `scripts/lint-baseline.json`; new findings fail CI. The baseline retains the existing style backlog and never expands automatically.

See [docs/specification.md](docs/specification.md) for the full design document.

## License

MIT License - see LICENSE file for details.
