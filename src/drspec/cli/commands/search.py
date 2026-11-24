"""Search command - grep-flag-compatible search over function name/
signature/body, plus semantic similarity.

Registered directly on the main app, not as a nested Typer sub-app: a
nested sub-app is a Click Group, which stops accepting its own options
after the first positional argument.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from drspec.cli.output import ErrorCode, error_response, output, success_response
from drspec.core.embedder import embed_text
from drspec.core.embedding_backlog import semantic_coverage
from drspec.core import docs as docs_core
from drspec.core.changed_notice import add_changed_notice, take_before
from drspec.core.index_notice import add_index_notice
from drspec.core.partial import add_not_indexed, partial_function_ids, skipped_matching_pattern
from drspec.core.live_search import InvalidPattern, known_languages, live_search, search_scope
from drspec.core import usage
from drspec.core.staleness import stale_paths_checked
from drspec.db import (
    any_artifact_has_embedding,
    count_search_artifacts,
    list_search_file_paths,
    search_artifacts,
    search_artifacts_semantic,
)
from drspec.db.queries import VALID_ARTIFACT_STATUSES, matched_only_by_qualified_name
from drspec.worker_ipc import WorkerIPCError, get_worker_connection

_VALID_CONFIDENCE_TIERS = frozenset({"EXTRACTED", "INFERRED", "AMBIGUOUS"})

DEFAULT_LIMIT = 50


def search_command(
    ctx: typer.Context,
    pattern: Optional[str] = typer.Argument(
        None,
        help="Pattern to search for — a literal substring by default, or a regex with -E. "
        "With --semantic, this is the natural-language/code query text instead. "
        "May be left out when --status is given, to list every function with that status.",
    ),
    ignore_case: bool = typer.Option(
        False, "--ignore-case", "-i", help="Case-insensitive match (grep's -i)."
    ),
    regex: bool = typer.Option(
        False, "--regex", "-E", help="Treat pattern as a regex, not a literal substring (grep's -E)."
    ),
    invert_match: bool = typer.Option(
        False, "--invert-match", "-v", help="Show non-matching functions instead (grep's -v)."
    ),
    count: bool = typer.Option(
        False, "--count", "-c", help="Print the total match count only (grep's -c)."
    ),
    files_with_matches: bool = typer.Option(
        False,
        "--files-with-matches",
        "-l",
        help="Print distinct file paths with at least one match only, no function detail (grep's -l).",
    ),
    include: Optional[str] = typer.Option(
        None, "--include", help="Filter by language (python, javascript, typescript, tsx, cpp) — grep's --include=*.py, by language instead of glob."
    ),
    path: Optional[str] = typer.Option(None, "--path", help="Filter by file path prefix."),
    docs_only: bool = typer.Option(
        False,
        "--docs-only",
        help="Only document sections (Markdown), by title, keyword and text. With --status STALE: the sections that describe code "
        "which changed since they were written, ranked.",
    ),
    code_only: bool = typer.Option(
        False,
        "--code-only",
        help="Only functions, as before documents were indexed (no 'kind' marker on the results).",
    ),
    undocumented: bool = typer.Option(
        False,
        "--undocumented",
        help="List the public functions that no document names by identifier, qualified name or path. The pattern, if given, narrows the list.",
    ),
    semantic: bool = typer.Option(
        False,
        "--semantic",
        help="Similarity search over the local embeddings instead of exact/regex text match.",
    ),
    confidence: Optional[str] = typer.Option(
        None,
        "--confidence",
        help="Only functions with at least one outgoing dependency edge at this confidence tier "
        "(EXTRACTED, INFERRED, or AMBIGUOUS) — e.g. find functions worth a --deep-scan review.",
    ),
    status: Optional[str] = typer.Option(None, "--status", help="Filter by contract status."),
    limit: int = typer.Option(DEFAULT_LIMIT, "--limit", help="Maximum results to return (matching lines with --lines/-A/-B/-C).", min=1),
    lines: bool = typer.Option(
        False,
        "--lines",
        help="Search the live contents of the indexed files instead of function bodies and "
        "list each match as file:line:text, labelled with its enclosing function. Reaches "
        "module-level lines (imports, constants) that no function body contains. Doesn't "
        "combine with -c/-l/--semantic/-v/--status/--confidence.",
    ),
    after_context: int = typer.Option(
        0, "--after-context", "-A", help="Print NUM lines of trailing context after each live match (grep's -A). Implies --lines.", min=0
    ),
    before_context: int = typer.Option(
        0, "--before-context", "-B", help="Print NUM lines of leading context before each live match (grep's -B). Implies --lines.", min=0
    ),
    context: int = typer.Option(
        0, "--context", "-C", help="Print NUM lines of context on both sides (grep's -C) — -A/-B override just their own side. Implies --lines.", min=0
    ),
) -> None:
    """Search functions and document sections, grep-flag-compatible.

    By default a pattern is looked for in function names/signatures/bodies and in the titles, keywords and text of
    document sections; each result says `Kind: function` or `Kind: section`. --code-only and --docs-only narrow it.
    All commands output labeled plain text by default; pass the top-level
    --json flag for machine-parseable output.

    Examples:
        drspec search --status PENDING            # functions that have no contract yet
        drspec search --status STALE --docs-only  # document sections whose code changed
        drspec search --undocumented              # functions no document names
        drspec search retry --docs-only           # only document sections
        drspec search "def process"               # literal substring
        drspec search -i "process"                 # case-insensitive
        drspec search -E "^_[a-z]+_handler$"        # regex
        drspec search -c "TODO"                     # count only
        drspec search -l "TODO"                     # matching files only
        drspec search --include python "parse"       # language filter
        drspec search --semantic "parses a config file"  # similarity search
        drspec search --lines "get_connection(" --path src/drspec/cli  # live line numbers
        drspec search -C 3 "get_connection(" --path src/drspec/cli/commands/serve.py  # with context
    """
    cli_ctx = ctx.obj or {}
    json_output = cli_ctx.get("json_output", False)
    db_path_override = cli_ctx.get("db_path")

    if count and files_with_matches:
        response = error_response(
            ErrorCode.INVALID_INPUT,
            "--count and --files-with-matches are mutually exclusive — pick one.",
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)

    if semantic and (ignore_case or regex or invert_match):
        response = error_response(
            ErrorCode.INVALID_INPUT,
            "--semantic has no literal/regex notion, so -i/-E/-v can't be combined with it.",
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)

    effective_before = before_context or context
    effective_after = after_context or context
    wants_context = effective_before > 0 or effective_after > 0

    wants_lines = lines or wants_context

    if code_only and docs_only:
        output(error_response(ErrorCode.INVALID_INPUT, "--code-only and --docs-only exclude each other."), json_output=json_output)
        raise typer.Exit(1)
    if docs_only and (semantic or count or files_with_matches or invert_match or confidence is not None or include or wants_lines or undocumented):
        output(
            error_response(ErrorCode.INVALID_INPUT, "--docs-only searches section text; it can't be combined with --semantic, -c, -l, -v, --confidence, --include, --lines or --undocumented."),
            json_output=json_output,
        )
        raise typer.Exit(1)
    if undocumented and (semantic or count or files_with_matches or invert_match or confidence is not None or wants_lines or status is not None):
        output(
            error_response(ErrorCode.INVALID_INPUT, "--undocumented lists functions; it can't be combined with --semantic, -c, -l, -v, --confidence, --lines or --status."),
            json_output=json_output,
        )
        raise typer.Exit(1)
    if docs_only and status is not None and status != "STALE":
        output(error_response(ErrorCode.INVALID_INPUT, "Document sections have one status, STALE: the sections whose code changed."), json_output=json_output)
        raise typer.Exit(1)

    if pattern is None:
        if (status is None and not undocumented) or semantic or wants_lines:
            response = error_response(
                ErrorCode.INVALID_INPUT,
                "A pattern is required. It can only be left out with --status or --undocumented, to list every function "
                "with that status or that no document names (not with --semantic or --lines).",
            )
            output(response, json_output=json_output)
            raise typer.Exit(1)
        pattern = ""  # an empty substring matches every function

    if wants_lines and (semantic or count or files_with_matches):
        response = error_response(
            ErrorCode.INVALID_INPUT,
            "--lines/-A/-B/-C only apply to the default match-listing mode, not --semantic/-c/-l.",
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)

    if wants_lines and (invert_match or status is not None or confidence is not None):
        response = error_response(
            ErrorCode.INVALID_INPUT,
            "--lines/-A/-B/-C search file contents, so -v/--status/--confidence (which filter "
            "functions) can't be combined with them.",
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)

    if include is not None and include not in known_languages():
        response = error_response(
            ErrorCode.INVALID_INPUT,
            f"Unknown language for --include: {include!r}. Known: {', '.join(sorted(known_languages()))}.",
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)

    if confidence is not None and confidence not in _VALID_CONFIDENCE_TIERS:
        response = error_response(
            ErrorCode.INVALID_INPUT,
            f"Invalid --confidence: {confidence}. Must be one of {sorted(_VALID_CONFIDENCE_TIERS)}.",
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)

    if status is not None and status not in VALID_ARTIFACT_STATUSES:
        response = error_response(
            ErrorCode.INVALID_INPUT,
            f"Invalid --status: {status}. Must be one of {sorted(VALID_ARTIFACT_STATUSES)}.",
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)

    drspec_dir = Path.cwd() / "_drspec"
    if not drspec_dir.exists():
        response = error_response(
            ErrorCode.DB_NOT_INITIALIZED,
            "DrSpec not initialized. Run 'drspec init' first.",
            {"expected_path": str(drspec_dir)},
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)

    db_path = db_path_override or (drspec_dir / "contracts.db")
    if not db_path.exists():
        response = error_response(
            ErrorCode.DB_NOT_INITIALIZED,
            "DrSpec not initialized. Run 'drspec init' first.",
            {"db_path": str(db_path)},
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)
    try:
        conn = get_worker_connection(db_path, read_only=True)
    except WorkerIPCError as e:
        response = error_response(
            ErrorCode.DATABASE_BUSY,
            f"Could not reach the drspec worker: {e}",
            {"db_path": str(db_path)},
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)

    documents_on = docs_core.enabled(conn)
    if docs_only:
        try:
            found = docs_core.section_matches(conn, pattern, limit, status, path)
        finally:
            conn.close()
        data = {"pattern": pattern, "semantic": False, "total_matches": len(found), "matches": found}
        if not documents_on:
            data["note"] = docs_core.OFF_NOTE
        output(success_response(data), json_output=json_output)
        return

    if undocumented:
        try:
            rows = docs_core.undocumented(conn, 100000)
            needle = pattern.lower()
            rows = [r for r in rows if needle in r["function_id"].lower() and (not path or r["file_path"].startswith(path))][:limit]
        finally:
            conn.close()
        data = {"pattern": pattern, "undocumented": True, "total_matches": len(rows), "matches": rows}
        if not documents_on:
            data["note"] = docs_core.OFF_NOTE
        output(success_response(data), json_output=json_output)
        return

    # Documents join the default listing only: the other modes (-c, -l, --semantic, --lines, -v, --confidence,
    # --include) are about functions.
    with_documents = documents_on and not (code_only or semantic or count or files_with_matches or wants_lines or invert_match or confidence is not None or include)
    before = take_before(conn)  # taken before the answer's reads (see changed_notice)

    try:
        if semantic:
            query_embedding = embed_text(pattern)
            matches = search_artifacts_semantic(
                conn,
                query_embedding,
                language=include,
                path_prefix=path,
                status=status,
                limit=limit,
            )
            stale_paths = stale_paths_checked(conn, [m.artifact.file_path for m in matches])
            usage.record_usage(conn, usage.nodes(m.artifact.function_id for m in matches))
            data = {
                "pattern": pattern,
                "semantic": True,
                **search_scope(conn, include, path),
                "total_matches": len(matches),
                "matches": [
                    {
                        "function_id": m.artifact.function_id,
                        "function_name": m.artifact.function_name,
                        "file_path": m.artifact.file_path,
                        "language": m.artifact.language,
                        "status": m.artifact.status,
                        "signature": m.artifact.signature,
                        "score": m.score,
                        "possibly_stale": m.artifact.file_path in stale_paths,
                    }
                    for m in matches
                ],
                **semantic_coverage(conn, include, path, status),  # how much it searched: what has no embedding cannot be found
            }
            # scan doesn't embed by default, so "no embeddings yet" is a
            # common state — distinguish it from a genuine zero-match result
            if not matches and not any_artifact_has_embedding(conn):
                data["message"] = (
                    "No functions have a local embedding yet — 'drspec scan' doesn't "
                    "compute embeddings by default. Run 'drspec scan --embed' first, "
                    "then retry this search."
                )
        elif wants_lines:
            data = add_index_notice(conn, live_search(
                conn,
                pattern,
                ignore_case=ignore_case,
                regex=regex,
                language=include,
                path_prefix=path,
                limit=limit,
                before=effective_before,
                after=effective_after,
            ))
        elif count:
            total = count_search_artifacts(
                conn,
                pattern,
                ignore_case=ignore_case,
                regex=regex,
                invert_match=invert_match,
                language=include,
                path_prefix=path,
                status=status,
                confidence=confidence,
            )
            data = {"pattern": pattern, "count": total, **search_scope(conn, include, path)}
        elif files_with_matches:
            file_paths = list_search_file_paths(
                conn,
                pattern,
                ignore_case=ignore_case,
                regex=regex,
                invert_match=invert_match,
                language=include,
                path_prefix=path,
                status=status,
                confidence=confidence,
            )
            data = {"pattern": pattern, "files": file_paths, "file_count": len(file_paths), **search_scope(conn, include, path)}
        else:
            results = search_artifacts(
                conn,
                pattern,
                ignore_case=ignore_case,
                regex=regex,
                invert_match=invert_match,
                language=include,
                path_prefix=path,
                status=status,
                confidence=confidence,
                limit=limit,
            )
            stale_paths = stale_paths_checked(conn, [a.file_path for a in results])
            usage.record_usage(conn, usage.nodes(a.function_id for a in results))
            partial_ids = partial_function_ids(conn, {a.file_path for a in results})
            matches = [
                {
                    **({"kind": "function"} if with_documents else {}),
                    "function_id": a.function_id,
                    "function_name": a.function_name,
                    "file_path": a.file_path,
                    "language": a.language,
                    "status": a.status,
                    "signature": a.signature,
                    "possibly_stale": a.file_path in stale_paths,
                    **({"partial": True} if a.function_id in partial_ids else {}),  # kept although its body was read in part
                    **({"matched_in": "qualified_name"} if not invert_match and matched_only_by_qualified_name(a, pattern, ignore_case, regex) else {}),
                }
                for a in results
            ]
            total_functions = len(results)
            if len(results) >= limit:  # there may be more than were asked for: count them, so that a cut-off is never silent
                total_functions = count_search_artifacts(
                    conn, pattern, ignore_case=ignore_case, regex=regex, invert_match=invert_match,
                    language=include, path_prefix=path, status=status, confidence=confidence,
                )
            if with_documents:
                matches += docs_core.section_matches(conn, pattern, limit, status, path)
            data = {
                "pattern": pattern,
                "semantic": False,
                **search_scope(conn, include, path),
                "total_matches": total_functions + (len(matches) - len(results)),
                "truncated": total_functions > len(results),
                "matches": matches,
            }
            if data["truncated"]:
                data["note"] = (data["note"] + " " if data.get("note") else "") + (
                    f"Showing {len(results)} of {total_functions} matching functions, best match first; raise --limit to see more."
                )
            if not invert_match:  # functions the pattern names that a parse error kept out of the index
                add_not_indexed(conn, data, skipped_matching_pattern(conn, pattern, regex=regex, ignore_case=ignore_case))

        if not wants_lines:  # (a line search reads the files themselves)
            add_changed_notice(conn, data, db_path.parent.parent, [m["file_path"] for m in data.get("matches") or [] if m.get("file_path")] + list(data.get("files") or []), before=before)
        response = success_response(data)
        output(response, json_output=json_output)

    except InvalidPattern as e:
        response = error_response(ErrorCode.INVALID_INPUT, str(e))
        output(response, json_output=json_output)
        raise typer.Exit(1)
    except Exception as e:
        response = error_response(
            ErrorCode.EXECUTION_ERROR,
            f"Error during search: {str(e)}",
        )
        output(response, json_output=json_output)
        raise typer.Exit(1)
    finally:
        conn.close()
