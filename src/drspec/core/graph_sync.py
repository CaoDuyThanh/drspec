"""Scan-time code graph synchronization.

Runs the full no-LLM pipeline `drspec scan` needs after parsing a batch of
files: node removal (cascade-delete), per-node hash-diff + embedding, then
edge sync (as-callee refresh, as-caller diff/promotion) for every node that
turned out new or changed. Kept out of `scan.py` itself so the CLI command
stays a thin wrapper and this orchestration is unit-testable directly.

Runs in two passes over the whole scan batch, not interleaved per function:
first every node's `code_hash`/`embedding` is brought up to date, *then*
edges are synced. A caller's edge hash depends on its callee's current
`code_hash` — if a file contains both a caller and its callee and both
changed in the same scan, the callee's row must already reflect its new
hash by the time the caller's edges are computed, regardless of which
function happened to come first in the fresh parse.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import duckdb

from drspec.core.call_resolver import (
    CallSite,
    ResolutionInput,
    ResolvedCall,
    resolve_calls,
)
from drspec.core.carry_over import plan_pairs, restore_snapshots, take_snapshots
from drspec.core.cpp_resolver import CppIndex, MemberAccess, build_cpp_index, resolve_cpp_calls
from drspec.core.js_modules import load_config as load_js_config
from drspec.core.file_records import is_packed, pack, unpack
from drspec.core.languages import is_c_family, suffixes_of
from drspec.core.js_resolver import JS_LANGUAGES, JsIndex, build_js_index, resolve_js_calls
from drspec.core import rust_reach
from drspec.core.provenance import IMPORTS_SIG_KEY, imports_digest
from drspec.core.rust_resolver import RustIndex, build_rust_index, resolve_rust_calls
from drspec.core.declarations import forget_declarations_not_in, is_declaration, match_waiting_declarations, revert_aliases_of, split_declarations, sync_declarations
from drspec.core.docs import has_documents, relink_for_names, update_link_states
from drspec.core.embedder import MODEL_VERSION, embed_text
from drspec.core.hasher import HASH_STRATEGY, compute_edge_hash, compute_hash
from drspec.core.project_facts import (
    bind_declared_types,
    build_classes_by_name,
    build_project_function_index,
    build_properties_by_name,
    class_identity,
    resolve_module_imports,
    resolve_name_imports,
)
from drspec.core.reference_resolver import (
    PropertyReadSite,
    ReferenceResolutionInput,
    ResolvedReference,
    resolve_references,
)
from drspec.core.scanner import FileImports, ScannedFunction
from drspec.db.queries import (
    replace_member_references_many,
    EdgeRecord,
    ReferenceRecord,
    check_embedding_length,
    schedule_removal,
    set_artifact_embedding,
    delete_dependency_edge,
    delete_reference_edge,
    get_artifact_code_hash,
    get_config,
    get_dependencies_by_callee,
    get_dependencies_by_caller,
    get_dependency_edge,
    list_scanned_file_paths,
    get_references_by_callee,
    get_references_by_caller,
    insert_artifact,
    IN_LIST,
    insert_artifacts,
    set_config,
    set_config_many,
    set_edge_hashes,
    upsert_dependency_edge,
    upsert_edges,
    upsert_reference_edge,
)
from drspec.parsers.models import (
    RawCall,
    RawModuleImport,
    RawNameImport,
    RawMemberAccess,
    RawPropertyRead,
)

CodeHashCache = "dict[str, str | None]"
"""function_id -> stored code_hash (or `None`, meaning confirmed absent),
memoized for the lifetime of one `sync_scan_results()` call. Avoids
re-fetching a full `Artifact` row (profiled at ~4.5x per function on a
real scan) just to read the one field this module actually needs."""


def _get_code_hash(conn: duckdb.DuckDBPyConnection, function_id: str, cache: dict) -> "str | None":
    """Cache-backed `code_hash` lookup; a DB round trip only on a genuine
    cache miss, memoized either way.
    """
    if function_id not in cache:
        cache[function_id] = get_artifact_code_hash(conn, function_id)
    return cache[function_id]


def _load_code_hashes(conn: duckdb.DuckDBPyConnection, function_ids: list, cache: dict) -> None:
    """Fill the cache for every id in `function_ids` that is not in it yet, with one
    query per 500 instead of one per id. An id with no artifact is cached as `None`."""
    missing = list(dict.fromkeys(function_id for function_id in function_ids if function_id not in cache))
    for start in range(0, len(missing), 500):
        chunk = missing[start : start + 500]
        marks = ", ".join("?" for _ in chunk)
        found = dict(conn.execute(f"SELECT function_id, code_hash FROM artifacts WHERE function_id IN ({marks})", chunk).fetchall())
        for function_id in chunk:
            cache[function_id] = found.get(function_id)


@dataclass
class SyncStats:
    """Aggregate result of one `sync_scan_results()` call."""

    resolution_reasons: dict[str, str] = field(default_factory=dict)
    embeddings_updated: int = 0
    context_invalidated: dict[str, str] = field(default_factory=dict)
    functions_new: int = 0
    functions_changed: int = 0
    functions_unchanged: int = 0
    functions_removed: int = 0
    carried_over: dict[str, str] = field(default_factory=dict)
    """new function_id -> the function_id whose contract and history it received."""
    edges_inserted: int = 0
    edges_updated: int = 0
    edges_promoted: int = 0
    edges_deleted: int = 0
    references_inserted: int = 0
    references_updated: int = 0
    references_deleted: int = 0
    """`code_references` counts, kept separate from `edges_*` (`calls`)
    since it's a structurally different relation. No `references_promoted`:
    references only ever resolve at `EXTRACTED` confidence, so there's no
    tier to promote across."""
    neighbours_re_resolved: int = 0
    """Functions in files this publish did not read whose calls were re-resolved because the code they call changed."""


def sync_scan_results(
    conn: duckdb.DuckDBPyConnection,
    functions: list[ScannedFunction],
    scanned_file_paths: Iterable[str],
    file_imports: "dict[str, FileImports] | None" = None,
    deep_scan: bool = False,
    embed: bool = False,
    force: bool = False,
    protected_file_paths: Iterable[str] = (),
    no_removal_paths: Iterable[str] = (),
    deleted_paths: Iterable[str] = (),
) -> SyncStats:
    """Run node removal, hash-diff/embedding, and edge/reference sync for
    one `drspec scan` invocation.

    Run it inside `db.staging.publish_transaction`: everything it writes commits
    together, and the functions it records for removal are deleted after that.

    Args:
        conn: DuckDB connection.
        functions: Every function from this scan's fresh parse (across
            however many files were scanned).
        scanned_file_paths: Every file path that was scanned, including
            ones that now have zero functions — needed so "all functions
            removed from this file" is still detected (a file with zero
            fresh functions wouldn't otherwise appear in `functions`).
        file_imports: Each scanned file's own real import facts, keyed by
            the same `file_path` used in `functions`/`scanned_file_paths`.
            A file with no entry here (or `None` overall) is treated as
            having no imports, not an error.
        deep_scan: Whether this invocation asked for the opt-in fallback
            resolution. The edge-diff scoping rule (a plain scan only ever
            touches `EXTRACTED` rows) applies regardless of which resolver
            tier produced a row.
        embed: Whether to compute local embeddings at all (off by default,
            since `embed_text()` is the single largest cost in a scan).
            When `False`, it's never called for a new/changed node, edge,
            or reference, and an existing embedding on something that
            changed is explicitly cleared (`NULL`), never left stale.
        force: Whether to re-resolve *every* scanned function's
            calls/property-reads, not just ones whose `code_hash` changed.
            The hash-based "unchanged -> skip" early exit has no way to
            know a function's calls might resolve differently now, e.g.
            because another module it calls into has since been scanned
            into the DB for the first time. An unchanged function's own
            `code_hash`/embedding are left alone either way; only its
            edge/reference resolution re-runs.

        no_removal_paths: Scanned files (partially parsed ones) whose stored
            functions missing from the fresh parse are kept, not removed.
        deleted_paths: Scanned files that are gone (or no longer part of the project): their per-file entries (record,
            imports, mentions) are removed instead of rewritten empty, as a fresh index has none.

    Returns:
        Aggregate counts of what the scan changed.
    """
    stats = SyncStats()
    file_imports = file_imports or {}
    code_hash_cache: dict = {}
    imports_changed_files = set()
    previous_contexts = {key[len("artifact:context:"):]: value for key, value in
                         conn.execute("SELECT key, value FROM config WHERE starts_with(key, 'artifact:context:')").fetchall()}

    convert_records(conn)
    protected_file_paths = set(protected_file_paths)
    scanned_file_paths = set(scanned_file_paths)
    deleted_paths = set(deleted_paths) & scanned_file_paths
    _record_scanned_file_snapshots(conn, scanned_file_paths)
    functions_by_file: dict[str, list[ScannedFunction]] = {}
    for func in functions:
        functions_by_file.setdefault(func.file_path, []).append(func)

    old_symbols = get_config(conn, "scan:symbols")
    old_facts = _symbol_facts(conn) if old_symbols is not None else None
    # A file that parsed only in part keeps what is stored for it: what is missing from its fresh
    # parse may simply be what the parser could not read.
    removable_paths = scanned_file_paths - set(no_removal_paths)
    planned_pairs = plan_pairs(conn, removable_paths, functions_by_file)
    snapshots = take_snapshots(conn, planned_pairs)
    gone = _sync_removed_nodes(conn, removable_paths, functions_by_file, stats)
    revert_aliases_of(conn, gone)
    definitions, declarations = split_declarations(functions)
    changed_or_new = _sync_nodes(conn, definitions, stats, embed, code_hash_cache)
    sync_declarations(conn, declarations, code_hash_cache)
    match_waiting_declarations(conn)
    stats.functions_removed += forget_declarations_not_in(conn, removable_paths, (d.function_id for d in declarations))
    carried = restore_snapshots(conn, snapshots)
    stats.carried_over = {new_id: pair.old_id for new_id, pair in carried.items()}
    facts_changed = _record_js_digests(conn, scanned_file_paths, file_imports)
    symbols = symbols_hash(conn)
    js_digest = _js_digest(conn)
    if js_digest:
        symbols = hashlib.sha256((symbols + js_digest).encode()).hexdigest()
    declarations_digest = _declarations_digest(conn)
    if declarations_digest:
        symbols = hashlib.sha256((symbols + declarations_digest).encode()).hexdigest()
    from drspec.core.settings import read_all_settings as _settings

    infer_by_name = bool(_settings(conn).get("resolve.infer_by_unique_name", True))
    resolver_identity = RESOLVER_VERSION + (INFER_SUFFIX if infer_by_name else "")  # (turning the setting on or off re-resolves, at the next publish)
    resolver_changed = get_config(conn, "scan:resolver_version") != resolver_identity
    symbols_changed = old_symbols != symbols or resolver_changed
    # Functions in files this scan did not read are re-resolved after a symbol change only if
    # the change could affect them. Everything is re-resolved when asked (`--force`), when the
    # resolver itself changed, or when there is no earlier symbol set to compare with.
    changed_names: Optional[set[str]] = None
    changed_stems: set[str] = set()
    changed_rust_function_names: set[str] = set()
    if symbols_changed and old_facts is not None and not force and not resolver_changed:
        new_facts = _symbol_facts(conn)
        changed_names, changed_stems = _symbol_change_tokens(old_facts, new_facts)
        changed_rust_function_names = _rust_function_names(old_facts, new_facts)
        if any(path.endswith(CPP_SUFFIXES) for path in facts_changed):
            changed_names.add(CPP_TYPES_CHANGED)  # a class or a return type changed: every C or C++ function may resolve differently
    rust_tokens: Optional[set[str]] = None  # what a change reaches in Rust code (`None`: all of it), worked out below once the old facts are read
    appeared_rust_files: set[str] = set()
    callers = {f.function_id: f for f in changed_or_new}
    for func in changed_or_new:
        stats.resolution_reasons[func.function_id] = "source_changed"
    # Persist extracted call/import facts, so a dependency scanned separately
    # can repair an earlier caller without rereading or trusting changed files.
    old_snapshots = _read_config_values(conn, ["scan:file:" + path for path in scanned_file_paths])
    old_imports = _read_config_values(conn, [IMPORTS_PREFIX + path for path in scanned_file_paths])
    new_snapshots: list[tuple[str, str]] = []
    for file_path in scanned_file_paths:
        if file_path in deleted_paths:
            continue  # nothing is stored for a file that is gone (removed below)
        imports = file_imports.get(file_path, FileImports())
        file_functions = functions_by_file.get(file_path, [])
        imports_dict = _imports_dict(imports)
        snapshot = {"imports_hash": hashlib.sha256(json.dumps(imports_dict, sort_keys=True).encode()).hexdigest(),
                    "language": file_functions[0].language if file_functions else None,
                    "functions": [_stored_function(f, code_hash_cache.get(f.function_id)) for f in file_functions]}
        key = "scan:file:" + file_path
        old = old_snapshots.get(key)
        old_snapshot = True if old else None  # (only whether there was one: its content is not read)
        previous_imports = old_imports.get(IMPORTS_PREFIX + file_path)
        import_changed = old_snapshot is None or previous_imports is None or json.loads(previous_imports) != imports_dict
        if import_changed:
            imports_changed_files.add(file_path)
        if old_snapshot is None and file_path.endswith(".rs"):
            appeared_rust_files.add(file_path)
        file_symbols_changed = symbols_changed or old_snapshot is None
        for func in file_functions:
            if is_declaration(func):
                continue  # a declaration has no body to resolve calls in
            if force or embed or file_symbols_changed or import_changed:
                callers[func.function_id] = func
                stats.resolution_reasons[func.function_id] = ("forced" if force else "symbols_changed" if file_symbols_changed else "imports_changed" if import_changed else "embedding_backfill")
        new_snapshots.append((key, pack(snapshot, file_path)))
        new_snapshots.append((MENTIONS_PREFIX + file_path, json.dumps(_mentions_of(file_functions, imports_dict), separators=(",", ":"))))
        if import_changed:
            new_snapshots += _imports_rows(file_path, imports_dict)
    set_config_many(conn, new_snapshots)
    _forget_files(conn, deleted_paths)
    if symbols_changed:
        # Only the files that can be touched by the change are read: a file whose recorded mentions (see `_mentions_of`)
        # hold none of the changed names or stems has nothing to re-resolve, and its record stays unread.
        mentions = dict(
            (key[len(MENTIONS_PREFIX):], value)
            for key, value in conn.execute("SELECT key, value FROM config WHERE starts_with(key, ?)", [MENTIONS_PREFIX]).fetchall()
        )
        if changed_names is not None:
            rust_tokens = _rust_tokens(old_imports, file_imports, facts_changed, appeared_rust_files, deleted_paths, mentions, changed_rust_function_names)
        wanted = []
        for (key,) in conn.execute("SELECT key FROM config WHERE starts_with(key, 'scan:file:')").fetchall():
            file_path = key[len("scan:file:"):]
            if file_path in scanned_file_paths or file_path in protected_file_paths:
                continue
            if changed_names is None or _may_be_affected(mentions.get(file_path), changed_names, changed_stems, rust_tokens):
                wanted.append(file_path)
        snapshots = _read_config_values(conn, ["scan:file:" + path for path in wanted])
        stored_imports = {key[len(IMPORTS_PREFIX):]: json.loads(value) for key, value in _read_config_values(conn, [IMPORTS_PREFIX + path for path in wanted]).items()}
        backfilled: list[tuple[str, str]] = []
        if len(wanted) > 100:  # many files to look at: every stored hash in one query, keeping the fresher values `_sync_nodes` cached
            for function_id, stored_hash in conn.execute("SELECT function_id, code_hash FROM artifacts").fetchall():
                code_hash_cache.setdefault(function_id, stored_hash)
        for file_path in wanted:
            value = snapshots.get("scan:file:" + file_path)
            if value is None:
                continue
            snapshot = unpack(value, file_path)
            file_import_facts = stored_imports.get(file_path, {"name_imports": [], "module_imports": [], "includes": []})
            rows = [_restore_function(row, file_path, snapshot.get("language")) for row in snapshot["functions"]]
            if mentions.get(file_path) is None or (rust_reach.is_rust_facts(file_import_facts.get("facts")) and '"r"' not in mentions[file_path]):  # a database from before the mentions (or before a Rust file's own): they are written now, from every function of the file
                backfilled.append((MENTIONS_PREFIX + file_path, json.dumps(_mentions_of([function for function, _hash in rows], file_import_facts), separators=(",", ":"))))
            _load_code_hashes(conn, [function.function_id for function, _hash in rows], code_hash_cache)
            restored = []
            for function, stored_hash in rows:
                # Stored facts are usable only while they match the stored artifacts.
                if not is_declaration(function) and _get_code_hash(conn, function.function_id, code_hash_cache) == stored_hash:
                    restored.append(function)
            functions_by_file[file_path] = restored
            file_imports[file_path] = FileImports(
                name_imports=[RawNameImport(**r) for r in file_import_facts["name_imports"]],
                module_imports=[RawModuleImport(**r) for r in file_import_facts["module_imports"]],
                includes=list(file_import_facts.get("includes", [])),
                facts=dict(file_import_facts.get("facts", {})))
            for func in restored:
                if changed_names is None or _may_resolve_differently(func, file_import_facts, changed_names, changed_stems, rust_tokens):
                    callers[func.function_id] = func
                    stats.resolution_reasons[func.function_id] = "symbols_changed"
                    stats.neighbours_re_resolved += 1
        if backfilled:
            set_config_many(conn, backfilled)
    set_config(conn, "scan:symbols", symbols)
    set_config(conn, "scan:resolver_version", resolver_identity)
    set_config(conn, "scan:hash_strategy", HASH_STRATEGY)
    _sync_edges(conn, list(callers.values()), functions_by_file, file_imports, deep_scan, embed, stats, code_hash_cache, infer_by_name=infer_by_name)
    _sync_references(conn, list(callers.values()), embed, stats, code_hash_cache, file_imports)
    if embed:
        _backfill_embeddings(conn, {f.function_id for f in functions}, stats)

    from drspec.core.provenance import refresh_contexts
    from drspec.db.queries import update_artifact_status

    contexts = refresh_contexts(conn)
    for function_id, file_path, status in conn.execute("SELECT function_id, file_path, status FROM artifacts WHERE function_id IN (SELECT function_id FROM contracts)").fetchall():
        if function_id in carried:
            continue  # its reason is that it was carried over from another function
        expected = get_config(conn, "contract:context:" + function_id)
        if expected != contexts[function_id] and (previous_contexts.get(function_id) != contexts[function_id] or status != "STALE"):
            reason = "IMPORTS_CHANGED" if file_path in imports_changed_files else "DEPENDENCY_CHANGED"
            update_artifact_status(conn, function_id, "STALE")
            set_config(conn, "artifact:stale_reason:" + function_id, reason)
            stats.context_invalidated[function_id] = reason

    # Documents follow the code they name: links drift, follow a rename or are marked removed, and a document
    # written before the function it names gets its link.
    if has_documents(conn) or conn.execute("SELECT 1 FROM doc_sections LIMIT 1").fetchone():
        from drspec.core.settings import read_all_settings

        doc_settings = read_all_settings(conn)
        if doc_settings.get("docs.enabled", False):  # while documents are off nothing is re-linked; stored rows stay as they are
            update_link_states(conn, stats.carried_over, planned_pairs)
            relink_for_names(conn, [name for f in changed_or_new for name in (f.name, f.qualified_name)], doc_settings)

    return stats


def _forget_files(conn: duckdb.DuckDBPyConnection, file_paths: Iterable[str]) -> None:
    """Remove what is stored per file for files that are gone (deleted, or ignored now): the record, the import facts, the mentions
    and what the parser lost in it (the `partial:` and `skipped:` records `status` counts)."""
    from drspec.core.partial import KEY_PREFIX as partial_prefix, SKIPPED_PREFIX as skipped_prefix

    keys = [prefix + path for path in file_paths for prefix in ("scan:file:", IMPORTS_PREFIX, IMPORTS_SIG_PREFIX, MENTIONS_PREFIX, partial_prefix, skipped_prefix)]
    for start in range(0, len(keys), 500):
        chunk = keys[start : start + 500]
        conn.execute(f"DELETE FROM config WHERE key IN ({', '.join('?' for _ in chunk)})", chunk)


def _read_config_values(conn: duckdb.DuckDBPyConnection, keys: list) -> dict:
    """`{key: value}` for the stored `config` keys among `keys`, in one query."""
    if not keys:
        return {}
    return dict(conn.execute(f"SELECT key, value FROM config WHERE key {IN_LIST}", [list(keys)]).fetchall())


def _function_record(function: ScannedFunction) -> dict:
    """The same dictionary `dataclasses.asdict` gives for a function, without walking every nested object through
    its generic recursion (it was 0.7 s of a 10 s publish): the nested records are plain dataclasses of plain values."""
    record = dict(function.__dict__)
    for key, value in record.items():
        if isinstance(value, list):
            record[key] = [dict(item.__dict__) if hasattr(item, "__dataclass_fields__") else item for item in value]
        elif isinstance(value, dict):
            record[key] = dict(value)
    return record


def _imports_dict(imports: FileImports) -> dict:
    """A file's import facts as stored; the JavaScript module facts are left out when there are none, so the
    stored form (and the fingerprints made from it) of every other file stays what it was."""
    # (written out: `asdict` copies every nested value one by one, 0.34 s for the 450 files of a Rust project; the dictionary
    # is only written out and compared, so the parser's own facts are not copied)
    stored = {
        "name_imports": [dict(item.__dict__) for item in imports.name_imports],
        "module_imports": [dict(item.__dict__) for item in imports.module_imports],
        "includes": list(imports.includes),
    }
    if imports.facts:
        stored["facts"] = imports.facts
    return stored


def _record_js_digests(conn: duckdb.DuckDBPyConnection, scanned_file_paths: Iterable[str], file_imports: dict) -> set[str]:
    """Remember a fingerprint of each scanned file's module facts (a JavaScript, TypeScript or Rust file: what it exports,
    re-exports and declares; a C or C++ file: its classes and the return types of its functions): another file's calls
    resolve through them, but they are not part of any function's identity, so the symbol fingerprint would not see them
    change. Returns the files whose facts are not what they were."""
    rows = []
    gone = []
    paths = list(scanned_file_paths)
    earlier = _read_config_values(conn, [_JS_DIGEST_PREFIX + path for path in paths])
    changed: set[str] = set()
    for file_path in paths:
        imports = file_imports.get(file_path)
        key = _JS_DIGEST_PREFIX + file_path
        if imports is not None and imports.facts:
            digest = hashlib.sha256(json.dumps(imports.facts, sort_keys=True).encode()).hexdigest()
            rows.append((key, digest))
            if earlier.get(key) != digest:
                changed.add(file_path)
        else:
            gone.append(key)
            if key in earlier:
                changed.add(file_path)
    for start in range(0, len(gone), 500):
        chunk = gone[start : start + 500]
        conn.execute(f"DELETE FROM config WHERE key IN ({', '.join('?' for _ in chunk)})", chunk)
    if rows:
        set_config_many(conn, rows)
    return changed


_JS_DIGEST_PREFIX = "scan:jsfacts:"


def _js_digest(conn: duckdb.DuckDBPyConnection) -> str:
    rows = conn.execute(
        "SELECT key, value FROM config WHERE starts_with(key, ?) AND substr(key, ?) IN (SELECT file_path FROM scanned_files) ORDER BY key",
        [_JS_DIGEST_PREFIX, len(_JS_DIGEST_PREFIX) + 1],
    ).fetchall()
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest() if rows else ""


def symbols_hash(conn: duckdb.DuckDBPyConnection) -> str:
    """A fingerprint of the stored functions' identities, parents and property flags: when it
    changes, some call may now resolve differently."""
    rows = conn.execute(
        "SELECT function_id, parent, is_property FROM artifacts WHERE function_id NOT IN (SELECT function_id FROM pending_removals) ORDER BY function_id"
    ).fetchall()
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def _declarations_digest(conn: duckdb.DuckDBPyConnection) -> str:
    """A fingerprint of the C++ declarations and the definitions they are aliases of: a declaration decides where a definition
    can be seen from, so when it comes, goes or changes its definition, some call may resolve differently. Empty when the
    project has none (its fingerprint is then what it always was)."""
    rows = conn.execute("SELECT function_id, definition_id FROM function_declarations ORDER BY function_id").fetchall()
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest() if rows else ""


def _symbol_facts(conn: duckdb.DuckDBPyConnection) -> dict[str, tuple]:
    """function_id -> (name, parent, is_property, file) for every stored function, and `declaration:<id>` -> (name,
    parent, False, file, the definition it is an alias of) for every C++ declaration."""
    rows = conn.execute(
        "SELECT function_id, function_name, parent, is_property, file_path FROM artifacts WHERE function_id NOT IN (SELECT function_id FROM pending_removals)"
    ).fetchall()
    facts = {row[0]: tuple(row[1:]) for row in rows}
    for function_id, name, parent, file_path, definition_id in conn.execute(
        "SELECT function_id, function_name, parent, file_path, definition_id FROM function_declarations"
    ).fetchall():
        facts["declaration:" + function_id] = (name, parent, False, file_path, definition_id)
    return facts


def _symbol_change_tokens(old: dict[str, tuple], new: dict[str, tuple]) -> tuple[set[str], set[str]]:
    """What a change of the project's symbols touched, as `(names, stems)`.

    `names` are the function names and the class names (every part of a nested
    one) of each function that was added, removed, moved or changed its parent or
    property status; `stems` are the file stems of files that appeared or
    disappeared, since a stem shared by two files makes an import of it ambiguous."""
    names: set[str] = set()
    for function_id in set(old) ^ set(new) | {fid for fid in set(old) & set(new) if old[fid] != new[fid]}:
        for facts in (old.get(function_id), new.get(function_id)):
            if facts is not None:
                names.add(facts[0])
                if facts[1]:
                    names.update(part for part in facts[1].split(".") if part)
    old_files = {facts[3] for facts in old.values()}
    new_files = {facts[3] for facts in new.values()}
    stems = {Path(file).stem for file in old_files ^ new_files}
    return names, stems


def _rust_tokens(old_imports: dict, file_imports: dict, facts_changed: set, appeared: set, deleted: set, mentions: dict, function_names: set) -> Optional[set[str]]:
    """What this scan changed in Rust code, for `rust_reach.reach`: the files whose module facts differ with their old facts
    (read from what was stored before this scan wrote them), the files that appeared or are gone, and the names of the
    functions that changed. `None`: the change cannot be bounded."""
    changed_facts: dict = {}
    for path in facts_changed:
        if not path.endswith(".rs"):
            continue
        raw = old_imports.get(IMPORTS_PREFIX + path)
        if raw is not None:
            old = json.loads(raw).get("facts", {})
        else:
            old = {} if path in appeared else None  # (an old file with nothing stored for it: its earlier facts are not known)
        imports = file_imports.get(path)
        changed_facts[path] = (old, {} if path in deleted or imports is None else imports.facts)
    gone = {path for path in deleted if path.endswith(".rs")}
    return rust_reach.reach(changed_facts, appeared, gone, mentions, function_names)


def _rust_function_names(old: dict[str, tuple], new: dict[str, tuple]) -> set[str]:
    """The names of the Rust functions that were added, removed or moved: only the name, not the type or module it is in
    (a call that names a method names it, and a call that names the type reaches it through facts `rust_reach` follows)."""
    names: set[str] = set()
    for function_id in set(old) ^ set(new) | {fid for fid in set(old) & set(new) if old[fid] != new[fid]}:
        for facts in (old.get(function_id), new.get(function_id)):
            if facts is not None and facts[3].endswith(".rs"):
                names.add(facts[0])
    return names


#: What a file mentions, apart from its record: the names its functions call, read or annotate with, and the names and
#: module stems it imports. A change of the project's symbols can alter how a function of the file resolves only when
#: it touched one of them (see `_may_resolve_differently`), so a file that mentions none is not read at all.
MENTIONS_PREFIX = "scan:mentions:"

#: Part of the resolver's identity: raising it re-resolves every call of every project on its next publish.
RESOLVER_VERSION = "scoped-classes:v4"  # v4: a C++ name with several definitions is the one the file sees (its includes, then its folder), not dropped for everyone; v3: Rust files record what they mention; v2: C++ receivers typed from bases, fields, return types, `auto`, elements, lambdas; v3: Rust files record what they mention (one full re-resolution writes it)
INFER_SUFFIX = "+infer-by-name"  # with `resolve.infer_by_unique_name` on, a C++ call on an untyped receiver gets an INFERRED edge (T-46)

#: A name no function has: put among the changed names when the classes or return types of a C or C++ file changed,
#: which can alter how any C or C++ call resolves (`_may_be_affected`, `_may_resolve_differently`).
CPP_TYPES_CHANGED = "\0cpp-types"
CPP_SUFFIXES = suffixes_of("cpp")


def _module_last(module_path: str) -> list:
    return [part for part in module_path.replace("/", ".").split(".") if part][-1:]


def _mentions_of(functions, imports: dict) -> dict:
    """What the functions of a file and its import facts (`_imports_dict` form) mention, for `_may_be_affected`. A
    JavaScript or TypeScript file, whose calls resolve through other files' exports, is always `{"all": true}`; a Rust
    file's mentions are the ones `rust_reach` looks at. The names are exactly the ones `_may_resolve_differently` looks
    at, so a file that mentions none of the changed names is a file that function would answer False for."""
    names: set = set()
    stems: set = set()
    c_family = False
    if rust_reach.is_rust_facts(imports.get("facts")):  # (also a file with no function: what it re-exports is followed from its mentions)
        return rust_reach.mentions(functions, imports["facts"])
    for func in functions:
        if func.language in JS_LANGUAGES:
            return {"all": True}
        c_family = c_family or is_c_family(func.language)
        for call in func.calls:
            names.add(call.name)
            names.add(call.receiver or "")
            names.update(_names_in(call.receiver))
        for read in func.property_reads:
            names.add(read.name)
            if read.receiver is not None:
                names.add(read.receiver)
        for annotation in func.declared_param_types.values():
            names.update(annotation.split("."))
            names.update(_names_in(annotation))
    for imported in imports.get("name_imports", []):
        names.add(imported["name"])
        names.add(imported.get("alias") or "")
        stems.update(_module_last(imported["module_path"]))
    for imported in imports.get("module_imports", []):
        names.add(imported.get("alias") or "")
        stems.update(_module_last(imported["module_path"]))
    mentions: dict = {"n": sorted(names), "s": sorted(stems)}
    if c_family:
        mentions["c"] = True  # (a change of the classes or return types of a C or C++ file touches every such file)
    return mentions


_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")


def _names_in(text: Optional[str]) -> list:
    """The identifiers in a declared type or a receiver expression that is not itself one (`std::vector<Body>`,
    `=getSteering()`, `h.get()`); none for a plain name, which is mentioned as it is."""
    if not text or text.isidentifier():
        return []
    return _IDENTIFIER.findall(text)


def _may_be_affected(stored: Optional[str], names: set, stems: set, rust_tokens: Optional[set] = None) -> bool:
    """Whether a file whose stored mentions are `stored` (`None`: not recorded yet) can be touched by a change of
    `names` and `stems`; for a Rust file, by a change with `rust_tokens` (`None`: one that cannot be bounded). Anything
    not understood counts as yes."""
    if stored is None:
        return True
    try:
        mentioned = json.loads(stored)
        if mentioned.get("all"):
            return True
        if mentioned.get("r"):
            return rust_tokens is None or not rust_tokens.isdisjoint(mentioned["n"])
        if CPP_TYPES_CHANGED in names and mentioned.get("c"):
            return True
        return bool(names.intersection(mentioned["n"])) or bool(stems.intersection(mentioned["s"]))
    except (ValueError, KeyError, TypeError, AttributeError):
        return True


def _may_resolve_differently(func: ScannedFunction, imports: dict, names: set[str], stems: set[str], rust_tokens: Optional[set[str]] = None) -> bool:
    """Whether a change that touched `names` and `stems` could change how `func`
    resolves. False only when nothing it calls, reads, annotates or imports is
    among them, in which case its stored edges are already what a fresh
    resolution would produce. A Rust function is asked about the tokens of
    `rust_reach` (`None`: the change cannot be bounded)."""
    if func.language == "rust":
        return rust_reach.function_may_resolve_differently(func, rust_tokens)
    if func.language in JS_LANGUAGES:
        return True  # a call resolves through the exports and re-exports of other files, which `names` does not cover
    if CPP_TYPES_CHANGED in names and is_c_family(func.language):
        return True
    if any(call.name in names or (call.receiver or "") in names or any(name in names for name in _names_in(call.receiver)) for call in func.calls):
        return True
    if any(read.name in names or read.receiver in names for read in func.property_reads):
        return True
    if any(part in names for annotation in func.declared_param_types.values() for part in [*annotation.split("."), *_names_in(annotation)]):
        return True
    for imported in imports.get("name_imports", []):
        module_last = [part for part in imported["module_path"].replace("/", ".").split(".") if part][-1:]
        if imported["name"] in names or (imported.get("alias") or "") in names or any(part in stems for part in module_last):
            return True
    for imported in imports.get("module_imports", []):
        module_last = [part for part in imported["module_path"].replace("/", ".").split(".") if part][-1:]
        if (imported.get("alias") or "") in names or any(part in stems for part in module_last):
            return True
    return False


#: Marks a database whose per-file records are in the form below; one without it is converted by the next publish.
RECORD_FORM_KEY = "scan:records:form"
RECORD_FORM = "4"
#: A file's import facts, apart from its record: what the contexts and a re-resolution read of a file are its imports,
#: and they should not need the record of every function to be decoded.
IMPORTS_PREFIX = "scan:imports:"
IMPORTS_SIG_PREFIX = IMPORTS_SIG_KEY  # the digest of a file's imports, written with them (a publish reads these, not the imports of every file)


def _imports_rows(file_path: str, imports_dict: dict) -> list:
    """The rows that hold a file's imports: the facts, and their digest as `provenance._signatures` takes it (worked out from
    the stored text, so it is what reading the facts back gives)."""
    text = json.dumps(imports_dict)
    return [(IMPORTS_PREFIX + file_path, text), (IMPORTS_SIG_PREFIX + file_path, imports_digest(json.loads(text)))]

#: What a stored function record leaves out when it has the default, and what `_restore_function` puts back.
_RECORD_DEFAULTS = {
    "parent": None, "decorators": [], "is_method": False, "is_async": False, "is_property": False, "calls": [],
    "property_reads": [], "symbol_reads": [], "first_param_name": None, "declared_param_types": {}, "parameter_tokens": [],
    "member_accesses": [],
}


def _stored_function(function: ScannedFunction, code_hash: Optional[str] = None) -> dict:
    """What the per-file record keeps of a function: what a later resolution needs (its identity, the calls and reads
    in it, its owner and flags) and the hash of its body. The text (body, signature), the lines and the file, language
    and qualified name are in `artifacts` or in the id and the record around it, so they are not copied here, and a
    value that is the default is left out."""
    record = _function_record(function)
    stored = {
        "function_id": record["function_id"],
        "name": record["name"],
        "code_hash": code_hash or compute_hash(function.body, function.language),
    }
    for key, default in _RECORD_DEFAULTS.items():
        if record.get(key) != default:
            stored[key] = record[key]
    return stored


def _restore_function(row: dict, file_path: str, language: Optional[str]) -> tuple:
    """`(function, stored body hash)` from a stored function record, of either form: the one above, or the earlier one
    that held the body and every field. A restored function has no body, signature or lines (nothing that re-resolves
    calls reads them)."""
    row = dict(row)
    stored_hash = row.pop("code_hash", None)
    if stored_hash is None and "body" in row:
        stored_hash = compute_hash(row["body"], row.get("language") or language or "")
    for key, default in _RECORD_DEFAULTS.items():
        row.setdefault(key, list(default) if isinstance(default, list) else dict(default) if isinstance(default, dict) else default)
    function_id = row["function_id"]
    row.setdefault("file_path", file_path)
    row.setdefault("language", language or "")
    row.setdefault("qualified_name", function_id[len(file_path) + 2 : function_id.rfind("#")] if "#" in function_id else row["name"])
    row.setdefault("signature", "")
    row.setdefault("body", "")
    row.setdefault("start_line", 0)
    row.setdefault("end_line", 0)
    row["calls"] = [RawCall(**r) for r in row["calls"]]
    row["property_reads"] = [RawPropertyRead(**r) for r in row["property_reads"]]
    row["member_accesses"] = [RawMemberAccess(**r) for r in row["member_accesses"]]
    row.pop("symbols_hash", None)
    return ScannedFunction(**row), stored_hash


def convert_records(conn: duckdb.DuckDBPyConnection) -> int:
    """Put the per-file records of an older database into the current form, once (a publish does it first, in its own
    transaction, and records the form): the imports are entries of their own, the text of a function is not copied, and
    a record is packed. Returns how many entries were written."""
    if get_config(conn, RECORD_FORM_KEY) == RECORD_FORM:
        return 0
    converted: list[tuple[str, str]] = []
    for key, value in conn.execute("SELECT key, value FROM config WHERE starts_with(key, 'scan:file:')").fetchall():
        if is_packed(value):
            continue
        file_path = key[len("scan:file:"):]
        snapshot = json.loads(value)
        functions = snapshot.get("functions", [])
        snapshot.pop("symbols_hash", None)
        imports = snapshot.pop("imports", None)
        if imports is not None:
            converted += _imports_rows(file_path, imports)
        if imports is not None or any("body" in row for row in functions):
            snapshot["language"] = (functions[0].get("language") if functions else None) or snapshot.get("language")
            rows = []
            for row in functions:
                function, stored_hash = _restore_function(row, file_path, snapshot["language"])
                rows.append(_stored_function(function, stored_hash))
            snapshot["functions"] = rows
        converted.append((key, pack(snapshot, file_path)))
    set_config_many(conn, converted)
    set_config(conn, RECORD_FORM_KEY, RECORD_FORM)
    return len(converted)


def _backfill_embeddings(conn, function_ids: set[str], stats: SyncStats) -> None:
    """Version each vector independently; model changes rebuild on the next
    embedding scan, without requiring source edits or --force.
    """
    specs = [("artifacts", "function_id", "body", "code_hash"),
             ("dependencies", "caller_id, callee_id", "call_site", "edge_hash"),
             ("code_references", "caller_id, callee_id", "reference_site", "edge_hash")]
    for table, ids, text_column, hash_column in specs:
        id_columns = ids.split(", ")
        if table == "artifacts":
            present_sql = "SELECT function_id, body, code_hash, EXISTS (SELECT 1 FROM artifact_embeddings e WHERE e.function_id = artifacts.function_id) FROM artifacts"
        else:
            present_sql = f"SELECT {ids}, {text_column}, {hash_column}, embedding IS NOT NULL FROM {table}"
        for row in conn.execute(present_sql).fetchall():
            keys, text, source_hash, present = row[:-3], row[-3], row[-2], row[-1]
            if not any(key in function_ids for key in keys) or text is None:
                continue
            meta_key = "embedding:" + json.dumps([table, *keys])
            version = json.dumps([MODEL_VERSION, source_hash])
            if present and get_config(conn, meta_key) == version:
                continue
            vector = embed_text(text)
            check_embedding_length(vector)
            where = " AND ".join(f"{column}=?" for column in id_columns)
            if table == "artifacts":
                set_artifact_embedding(conn, keys[0], vector)
            else:
                conn.execute(f"UPDATE {table} SET embedding=? WHERE {where}", [vector, *keys])
            set_config(conn, meta_key, version)
            stats.embeddings_updated += 1


def _record_scanned_file_snapshots(conn: duckdb.DuckDBPyConnection, scanned_file_paths: Iterable[str]) -> None:
    """Record each scanned file's current on-disk mtime/size — the
    snapshot the background staleness worker later diffs against to
    detect drift. A file that's vanished between being scanned and this
    call (a rare race, not worth failing the whole scan over) is skipped.
    """
    rows = []
    for file_path in scanned_file_paths:
        try:
            file_stat = Path(file_path).stat()
        except OSError:
            continue
        rows.append((file_path, file_stat.st_mtime, file_stat.st_size))
    for start in range(0, len(rows), 300):
        chunk = rows[start : start + 300]
        values = ", ".join("(?, ?, ?, CURRENT_TIMESTAMP)" for _ in chunk)
        conn.execute(
            f"INSERT INTO scanned_files (file_path, mtime, size, last_scanned_at) VALUES {values} "
            "ON CONFLICT (file_path) DO UPDATE SET mtime = excluded.mtime, size = excluded.size, last_scanned_at = excluded.last_scanned_at",
            [value for row in chunk for value in row],
        )


def _sync_removed_nodes(
    conn: duckdb.DuckDBPyConnection,
    scanned_file_paths: Iterable[str],
    functions_by_file: dict[str, list[ScannedFunction]],
    stats: SyncStats,
) -> list[str]:
    """For each scanned file, record every stored artifact whose `function_id` is no
    longer in that file's fresh parse as to be removed. The rows go in
    `complete_pending_removals`, once this transaction has committed.
    """
    gone: list[str] = []
    paths = list(dict.fromkeys(scanned_file_paths))
    stored: dict[str, list[str]] = {}
    if paths:
        for file_path, function_id in conn.execute(f"SELECT file_path, function_id FROM artifacts WHERE file_path {IN_LIST}", [paths]).fetchall():
            stored.setdefault(file_path, []).append(function_id)
    for file_path in paths:
        fresh_ids = {f.function_id for f in functions_by_file.get(file_path, [])}
        gone.extend(function_id for function_id in stored.get(file_path, []) if function_id not in fresh_ids)
    stats.functions_removed += schedule_removal(conn, gone)
    return gone


def _load_artifact_details(conn: duckdb.DuckDBPyConnection, function_ids: list) -> dict:
    """`{function_id: (code_hash, signature, start_line, end_line, parent, is_property)}` for the stored functions among
    `function_ids`, in one query."""
    ids = list(dict.fromkeys(function_ids))
    found: dict = {}
    if not ids:
        return found
    for row in conn.execute(
        f"SELECT function_id, code_hash, signature, start_line, end_line, parent, is_property, language FROM artifacts WHERE function_id {IN_LIST}", [ids]
    ).fetchall():
        found[row[0]] = tuple(row[1:])
    return found


def _sync_nodes(
    conn: duckdb.DuckDBPyConnection,
    functions: list[ScannedFunction],
    stats: SyncStats,
    embed: bool,
    code_hash_cache: dict,
) -> list[ScannedFunction]:
    """Hash-diff every remaining node; for new/changed ones, recompute the
    embedding (only when `embed`) and write both. Unchanged nodes are
    skipped entirely, which is the cheap early-exit this module relies on.

    Populates `code_hash_cache` for every function touched here (changed,
    new, or unchanged), so `_sync_edges`/`_sync_references` can reuse it
    without a second DB round trip.

    Returns:
        The subset of `functions` that turned out new or changed, for
        the edge-sync pass.
    """
    changed_or_new: list[ScannedFunction] = []
    # What is stored for these functions, in a few queries rather than two per function: a function whose hash and
    # the details beside it are as stored needs no write at all.
    stored = _load_artifact_details(conn, [func.function_id for func in functions])
    for function_id in {func.function_id for func in functions}:
        code_hash_cache.setdefault(function_id, stored[function_id][0] if function_id in stored else None)

    # Functions new to the index are written together at the end, in a few statements, not one statement each. A
    # function that appears twice in the batch keeps the later one, as writing them in turn did.
    new_rows: dict[str, tuple] = {}

    def row_of(func: ScannedFunction, code_hash: str) -> tuple:
        return (func.function_id, func.file_path, func.name, func.signature, func.body, code_hash, func.language,
                func.start_line, func.end_line, func.parent, "PENDING", func.is_property)

    for func in functions:
        code_hash = compute_hash(func.body, func.language)
        existing_hash = code_hash_cache[func.function_id]
        is_new = existing_hash is None

        if not is_new and existing_hash == code_hash:
            stats.functions_unchanged += 1
            if func.function_id in new_rows:
                new_rows[func.function_id] = row_of(func, code_hash)
                continue
            current = stored.get(func.function_id)
            if current is not None and current[1:] == (func.signature, func.start_line, func.end_line, func.parent, func.is_property, func.language):
                continue
            insert_artifact(conn, function_id=func.function_id, file_path=func.file_path,
                            function_name=func.name, signature=func.signature, body=func.body,
                            code_hash=code_hash, language=func.language, start_line=func.start_line,
                            end_line=func.end_line, parent=func.parent, is_property=func.is_property)
            continue

        if is_new or func.function_id in new_rows:
            new_rows[func.function_id] = row_of(func, code_hash)
        else:
            insert_artifact(
                conn,
                function_id=func.function_id,
                file_path=func.file_path,
                function_name=func.name,
                signature=func.signature,
                body=func.body,
                code_hash=code_hash,
                language=func.language,
                start_line=func.start_line,
                end_line=func.end_line,
                parent=func.parent,
                embedding=None,
                is_property=func.is_property,
            )
        code_hash_cache[func.function_id] = code_hash

        if is_new:
            stats.functions_new += 1
        else:
            stats.functions_changed += 1
        changed_or_new.append(func)

    insert_artifacts(conn, list(new_rows.values()))
    return changed_or_new


def _sync_edges(
    conn: duckdb.DuckDBPyConnection,
    callers_to_resolve: list[ScannedFunction],
    functions_by_file: dict[str, list[ScannedFunction]],
    file_imports: dict[str, FileImports],
    deep_scan: bool,
    embed: bool,
    stats: SyncStats,
    code_hash_cache: dict,
    infer_by_name: bool = False,
) -> None:
    """For every function in `callers_to_resolve`, refresh edges where
    it's the callee, then diff its own outgoing calls as the caller.
    Normally just the new/changed subset of this scan's functions — the
    whole scanned batch instead under `--force`, since every one of them
    could have calls worth re-resolving, not only ones whose own
    `code_hash` changed.

    Project-wide facts (`classes_by_name`, and `project_function_index`
    when `deep_scan`) are built once here, not per caller — they're the
    same for every caller in this scan batch. Per-file import resolution
    is cached too, since every function in the same file shares it.
    """
    if not callers_to_resolve:
        return

    classes_by_name = build_classes_by_name(conn)
    project_function_index = build_project_function_index(conn) if deep_scan else {}
    cpp_index: Optional[CppIndex] = None
    js_index: Optional[JsIndex] = None
    rust_index: Optional[RustIndex] = None
    unresolved_js: dict[str, dict[str, str]] = {}
    names_js: dict[str, list[str]] = {}
    names_rust: dict[str, list[str]] = {}
    unresolved_rust: dict[str, dict[str, str]] = {}
    member_rows: dict[str, list] = {}

    stored_imports: dict = {}  # every file's stored import facts as text, read in one query when the first is asked for

    def stored_imports_of(file_path: str) -> Optional[str]:
        if not stored_imports:
            stored_imports.update((key[len(IMPORTS_PREFIX):], value) for key, value in conn.execute("SELECT key, value FROM config WHERE starts_with(key, ?)", [IMPORTS_PREFIX]).fetchall())
            stored_imports.setdefault("", None)  # (so that a project with none is not read again)
        return stored_imports.get(file_path)

    def js_facts_for(file_path: str) -> dict:
        imports = file_imports.get(file_path)
        if imports is not None:
            return imports.facts
        raw = stored_imports_of(file_path)
        return dict(json.loads(raw).get("facts", {})) if raw else {}

    def includes_for(file_path: str) -> list[str]:
        imports = file_imports.get(file_path)
        if imports is not None:
            return list(imports.includes)
        raw = stored_imports_of(file_path)
        return list(json.loads(raw).get("includes", [])) if raw else []

    # A declaration that is an alias has no artifact: a call to it is a call to its definition.
    aliases = dict(conn.execute("SELECT function_id, definition_id FROM function_declarations WHERE definition_id IS NOT NULL").fetchall())
    import_cache: dict[str, tuple[dict[str, list[str]], dict[str, dict[str, list[str]]]]] = {}
    facts_cache: dict = {}  # per-file import bindings, valid while the artifacts stay as they are
    edge_rows = _EdgeRows(conn, "dependencies", "call_site", EdgeRecord, [f.function_id for f in callers_to_resolve])
    overloads: dict = {}  # which stored functions have an overload, filled when first asked
    same_file_cache: dict[str, dict] = {}

    for func in callers_to_resolve:
        # Always a cache hit: _sync_nodes populates this entry for every function it processes.
        caller_hash = code_hash_cache[func.function_id]

        _refresh_callee_edges(conn, func.function_id, caller_hash, embed, stats, code_hash_cache, edge_rows)

        # What a file's top-level functions are called, built once for the file: it is the same for every function in
        # it, and rebuilding it for each cost time that grew with the square of the number of functions of a file.
        same_file_functions = same_file_cache.get(func.file_path)
        if same_file_functions is None:
            same_file_functions = same_file_cache[func.file_path] = {
                name: sorted({aliases.get(function_id, function_id) for function_id in ids})
                for name, ids in _build_same_file_functions(functions_by_file[func.file_path]).items()
            }

        if func.file_path not in import_cache:
            imports = file_imports.get(func.file_path)
            import_cache[func.file_path] = (
                resolve_name_imports(conn, imports.name_imports if imports else [], facts_cache),
                resolve_module_imports(conn, imports.module_imports if imports else [], facts_cache),
            )
        imported_names, imported_modules = import_cache[func.file_path]

        if func.language in JS_LANGUAGES:
            if js_index is None:
                js_index = build_js_index(conn, js_facts_for, list_scanned_file_paths(conn), _js_config(conn))
            outcome = resolve_js_calls(
                func.function_id, func.file_path, func.calls, func.declared_param_types, js_index,
                caller_class=func.parent if func.is_method else None,
            )
            unresolved_js[func.function_id] = outcome.unresolved
            _remember_names(func, outcome.unresolved, names_js)
            _sync_caller_edges(conn, func.function_id, caller_hash, outcome.resolved, deep_scan, embed, stats, code_hash_cache, edge_rows, overloads)
            continue

        if func.language == "rust":
            if rust_index is None:
                rust_index = build_rust_index(conn, js_facts_for, list_scanned_file_paths(conn), _project_root(conn))
            outcome = resolve_rust_calls(func.function_id, func.file_path, func.calls, func.declared_param_types, rust_index)
            unresolved_rust[func.function_id] = outcome.unresolved
            _remember_names(func, outcome.unresolved, names_rust)
            _sync_caller_edges(conn, func.function_id, caller_hash, outcome.resolved, deep_scan, embed, stats, code_hash_cache, edge_rows, overloads)
            continue

        if is_c_family(func.language):
            if cpp_index is None:
                cpp_index = build_cpp_index(conn, includes_for, list_scanned_file_paths(conn), js_facts_for)
            members: list = []
            resolved = resolve_cpp_calls(
                func.function_id,
                func.file_path,
                [CallSite(receiver=c.receiver, name=c.name, text=c.text) for c in func.calls],
                func.declared_param_types,
                cpp_index,
                caller_class=func.parent if func.is_method else None,
                infer_by_name=infer_by_name,
                member_accesses=[MemberAccess(a.receiver, a.member, a.kind, a.line, a.text, a.call_receiver, a.call_name, a.arg_index, a.written_through) for a in func.member_accesses],
                members_out=members,
            )
            member_rows[func.function_id] = [(m.owner_class, m.member, m.kind, func.file_path, m.line, m.site, m.receiver, m.written_through) for m in members]
            _sync_caller_edges(conn, func.function_id, caller_hash, resolved, deep_scan, embed, stats, code_hash_cache, edge_rows, overloads, inferred_in_scope=True)
            continue

        resolved = resolve_calls(
            ResolutionInput(
                caller_id=func.function_id,
                call_sites=[CallSite(receiver=c.receiver, name=c.name, text=c.text) for c in func.calls],
                caller_parent=class_identity(func.file_path, func.parent) if func.is_method and func.parent else None,
                bare_calls_imply_self=is_c_family(func.language),
                self_aliases=_determine_self_aliases(func),
                same_file_functions=same_file_functions,
                classes_by_name=classes_by_name,
                imported_names=imported_names,
                imported_modules=imported_modules,
                declared_types=bind_declared_types(conn, func.file_path, file_imports.get(func.file_path), func.declared_param_types, classes_by_name, facts_cache),
                deep_scan=deep_scan,
                project_function_index=project_function_index,
            )
        )
        _sync_caller_edges(conn, func.function_id, caller_hash, resolved, deep_scan, embed, stats, code_hash_cache, edge_rows, overloads)

    if edge_rows is not None:
        edge_rows.flush()
    replace_member_references_many(conn, member_rows)  # the member accesses of the C++ functions read again (their old ones go)
    _store_unresolved(conn, unresolved_js, JS_UNRESOLVED_PREFIX, names_js)
    _store_unresolved(conn, unresolved_rust, RUST_UNRESOLVED_PREFIX, names_rust)


JS_UNRESOLVED_PREFIX = "js:unresolved:"
RUST_UNRESOLVED_PREFIX = "rust:unresolved:"


def _remember_names(func, unresolved: dict[str, str], names: dict[str, list[str]]) -> None:
    """The names called by the calls of `func` that were not found, one per unresolved call text, for `_store_unresolved`."""
    name_of = {}
    for call in func.calls:
        name_of.setdefault(call.text, call.name)
    not_found = sorted(name_of.get(text, "") for text, reason in unresolved.items() if reason == "not found")
    if not_found:
        names[func.function_id] = not_found


def _store_unresolved(
    conn: duckdb.DuckDBPyConnection, unresolved: dict[str, dict[str, str]], prefix: str, names: Optional[dict[str, list[str]]] = None
) -> None:
    """Why each call of a JavaScript, TypeScript or Rust function was not tied to a function, as counts per reason,
    for `status`, and the names of the calls that were "not found" (`status` tells a library call from a function of
    the project by them). Rows of functions that no longer exist are ignored when read. One statement for the rows to
    write and one for those to remove, not one per function."""
    rows = []
    cleared = []
    for function_id, calls in unresolved.items():
        counts: dict[str, Any] = {}
        for reason in calls.values():
            counts[reason] = counts.get(reason, 0) + 1
        if names and function_id in names and counts.get("not found") == len(names[function_id]):
            counts["names"] = names[function_id]
        if counts:
            rows.append((prefix + function_id, json.dumps(counts, sort_keys=True)))
        else:
            cleared.append(prefix + function_id)
    if cleared:
        conn.execute(f"DELETE FROM config WHERE key {IN_LIST}", [cleared])
    if rows:
        set_config_many(conn, rows)


def _project_root(conn: duckdb.DuckDBPyConnection) -> Optional[Path]:
    """The project folder, when the database sits in its usual `<root>/_drspec/` place."""
    from drspec.core.discovery import project_root_for

    try:
        path = conn.execute("PRAGMA database_list").fetchall()[0][2]
    except (duckdb.Error, IndexError):
        return None
    return project_root_for(Path(path)) if path else None


def _js_config(conn: duckdb.DuckDBPyConnection):
    """The project's tsconfig / package.json settings; no settings when the project folder is unknown."""
    from drspec.core.js_modules import JsConfig

    root = _project_root(conn)
    return load_js_config(root) if root is not None else JsConfig()


def _build_same_file_functions(funcs: list[ScannedFunction]) -> dict[str, list[str]]:
    """function_name -> [function_id, ...] for one file's fresh parse —
    top-level (non-method) functions only.

    Methods are excluded on purpose: a bare call (`helper()`) never means
    "a method on some class" in Python, JS, or C++ unless that class is
    the caller's own, which is handled separately via
    `bare_calls_imply_self`/`classes_by_name`. Including methods here
    caused a false "ambiguous" case for a free function and a same-named
    method in the same file (e.g. free `add()` next to `Calculator.add`),
    even though a bare call can never reach an unrelated class's method.
    """
    mapping: dict[str, list[str]] = {}
    for f in funcs:
        if f.parent is None:
            mapping.setdefault(f.name, []).append(f.function_id)
    return mapping


def _determine_self_aliases(func: ScannedFunction) -> list[str]:
    """Which receiver identifiers mean "this instance" for `func`, if
    it's a method at all. Never includes `"super"` — that's handled as
    its own always-unresolved case in `call_resolver.py`."""
    if not func.is_method:
        return []
    if func.language == "python":
        if "staticmethod" in func.decorators:
            return []
        return [func.first_param_name] if func.first_param_name else []
    return ["this"]  # JS/C++: a fixed keyword, not a parameter name


class _EdgeRows:
    """The rows of an edge table (`dependencies` or `code_references`) that one publish reads, read in a few
    queries instead of one or two per function, and kept in step with what the publish writes.

    Only this publish writes these rows while it runs (it holds the connection), so a row read once stays right as
    long as every write goes through this object. A publish then asks the database a number of questions that does not
    grow with the number of functions."""

    def __init__(self, conn: duckdb.DuckDBPyConnection, table: str, site_column: str, record, function_ids: list) -> None:
        self.table = table
        self._conn = conn
        self._site_column = site_column
        self._pending: dict[tuple[str, str], tuple] = {}  # rows to write at `flush`, by pair
        self._record = record
        ids = list(dict.fromkeys(function_ids))
        self._ids = set(ids)
        self._rows: dict[tuple[str, str], Any] = {}
        self._by_caller: dict[str, dict[str, Any]] = {function_id: {} for function_id in ids}
        self._by_callee: dict[str, dict[str, Any]] = {function_id: {} for function_id in ids}
        columns = f"caller_id, callee_id, {site_column}, edge_hash, NULL, confidence, created_at"
        for side in ("caller_id", "callee_id"):
            if not ids:
                break
            for row in conn.execute(f"SELECT {columns} FROM {table} WHERE {side} {IN_LIST}", [ids]).fetchall():
                key = (row[0], row[1])
                if key not in self._rows:
                    self._register(record.from_row(row))

    def _register(self, edge) -> None:
        self._rows[(edge.caller_id, edge.callee_id)] = edge
        if edge.caller_id in self._ids:
            self._by_caller[edge.caller_id][edge.callee_id] = edge
        if edge.callee_id in self._ids:
            self._by_callee[edge.callee_id][edge.caller_id] = edge

    def into(self, callee_id: str) -> list:
        return list(self._by_callee.get(callee_id, {}).values())

    def out_of(self, caller_id: str) -> dict:
        return dict(self._by_caller.get(caller_id, {}))

    def get(self, caller_id: str, callee_id: str):
        return self._rows.get((caller_id, callee_id))

    def forget(self, caller_id: str, callee_id: str) -> None:
        self._pending.pop((caller_id, callee_id), None)
        edge = self._rows.pop((caller_id, callee_id), None)
        if edge is not None:
            self._by_caller.get(caller_id, {}).pop(callee_id, None)
            self._by_callee.get(callee_id, {}).pop(caller_id, None)

    def put(self, caller_id: str, callee_id: str, site: str, edge_hash: str, confidence: str) -> None:
        self.forget(caller_id, callee_id)
        self._register(self._record.from_row((caller_id, callee_id, site, edge_hash, None, confidence, None)))

    def rehash(self, callee_id: str, changed: list) -> None:
        for caller_id, new_hash in changed:
            edge = self._rows.get((caller_id, callee_id))
            if edge is not None:
                edge.edge_hash = new_hash
            waiting = self._pending.get((caller_id, callee_id))
            if waiting is not None:  # not in the table yet, so the update there missed it
                self._pending[(caller_id, callee_id)] = waiting[:3] + (new_hash,) + waiting[4:]

    def store(self, caller_id: str, callee_id: str, site: str, edge_hash: str, confidence: str) -> None:
        """`put`, and write the row at the next `flush` together with the others."""
        self.put(caller_id, callee_id, site, edge_hash, confidence)
        self._pending[(caller_id, callee_id)] = (caller_id, callee_id, site, edge_hash, confidence)

    def flush(self) -> None:
        """Write the stored rows: a few statements, each with many rows. Call before the table is read from the database."""
        if self._pending:
            upsert_edges(self._conn, self.table, self._site_column, list(self._pending.values()))
            self._pending.clear()


def _refresh_callee_edges(
    conn: duckdb.DuckDBPyConnection,
    callee_id: str,
    callee_hash: str,
    embed: bool,
    stats: SyncStats,
    code_hash_cache: dict,
    rows: Optional[_EdgeRows] = None,
) -> None:
    """This node's own `code_hash` just changed, so every edge where it's
    the callee needs a fresh `edge_hash`/`embedding` — the caller side
    (`call_site`, `confidence`) is untouched. The embedding is only
    recomputed when `embed`; otherwise it's cleared to `NULL` rather than
    left stale, even though only the callee's hash changed here.
    """
    edges = rows.into(callee_id) if rows is not None else get_dependencies_by_callee(conn, callee_id)
    _load_code_hashes(conn, [edge.caller_id for edge in edges], code_hash_cache)
    changed: list[tuple[str, str]] = []
    for edge in edges:
        caller_hash = _get_code_hash(conn, edge.caller_id, code_hash_cache)
        if caller_hash is None or edge.call_site is None:
            continue  # defensive only — FK guarantees the artifact exists

        new_hash = compute_edge_hash(caller_hash, callee_hash, edge.call_site)
        if new_hash == edge.edge_hash:
            continue
        changed.append((edge.caller_id, new_hash))
    set_edge_hashes(conn, "dependencies", callee_id, changed)
    if rows is not None:
        rows.rehash(callee_id, changed)
    stats.edges_updated += len(changed)


def _sync_caller_edges(
    conn: duckdb.DuckDBPyConnection,
    caller_id: str,
    caller_hash: str,
    resolved: list[ResolvedCall],
    deep_scan: bool,
    embed: bool,
    stats: SyncStats,
    code_hash_cache: dict,
    rows: Optional[_EdgeRows] = None,
    overloads: Optional[dict] = None,
    inferred_in_scope: bool = False,
) -> None:
    """Scoped diff of this caller's fresh resolution against its stored
    edges, with the insert-vs-promotion check across all tiers.

    `inferred_in_scope`: the resolver of this caller makes INFERRED edges itself (a C++ call on an untyped receiver, to the
    one method of that name), so those are diffed like EXTRACTED ones: one that is no longer found is deleted.
    """
    fresh_by_callee = {call.callee_id: call for call in resolved}

    # A plain scan resolves EXTRACTED calls, plus the AMBIGUOUS ones to every overload of a
    # name. It has no basis to delete/downgrade an existing INFERRED row, or an AMBIGUOUS one
    # from the project-wide fallback — those are left untouched. --deep-scan diffs every tier.
    existing_scoped = {
        edge.callee_id: edge
        for edge in (rows.out_of(caller_id).values() if rows is not None else get_dependencies_by_caller(conn, caller_id, confidences=None))
        if deep_scan or edge.confidence == "EXTRACTED" or (inferred_in_scope and edge.confidence == "INFERRED") or (edge.confidence == "AMBIGUOUS" and _has_overloads(conn, edge.callee_id, overloads))
    }

    for callee_id in existing_scoped:
        if callee_id not in fresh_by_callee:
            delete_dependency_edge(conn, caller_id, callee_id)
            if rows is not None:
                rows.forget(caller_id, callee_id)
            stats.edges_deleted += 1

    for callee_id, call in fresh_by_callee.items():
        callee_hash = _get_code_hash(conn, callee_id, code_hash_cache)
        if callee_hash is None:
            continue  # defensive only — same_file_functions is backed by inserted artifacts

        edge_hash = compute_edge_hash(caller_hash, callee_hash, call.call_site)
        embedding = None

        # Check any tier, not just this scan's scope: PRIMARY KEY (caller_id, callee_id) holds
        # exactly one row regardless of confidence, so a same-tier-only comparison could miss
        # a row that already exists at a different tier.
        existing_any = rows.get(caller_id, callee_id) if rows is not None else get_dependency_edge(conn, caller_id, callee_id)
        if existing_any and existing_any.edge_hash == edge_hash and existing_any.confidence == call.confidence:
            continue
        if rows is not None:
            rows.store(caller_id, callee_id, call.call_site, edge_hash, call.confidence)
        else:
            upsert_dependency_edge(conn, caller_id, callee_id, call.call_site, edge_hash, embedding, call.confidence)

        if existing_any is None:
            stats.edges_inserted += 1
        elif existing_any.confidence != call.confidence:
            stats.edges_promoted += 1
        else:
            stats.edges_updated += 1


def _has_overloads(conn: duckdb.DuckDBPyConnection, function_id: str, cache: Optional[dict] = None) -> bool:
    """Whether another stored function of the same file has this one's name and parent. With a `cache` (one per publish,
    while the stored functions do not change) the functions are grouped once and each question is a lookup."""
    if cache is not None:
        if "overloaded" not in cache:
            groups: dict[tuple, list[str]] = {}
            for stored_id, file_path, name, parent in conn.execute("SELECT function_id, file_path, function_name, parent FROM artifacts").fetchall():
                groups.setdefault((file_path, name, parent), []).append(stored_id)
            cache["overloaded"] = {stored_id for ids in groups.values() if len(ids) > 1 for stored_id in ids}
        return function_id in cache["overloaded"]
    row = conn.execute(
        """
        SELECT COUNT(*) FROM artifacts a JOIN artifacts b
          ON a.file_path = b.file_path AND a.function_name = b.function_name AND a.parent IS NOT DISTINCT FROM b.parent
        WHERE a.function_id = ? AND b.function_id <> a.function_id
        """,
        [function_id],
    ).fetchone()
    return bool(row and row[0])


def _sync_references(
    conn: duckdb.DuckDBPyConnection,
    callers_to_resolve: list[ScannedFunction],
    embed: bool,
    stats: SyncStats,
    code_hash_cache: dict,
    file_imports: dict[str, FileImports],
) -> None:
    """`_sync_edges` counterpart for the separate `code_references` table:
    for every function in `callers_to_resolve`, refresh reference rows
    where it's the callee (the property being read), then diff its own
    property reads as the caller. Python-only in practice — a non-Python
    function's `property_reads` is always empty — but the callee-refresh
    pass still runs regardless of language, since it could be a property
    read *from* elsewhere.

    No `deep_scan`/confidence-tier scoping here, unlike `_sync_edges`:
    every `code_references` row is always `EXTRACTED` (never guessed), so
    there's no second tier a plain scan would need to leave untouched.
    """
    if not callers_to_resolve:
        return

    properties_by_name = build_properties_by_name(conn)
    facts_cache: dict = {}
    reference_rows = _EdgeRows(conn, "code_references", "reference_site", ReferenceRecord, [f.function_id for f in callers_to_resolve])

    for func in callers_to_resolve:
        # Always a cache hit — same reasoning as `_sync_edges` above.
        caller_hash = code_hash_cache[func.function_id]

        _refresh_callee_references(conn, func.function_id, caller_hash, embed, stats, code_hash_cache, reference_rows)

        resolved = resolve_references(
            ReferenceResolutionInput(
                caller_id=func.function_id,
                property_reads=[
                    PropertyReadSite(receiver=r.receiver, name=r.name, text=r.text) for r in func.property_reads
                ],
                caller_parent=class_identity(func.file_path, func.parent) if func.is_method and func.parent else None,
                self_aliases=_determine_self_aliases(func),
                properties_by_name=properties_by_name,
                declared_types=bind_declared_types(conn, func.file_path, file_imports.get(func.file_path), func.declared_param_types, properties_by_name, facts_cache),
            )
        )
        _sync_caller_references(conn, func.function_id, caller_hash, resolved, embed, stats, code_hash_cache, reference_rows)
    if reference_rows is not None:
        reference_rows.flush()


def _refresh_callee_references(
    conn: duckdb.DuckDBPyConnection,
    callee_id: str,
    callee_hash: str,
    embed: bool,
    stats: SyncStats,
    code_hash_cache: dict,
    rows: Optional[_EdgeRows] = None,
) -> None:
    """Mirrors `_refresh_callee_edges`, for `code_references`."""
    references = rows.into(callee_id) if rows is not None else get_references_by_callee(conn, callee_id)
    _load_code_hashes(conn, [ref.caller_id for ref in references], code_hash_cache)
    changed: list[tuple[str, str]] = []
    for ref in references:
        caller_hash = _get_code_hash(conn, ref.caller_id, code_hash_cache)
        if caller_hash is None or ref.reference_site is None:
            continue  # defensive only — FK guarantees the artifact exists

        new_hash = compute_edge_hash(caller_hash, callee_hash, ref.reference_site)
        if new_hash == ref.edge_hash:
            continue
        changed.append((ref.caller_id, new_hash))
    set_edge_hashes(conn, "code_references", callee_id, changed)
    if rows is not None:
        rows.rehash(callee_id, changed)
    stats.references_updated += len(changed)


def _sync_caller_references(
    conn: duckdb.DuckDBPyConnection,
    caller_id: str,
    caller_hash: str,
    resolved: list[ResolvedReference],
    embed: bool,
    stats: SyncStats,
    code_hash_cache: dict,
    rows: Optional[_EdgeRows] = None,
) -> None:
    """Mirrors `_sync_caller_edges`, for `code_references` — simpler, since
    every row is always `EXTRACTED`: no tier scoping, and no promotion
    case (insert vs. update is decided purely by "did this pair already
    exist," not by any confidence comparison)."""
    fresh_by_callee = {ref.callee_id: ref for ref in resolved}
    existing = rows.out_of(caller_id) if rows is not None else {ref.callee_id: ref for ref in get_references_by_caller(conn, caller_id)}

    for callee_id in existing:
        if callee_id not in fresh_by_callee:
            delete_reference_edge(conn, caller_id, callee_id)
            if rows is not None:
                rows.forget(caller_id, callee_id)
            stats.references_deleted += 1

    for callee_id, ref in fresh_by_callee.items():
        callee_hash = _get_code_hash(conn, callee_id, code_hash_cache)
        if callee_hash is None:
            continue  # defensive only — properties_by_name is backed by inserted artifacts

        edge_hash = compute_edge_hash(caller_hash, callee_hash, ref.reference_site)
        if callee_id in existing and existing[callee_id].edge_hash == edge_hash:
            continue
        embedding = None
        if rows is not None:
            rows.store(caller_id, callee_id, ref.reference_site, edge_hash, ref.confidence)
        else:
            upsert_reference_edge(conn, caller_id, callee_id, ref.reference_site, edge_hash, embedding, ref.confidence)

        if callee_id in existing:
            stats.references_updated += 1
        else:
            stats.references_inserted += 1
