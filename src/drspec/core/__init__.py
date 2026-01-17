"""Core utilities for DrSpec."""

from __future__ import annotations

from drspec.core.resources import get_templates_path, get_schema_path
from drspec.core.scanner import (
    Scanner,
    ScannedFunction,
    ScanProgress,
    ScanResult,
    scan_file,
    scan_directory,
    LANGUAGE_MAP,
    DEFAULT_IGNORES,
)
from drspec.core.hasher import compute_hash, normalize_code
from drspec.core.status import (
    StatusSummary,
    get_status_summary,
    get_artifacts_by_status,
    get_stale_artifacts,
    get_pending_artifacts,
    get_broken_artifacts,
    get_review_artifacts,
    mark_verified,
    mark_needs_review,
    mark_broken,
    mark_pending,
    get_file_status_summary,
    get_language_status_summary,
    bulk_update_status,
    reset_stale_to_pending,
    reset_broken_to_pending,
)
from drspec.core.hints import (
    Hint,
    HintType,
    extract_hints,
    extract_hints_simple,
    hints_to_json,
)
from drspec.core.handoff import (
    HandoffMessage,
    format_handoff_message,
    create_debugger_to_architect_handoff,
    create_librarian_to_architect_handoff,
    create_judge_to_vision_handoff,
    create_handoff,
    create_handoff_from_missing_report,
)

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
