"""Hand-rolled MCP stdio server — no `mcp` SDK dependency.

The official `mcp` Python SDK requires Python >=3.10, while drspec
supports down to 3.8; depending on it would silently drop support for
users still on 3.8/3.9. The protocol slice actually needed — an
`initialize` handshake, `tools/list`, `tools/call` over
newline-delimited JSON-RPC 2.0 on stdio — is small enough to
reproduce directly.

Targets the classic, handshake-based protocol revision
(`2025-06-18`), the one real MCP clients (including Claude Code)
actually speak. A newer draft drops the handshake for per-request
`_meta` versioning, but no real client implements it yet.

Every drspec CLI command has a matching tool here, each reusing (not
duplicating) the implementation the CLI command itself calls.
Excluded: `worker run` (blocks until idle-out/`SIGTERM`) and
`serve`/`mcp-serve` (long-running servers) — neither fits a
request/response tool call.

`init`'s tool is narrower than the CLI command: it skips interactive
IDE-integration setup (editor launcher files, overwrite prompts),
which has no non-interactive, agent-facing form, and does only the
part that matters for an agent's own subsequent DB access. It's also
the one tool that never goes through the worker — there's no worker
or DB file yet at bootstrap.

`verify_run` carries the same execution risk `drspec verify run`
already does — the same timeout-bounded script execution, just
invoked via a tool call instead of `Bash`.

DB access opens a fresh connection per call, closed before returning
— never held open across calls. Connections are read-only except for
`learn_history`/`learn_stats`/`learn_analyze`, which run
`init_learning_schema`'s DDL first (a read-only connection rejects
DDL outright).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional, TextIO

import duckdb

from drspec import __version__
from drspec.core.carry_over import carry_over_record
from drspec.core import compact_format, contract_guidance, tracing
from drspec.core.embedding_backlog import semantic_coverage
from drspec.core.trace_report import TraceError, trace_summary
from drspec.worker_ipc import WorkerIPCError, get_worker_connection
from drspec.core.dependency_info import find_similar_functions, get_dependency_info
from drspec.core.unresolved import add_unresolved_callers
from drspec.core.embedder import embed_text
from drspec.core.hints import extract_hints_simple
from drspec.core.provenance import contract_freshness
from drspec.contracts import (
    Contract,
    execute_verification,
    generate_verification_script,
    validate_contract,
)
from drspec.db import (
    AmbiguousFunctionId,
    resolve_function_id,
    any_artifact_has_embedding,
    count_artifacts,
    count_contracts,
    count_search_artifacts,
    count_unused_artifacts,
    get_artifact,
    get_contract,
    get_contract_confidence_stats,
    get_edge_stats,
    insert_contract,
    insert_reasoning_trace,
    list_search_file_paths,
    list_unused_artifacts,
    only_test_caller_function_ids,
    search_artifacts,
    search_artifacts_semantic,
    update_artifact_status,
)
from drspec.db.queries import (
    VALID_ARTIFACT_STATUSES,
    matched_only_by_qualified_name,
    calculate_confidence_with_findings,
    get_all_vision_findings,
    get_artifact_code_hash,
    get_vision_findings,
    insert_vision_finding,
    set_config,
    update_vision_finding_status,
)
from drspec.learning.history import (
    LearningEvent,
    get_learning_history,
    get_learning_stats,
    init_learning_schema,
    insert_learning_event,
)

# Reuses the CLI's own Graphify-style plain-text renderer for every
# tool's default (non-JSON) output, rather than duplicating it.
from drspec.cli.output import render_text, success_response

# Reused from the CLI modules rather than duplicated — no natural home
# in `core/` yet (unlike `deps_get`/`find_similar_functions`, already
# extracted for this same dual-caller reason).
from drspec.cli.commands.contract import _check_coverage
from drspec.cli.commands.contract import find_similar_functions as _contract_similar_functions

from drspec.core import docs as docs_core
from drspec.core.language_health import language_blocks
from drspec.core.declarations import declared_in, is_declaration_only
from drspec.core.partial import partial_summary, read_with_warnings
from drspec.core.changed_notice import add_changed_notice, take_before
from drspec.core.index_notice import UNUSED_CAVEAT, add_index_notice, queue_if_unindexed
from drspec.core.partial import add_callers_caveat, add_not_indexed, not_found_with_reason, partial_files, partial_function_ids, skipped_matching_pattern
from drspec.core.live_search import InvalidPattern, known_languages, live_search, search_scope

# Reused, not duplicated, same reasoning as above.
from drspec.core.ide.prompts import detect_project_root
from drspec.cli.commands.init import (
    AGENTS_FOLDER,
    DB_NAME,
    DRSPEC_FOLDER,
    copy_agent_templates,
    is_initialized_database,
    update_gitignore,
)
from drspec.contracts import DEFAULT_CONFIDENCE_THRESHOLD, set_confidence_threshold
from drspec.core.resources import get_templates_path
from drspec.db.connection import ensure_db_directory, get_connection, init_schema
from drspec.staleness_worker import get_worker_status, stop_worker
from drspec.cli.commands.config import setting_record
from drspec.core.settings import SettingError, get_setting, list_settings, reset_setting, set_setting
from drspec.core import usage
from drspec.core.backlog import maintenance_backlog, project_folder
from drspec.core.staleness import stale_paths_checked
from drspec.learning.diff import analyze_commit_range
from drspec.learning.patterns import extract_all_patterns
from drspec.learning.strengthening import strengthen_contract

PROTOCOL_VERSION_FALLBACK = "2025-06-18"
SERVER_NAME = "drspec"

_VALID_DIRECTIONS = ("callers", "callees", "both")

_TOOLS: list[dict[str, Any]] = [
    {
        "name": "search",
        "description": (
            "Search drspec's local code graph for functions by name, signature, or body "
            "text, and the document sections (Markdown) of the project by title, keyword and text — "
            "each result says kind: function or kind: section; code_only and docs_only narrow it. "
            "drspec's primary code search and the grep replacement: a literal substring (default), a regex (regex: "
            "true), or a local-embedding similarity search (semantic: true) over the functions; with lines: true, the live text of the "
            "indexed files, module-level lines and constants included, each match labelled with its function. Use it before "
            "Bash/Grep for any code question in a drspec-scanned project."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Literal substring (default), a regex (if regex=true), "
                    "or a natural-language/code query (if semantic=true). May be omitted when "
                    "status is given, to list every function with that status "
                    "(e.g. status=PENDING for functions that still need a contract).",
                },
                "docs_only": {
                    "type": "boolean",
                    "description": "Only document sections (Markdown), found by words in their title, keywords and text. "
                    "With status=STALE: the sections that describe code which changed since they were written, ranked.",
                    "default": False,
                },
                "code_only": {
                    "type": "boolean",
                    "description": "Only functions, as before documents were indexed (no 'kind' marker on the results).",
                    "default": False,
                },
                "undocumented": {
                    "type": "boolean",
                    "description": "List the public functions that no document names by identifier, qualified name or path (pattern narrows the list).",
                    "default": False,
                },
                "semantic": {
                    "type": "boolean",
                    "description": "Similarity search over local embeddings instead of exact/regex text match.",
                    "default": False,
                },
                "regex": {
                    "type": "boolean",
                    "description": "Treat pattern as a regex instead of a literal substring. Ignored if semantic=true.",
                    "default": False,
                },
                "ignore_case": {
                    "type": "boolean",
                    "description": "Case-insensitive match. Ignored if semantic=true.",
                    "default": False,
                },
                "invert_match": {
                    "type": "boolean",
                    "description": "Show non-matching functions instead (grep's -v). Ignored if semantic=true.",
                    "default": False,
                },
                "count": {
                    "type": "boolean",
                    "description": "Return the total match count only, no per-match detail (grep's -c). "
                    "Mutually exclusive with files_with_matches/lines/before_context/after_context/context.",
                    "default": False,
                },
                "files_with_matches": {
                    "type": "boolean",
                    "description": "Return distinct matching file paths only, no function detail (grep's -l). "
                    "Mutually exclusive with count/lines/before_context/after_context/context.",
                    "default": False,
                },
                "language": {
                    "type": "string",
                    "description": "Filter by language: python, javascript, typescript, tsx, cpp, or rust.",
                },
                "path_prefix": {
                    "type": "string",
                    "description": "Filter by file path prefix.",
                },
                "status": {
                    "type": "string",
                    "description": f"Filter by contract status. One of: {sorted(VALID_ARTIFACT_STATUSES)}.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results to return (matching lines with lines/context).",
                    "default": 50,
                    "minimum": 1,
                },
                "lines": {
                    "type": "boolean",
                    "description": "Search the live contents of the indexed files instead of function bodies and "
                    "return each match as file:line:text, labelled with its enclosing function. Reaches "
                    "module-level lines (imports, constants) that no function body contains. Not "
                    "combinable with semantic/count/files_with_matches/invert_match/status.",
                    "default": False,
                },
                "before_context": {
                    "type": "integer",
                    "description": "Lines of leading context before each live match (grep's -B). Implies lines=true.",
                    "default": 0,
                    "minimum": 0,
                },
                "after_context": {
                    "type": "integer",
                    "description": "Lines of trailing context after each live match (grep's -A). Implies lines=true.",
                    "default": 0,
                    "minimum": 0,
                },
                "context": {
                    "type": "integer",
                    "description": "Lines of context on both sides (grep's -C) — before_context/after_context "
                    "override just their own side. Implies lines=true.",
                    "default": 0,
                    "minimum": 0,
                },
            },
        },
    },
    {
        "name": "refs_get",
        "description": (
            "Who reads and writes a member (a field of a class; C++): every function that does, grouped by owner class, file and "
            "function, each access with its kind (read/write), line and the line of code, and the accesses left unresolved (receiver "
            "type unknown) counted and named, so a short list is not read as 'nobody uses it'. Use it instead of a regex over lines "
            "when tracing state."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "`Owner::member` (for example AvoidanceResult::data), or a member name alone: every class that has it is listed."},
                "kind": {"type": "string", "enum": ["read", "write"], "description": "Only the accesses that read or that write (a `+=` or `++` is both)."},
                "context": {"type": "integer", "description": "Lines of code around each access, inside its function.", "default": 0, "minimum": 0, "maximum": 50},
                "limit": {"type": "integer", "description": "Maximum accesses to return (and of unresolved functions or files listed).", "default": 50, "minimum": 1},
                "unresolved": {"type": "string", "enum": ["files", "full", "count"], "default": "files",
                               "description": "The unresolved accesses: `files` (counted by file), `full` (each function with its lines), `count` (only the numbers)."},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "strings_get",
        "description": (
            "Use it instead of grepping for a YAML config key in the yaml files and the code. Who sets a key of a YAML config file "
            "(ROS parameters and the like: `avoidance/timeout`, or `avoidance.timeout`), with the file, "
            "line and value, and which code reads it: the functions (or the module level) whose string literal equals the key, with the line "
            "of code. A key set in yaml with no literal in code is said so (a prefix added at run time is a possible prefix site, not a read), and the reverse too. A key ending with `/` lists each leaf with its value and reader. Exact matches "
            "only: a key built at run time is not found."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "The key path, joined with `/` (avoidance/timeout); a leading `/` or `~/` and the dot form are the same key. A trailing `/` lists the sub-keys."},
                "limit": {"type": "integer", "description": "Maximum sites of each kind to return, and leaves under a prefix.", "default": 50, "minimum": 1},
                "problems": {"type": "boolean", "description": "List the yaml files that could not be read (otherwise only their count).", "default": False},
            },
            "required": ["key"],
        },
    },
    {
        "name": "deps_get",
        "description": (
            "Get a function's callers and/or callees from drspec's local code graph "
            "(calls, plus Python @property reads). Use it instead of grepping for a function's name to trace its usages. "
            "function_id format: "
            "'path/to/file.py::name#hash'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "function_id": {
                    "type": "string",
                    "description": "Function ID, format: 'path/to/file.py::name#hash' as printed by search; the '#hash' may be left out when only one function in the file has that name.",
                },
                "depth": {
                    "type": "integer",
                    "description": "Dependency traversal depth.",
                    "default": 1,
                    "minimum": 1,
                    "maximum": 5,
                },
                "direction": {
                    "type": "string",
                    "description": "Which direction to traverse.",
                    "enum": list(_VALID_DIRECTIONS),
                    "default": "both",
                },
            },
            "required": ["function_id"],
        },
    },
    {
        "name": "unused",
        "description": (
            "Use it instead of grepping for a function's callers to find dead code. "
            "List functions with no real-code caller/reference anywhere in drspec's local "
            "code graph — a dead-code *candidate* list, not proof (design.md §19 Finding 2, "
            "widened by §23). Includes both a function with zero callers at all and one "
            "whose only caller/reference is itself a test (see each result's "
            "only_test_callers field). Dunder/magic methods are excluded automatically; CLI "
            "entry points and anything reflection/framework-invoked are NOT excluded "
            "automatically and may still appear here — use path_prefix to exclude a known "
            "entry-point directory if needed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path_prefix": {
                    "type": "string",
                    "description": "Filter by file path prefix — e.g. exclude a known entry-point directory.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results to return.",
                    "default": 100,
                    "minimum": 1,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "status",
        "description": (
            "Project-wide drspec status: artifact counts by lifecycle status, the background backlog (files waiting for a re-scan, embedding progress), "
            "contract counts/confidence distribution, and code-graph edge counts (design.md "
            "§20) — the drspec-native alternative to a raw DB query for a project health check."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "partial": {"type": "boolean", "description": "List every file that was read only in part (a parse error kept some functions out of the index) instead of the status: the file, the first error line, the names of the skipped functions, the number of partial functions and when it was last read.", "default": False},
                "limit": {"type": "integer", "description": "With partial: list at most this many files, the worst first (0 lists all).", "default": 0, "minimum": 0},
                "details": {"type": "boolean", "description": "The whole status as before (the partly parsed files, the largest tables, the resolution reasons, the confidence distribution). By default the text is the counts, one line per block; JSON is the whole status.", "default": False},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "contract_get",
        "description": "Get a function's saved contract (invariants, intent, confidence, freshness/verification state).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "function_id": {"type": "string", "description": "Format: 'path/to/file.py::name#hash' as printed by search; the '#hash' may be left out when only one function in the file has that name."},
            },
            "required": ["function_id"],
        },
    },
    {
        "name": "contract_list",
        "description": "List saved contracts, optionally filtered by status, file path prefix, or minimum confidence.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": f"Filter by artifact status. One of: {sorted(VALID_ARTIFACT_STATUSES)}."},
                "path_prefix": {"type": "string", "description": "Filter by file path prefix."},
                "min_confidence": {"type": "integer", "description": "Minimum confidence score (0-100).", "default": 0, "minimum": 0, "maximum": 100},
                "limit": {"type": "integer", "description": "Maximum results to return.", "default": 50, "minimum": 1, "maximum": 500},
                "offset": {"type": "integer", "description": "Number of contracts to skip.", "default": 0, "minimum": 0},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "source_get",
        "description": (
            "Get the stored source for a specific function (signature, body, line numbers, "
            "@invariant hints) — the drspec-native alternative to opening the file and "
            "finding the function by hand."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "function_id": {"type": "string", "description": "Format: 'path/to/file.py::name#hash' as printed by search; the '#hash' may be left out when only one function in the file has that name."},
            },
            "required": ["function_id"],
        },
    },
    {
        "name": "verify_script",
        "description": "Get the generated (or stored) Python verification script for a function's contract.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "function_id": {"type": "string", "description": "Format: 'path/to/file.py::name#hash' as printed by search; the '#hash' may be left out when only one function in the file has that name."},
            },
            "required": ["function_id"],
        },
    },
    {
        "name": "learn_history",
        "description": "Show recorded learning events from past bug-fix analysis (design.md 'drspec learn analyze'), optionally filtered by function.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "function_id": {"type": "string", "description": "Filter by function ID."},
                "limit": {"type": "integer", "description": "Maximum events to return.", "default": 20, "minimum": 1},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "learn_stats",
        "description": "Summary statistics across all recorded learning events (commits analyzed, invariants suggested/validated, pattern distribution).",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "vision_list",
        "description": "List vision-analysis findings (anomalies spotted in a plotted function), optionally filtered by function, status, or significance.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "function_id": {"type": "string", "description": "Filter by function ID."},
                "status": {"type": "string", "description": "Filter by status: NEW, ADDRESSED, IGNORED."},
                "significance": {"type": "string", "description": "Filter by significance: HIGH, MEDIUM, LOW."},
                "limit": {"type": "integer", "description": "Maximum results to return.", "default": 20, "minimum": 1},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "scan",
        "description": (
            "Scan source files and documents, build/update the function dependency graph, and optionally "
            "embed both. The background worker reads the files that were added, changed or removed (skipping "
            "unchanged ones) ahead of everything else queued; this tool waits up to 60 seconds and then answers "
            "with the progress so far (the worker goes on). Writes to the project's drspec database; not a read."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File, directory, function ID or document section ID to scan. Defaults to every added, changed or removed file of the project."},
                "targets": {"type": "array", "items": {"type": "string"}, "description": "Several of the above at once."},
                "recursive": {"type": "boolean", "description": "Recursively scan directories.", "default": True},
                "deep_scan": {"type": "boolean", "description": "Also attempt fallback call resolution (INFERRED/AMBIGUOUS confidence) for the files this scan reads.", "default": False},
                "embed": {"type": "boolean", "description": "Compute local embeddings for the files this scan reads (needed for search's semantic=true). Off by default — the single largest cost in a scan.", "default": False},
                "full": {"type": "boolean", "description": "Re-read and re-publish every file and document, changed or not, and re-resolve every call link.", "default": False},
                "force": {"type": "boolean", "description": "Old name of full.", "default": False},
                "no_wait": {"type": "boolean", "description": "Queue the files at background priority and return at once.", "default": False},
                "code_only": {"type": "boolean", "description": "Read code files only.", "default": False},
                "docs_only": {"type": "boolean", "description": "Read documents only.", "default": False},
                "verbose": {"type": "boolean", "description": "The whole summary as before (every counter, every function re-read). By default the text is one line for the result and one for each thing that needs attention; JSON is the whole summary.", "default": False},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "init",
        "description": (
            "Bootstrap drspec in a project: create _drspec/contracts.db, copy agent "
            "templates, set the confidence threshold, update .gitignore, and start the worker so that the project is indexed "
            "in the background (scan=true scans it at full speed and waits). Narrower than "
            "'drspec init' on the CLI — never touches IDE integration (Cursor/Claude Code/"
            "GitHub Copilot/Codex launcher files), which has no non-interactive, agent-facing "
            "form; use the CLI directly for that."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "Project root directory. Defaults to auto-detected/current directory."},
                "force": {"type": "boolean", "description": "On an already-initialized project, overwrite the agent templates without skipping. Never rebuilds or clears the database, same as the CLI.", "default": False},
                "confidence_threshold": {
                    "type": "integer", "description": "Confidence threshold for VERIFIED status (0-100). Applied only when creating the database; ignored on an already-initialized project.",
                    "default": DEFAULT_CONFIDENCE_THRESHOLD, "minimum": 0, "maximum": 100,
                },
                "scan": {
                    "type": "boolean",
                    "description": (
                        "After the setup, scan the whole project at full speed, as the scan tool does: it waits up to 60 seconds and "
                        "then answers, leaving the scan running. Without it the worker is started and indexes the project in the "
                        "background, at about 20% of the machine."
                    ),
                    "default": False,
                },
                "no_wait": {"type": "boolean", "description": "With scan: queue the files at background priority and return at once.", "default": False},
                "no_index": {"type": "boolean", "description": "Do not start the worker: the setup only.", "default": False},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "deps_plot",
        "description": (
            "Generate a dependency graph visualization for a function — a static PNG, or "
            "(html=true) a self-contained interactive HTML page. Writes an image/HTML file "
            "as its result, not something returned inline."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "function_id": {"type": "string", "description": "Function ID to plot dependencies for."},
                "depth": {"type": "integer", "description": "Dependency depth.", "default": 2, "minimum": 1, "maximum": 5},
                "direction": {"type": "string", "description": "callers, callees, or both.", "enum": list(_VALID_DIRECTIONS), "default": "both"},
                "output_path": {"type": "string", "description": "Output file path. Defaults to _drspec/plots/{hash}_deps.png (or .html)."},
                "html": {"type": "boolean", "description": "Generate an interactive HTML page instead of a static PNG.", "default": False},
            },
            "required": ["function_id"],
        },
    },
    {
        "name": "contract_save",
        "description": (
            "Save a contract for a function. Validates against the contract JSON schema "
            "before saving, and updates the artifact's status based on confidence."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "function_id": {"type": "string", "description": "Function ID the contract is for."},
                "contract_json": {"type": "string", "description": "Contract JSON, matching drspec's contract schema (function_signature, intent_summary, invariants, ...)."},
                "confidence": {"type": "integer", "description": "Confidence score (0-100).", "minimum": 0, "maximum": 100},
                "agent": {"type": "string", "description": "Agent name for the reasoning trace.", "default": "judge"},
                "trace": {"type": "string", "description": "Optional reasoning trace (JSON or plain text)."},
            },
            "required": ["function_id", "contract_json", "confidence"],
        },
    },
    {
        "name": "verify_run",
        "description": (
            "Run a function's contract verification script against a real input/output "
            "sample, and record the result. Executes the (generated or stored) Python "
            "verification script with the same timeout-bounded mechanism 'drspec verify "
            "run' already uses — not a new execution risk, the same one that command carries."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "function_id": {"type": "string", "description": "Function ID to verify."},
                "input_data": {"type": "object", "description": "Input parameters, as a JSON object."},
                "output_data": {"description": "Actual output from the function — any JSON value."},
                "timeout": {"type": "number", "description": "Maximum script execution time in seconds.", "default": 1.0},
            },
            "required": ["function_id", "input_data", "output_data"],
        },
    },
    {
        "name": "learn_analyze",
        "description": (
            "Analyze a git commit range for bug-fix patterns and suggest/apply contract "
            "strengthening. Runs real git log analysis — slower and more I/O-heavy than "
            "this project's other tools."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "commit_range": {"type": "string", "description": "Git commit range, e.g. 'HEAD~10..HEAD'."},
                "dry_run": {"type": "boolean", "description": "Show what would be learned without recording events.", "default": False},
                "bug_fixes_only": {"type": "boolean", "description": "Only analyze commits that appear to be bug fixes.", "default": True},
                "repo_path": {"type": "string", "description": "Path to the git repository. Defaults to the current directory."},
            },
            "required": ["commit_range"],
        },
    },
    {
        "name": "vision_save",
        "description": "Save a vision-analysis finding (an anomaly spotted in a plotted function) to the database.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "function_id": {"type": "string", "description": "Function ID the finding relates to."},
                "finding_type": {"type": "string", "description": "outlier, discontinuity, boundary, correlation, or missing_pattern."},
                "significance": {"type": "string", "description": "HIGH, MEDIUM, or LOW."},
                "description": {"type": "string", "description": "Description of the finding."},
                "location": {"type": "string", "description": "Where in the plot it was observed."},
                "invariant": {"type": "string", "description": "Suggested invariant implication."},
                "plot_path": {"type": "string", "description": "Path to the plot image."},
            },
            "required": ["function_id", "finding_type", "significance", "description"],
        },
    },
    {
        "name": "vision_update",
        "description": "Update a vision finding's status (e.g. after addressing or dismissing it).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "finding_id": {"type": "integer", "description": "Finding ID to update."},
                "status": {"type": "string", "description": "NEW, ADDRESSED, or IGNORED."},
                "note": {"type": "string", "description": "Resolution note."},
            },
            "required": ["finding_id", "status"],
        },
    },
    {
        "name": "worker_stop",
        "description": "Stop the running background staleness worker, if one is running, and wait for it to exit. A clean, explicit alternative to killing the PID by hand.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "config_get",
        "description": (
            "Show one drspec setting, or all of them when no key is given. Settings control the "
            "background worker (poll interval, files re-scanned per pass) and background embedding "
            "(on/off, how many items, CPU/thread/load/memory limits)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string", "description": "Setting key, e.g. 'worker.poll_interval'. Omit to list all."}},
            "additionalProperties": False,
        },
    },
    {
        "name": "config_set",
        "description": "Change a drspec setting, or restore its default with reset=true. The background worker applies it on its next pass.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Setting key."},
                "value": {"description": "New value (number or boolean, as the setting requires)."},
                "reset": {"type": "boolean", "description": "Restore the default instead of setting a value.", "default": False},
            },
            "required": ["key"],
            "additionalProperties": False,
        },
    },
    {
        "name": "worker_status",
        "description": "Report whether the background staleness worker is running (and its PID), and how many files are currently flagged as possibly stale.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "trace_summary",
        "description": (
            "Summarize drspec's optional trace (a log kept outside the project; the path is `drspec trace path`): per event the count, total, median, 95th "
            "percentile and maximum milliseconds, then the slowest events with their correlation ids (cid). "
            "Pass cid to get one command's whole path from the command to the worker's requests. The trace is "
            "off unless 'trace.level' is set to slow or all (config_set)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": {"type": "string", "description": "Only events from the last period, such as 30m, 1h or 2d."},
                "slowest": {"type": "integer", "minimum": 0, "default": 20, "description": "How many of the slowest events to list."},
                "cid": {"type": "string", "description": "Return this one command's path instead."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "ack",
        "description": (
            "Mark a document section as reviewed: the document is still right, whatever the code did. Its links take the code as it is "
            "now, so it stops showing in search with status=STALE and docs_only=true until the code changes again. "
            "identifier is a section ID as search prints it ('docs/guide.md::/Guide/Widget/'; the '#hash' may be left out)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "identifier": {"type": "string", "description": "Document section ID."},
                "function_id": {"type": "string", "description": "Only this function's link; default: all of the section's."},
            },
            "required": ["identifier"],
            "additionalProperties": False,
        },
    },
]

# Every tool gets the same `format` opt-in the CLI's `--json` is.
# Injected once here, mechanically, rather than repeated by hand in
# every dict above.
_FORMAT_SCHEMA_PROPERTY: dict[str, Any] = {
    "type": "string",
    "enum": ["text", "json", "compact"],
    "default": "text",
    "description": (
        'Output format. "text" (default) returns the same Graphify-style labeled plain '
        'text the equivalent CLI command prints. "json" returns strict, machine-parseable '
        'JSON instead, for callers that need to parse the result programmatically. "compact" '
        "returns the same data in about a third fewer characters (a `~B` result; the server "
        "instructions say how to read it); `source_get` is always returned as text."
    ),
}
for _tool_schema in _TOOLS:
    _tool_schema["inputSchema"]["properties"]["format"] = dict(_FORMAT_SCHEMA_PROPERTY)
del _tool_schema


def _connect_readonly(db_path: Path):
    """A fresh IPC connection per tool call to the worker — the only
    process that opens the DB file directly — never held open across calls."""
    return get_worker_connection(db_path, read_only=True)


def _db_unavailable_message(e: Exception) -> str:
    return (
        f"Could not open the drspec database read-only ({e}). If this is a fresh "
        "project, run 'drspec init' and 'drspec scan' first. If 'drspec scan' is "
        "running concurrently, this is likely transient — retry. If an old worker "
        "still holds the database, run 'drspec worker stop', then run the command again."
    )


def _tool_search(db_path: Path, arguments: dict[str, Any]) -> tuple[Any, bool]:
    pattern = arguments.get("pattern")

    status = arguments.get("status")
    if status is not None and status not in VALID_ARTIFACT_STATUSES:
        return f"Invalid status: {status!r}. Must be one of {sorted(VALID_ARTIFACT_STATUSES)}.", True

    semantic = bool(arguments.get("semantic", False))
    limit = int(arguments.get("limit", 50))
    ignore_case = bool(arguments.get("ignore_case", False))
    regex = bool(arguments.get("regex", False))
    invert_match = bool(arguments.get("invert_match", False))
    count = bool(arguments.get("count", False))
    files_with_matches = bool(arguments.get("files_with_matches", False))

    context_value = int(arguments.get("context", 0))
    effective_before = int(arguments.get("before_context", 0)) or context_value
    effective_after = int(arguments.get("after_context", 0)) or context_value
    wants_context = effective_before > 0 or effective_after > 0
    wants_lines = bool(arguments.get("lines", False)) or wants_context

    language = arguments.get("language")
    if language is not None and language not in known_languages():
        return f"Unknown language: {language!r}. Known: {', '.join(sorted(known_languages()))}.", True
    docs_only = bool(arguments.get("docs_only", False))
    code_only = bool(arguments.get("code_only", False))
    undocumented = bool(arguments.get("undocumented", False))
    if docs_only and code_only:
        return "code_only and docs_only exclude each other.", True
    if docs_only and (semantic or count or files_with_matches or invert_match or wants_lines or undocumented or arguments.get("language")):
        return "docs_only searches section text; it can't be combined with semantic, count, files_with_matches, invert_match, lines, undocumented or language.", True
    if undocumented and (semantic or count or files_with_matches or invert_match or wants_lines or status is not None):
        return "undocumented lists functions; it can't be combined with semantic, count, files_with_matches, invert_match, lines or status.", True
    if docs_only and status is not None and status != "STALE":
        return "Document sections have one status, STALE: the sections whose code changed.", True

    if not pattern:
        if (status is None and not undocumented) or semantic or wants_lines:
            return (
                "Missing required argument: pattern. It can only be left out when status or undocumented is given, "
                "to list every function with that status or that no document names (not with semantic or lines)."
            ), True
        pattern = ""  # an empty substring matches every function

    if count and files_with_matches:
        return "count and files_with_matches are mutually exclusive — pick one.", True
    if (wants_lines or count or files_with_matches) and semantic:
        return "lines/count/files_with_matches have no meaning combined with semantic=true.", True
    if (count or files_with_matches) and wants_lines:
        return "lines/before_context/after_context/context only apply to the default match-listing mode.", True
    if wants_lines and (invert_match or status is not None):
        return (
            "lines/before_context/after_context/context search file contents, so "
            "invert_match/status (which filter functions) can't be combined with them."
        ), True

    try:
        conn = _connect_readonly(db_path)
    except (duckdb.Error, WorkerIPCError) as e:
        return _db_unavailable_message(e), True

    documents_on = docs_core.enabled(conn)
    with_documents = documents_on and not (code_only or semantic or count or files_with_matches or wants_lines or invert_match or arguments.get("language"))
    before = take_before(conn)  # taken before the answer's reads (see changed_notice)
    try:
        if docs_only:
            found = docs_core.section_matches(conn, pattern, limit, status, arguments.get("path_prefix"))
            data = {"pattern": pattern, "semantic": False, "total_matches": len(found), "matches": found}
            if not documents_on:
                data["note"] = docs_core.OFF_NOTE
            return data, False
        if undocumented:
            prefix = arguments.get("path_prefix") or ""
            rows = [r for r in docs_core.undocumented(conn, 100000) if pattern.lower() in r["function_id"].lower() and r["file_path"].startswith(prefix)][:limit]
            data = {"pattern": pattern, "undocumented": True, "total_matches": len(rows), "matches": rows}
            if not documents_on:
                data["note"] = docs_core.OFF_NOTE
            return data, False
        if count:
            total = count_search_artifacts(
                conn, pattern, ignore_case=ignore_case, regex=regex, invert_match=invert_match,
                language=arguments.get("language"), path_prefix=arguments.get("path_prefix"), status=status,
            )
            return {"pattern": pattern, "count": total, **search_scope(conn, arguments.get("language"), arguments.get("path_prefix"))}, False
        if files_with_matches:
            file_paths = list_search_file_paths(
                conn, pattern, ignore_case=ignore_case, regex=regex, invert_match=invert_match,
                language=arguments.get("language"), path_prefix=arguments.get("path_prefix"), status=status,
            )
            return {"pattern": pattern, "files": file_paths, "file_count": len(file_paths), **search_scope(conn, arguments.get("language"), arguments.get("path_prefix"))}, False
        if wants_lines:
            try:
                return add_index_notice(conn, live_search(
                    conn, pattern, ignore_case=ignore_case, regex=regex,
                    language=arguments.get("language"), path_prefix=arguments.get("path_prefix"),
                    limit=limit, before=effective_before, after=effective_after,
                )), False
            except InvalidPattern as e:
                return str(e), True
        if semantic:
            query_embedding = embed_text(pattern)
            matches = search_artifacts_semantic(
                conn,
                query_embedding,
                language=arguments.get("language"),
                path_prefix=arguments.get("path_prefix"),
                status=status,
                limit=limit,
            )
            if not matches and not any_artifact_has_embedding(conn):
                return (
                    "No functions have a local embedding yet — 'drspec scan' doesn't "
                    "compute embeddings by default. Run 'drspec scan --embed' first, "
                    "then retry this search."
                ), True
            scope = search_scope(conn, arguments.get("language"), arguments.get("path_prefix"))
            coverage = semantic_coverage(conn, arguments.get("language"), arguments.get("path_prefix"), status)  # how much it searched
            stale_paths = stale_paths_checked(conn, [m.artifact.file_path for m in matches])
            usage.record_usage(conn, usage.nodes(m.artifact.function_id for m in matches))
            results = [
                {
                    "function_id": m.artifact.function_id,
                    "function_name": m.artifact.function_name,
                    "file_path": m.artifact.file_path,
                    "status": m.artifact.status,
                    "signature": m.artifact.signature,
                    "score": m.score,
                    "possibly_stale": m.artifact.file_path in stale_paths,
                }
                for m in matches
            ]
        else:
            matches = search_artifacts(
                conn,
                pattern,
                ignore_case=ignore_case,
                regex=regex,
                invert_match=invert_match,
                language=arguments.get("language"),
                path_prefix=arguments.get("path_prefix"),
                status=status,
                limit=limit,
            )
            scope = search_scope(conn, arguments.get("language"), arguments.get("path_prefix"))
            stale_paths = stale_paths_checked(conn, [a.file_path for a in matches])
            usage.record_usage(conn, usage.nodes(a.function_id for a in matches))
            partial_ids = partial_function_ids(conn, {a.file_path for a in matches})
            results = [
                {
                    **({"kind": "function"} if with_documents else {}),
                    "function_id": a.function_id,
                    "function_name": a.function_name,
                    "file_path": a.file_path,
                    "status": a.status,
                    "signature": a.signature,
                    "possibly_stale": a.file_path in stale_paths,
                    **({"partial": True} if a.function_id in partial_ids else {}),
                    **({"matched_in": "qualified_name"} if not invert_match and matched_only_by_qualified_name(a, pattern, ignore_case, regex) else {}),
                }
                for a in matches
            ]
            functions_shown = len(results)
            total_functions = functions_shown
            if functions_shown >= limit:  # there may be more than were asked for: count them, so a cut-off is never silent
                total_functions = count_search_artifacts(
                    conn, pattern, ignore_case=ignore_case, regex=regex, invert_match=invert_match,
                    language=arguments.get("language"), path_prefix=arguments.get("path_prefix"), status=status,
                )
            if with_documents:
                results += docs_core.section_matches(conn, pattern, limit, status, arguments.get("path_prefix"))
            not_indexed = [] if invert_match else skipped_matching_pattern(conn, pattern, regex=regex, ignore_case=ignore_case)
        changed = add_changed_notice(conn, {}, db_path.parent.parent, [r["file_path"] for r in results if r.get("file_path")], before=before)  # (the files the answer lists)
    finally:
        conn.close()

    data = {"pattern": pattern, "semantic": semantic, **scope, "total_matches": len(results), "matches": results}
    if "changed_since_scan" in changed:
        data.update(changed)
    if semantic:
        data.update(coverage)
    if not semantic:
        data["total_matches"] = total_functions + (len(results) - functions_shown)
        data["truncated"] = total_functions > functions_shown
        if data["truncated"]:
            sentence = f"Showing {functions_shown} of {total_functions} matching functions, best match first; raise limit to see more."
            data["note"] = f"{data['note']} {sentence}" if data.get("note") else sentence
        add_not_indexed(None, data, not_indexed)  # functions the pattern names that a parse error kept out of the index
    return data, False


def _resolve_id(conn: Any, function_id: str) -> tuple[Optional[str], Optional[str]]:
    """`(stored identifier, None)`, or `(None, message)` when `file::name` matches several
    functions of the file (the connection is closed in that case)."""
    try:
        resolved = resolve_function_id(conn, function_id)
        queue_if_unindexed(conn, resolved)
        return resolved, None
    except AmbiguousFunctionId as e:
        conn.close()
        return None, str(e)


def _tool_strings_get(db_path: Path, arguments: dict[str, Any]) -> tuple[Any, bool]:
    key = arguments.get("key")
    if not key:
        return "Missing required argument: key", True
    from drspec.core.strings import add_strings_notice, get_strings

    try:
        conn = _connect_readonly(db_path)
    except (duckdb.Error, WorkerIPCError) as e:
        return _db_unavailable_message(e), True
    try:
        before = take_before(conn)  # taken before the answer's reads (see changed_notice)
        data = get_strings(conn, key, limit=int(arguments.get("limit", 50)), root=db_path.parent.parent, problems=bool(arguments.get("problems", False)))
        add_strings_notice(conn, data, db_path.parent.parent, before=before)
        return data, False
    except ValueError as e:
        return str(e), True
    finally:
        conn.close()


def _tool_refs_get(db_path: Path, arguments: dict[str, Any]) -> tuple[Any, bool]:
    symbol = arguments.get("symbol")
    if not symbol:
        return "Missing required argument: symbol", True
    from drspec.core.refs import get_refs

    try:
        conn = _connect_readonly(db_path)
    except (duckdb.Error, WorkerIPCError) as e:
        return _db_unavailable_message(e), True
    try:
        return get_refs(conn, symbol, kind=arguments.get("kind"), context=int(arguments.get("context", 0)), limit=int(arguments.get("limit", 50)),
                        unresolved=arguments.get("unresolved", "files"), root=db_path.parent.parent), False
    except ValueError as e:
        return str(e), True
    finally:
        conn.close()


def _tool_deps_get(db_path: Path, arguments: dict[str, Any]) -> tuple[Any, bool]:
    function_id = arguments.get("function_id")
    if not function_id:
        return "Missing required argument: function_id", True

    direction = arguments.get("direction", "both")
    if direction not in _VALID_DIRECTIONS:
        return f"Invalid direction: {direction!r}. Must be one of {_VALID_DIRECTIONS}.", True

    depth = max(1, min(5, int(arguments.get("depth", 1))))

    try:
        conn = _connect_readonly(db_path)
    except (duckdb.Error, WorkerIPCError) as e:
        return _db_unavailable_message(e), True

    target = docs_core.document_target(conn, function_id)
    if target is not None:  # a document section or a document: the functions it describes
        try:
            return docs_core.section_dependencies(conn, *target), False
        finally:
            conn.close()

    function_id, failure = _resolve_id(conn, function_id)
    if failure:
        return failure, True
    before = take_before(conn)  # taken before the answer's reads (see changed_notice)

    try:
        artifact = get_artifact(conn, function_id)
        if artifact is None:
            similar = find_similar_functions(conn, function_id)
            message = f"Function not found: {function_id}."
            message, _details = not_found_with_reason(conn, message, function_id, {})
            if similar:
                message += f" Did you mean one of: {', '.join(similar)}?"
            return message, True

        callees, callers = get_dependency_info(conn, function_id, depth)
        if direction == "callees":
            callers = []
        elif direction == "callers":
            callees = []
        usage.record_usage(conn, usage.dependency_items(function_id, callees, callers), "usage.lookup_weight")

        result = {
            "function_id": function_id,
            "function_name": artifact.function_name,
            "file_path": artifact.file_path,
            "status": artifact.status,
            "possibly_stale": artifact.file_path in stale_paths_checked(conn, [artifact.file_path]),
            "depth": depth,
            "direction": direction,
            "callees": callees,
            "callers": callers,
        }
        result = add_unresolved_callers(conn, result, artifact, direction)
        result = add_callers_caveat(conn, result, direction)
        documented = docs_core.documented_in(conn, artifact.function_id)
        if documented:
            result["documented_in"] = documented
        add_index_notice(conn, result)
        add_changed_notice(conn, result, db_path.parent.parent, [artifact.file_path, *(e["file_path"] for e in callees + callers)], before=before)
    finally:
        conn.close()

    return result, False


def _tool_unused(db_path: Path, arguments: dict[str, Any]) -> tuple[Any, bool]:
    path_prefix = arguments.get("path_prefix")
    limit = int(arguments.get("limit", 100))

    try:
        conn = _connect_readonly(db_path)
    except (duckdb.Error, WorkerIPCError) as e:
        return _db_unavailable_message(e), True

    try:
        results = list_unused_artifacts(conn, path_prefix=path_prefix, limit=limit)
        total_count = count_unused_artifacts(conn, path_prefix=path_prefix)
        stale_paths = stale_paths_checked(conn, [a.file_path for a in results])
        only_test_called = only_test_caller_function_ids(conn, [a.function_id for a in results])
    finally:
        conn.close()

    result = {
        "total_count": total_count,
        "returned": len(results),
        "limit": limit,
        "path_filter": path_prefix,
        "caveat": UNUSED_CAVEAT,
        "functions": [
            {
                "function_id": a.function_id,
                "function_name": a.function_name,
                "file_path": a.file_path,
                "status": a.status,
                "possibly_stale": a.file_path in stale_paths,
                "only_test_callers": a.function_id in only_test_called,
            }
            for a in results
        ],
    }
    return result, False


def _tool_status(db_path: Path, arguments: dict[str, Any]) -> tuple[Any, bool]:
    try:
        conn = _connect_readonly(db_path)
    except (duckdb.Error, WorkerIPCError) as e:
        return _db_unavailable_message(e), True

    if arguments.get("partial"):
        try:
            return partial_files(conn, max(0, int(arguments.get("limit", 0)))), False
        finally:
            conn.close()

    try:
        artifact_total = count_artifacts(conn)
        artifact_by_status = {status: count_artifacts(conn, status=status) for status in VALID_ARTIFACT_STATUSES}
        contract_total = count_contracts(conn)
        confidence_stats = get_contract_confidence_stats(conn)
        edge_stats = get_edge_stats(conn)
        items_needing_attention = sum(
            artifact_by_status.get(s, 0) for s in ("PENDING", "BROKEN", "NEEDS_REVIEW", "STALE")
        )
        result = {
            "artifacts": {"total": artifact_total, "by_status": artifact_by_status},
            "backlog": maintenance_backlog(conn, root=project_folder(db_path)),
            "contracts": {"total": contract_total, "confidence": confidence_stats},
            "edges": edge_stats,
            "summary": {
                "items_needing_attention": items_needing_attention,
                "completion_rate": round(artifact_by_status.get("VERIFIED", 0) / artifact_total * 100, 1)
                if artifact_total > 0
                else 0.0,
            },
        }
        partial = partial_summary(conn)
        if partial:
            result["partially_parsed"] = partial
        elif read_with_warnings(conn):
            result["files_read_with_warnings"] = read_with_warnings(conn)
        for language_key, block in language_blocks(conn).items():
            result[language_key] = block
        documents = docs_core.summary(conn)
        if documents:
            result["documents"] = documents
        from drspec.core.ide.mcp_registration import registration_status

        registration = registration_status(db_path.parent.parent if db_path.parent.name == "_drspec" else Path.cwd())
        if registration:
            result["mcp_server"] = registration
        from drspec.core.storage_report import database_report

        storage = database_report(conn)
        if storage:
            result["database"] = storage
    finally:
        conn.close()

    return result, False






def _tool_contract_get(db_path: Path, arguments: dict[str, Any]) -> tuple[Any, bool]:
    function_id = arguments.get("function_id")
    if not function_id:
        return "Missing required argument: function_id", True

    try:
        conn = _connect_readonly(db_path)
    except (duckdb.Error, WorkerIPCError) as e:
        return _db_unavailable_message(e), True

    function_id, failure = _resolve_id(conn, function_id)
    if failure:
        return failure, True

    try:
        contract_row = get_contract(conn, function_id)
        if contract_row is None:
            similar = _contract_similar_functions(conn, function_id)
            message = f"No contract exists for function '{function_id}'."
            if similar:
                message += f" Did you mean one of: {', '.join(similar)}?"
            return message, True

        artifact = get_artifact(conn, function_id)
        contract_data = json.loads(contract_row["contract_json"])
        raw_confidence = contract_row["confidence_score"]
        base_confidence = int(raw_confidence) if raw_confidence > 1.0 else int(raw_confidence * 100)
        findings = get_vision_findings(conn, function_id)
        adjusted_confidence = calculate_confidence_with_findings(base_confidence, findings)

        result = {
            "function_id": function_id,
            "contract": contract_data,
            "confidence": {
                "base": base_confidence,
                "adjusted": adjusted_confidence,
                "active_findings": sum(1 for f in findings if f.status == "NEW"),
            },
            "status": artifact.status if artifact else "UNKNOWN",
            "freshness": contract_freshness(conn, function_id, contract_row),
            "check_coverage": _check_coverage(contract_data),
        }
        carried = carry_over_record(conn, function_id)
        if carried is not None:
            result["carried_over"] = carried
    finally:
        conn.close()

    return result, False


def _tool_contract_list(db_path: Path, arguments: dict[str, Any]) -> tuple[Any, bool]:
    status = arguments.get("status")
    if status is not None:
        status = status.upper()
        if status not in VALID_ARTIFACT_STATUSES:
            return f"Invalid status: {status!r}. Must be one of {sorted(VALID_ARTIFACT_STATUSES)}.", True
    path_prefix = arguments.get("path_prefix")
    min_confidence = int(arguments.get("min_confidence", 0))
    limit = int(arguments.get("limit", 50))
    offset = int(arguments.get("offset", 0))

    try:
        conn = _connect_readonly(db_path)
    except (duckdb.Error, WorkerIPCError) as e:
        return _db_unavailable_message(e), True

    try:
        conditions = ["c.confidence_score >= ?"]
        params: list = [min_confidence / 100.0]
        if status:
            conditions.append("a.status = ?")
            params.append(status)
        if path_prefix:
            conditions.append("c.function_id LIKE ?")
            params.append(f"{path_prefix}%")
        where_clause = " AND ".join(conditions)

        total = conn.execute(
            f"SELECT COUNT(*) FROM contracts c JOIN artifacts a ON c.function_id = a.function_id WHERE {where_clause}",
            params,
        ).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT c.function_id, c.confidence_score, a.status, c.updated_at, a.file_path
            FROM contracts c JOIN artifacts a ON c.function_id = a.function_id
            WHERE {where_clause}
            ORDER BY c.updated_at DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
    finally:
        conn.close()

    result = {
        "total": total,
        "returned": len(rows),
        "contracts": [
            {
                "function_id": row[0],
                "file_path": row[4],
                "confidence": int(row[1]) if row[1] > 1.0 else int(row[1] * 100),
                "status": row[2],
                "updated_at": row[3].isoformat() if row[3] else None,
            }
            for row in rows
        ],
    }
    return result, False


def _tool_source_get(db_path: Path, arguments: dict[str, Any]) -> tuple[Any, bool]:
    function_id = arguments.get("function_id")
    if not function_id:
        return "Missing required argument: function_id", True

    try:
        conn = _connect_readonly(db_path)
    except (duckdb.Error, WorkerIPCError) as e:
        return _db_unavailable_message(e), True

    target = docs_core.document_target(conn, function_id)
    if target is not None:  # a document section or a document: its text
        try:
            return docs_core.section_source(conn, *target), False
        finally:
            conn.close()

    function_id, failure = _resolve_id(conn, function_id)
    if failure:
        return failure, True

    try:
        artifact = get_artifact(conn, function_id)
        if artifact is None:
            similar = _contract_similar_functions(conn, function_id)
            message = f"No function exists with ID '{function_id}'."
            message, _details = not_found_with_reason(conn, message, function_id, {})
            if similar:
                message += f" Did you mean one of: {', '.join(similar)}?"
            return message, True

        result = {
            "function_id": artifact.function_id,
            "file_path": artifact.file_path,
            "function_name": artifact.function_name,
            "language": artifact.language,
            "start_line": artifact.start_line,
            "end_line": artifact.end_line,
            "signature": artifact.signature,
            "body": artifact.body,
            "hints": extract_hints_simple(artifact.body, artifact.start_line),
            "possibly_stale": artifact.file_path in stale_paths_checked(conn, [artifact.file_path]),
        }
        usage.record_usage(conn, usage.nodes([artifact.function_id]), "usage.lookup_weight")
        if artifact.function_id in partial_function_ids(conn, [artifact.file_path]):
            result["partial"] = True  # kept although its body was read in part
        headers = declared_in(conn, artifact.function_id)
        if headers:
            result["declared_in"] = headers
        if is_declaration_only(conn, artifact.function_id):
            result["declaration_only"] = True
        documented = docs_core.documented_in(conn, artifact.function_id)
        if documented:
            result["documented_in"] = documented
        add_index_notice(conn, result)
    finally:
        conn.close()

    return result, False


def _tool_verify_script(db_path: Path, arguments: dict[str, Any]) -> tuple[Any, bool]:
    function_id = arguments.get("function_id")
    if not function_id:
        return "Missing required argument: function_id", True

    try:
        conn = _connect_readonly(db_path)
    except (duckdb.Error, WorkerIPCError) as e:
        return _db_unavailable_message(e), True

    function_id, failure = _resolve_id(conn, function_id)
    if failure:
        return failure, True

    try:
        contract_data = get_contract(conn, function_id)
        if contract_data is None:
            return f"No contract exists for function '{function_id}'", True

        contract = Contract.from_json(contract_data["contract_json"])
        script_content = contract_data.get("verification_script")
        if not script_content or (
            "Generated by DrSpec" in script_content and "DRSPEC_VERIFIER_VERSION = 2" not in script_content
        ):
            script_content = generate_verification_script(contract, function_id)
    finally:
        conn.close()

    return {"function_id": function_id, "script": script_content}, False


def _tool_learn_history(db_path: Path, arguments: dict[str, Any]) -> tuple[Any, bool]:
    function_id = arguments.get("function_id")
    limit = int(arguments.get("limit", 20))

    # Not `_connect_readonly` — `init_learning_schema` below runs DDL,
    # which a read-only connection rejects outright.
    try:
        conn = get_worker_connection(db_path)
    except (duckdb.Error, WorkerIPCError) as e:
        return _db_unavailable_message(e), True

    if function_id:
        function_id, failure = _resolve_id(conn, function_id)
        if failure:
            return failure, True

    try:
        init_learning_schema(conn)
        events = get_learning_history(conn, function_id=function_id, limit=limit)
    finally:
        conn.close()

    result = {"count": len(events), "events": [e.to_dict() for e in events]}
    return result, False


def _tool_learn_stats(db_path: Path, arguments: dict[str, Any]) -> tuple[Any, bool]:
    del arguments
    # Not `_connect_readonly` — `get_learning_stats` calls
    # `init_learning_schema` internally, which runs DDL.
    try:
        conn = get_worker_connection(db_path)
    except (duckdb.Error, WorkerIPCError) as e:
        return _db_unavailable_message(e), True

    try:
        stats = get_learning_stats(conn)
    finally:
        conn.close()

    return stats, False


def _tool_vision_list(db_path: Path, arguments: dict[str, Any]) -> tuple[Any, bool]:
    function_id = arguments.get("function_id")
    status = arguments.get("status")
    significance = arguments.get("significance")
    limit = int(arguments.get("limit", 20))

    try:
        conn = _connect_readonly(db_path)
    except (duckdb.Error, WorkerIPCError) as e:
        return _db_unavailable_message(e), True

    if function_id:
        function_id, failure = _resolve_id(conn, function_id)
        if failure:
            return failure, True

    try:
        if function_id:
            findings = get_vision_findings(conn, function_id, status=status, significance=significance)
        else:
            findings = get_all_vision_findings(conn, status=status, significance=significance, limit=limit)
        findings = findings[:limit]
    finally:
        conn.close()

    result = {
        "total": len(findings),
        "findings": [
            {
                "id": f.id,
                "function_id": f.function_id,
                "finding_type": f.finding_type,
                "significance": f.significance,
                "description": f.description,
                "status": f.status,
                "created_at": f.created_at.isoformat() if f.created_at else None,
            }
            for f in findings
        ],
    }
    return result, False


#: How long the scan tool waits for the worker before answering with what is done so far.
SCAN_WAIT_SECONDS = 60.0


def _tool_scan(db_path: Path, arguments: dict[str, Any]) -> tuple[Any, bool]:
    from drspec.cli.commands.scan import resolve_targets
    from drspec.core.scan_service import new_scan_id, run_scan

    raw_targets = arguments.get("targets") or ([arguments["path"]] if arguments.get("path") else [])
    if isinstance(raw_targets, str):
        raw_targets = [raw_targets]
    targets, missing = resolve_targets([str(target) for target in raw_targets])
    if missing is not None:
        return f"Path does not exist: {missing}", True
    if not db_path.parent.exists() or not db_path.exists():
        return "DrSpec not initialized. Run the init tool (or 'drspec init') first.", True
    if arguments.get("code_only") and arguments.get("docs_only"):
        return "code_only and docs_only exclude each other.", True

    full = bool(arguments.get("full") or arguments.get("force"))
    wait = not bool(arguments.get("no_wait", False))
    spec = {
        "scan_id": new_scan_id(),
        "targets": targets,
        "recursive": bool(arguments.get("recursive", True)),
        "full": full,
        "deep": bool(arguments.get("deep_scan", False)),
        "embed": bool(arguments.get("embed", False)),
        "code_only": bool(arguments.get("code_only", False)),
        "docs_only": bool(arguments.get("docs_only", False)),
        "wait": wait,
    }
    try:
        worker_conn = get_worker_connection(db_path)
    except WorkerIPCError as e:
        return _db_unavailable_message(e), True
    try:
        summary = run_scan(worker_conn, spec, max_wait=SCAN_WAIT_SECONDS)
    finally:
        worker_conn.close()
    if summary.get("publish_error"):
        return f"Error during scan: {summary['publish_error']}", True
    result = {"path": targets[0] if len(targets) == 1 else (" ".join(targets) if targets else str(Path.cwd())), **summary}
    return result, False


def _begin_indexing(result: dict[str, Any], db_path: Path, arguments: dict[str, Any]) -> tuple[Any, bool]:
    """What the init tool adds to a setup that worked: the worker is started, and with `scan` the whole project is scanned
    (the same call as the scan tool, waiting up to `SCAN_WAIT_SECONDS`)."""
    from drspec.core.indexing import scan_message, scan_whole_project, start_indexing

    if arguments.get("no_index") and arguments.get("scan"):
        return "scan needs the worker; it cannot be combined with no_index.", True
    if arguments.get("no_index"):
        return result, False
    result["indexing"] = start_indexing(db_path)
    if arguments.get("scan"):
        try:
            summary = scan_whole_project(db_path, wait=not bool(arguments.get("no_wait", False)), max_wait=SCAN_WAIT_SECONDS)
        except WorkerIPCError as e:
            result["scan"] = {"state": "failed", "error": str(e)}
            return result, False
        result["scan"] = summary
        result["indexing"] = {"started": True, "message": scan_message(summary)}
    return result, False


def _tool_init(db_path: Path, arguments: dict[str, Any]) -> tuple[Any, bool]:
    del db_path  # this tool resolves its own path — init precedes any configured db_path

    project_root_arg = arguments.get("project_root")
    project_root = Path(project_root_arg).expanduser().resolve() if project_root_arg else detect_project_root(Path.cwd())
    force = bool(arguments.get("force", False))
    confidence_threshold = int(arguments.get("confidence_threshold", DEFAULT_CONFIDENCE_THRESHOLD))

    drspec_folder = project_root / DRSPEC_FOLDER
    target_db_path = drspec_folder / DB_NAME
    agents_folder = drspec_folder / AGENTS_FOLDER

    is_reinit = drspec_folder.exists() and target_db_path.exists() and is_initialized_database(target_db_path)
    if is_reinit:
        # Same as the CLI: an initialized project's database is never rebuilt.
        # `force` only refreshes the agent templates.
        existing_agents = len(list(agents_folder.glob("*.md"))) if agents_folder.exists() else 0
        if existing_agents > 0 and not force:
            return _begin_indexing({
                "message": "DrSpec already initialized (no changes made)",
                "drspec_folder": str(drspec_folder),
                "database": str(target_db_path),
                "already_initialized": True,
                "overwrite_skipped": True,
            }, target_db_path, arguments)
        try:
            copied_templates = copy_agent_templates(get_templates_path(), agents_folder)
        except Exception as e:
            return f"Initialization failed: {e}", True
        return _begin_indexing({
            "message": "DrSpec already initialized",
            "drspec_folder": str(drspec_folder),
            "database": str(target_db_path),
            "agents_folder": str(agents_folder),
            "agents_updated": copied_templates,
            "already_initialized": True,
        }, target_db_path, arguments)

    try:
        drspec_folder.mkdir(parents=True, exist_ok=True)
        ensure_db_directory(target_db_path)
        conn = get_connection(target_db_path)
        try:
            init_schema(conn, rebuild=force)
            set_confidence_threshold(conn, confidence_threshold)
        finally:
            conn.close()

        copied_templates = copy_agent_templates(get_templates_path(), agents_folder)
        gitignore_updated = update_gitignore(project_root)
    except Exception as e:
        return f"Initialization failed: {e}", True

    return _begin_indexing({
        "message": "DrSpec initialized successfully",
        "drspec_folder": str(drspec_folder),
        "database": str(target_db_path),
        "agents_folder": str(agents_folder),
        "templates_copied": copied_templates,
        "gitignore_updated": gitignore_updated,
        "confidence_threshold": confidence_threshold,
        "already_initialized": False,
    }, target_db_path, arguments)


def _tool_deps_plot(db_path: Path, arguments: dict[str, Any]) -> tuple[Any, bool]:
    function_id = arguments.get("function_id")
    if not function_id:
        return "Missing required argument: function_id", True

    direction = arguments.get("direction", "both")
    if direction not in _VALID_DIRECTIONS:
        return f"Invalid direction: {direction!r}. Must be one of {_VALID_DIRECTIONS}.", True
    depth = max(1, min(5, int(arguments.get("depth", 2))))
    html_output = bool(arguments.get("html", False))
    output_path = arguments.get("output_path")

    try:
        conn = _connect_readonly(db_path)
    except (duckdb.Error, WorkerIPCError) as e:
        return _db_unavailable_message(e), True

    function_id, failure = _resolve_id(conn, function_id)
    if failure:
        return failure, True

    try:
        artifact = get_artifact(conn, function_id)
        if artifact is None:
            similar = _contract_similar_functions(conn, function_id)
            message = f"Function not found: {function_id}."
            if similar:
                message += f" Did you mean one of: {', '.join(similar)}?"
            return message, True

        from drspec.visualization.grapher import generate_dependency_graph, generate_dependency_graph_html

        generate_fn = generate_dependency_graph_html if html_output else generate_dependency_graph
        plot_result = generate_fn(conn=conn, function_id=function_id, depth=depth, direction=direction, output_file=output_path)
    finally:
        conn.close()

    return {
        "function_id": function_id,
        "output_path": plot_result.path,
        "html": html_output,
        "nodes": plot_result.nodes,
        "edges": plot_result.edges,
        "depth": depth,
        "center_function": artifact.function_name,
        "width": plot_result.width,
        "height": plot_result.height,
    }, False






def _tool_contract_save(db_path: Path, arguments: dict[str, Any]) -> tuple[Any, bool]:
    function_id = arguments.get("function_id")
    contract_json_str = arguments.get("contract_json")
    confidence = arguments.get("confidence")
    if not function_id or not contract_json_str or confidence is None:
        return "Missing required argument: function_id, contract_json, and confidence are all required.", True

    validation_result = validate_contract(contract_json_str)
    if not validation_result.success:
        details = validation_result.error.details if validation_result.error else {}
        return f"Contract validation failed: {json.dumps(details)}", True

    try:
        conn = get_worker_connection(db_path)
    except (duckdb.Error, WorkerIPCError) as e:
        return _db_unavailable_message(e), True

    function_id, failure = _resolve_id(conn, function_id)
    if failure:
        return failure, True

    try:
        artifact = get_artifact(conn, function_id)
        if artifact is None:
            return f"No artifact exists for function '{function_id}'. Run a scan first.", True

        confidence_score = int(confidence) / 100.0
        new_status = "NEEDS_REVIEW"
        update_artifact_status(conn, function_id, new_status)
        insert_contract(conn, function_id, contract_json_str, confidence_score)

        trace = arguments.get("trace")
        if trace:
            agent = arguments.get("agent", "judge")
            try:
                json.loads(trace)
                insert_reasoning_trace(conn, function_id, agent, trace)
            except json.JSONDecodeError:
                insert_reasoning_trace(conn, function_id, agent, json.dumps({"note": trace}))
    finally:
        conn.close()

    return {
        "function_id": function_id, "confidence_score": int(confidence),
        "status": new_status, "message": "Contract saved successfully",
    }, False


def _tool_verify_run(db_path: Path, arguments: dict[str, Any]) -> tuple[Any, bool]:
    function_id = arguments.get("function_id")
    input_data = arguments.get("input_data")
    output_data = arguments.get("output_data")
    if not function_id or input_data is None or "output_data" not in arguments:
        return "Missing required argument: function_id, input_data, and output_data are all required.", True
    timeout = float(arguments.get("timeout", 1.0))

    try:
        conn = get_worker_connection(db_path)
    except (duckdb.Error, WorkerIPCError) as e:
        return _db_unavailable_message(e), True

    function_id, failure = _resolve_id(conn, function_id)
    if failure:
        return failure, True

    try:
        contract_row = get_contract(conn, function_id)
        if contract_row is None:
            return f"No contract exists for function '{function_id}'", True

        contract = Contract.from_json(contract_row["contract_json"])
        script = contract_row.get("verification_script")
        if not script or ("Generated by DrSpec" in script and "DRSPEC_VERIFIER_VERSION = 2" not in script):
            script = generate_verification_script(contract, function_id)

        result = execute_verification(script=script, input_data=input_data, output_data=output_data, timeout=timeout)

        import hashlib

        current_hash = get_artifact_code_hash(conn, function_id)
        freshness = contract_freshness(conn, function_id, contract_row)
        current = freshness["current"]
        evidence = dict(
            result.to_dict(), source_hash=current_hash,
            context_hash=freshness["current_context_hash"], contract_context_hash=freshness["contract_context_hash"],
            contract_source_hash=contract_row.get("source_hash"),
            contract_hash=hashlib.sha256(contract_row["contract_json"].encode()).hexdigest(),
            script_hash=hashlib.sha256(script.encode()).hexdigest(), current=current,
        )
        set_config(conn, "verification:" + function_id, json.dumps(evidence))
        fully_verified = current and result.status == "passed" and result.passed
        if fully_verified:
            update_artifact_status(conn, function_id, "VERIFIED")
        elif current:
            update_artifact_status(conn, function_id, "NEEDS_REVIEW")
    finally:
        conn.close()

    if result.error:
        return f"Verification script execution failed: {result.error} — {result.message}", True

    return {
        "function_id": function_id, "passed": fully_verified, "status": result.status,
        "current": current, "message": result.message,
        "execution_time_ms": int(result.execution_time * 1000),
        "invariants": {
            "checked": result.invariants_checked, "passed": result.invariants_passed,
            "failed": result.invariants_failed, "skipped": result.invariants_skipped,
        },
    }, False


def _tool_learn_analyze(db_path: Path, arguments: dict[str, Any]) -> tuple[Any, bool]:
    commit_range = arguments.get("commit_range")
    if not commit_range or ".." not in commit_range:
        return "Missing/invalid required argument: commit_range, format 'start..end'.", True
    start_ref, end_ref = commit_range.split("..", 1)
    repo = arguments.get("repo_path") or "."
    dry_run = bool(arguments.get("dry_run", False))
    bug_fixes_only = bool(arguments.get("bug_fixes_only", True))

    try:
        subprocess.run(["git", "-C", repo, "rev-parse", "--git-dir"], capture_output=True, check=True)
    except subprocess.CalledProcessError:
        return f"Not a git repository: {repo}", True

    analyses = analyze_commit_range(start_ref=start_ref, end_ref=end_ref, repo_path=repo, bug_fixes_only=bug_fixes_only)
    if not analyses:
        return {"commits_analyzed": 0, "patterns_found": 0, "suggestions": 0, "results": []}, False

    try:
        conn = get_worker_connection(db_path) if db_path.exists() else None
    except WorkerIPCError:
        conn = None

    total_patterns = 0
    total_suggestions = 0
    results = []
    try:
        for analysis in analyses:
            commit = analysis.commit
            patterns = extract_all_patterns(commit.files, analysis.modified_functions)
            total_patterns += len(patterns)
            for func_id, file_path, func_name in analysis.modified_functions:
                func_patterns = [p for p in patterns if p.file_path == file_path and (
                    p.function_name == func_name or (p.function_name and func_name in p.function_name)
                )] or [p for p in patterns if p.file_path == file_path]

                existing_contract = None
                if conn:
                    contract_row = get_contract(conn, func_id)
                    if contract_row:
                        try:
                            existing_contract = Contract.from_json(contract_row["contract_json"])
                        except Exception:
                            pass

                strengthening = strengthen_contract(function_id=func_id, patterns=func_patterns, existing_contract=existing_contract)
                if strengthening.new_invariants:
                    total_suggestions += len(strengthening.new_invariants)

                if conn and not dry_run and func_patterns:
                    for pattern in func_patterns:
                        event = LearningEvent(
                            commit_sha=commit.commit_sha, commit_message=commit.message, function_id=func_id,
                            pattern_type=pattern.pattern_type, pattern_description=pattern.description,
                            contract_modified=False, confidence_boost=strengthening.confidence_boost,
                            new_invariants_added=0, invariants_validated=len(strengthening.validated_invariants),
                        )
                        insert_learning_event(conn, event)

                results.append({
                    "commit": commit.short_sha,
                    "commit_message": commit.message.split("\n")[0],
                    **strengthening.to_dict(),
                })
    finally:
        if conn is not None:
            conn.close()

    return {
        "commits_analyzed": len(analyses), "patterns_found": total_patterns,
        "suggestions": total_suggestions, "results": results, "dry_run": dry_run,
    }, False


def _tool_vision_save(db_path: Path, arguments: dict[str, Any]) -> tuple[Any, bool]:
    function_id = arguments.get("function_id")
    finding_type = arguments.get("finding_type")
    significance = arguments.get("significance")
    description = arguments.get("description")
    if not function_id or not finding_type or not significance or not description:
        return "Missing required argument: function_id, finding_type, significance, and description are all required.", True

    try:
        conn = get_worker_connection(db_path)
    except (duckdb.Error, WorkerIPCError) as e:
        return _db_unavailable_message(e), True

    function_id, failure = _resolve_id(conn, function_id)
    if failure:
        return failure, True

    try:
        finding_id = insert_vision_finding(
            conn=conn, function_id=function_id, finding_type=finding_type, significance=significance.upper(),
            description=description, location=arguments.get("location"),
            invariant_implication=arguments.get("invariant"), plot_path=arguments.get("plot_path"),
        )
    except ValueError as e:
        return str(e), True
    finally:
        conn.close()

    return {
        "finding_id": finding_id, "function_id": function_id,
        "finding_type": finding_type, "significance": significance.upper(), "status": "NEW",
    }, False


def _tool_vision_update(db_path: Path, arguments: dict[str, Any]) -> tuple[Any, bool]:
    finding_id = arguments.get("finding_id")
    status = arguments.get("status")
    if finding_id is None or not status:
        return "Missing required argument: finding_id and status are both required.", True

    try:
        conn = get_worker_connection(db_path)
    except (duckdb.Error, WorkerIPCError) as e:
        return _db_unavailable_message(e), True

    try:
        updated = update_vision_finding_status(
            conn=conn, finding_id=int(finding_id), status=status.upper(), resolution_note=arguments.get("note"),
        )
    except ValueError as e:
        return str(e), True
    finally:
        conn.close()

    if not updated:
        return f"Finding {finding_id} not found", True

    return {"finding_id": finding_id, "status": status.upper(), "resolution_note": arguments.get("note")}, False


def _tool_worker_stop(db_path: Path, arguments: dict[str, Any]) -> tuple[Any, bool]:
    del arguments
    try:
        pid, exited = stop_worker(db_path)
    except OSError as e:
        return f"Could not stop worker: {e}", True

    if pid is None:
        return {"stopped": False, "message": "No worker is running."}, False
    if not exited:
        return f"Worker (pid {pid}) was signaled but is still running.", True
    return {"stopped": True, "pid": pid}, False


def _tool_config_get(db_path: Path, arguments: dict[str, Any]) -> tuple[Any, bool]:
    key = arguments.get("key")
    try:
        conn = get_worker_connection(db_path)
    except (duckdb.Error, WorkerIPCError) as e:
        return _db_unavailable_message(e), True
    try:
        if key is None:
            return {"settings": list_settings(conn)}, False
        try:
            value = get_setting(conn, key)
        except SettingError as e:
            return str(e), True
        return setting_record(key, value), False
    finally:
        conn.close()


def _tool_config_set(db_path: Path, arguments: dict[str, Any]) -> tuple[Any, bool]:
    key = arguments.get("key")
    if not key:
        return "Missing required argument: key", True
    reset = bool(arguments.get("reset", False))
    if not reset and "value" not in arguments:
        return "Missing required argument: value (or pass reset=true)", True
    try:
        conn = get_worker_connection(db_path)
    except (duckdb.Error, WorkerIPCError) as e:
        return _db_unavailable_message(e), True
    try:
        try:
            value = reset_setting(conn, key) if reset else set_setting(conn, key, arguments["value"])
        except SettingError as e:
            return str(e), True
        return setting_record(key, value), False
    finally:
        conn.close()


def _tool_worker_status(db_path: Path, arguments: dict[str, Any]) -> tuple[Any, bool]:
    del arguments
    return get_worker_status(db_path), False


def _tool_trace_summary(db_path: Path, arguments: dict[str, Any]) -> tuple[Any, bool]:
    try:
        slowest = int(arguments.get("slowest", 20))
    except (TypeError, ValueError):
        return "slowest must be a whole number", True
    try:
        return trace_summary(Path(db_path), since=arguments.get("since"), slowest=slowest, cid=arguments.get("cid")), False
    except TraceError as e:
        return str(e), True


def _tool_ack(db_path: Path, arguments: dict[str, Any]) -> tuple[Any, bool]:
    identifier = arguments.get("identifier")
    if not identifier:
        return "Missing required argument: identifier", True
    try:
        conn = get_worker_connection(db_path)
    except WorkerIPCError as e:
        return _db_unavailable_message(e), True
    try:
        target = docs_core.document_target(conn, identifier)
        if target is None or target[0] != "section":
            return f"No document section matches '{identifier}'. ack takes a section ID, as search prints it.", True
        function = None
        if arguments.get("function_id"):
            function, failure = _resolve_id(conn, arguments["function_id"])
            if failure:
                return failure, True
        return {"section_id": target[1], "acknowledged": docs_core.acknowledge(conn, target[1], function)}, False
    finally:
        conn.close()


_TOOL_HANDLERS: dict[str, Callable[[Path, dict[str, Any]], tuple[Any, bool]]] = {
    "ack": _tool_ack,
    "search": _tool_search,
    "deps_get": _tool_deps_get,
    "refs_get": _tool_refs_get,
    "strings_get": _tool_strings_get,
    "unused": _tool_unused,
    "status": _tool_status,
    "contract_get": _tool_contract_get,
    "contract_list": _tool_contract_list,
    "source_get": _tool_source_get,
    "verify_script": _tool_verify_script,
    "learn_history": _tool_learn_history,
    "learn_stats": _tool_learn_stats,
    "vision_list": _tool_vision_list,
    "scan": _tool_scan,
    "init": _tool_init,
    "deps_plot": _tool_deps_plot,
    "contract_save": _tool_contract_save,
    "verify_run": _tool_verify_run,
    "learn_analyze": _tool_learn_analyze,
    "vision_save": _tool_vision_save,
    "vision_update": _tool_vision_update,
    "worker_stop": _tool_worker_stop,
    "worker_status": _tool_worker_status,
    "trace_summary": _tool_trace_summary,
    "config_get": _tool_config_get,
    "config_set": _tool_config_set,
}


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str, data: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": err}


UPGRADE_NOTICE = "[drspec was upgraded after this MCP server started, so it still runs the old code. Restart the client (or this server) to get the new one.]"
_upgrade_notice_given = False
_started_build: Optional[str] = None


def _take_upgrade_notice() -> Optional[str]:
    """The notice, once: the first time the installed code is not the code this server started with. An MCP server keeps the
    code it started with, unlike the worker, which replaces itself, so only a restart brings the new code."""
    global _upgrade_notice_given, _started_build
    from drspec.core.build_id import build_id, installed_build_id

    if _started_build is None:
        _started_build = build_id()
    if _upgrade_notice_given:
        return None
    try:
        changed = installed_build_id() != _started_build
    except Exception:  # noqa: BLE001 - a notice must never fail a call
        return None
    if not changed:
        return None
    _upgrade_notice_given = True
    return UPGRADE_NOTICE


def _with_upgrade_notice(text: str) -> str:
    """The first tool result (in text) after the installed code changed carries a one-line notice in front of it. A result in
    `format: "json"` does not: it is a document, and the notice goes in a block of its own (`_tool_call_result`)."""
    notice = _take_upgrade_notice()
    return f"{notice}\n\n{text}" if notice else text


def _tool_call_result(text: str, is_error: bool, notice: Optional[str] = None) -> dict[str, Any]:
    content = [{"type": "text", "text": text}]
    if notice:
        content.append({"type": "text", "text": notice})  # (a second block: the first stays exactly the document)
    return {"content": content, "isError": is_error}


def _format_of(tool_name: str, fmt: Optional[str]) -> Optional[str]:
    """The format a tool's result is rendered in: what was asked, except that the text of `source_get` is code an assistant may copy
    into an edit, so it is never compacted."""
    return None if fmt == "compact" and tool_name == "source_get" else fmt


def _render_tool_result(payload: Any, fmt: Optional[str]) -> str:
    """Turn a `_tool_*` handler's bare-dict payload into response text,
    centrally, instead of inside each of the 24 handlers.

    `format: "json"` keeps the old always-JSON behavior; anything else
    (including omitted) renders the same plain text the CLI prints, via
    `cli/output.py`'s renderer — wrapped in `success_response` first
    since that renderer expects `{"success", "data", "error"}`, not a
    bare dict.
    """
    if fmt == "json":
        return json.dumps(payload)
    text = render_text(success_response(payload))
    if fmt == "compact":
        compact = compact_format.encode(payload)
        return compact if len(compact) <= len(text) else text  # (a very small result is shorter as text: the marker and the quoting cost more)
    return text


def _trace_tool_call(tool_name: str, arguments: dict[str, Any], text: str, is_error: bool, started: float) -> None:
    """One `command` event per tool call (argument names only; a search
    pattern, cut short, only at level `all`)."""
    totals = tracing.take_totals()
    if not tracing.enabled():
        return
    try:
        fields: dict[str, Any] = {
            "command": tool_name,
            "args": sorted(arguments),
            "status": "error" if is_error else "ok",
            "out_bytes": len(text.encode("utf-8")),
            "connect_ms": round(totals.get("connect_ms", 0.0), 3),
            "worker_started": bool(totals.get("worker_starts")),
            "ipc_ms": round(totals.get("ipc_ms", 0.0), 3),
            "ipc_calls": int(totals.get("ipc_calls", 0)),
        }
        pattern = arguments.get("pattern")
        if tool_name == "search" and tracing.level() == "all" and isinstance(pattern, str):
            fields["pattern_length"] = len(pattern)
            fields["pattern_mode"] = "semantic" if arguments.get("semantic") else "regex" if arguments.get("regex") else "literal"
        tracing.emit("command", (time.perf_counter() - started) * 1000.0, cid=tracing.current_cid(), **fields, **tracing.process_usage())
    except Exception:  # noqa: BLE001 - tracing must never affect a tool call
        pass
    finally:
        tracing.set_cid(None)


def handle_message(message: dict[str, Any], db_path: Path) -> Optional[dict[str, Any]]:
    """Handle one parsed JSON-RPC message. Returns the response to write
    back, or None for a notification (which gets no response at all —
    per spec, the server MUST NOT write anything to stdout that isn't a
    valid MCP message, and a response to a notification would be one).
    """
    method = message.get("method")
    request_id = message.get("id")
    is_notification = "id" not in message

    if method == "initialize":
        params = message.get("params") or {}
        client_protocol_version = params.get("protocolVersion") or PROTOCOL_VERSION_FALLBACK
        # Echoes back whatever protocolVersion the client asked for —
        # nothing here is revision-specific, and pinning one hardcoded
        # date risks rejecting a client whose own supported range
        # doesn't include it.
        return _result(
            request_id,
            {
                "protocolVersion": client_protocol_version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
                "instructions": contract_guidance.mcp_instructions(),
            },
        )

    if method == "notifications/initialized":
        return None

    if method == "ping":
        return _result(request_id, {})

    if method == "tools/list":
        return _result(request_id, {"tools": _TOOLS})

    if method == "tools/call":
        params = message.get("params") or {}
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        handler = _TOOL_HANDLERS.get(tool_name)
        if handler is None:
            return _error(request_id, -32602, f"Unknown tool: {tool_name}")
        started = time.perf_counter()
        tracing.set_cid(tracing.new_cid())
        try:
            payload, is_error = handler(db_path, arguments)
        except Exception as e:  # noqa: BLE001 - a bad call becomes a tool error, not a dead server
            payload, is_error = f"Internal error running tool {tool_name!r}: {e}", True
        # Error payloads are already a plain string — left as-is.
        # Success payloads are a bare dict, rendered per the `format`
        # argument (default: CLI-style plain text; "json": raw JSON).
        if is_error:
            text = payload
        elif tool_name == "status" and arguments.get("format") != "json" and not arguments.get("details") and not arguments.get("partial"):
            from drspec.core.status_text import short_text  # the default status text: the counts, one line per block

            text = short_text(payload)
        elif tool_name == "scan" and arguments.get("format") != "json" and not arguments.get("verbose"):
            from drspec.core.scan_text import short_text  # the default scan text: a line for the result, one for each thing that needs attention

            text = short_text(payload)
        else:
            text = _render_tool_result(payload, _format_of(tool_name, arguments.get("format")))
        notice = None
        if arguments.get("format") == "json":
            notice = _take_upgrade_notice()  # a JSON result stays a JSON document; the notice is a second content block
        else:
            text = _with_upgrade_notice(text)
        _trace_tool_call(tool_name, arguments, text, is_error, started)
        return _result(request_id, _tool_call_result(text, is_error, notice))

    if is_notification:
        return None  # unrecognized notification — ignore, per the spec's liberal posture on these

    return _error(request_id, -32601, f"Method not found: {method}")


def run(db_path: Path, in_stream: TextIO = sys.stdin, out_stream: TextIO = sys.stdout) -> None:
    """Blocking stdio loop: one newline-delimited JSON-RPC message per
    line in, one response per line out. Exits on EOF (stdin closed) —
    the spec's documented shutdown signal; neither stream is closed
    here, since the client owns that lifecycle.
    """
    tracing.set_proc("mcp")
    tracing.bind(db_path)
    global _started_build
    from drspec.core.build_id import build_id

    _started_build = build_id()  # the code this server runs, so a later reinstall can be told from it
    for line in in_stream:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            out_stream.write(json.dumps(_error(None, -32700, "Parse error")) + "\n")
            out_stream.flush()
            continue

        response = handle_message(message, db_path)
        if response is not None:
            out_stream.write(json.dumps(response) + "\n")
            out_stream.flush()
