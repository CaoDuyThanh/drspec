"""Core utilities for DrSpec.

The names below are imported from their modules when first used (`from drspec.core import Scanner`), not when the package is:
`import drspec.core.tracing` (every CLI command does) must not load the parsers, the embedder or DuckDB."""

from __future__ import annotations

import importlib
from typing import Any

_EXPORTS = {
    "get_templates_path": "drspec.core.resources",
    "get_schema_path": "drspec.core.resources",
    "Scanner": "drspec.core.scanner",
    "ScannedFunction": "drspec.core.scanner",
    "ScanProgress": "drspec.core.scanner",
    "ScanResult": "drspec.core.scanner",
    "scan_file": "drspec.core.scanner",
    "scan_directory": "drspec.core.scanner",
    "LANGUAGE_MAP": "drspec.core.scanner",
    "DEFAULT_IGNORES": "drspec.core.scanner",
    "compute_hash": "drspec.core.hasher",
    "normalize_code": "drspec.core.hasher",
    "embed_text": "drspec.core.embedder",
    "EMBEDDING_DIM": "drspec.core.embedder",
    "MODEL_NAME": "drspec.core.embedder",
    "StatusSummary": "drspec.core.status",
    "get_status_summary": "drspec.core.status",
    "get_artifacts_by_status": "drspec.core.status",
    "get_stale_artifacts": "drspec.core.status",
    "get_pending_artifacts": "drspec.core.status",
    "get_broken_artifacts": "drspec.core.status",
    "get_review_artifacts": "drspec.core.status",
    "mark_verified": "drspec.core.status",
    "mark_needs_review": "drspec.core.status",
    "mark_broken": "drspec.core.status",
    "mark_pending": "drspec.core.status",
    "get_file_status_summary": "drspec.core.status",
    "get_language_status_summary": "drspec.core.status",
    "bulk_update_status": "drspec.core.status",
    "reset_stale_to_pending": "drspec.core.status",
    "reset_broken_to_pending": "drspec.core.status",
    "Hint": "drspec.core.hints",
    "HintType": "drspec.core.hints",
    "extract_hints": "drspec.core.hints",
    "extract_hints_simple": "drspec.core.hints",
    "hints_to_json": "drspec.core.hints",
    "HandoffMessage": "drspec.core.handoff",
    "format_handoff_message": "drspec.core.handoff",
    "create_debugger_to_architect_handoff": "drspec.core.handoff",
    "create_librarian_to_architect_handoff": "drspec.core.handoff",
    "create_judge_to_vision_handoff": "drspec.core.handoff",
    "create_handoff": "drspec.core.handoff",
    "create_handoff_from_missing_report": "drspec.core.handoff",
}


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value  # (found once, then plain attribute access)
    return value


__all__ = [
    # Resources
    "get_templates_path",
    "get_schema_path",
    # Scanner
    "Scanner",
    "ScannedFunction",
    "ScanProgress",
    "ScanResult",
    "scan_file",
    "scan_directory",
    "LANGUAGE_MAP",
    "DEFAULT_IGNORES",
    # Hasher
    "compute_hash",
    "normalize_code",
    # Embedder
    "embed_text",
    "EMBEDDING_DIM",
    "MODEL_NAME",
    # Status tracking
    "StatusSummary",
    "get_status_summary",
    "get_artifacts_by_status",
    "get_stale_artifacts",
    "get_pending_artifacts",
    "get_broken_artifacts",
    "get_review_artifacts",
    "mark_verified",
    "mark_needs_review",
    "mark_broken",
    "mark_pending",
    "get_file_status_summary",
    "get_language_status_summary",
    "bulk_update_status",
    "reset_stale_to_pending",
    "reset_broken_to_pending",
    # Hints
    "Hint",
    "HintType",
    "extract_hints",
    "extract_hints_simple",
    "hints_to_json",
    # Handoff messaging
    "HandoffMessage",
    "format_handoff_message",
    "create_debugger_to_architect_handoff",
    "create_librarian_to_architect_handoff",
    "create_judge_to_vision_handoff",
    "create_handoff",
    "create_handoff_from_missing_report",
]
