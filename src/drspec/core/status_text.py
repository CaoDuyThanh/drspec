"""The text `drspec status` prints by default: the counts, one line for each block, and nothing for a block that is all zero. The lists
(the partly parsed files, the largest tables, the resolution reasons, the confidence distribution) are behind `--details`, which prints the
whole text as before. JSON is the whole data, always. Anything that needs an action stays on a line of its own: the parser upgrade, the
backlog (files waiting, in retry wait, the embedding backlog), the yaml files that PyYAML does not read, the MCP registration note."""

from __future__ import annotations

from typing import Any, Dict, List

_STATUSES = ("PENDING", "STALE", "VERIFIED", "BROKEN", "NEEDS_REVIEW")
_LANGUAGE_NAMES = {"python": "Python", "cpp": "Cpp", "javascript": "JavaScript", "rust": "Rust"}


def _number(value: Any) -> str:
    return f"{value:,}" if isinstance(value, int) else str(value)


def _verification(data: Dict[str, Any]) -> List[str]:
    evidence = data.get("verification_evidence") or {}
    if not any(evidence.values()):
        return []
    return [
        "Verification: " + ", ".join(f"{name} {evidence.get(name, 0)}" for name in ("unverified", "passed", "failed", "partial", "error", "stale"))
    ]


def _artifacts(data: Dict[str, Any]) -> List[str]:
    artifacts = data.get("artifacts") or {}
    if not artifacts.get("total"):
        return []
    by = artifacts.get("by_status") or {}
    parts = ", ".join(f"{status.lower().replace('_', ' ')} {by.get(status, 0)}" for status in _STATUSES)
    return [f"Artifacts: {artifacts['total']} ({parts})"]


def _summary(data: Dict[str, Any]) -> List[str]:
    summary = data.get("summary") or {}
    if "items_needing_attention" not in summary:
        return []
    return [f"Items Needing Attention: {summary['items_needing_attention']}, completion {summary.get('completion_rate', 0.0)}%"]


def _backlog(data: Dict[str, Any]) -> List[str]:
    backlog = data.get("backlog") or {}
    lines = []
    pending = [backlog.get("files_pending_rescan", 0), backlog.get("files_pending_discovery", 0)]
    retrying = backlog.get("files_in_retry_wait", 0)
    if any(pending) or retrying:
        line = f"Backlog: {pending[0]} pending rescan, {pending[1]} pending discovery"
        if retrying:
            line += f", {retrying} in retry wait"
        lines.append(line)
    if backlog.get("files_unreadable"):  # a file that cannot be read as it is: not work, but it is listed by --details
        lines.append(f"Unreadable files: {backlog['files_unreadable']} (list: --details)")
    embedding = backlog.get("embedding") or {}
    if embedding:
        # The message is the sentence T-156 wrote: "N of M embedded (top_n = K): the others are not found by meaning" (the warning is the
        # part that acts on a partly embedded index, so it stays on the line).
        message = embedding.get("message") or f"{embedding.get('embedded', 0)} of {embedding.get('total', 0)} embedded (top_n {embedding.get('top_n', 0)})"
        line = f"Embedding: {message}"
        if embedding.get("state") not in (None, "idle"):
            line += f" [{embedding['state']}]"
        if embedding.get("backlog"):
            line += f", backlog {embedding['backlog']}"
        lines.append(line)
    return lines


def _contracts(data: Dict[str, Any]) -> List[str]:
    contracts = data.get("contracts") or {}
    if not contracts.get("total"):
        return []
    average = (contracts.get("confidence") or {}).get("average", 0.0)
    return [f"Contracts: {contracts['total']} (average confidence {average})"]


def _edge_lines(data: Dict[str, Any]) -> List[str]:
    edges = data.get("edges") or {}
    lines = []
    for name, label in (("calls", "Calls"), ("references", "References")):
        block = edges.get(name) or {}
        if block.get("total"):
            by = block.get("by_confidence") or {}
            lines.append(f"Edges {label}: {block['total']} (extracted {by.get('EXTRACTED', 0)}, inferred {by.get('INFERRED', 0)}, ambiguous {by.get('AMBIGUOUS', 0)})")
    return lines


def _languages(data: Dict[str, Any]) -> List[str]:
    lines = []
    for key, name in _LANGUAGE_NAMES.items():
        block = data.get(key)
        if not block or not block.get("files"):
            continue
        parts = [f"{_number(block['files'])} files", f"{block.get('files_partially_parsed', 0)} partially parsed"]
        if block.get("skipped_constructs"):
            parts.append(f"{block['skipped_constructs']} skipped constructs")
        if "calls" in block:
            share = block.get("unresolved_share")
            unresolved = f"{block.get('unresolved_calls', 0)} unresolved" + (f" ({share}%)" if share is not None else "")
            parts.append(f"{_number(block['calls'])} calls, {unresolved}")
        if block.get("macro_invocations"):
            parts.append(f"{block['macro_invocations']} macro invocations")
        lines.append(f"{name}: " + ", ".join(parts))
    return lines


def _partially_parsed(data: Dict[str, Any]) -> List[str]:
    partial = data.get("partially_parsed")
    if partial and partial.get("files"):
        return [
            f"Partially Parsed: {partial['files']} files, {partial.get('functions_skipped', 0)} functions skipped, "
            f"{partial.get('functions_partial', 0)} partial (use --details to list them)"
        ]
    if data.get("files_read_with_warnings"):
        return [f"Read With Warnings: {data['files_read_with_warnings']} file(s) (use --details to list them)"]
    return []


def _attention(data: Dict[str, Any]) -> List[str]:
    lines = []
    upgrade = data.get("parser_upgrade")
    if upgrade and upgrade.get("message"):
        lines.append(f"Parser Upgrade: {upgrade['message']}")
    yaml_files = data.get("yaml_config")
    if yaml_files:
        line = (
            f"Yaml Config: {yaml_files.get('files', 0)} file(s), {yaml_files.get('keys', 0)} keys, "
            f"{yaml_files.get('files_with_problems', 0)} with problems, {yaml_files.get('files_skipped', 0)} skipped"
        )
        if yaml_files.get("files_without_pyyaml"):
            line += f"; {yaml_files['files_without_pyyaml']} not read: PyYAML is not installed (pip install pyyaml)"
        lines.append(line)
    mcp = data.get("mcp_server") or {}
    if mcp.get("note"):
        lines.append(f"MCP Server: {mcp['note']}")
    return lines


def _members(data: Dict[str, Any]) -> List[str]:
    members = data.get("member_references")
    if not members or not members.get("total"):
        return []
    kinds = members.get("by_kind") or {}
    return [
        f"Member References: {_number(members['total'])} (resolved {members.get('resolved', 0)}, read {kinds.get('read', 0)}, "
        f"write {kinds.get('write', 0)}, readwrite {kinds.get('readwrite', 0)}, unresolved {members.get('unresolved', 0)})"
    ]


def _generated(data: Dict[str, Any]) -> List[str]:
    """Left out on purpose, but a surprise to a person (ROS `devel/` and `install/`): named on the line, and how to read one."""
    left = (data.get("generated_folders") or {}).get("left_out") or []
    if not left:
        return []
    names = [text.split(": ", 1)[1] if ": " in text else text for text in left]  # "generated folder: devel (catkin)" -> "devel (catkin)"
    if len(names) <= 5:
        return [f"Generated folders left out: {', '.join(names)} (scan.include reads one)"]
    return [f"{len(names)} generated folders left out (scan.include reads one; list: --details)"]


def _documents(data: Dict[str, Any]) -> List[str]:
    documents = data.get("documents")
    if not documents:
        return []
    return ["Documents: " + ", ".join(f"{key.replace('_', ' ')} {_number(value)}" for key, value in documents.items() if isinstance(value, (int, float)))]


def _database_line(data: Dict[str, Any]) -> List[str]:
    database = data.get("database")
    if not database:
        return []
    return [
        f"Database: {database.get('file_mb', 0)} MB file, {database.get('used_mb', 0)} MB used, "
        f"{database.get('free_mb', 0)} MB free ({database.get('free_percent', 0)}%)"
    ]


def _parsers(data: Dict[str, Any]) -> List[str]:
    parsers = data.get("parsers")
    if not parsers:
        return []
    held = parsers.get("held_down_by")
    return [f"Parsers: {parsers.get('count', 0)}" + (f" (held down by {held})" if held else "")]


def short_lines(data: Dict[str, Any]) -> List[str]:
    """The default text of `status`: one line per block that is not empty, the actionable lines among them."""
    lines: List[str] = []
    for block in (
        _verification, _artifacts, _summary, _backlog, _attention, _contracts, _edge_lines, _languages,
        _partially_parsed, _members, _generated, _documents, _database_line, _parsers,
    ):
        lines.extend(block(data))
    return lines


def short_text(data: Dict[str, Any]) -> str:
    return "\n".join(short_lines(data))
