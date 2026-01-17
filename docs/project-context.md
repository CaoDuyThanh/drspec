---
project_name: 'drspec'
user_name: 'Thanh'
date: '2026-01-06'
sections_completed: ['technology_stack', 'language_rules', 'framework_rules', 'testing_rules', 'code_quality', 'workflow_rules', 'critical_rules']
---

# Project Context for AI Agents

_This file contains critical rules and patterns that AI agents must follow when implementing code in DrSpec. Focus on unobvious details that agents might otherwise miss._

---

## Technology Stack & Versions

| Technology | Version | Purpose |
|------------|---------|---------|
| Python | 3.9+ | Implementation language |
| PyInstaller | Latest | Binary packaging (bundles Python runtime) |
| Typer | Latest | CLI framework with type hints |
| Pydantic | v2 | Contract schema validation |
| DuckDB | Latest | Single-file SQL database |
| py-tree-sitter | Latest | AST extraction (Python/JS/C++) |
| matplotlib | Latest | Visualization |
| networkx | Latest | Dependency graph |

**Distribution:**
- npm: postinstall downloads platform binary
- pip: wheel contains platform binary
- No Python runtime required on user machine

---

## Critical Implementation Rules

### Agent-First Design Principles

DrSpec is designed for AI agents (Claude Code, Cursor, Copilot) to consume via CLI. **DrSpec does NOT call LLM APIs directly** - agents run in user's chat session.

| Principle | Rule |
|-----------|------|
| JSON-first output | All commands output JSON by default |
| No interactive prompts | Commands must work non-interactively |
| Predictable error format | Structured error responses with codes |
| Stateless commands | Each invocation is self-contained |
| Idempotent operations | Safe for agents to retry |
| Bash-executable | Compatible with Bash tool calls |

### Naming Conventions (MUST FOLLOW)

| Element | Convention | Example |
|---------|------------|---------|
| Database columns | `snake_case` | `function_id`, `code_hash`, `confidence_score` |
| JSON fields | `snake_case` | `{"function_signature": "...", "intent_summary": "..."}` |
| Function IDs | `filepath::function_name` | `src/utils/parser.py::extract_tokens` |
| Error codes | `SCREAMING_SNAKE_CASE` | `CONTRACT_NOT_FOUND`, `INVALID_SCHEMA` |
| File paths | Relative to project root | `src/utils/parser.py` (never absolute) |
| Python variables | `snake_case` | `contract_data`, `queue_item` |
| Python classes | `PascalCase` | `Contract`, `Invariant`, `ScanResult` |

### Function ID Format

**Format:** `{relative_filepath}::{function_name}`

- Filepath is relative to project root
- Double colon `::` separates file from function
- Function name is the identifier as declared in source

**Examples:**
- `src/utils/parser.py::extract_tokens`
- `lib/api/handler.js::processRequest`

### CLI Response Format (MUST FOLLOW)

**Success Response:**
```json
{
    "success": true,
    "data": { },
    "error": null
}
```

**Error Response:**
```json
{
    "success": false,
    "data": null,
    "error": {
        "code": "ERROR_CODE",
        "message": "Human-readable message",
        "details": { }
    }
}
```

### Error Code Catalog

| Code | Meaning |
|------|---------|
| `DB_NOT_INITIALIZED` | `_drspec/` folder not found, run `drspec init` |
| `CONTRACT_NOT_FOUND` | No contract exists for the given function ID |
| `INVALID_SCHEMA` | Contract JSON failed Pydantic validation |
| `QUEUE_EMPTY` | No pending items in processing queue |
| `FUNCTION_NOT_FOUND` | Function ID not in artifacts table |
| `PARSE_ERROR` | Tree-sitter failed to parse source file |
| `VERIFICATION_FAILED` | Verification script returned False |

### Status Values

| Status | Meaning |
|--------|---------|
| `PENDING` | Function scanned, no contract yet |
| `VERIFIED` | Contract exists with confidence >= 70% |
| `NEEDS_REVIEW` | Contract exists with confidence < 70% |
| `STALE` | Source hash changed, contract needs refresh |
| `BROKEN` | Verification script fails against test data |

---

## Contract Schema (Pydantic)

```python
from pydantic import BaseModel
from typing import List, Literal

class Invariant(BaseModel):
    name: str
    logic: str
    criticality: Literal["HIGH", "MEDIUM", "LOW"]
    on_fail: Literal["error", "warn"]

class Contract(BaseModel):
    function_signature: str
    intent_summary: str
    invariants: List[Invariant]
    io_examples: List[dict] = []
```

**Validation is MANDATORY before saving to DuckDB.** LLMs are non-deterministic and may produce invalid schemas.

---

## Path Handling Rules

- **All paths in CLI output are relative to project root**
- **Path separator:** Always forward slash `/` (even on Windows)
- Never use absolute paths in JSON output
- Function IDs use `::` separator, not path separators

---

## Project Structure

```
drspec/
├── src/
│   └── drspec/
│       ├── __init__.py
│       ├── __main__.py              # Entry point
│       ├── cli/
│       │   ├── app.py               # Typer app with --json flag
│       │   ├── output.py            # JSON/human output formatter
│       │   └── commands/            # Grouped subcommands
│       ├── core/
│       │   ├── scanner.py           # Tree-sitter extraction
│       │   ├── hasher.py            # Normalized hash computation
│       │   └── queue.py             # Processing queue management
│       ├── contracts/
│       │   ├── schema.py            # Pydantic contract models
│       │   ├── generator.py         # Verification script generation
│       │   └── validator.py         # Contract validation
│       ├── db/
│       │   ├── connection.py        # DuckDB connection
│       │   ├── schema.sql           # Table definitions
│       │   └── queries.py           # Typed query functions
│       └── visualization/
│           └── plotter.py           # matplotlib/networkx plots
│       └── agents/                  # Agent prompts bundled with package
│           ├── librarian.md         # Librarian agent prompt (Iris)
│           ├── proposer.md          # Proposer agent prompt (Marcus)
│           ├── critic.md            # Critic agent prompt (Diana)
│           ├── judge.md             # Judge agent prompt (Solomon)
│           ├── debugger.md          # Debugger agent prompt (Sherlock)
│           ├── vision_analyst.md    # Vision Analyst agent prompt (Aurora)
│           └── helpers/
│               └── cli.md           # Shared CLI reference for agents
├── tests/
├── pyproject.toml
└── pyinstaller.spec                 # Binary build config
```

**On `drspec init`:** Agent prompts are copied from the bundled `drspec/agents/` to `_drspec/agents/` in the user's project.

---

## Testing Rules

- **Unit Tests:** `tests/unit/` - Isolated component tests
- **Integration Tests:** `tests/integration/` - CLI and DB tests
- **Fixtures:** `tests/fixtures/` - Sample files and contracts
- Run tests: `pytest tests/`
- Tests must validate naming conventions
- CLI commands must validate function ID format

---

## Critical Don't-Miss Rules

### NEVER Do These:

1. **NEVER use absolute paths** in any CLI output
2. **NEVER use camelCase** for JSON fields or database columns
3. **NEVER call LLM APIs** from DrSpec code - agents run in user's chat
4. **NEVER use interactive prompts** - all commands must be non-interactive
5. **NEVER use migrations** - database uses rebuild strategy
6. **NEVER use single colon** for function IDs - always double colon `::`

### ALWAYS Do These:

1. **ALWAYS validate contracts with Pydantic** before DuckDB write
2. **ALWAYS return JSON response wrapper format**
3. **ALWAYS use snake_case** for all naming
4. **ALWAYS use relative paths** from project root
5. **ALWAYS bundle Tree-sitter grammars** in PyInstaller binary
6. **ALWAYS copy agent templates** to `_drspec/agents/` on init

### Init Command Behavior

When `drspec init` runs:
1. Creates `_drspec/` folder in project root
2. Creates `_drspec/contracts.db` with schema
3. Copies bundled `drspec/agents/` to `_drspec/agents/` (including helpers/)
4. Adds `_drspec/` to `.gitignore` if not present
5. Returns JSON success response

---

## Component Boundaries

| Component | Responsibility | Dependencies |
|-----------|---------------|--------------|
| CLI Layer | Parse commands, format output | Core, DB |
| Core Layer | Business logic (scan, hash, queue) | DB |
| Contracts Layer | Schema validation, script generation | Pydantic |
| DB Layer | DuckDB operations, queries | DuckDB |
| Visualization | Plot generation | matplotlib, networkx |

---

## Database Tables

| Table | Owner | Access Pattern |
|-------|-------|----------------|
| `artifacts` | Scanner | Write on scan, read by all |
| `contracts` | Judge | Write on save, read by Debugger |
| `queue` | Queue Manager | FIFO with priority |
| `reasoning_traces` | All Agents | Append-only audit trail |
| `dependencies` | Scanner | Graph queries |

---

**Document Version:** 1.0
**Last Updated:** 2026-01-06
**Source:** Architecture Decision Document
