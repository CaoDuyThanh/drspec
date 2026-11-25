"""The text a scan prints by default: one line for the result (the counts that are not zero), then one line for each thing that needs
attention (a file that failed, a function re-read or re-resolved, a file parsed in part, the worker behind, a note). A long list is a count,
with `--verbose` named for the list. `--verbose` prints the whole summary as it always did, and JSON is the whole summary, always.

Nothing that is not zero is dropped: every counter that is not zero is in the first line, and every problem is one line of its own (the
tests check each)."""

from __future__ import annotations

from typing import Any, Dict, List

#: The counters a first line names when they are not zero, in the order they are printed.
COUNTS = (
    ("functions_new", "new"),
    ("functions_changed", "changed"),
    ("functions_unchanged", "unchanged"),
    ("functions_removed", "removed"),
    ("functions_removed_as_ignored", "removed as ignored"),
    ("edges_inserted", "edges inserted"),
    ("edges_updated", "edges updated"),
    ("edges_promoted", "edges promoted"),
    ("edges_deleted", "edges deleted"),
    ("references_inserted", "references inserted"),
    ("references_updated", "references updated"),
    ("references_deleted", "references deleted"),
    ("neighbours_re_resolved", "neighbours re-resolved"),
    ("embeddings_updated", "embeddings updated"),
    ("documents_indexed", "documents indexed"),
    ("files_unchanged", "files unchanged"),
    ("files_failed", "failed"),
    ("files_cancelled", "cancelled"),
    ("files_removed_as_ignored", "files removed as ignored"),
    ("files_skipped_by_gitignore", "skipped by .gitignore"),
    ("documentation_sections_to_review", "documentation sections to review"),
)
#: How many entries a line lists before it becomes a count with `--verbose` named.
LIST_LIMIT = 5


def _counts(data: Dict[str, Any]) -> List[str]:
    found = []
    for key, label in COUNTS:
        value = data.get(key) or 0
        if value:
            found.append(f"{label} {value}")
    carried = (data.get("carried_over") or {}).get("total", 0)
    if carried:
        found.append(f"carried over {carried}")
    invalidated = (data.get("contracts_invalidated") or {}).get("total", 0)
    if invalidated:
        found.append(f"contracts invalidated {invalidated}")
    return found


def _attention(data: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    failed = data.get("files_failed") or 0
    errors = data.get("errors") or []
    # A read failure has no line; a syntax error of a parsed file has one. The count of syntax errors is every error but the failed files.
    failures = [error for error in errors if error.get("line") is None]
    syntax = [error for error in errors if error.get("line") is not None]
    if failed:
        first = f": {failures[0].get('file')}: {failures[0].get('message')}" if failures else ""
        more = f" (and {failed - 1} more; list: --verbose)" if failed > 1 else " (list: --verbose)"
        lines.append(f"{failed} file(s) could not be read{first}{more}")
    syntax_count = max(0, (data.get("error_count") or 0) - failed)
    if syntax_count and syntax:
        files = list(dict.fromkeys(error.get("file") for error in syntax))
        first = syntax[0]
        more = f" (and {len(files) - 1} more; list: --verbose)" if len(files) > 1 else ""
        lines.append(
            f"{len(files)} file(s) with {syntax_count} syntax error(s): {files[0]} (first at line {first.get('line')}: {first.get('message')}){more}"
        )
    reasons = data.get("resolution_reasons") or {}
    if reasons.get("total"):
        by = ", ".join(f"{reason} {number}" for reason, number in (reasons.get("by_reason") or {}).items())
        lines.append(f"{reasons['total']} function(s) re-resolved ({by}); list: --verbose")
    if data.get("files_partially_parsed"):
        lines.append(
            f"{data['files_partially_parsed']} file(s) parsed in part ({data.get('functions_skipped', 0)} function(s) skipped, "
            f"{data.get('functions_partial', 0)} partial): drspec status --partial lists them"
        )
    if data.get("files_read_with_warnings") and not syntax:  # (with the syntax errors above, the warnings are said there, once)
        lines.append(f"{data['files_read_with_warnings']} file(s) read with parser warnings")
    if data.get("skipped_constructs"):
        lines.append(f"{data['skipped_constructs']} construct(s) skipped by the parser")
    if data.get("removed_note"):
        lines.append(str(data["removed_note"]))
    if data.get("skipped_note"):
        lines.append(str(data["skipped_note"]))
    left_out = data.get("left_out") or []
    if left_out:
        names = [text.split(": ", 1)[1] if ": " in text else text for text in left_out]
        if len(names) <= LIST_LIMIT:  # named once, with the way to read one (the folder's name is the first word of each)
            lines.append(f"generated folders left out: {', '.join(names)}; read one with: drspec config set scan.include <folder>")
        else:
            lines.append(f"{len(names)} generated folder(s) left out; list: --verbose")
    if data.get("files_remaining"):
        lines.append(f"the worker is behind: {data['files_remaining']} file(s) still queued")
    if data.get("files_in_flight"):
        lines.append(f"{data['files_in_flight']} file(s) in the batch being published are not counted yet")
    if data.get("note"):
        lines.append(str(data["note"]))
    if data.get("hint"):
        lines.append(str(data["hint"]))
    return lines


def short_lines(data: Dict[str, Any]) -> List[str]:
    """The default text of a scan: the message (as `✓`) with the counts that are not zero, then the lines that need attention."""
    message = data.get("message") or ""
    counts = _counts(data)
    first = f"✓ {message}" if message else "✓ Scan finished"
    if counts and data.get("state", "done") == "done":
        first += f" ({', '.join(counts)})"
    lines = [first]
    if counts and data.get("state", "done") != "done":
        lines.append(f"counts: {', '.join(counts)}")
    lines.extend(_attention(data))
    return lines


def short_text(data: Dict[str, Any]) -> str:
    return "\n".join(short_lines(data))
