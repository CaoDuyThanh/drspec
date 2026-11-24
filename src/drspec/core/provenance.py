"""Contract context provenance, separate from a function's source hash."""

from __future__ import annotations

import hashlib
import json

from drspec.db.queries import get_config, set_config_many


IMPORTS_KEY = 'scan:imports:'
IMPORTS_SIG_KEY = 'scan:imports_sig:'
_EMPTY_IMPORTS = hashlib.sha1(b'{}').hexdigest()[:12]


def imports_digest(value) -> str:
    """The short digest of one file's imports that a function's signature holds (`_signatures`). It is stored beside the
    imports when they are written (`IMPORTS_SIG_KEY`), so a publish does not decode and encode the imports of every file
    of the project to get it."""
    return hashlib.sha1(json.dumps(value, sort_keys=True).encode('utf-8')).hexdigest()[:12]


class _StoredImports:
    """The imports of every file as stored: the digest of each, read as it is, and the imports themselves as text, decoded
    only for a file a fingerprint is worked out for. A file whose digest was not stored (a database from before it) gets
    it worked out once, from its text, and `learned` holds those for `refresh_contexts` to write."""

    def __init__(self, texts: dict, stored_digests: dict) -> None:
        self._texts = texts
        self._decoded: dict = {}
        self.learned: dict = {}
        self.digests: dict = {}
        for path, text in texts.items():
            digest = stored_digests.get(path)
            if digest is None:
                digest = self.learned[path] = imports_digest(json.loads(text) if text else {})
            self.digests[path] = digest

    def get(self, path, default=None):
        value = self._decoded.get(path)
        if value is None:
            text = self._texts.get(path)
            if text is None:
                return default
            value = self._decoded[path] = json.loads(text) if text else {}
        return value


def _read_inputs(conn) -> tuple:
    """What a context is made of, as stored: `(hashes, files, imports, edges)`."""
    hashes: dict = {}
    files: dict = {}
    for function_id, code_hash, file_path in conn.execute("SELECT function_id, code_hash, file_path FROM artifacts").fetchall():
        hashes[function_id] = code_hash
        files[function_id] = file_path
    # the imports of each file are entries of their own, so the records of the functions are not read
    texts = {key[len(IMPORTS_KEY):]: value for key, value in conn.execute("SELECT key, value FROM config WHERE starts_with(key, ?)", [IMPORTS_KEY]).fetchall()}
    stored_digests = {key[len(IMPORTS_SIG_KEY):]: value for key, value in conn.execute("SELECT key, value FROM config WHERE starts_with(key, ?)", [IMPORTS_SIG_KEY]).fetchall()}
    imports = _StoredImports(texts, stored_digests)
    edges = {key: [] for key in hashes}
    for table, site in [('dependencies', 'call_site'), ('code_references', 'reference_site')]:
        for caller, callee, text, confidence in conn.execute(f'SELECT caller_id, callee_id, {site}, confidence FROM {table}').fetchall():
            edges[caller].append((callee, table, text, confidence))
    return hashes, files, imports, edges


def compute_contexts(conn) -> dict[str, str]:
    """Fingerprint imports and the reachable call/reference graph.

    Walk each reachable set once per root, including cycles without recursion.
    A dependency's implementation/imports and every resolved binding contribute;
    the root's own source stays a separate provenance field.
    """
    return _contexts_of(*_read_inputs(conn))


def _contexts_of(hashes: dict, files: dict, imports: dict, edges: dict, roots=None) -> dict[str, str]:
    """The fingerprints of `roots` (every function when not given)."""
    # The payload of a root is the JSON of `['dependency-context:v1', records]`, one record per reachable node in name
    # order, the root's own record without its hash. Each record is written once here, and a root's payload is those
    # pieces joined in the same bytes `json.dumps` of the whole would give, so the fingerprints stay what they were.
    order = sorted(hashes)
    rank = {node: index for index, node in enumerate(order)}
    ordered_edges: dict = {}
    edges_json: dict = {}
    imports_json: dict = {}
    pieces: dict = {}
    dump = json.dumps

    def edges_of(node: str) -> list:
        found = ordered_edges.get(node)
        if found is None:
            found = ordered_edges[node] = sorted(edges[node])
        return found

    def record(node: str, own_hash) -> str:
        # `json.dumps([node, hash, imports, edges], sort_keys=True)` with the two big parts written once: a file's imports
        # are the same for all its functions and a node's edges for every root that reaches it
        path = files[node]
        file_imports = imports_json.get(path)
        if file_imports is None:
            file_imports = imports_json[path] = dump(imports.get(path, {}), sort_keys=True)
        written_edges = edges_json.get(node)
        if written_edges is None:
            written_edges = edges_json[node] = dump(edges_of(node), sort_keys=True)
        return f"[{dump(node)}, {dump(own_hash)}, {file_imports}, {written_edges}]"

    def piece(node: str) -> str:
        found = pieces.get(node)
        if found is None:
            found = pieces[node] = record(node, hashes[node])
        return found

    prefix = '["dependency-context:v1", ['
    contexts = {}
    for root in (hashes if roots is None else roots):
        visited = set()
        pending = [root]
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            pending.extend(edge[0] for edge in edges[current])
        root_piece = record(root, None)
        members = [root_piece if node == root else piece(node) for node in sorted(visited, key=rank.__getitem__)]
        payload = prefix + ', '.join(members) + ']]'
        contexts[root] = hashlib.sha256(payload.encode('utf-8')).hexdigest()
    return contexts


def current_context(conn, function_id: str) -> str:
    value = get_config(conn, 'artifact:context:' + function_id)
    return value if value is not None else compute_contexts(conn).get(function_id, 'unknown')


CONTEXT_KEY_PREFIX = 'artifact:context:'


INPUTS_KEY = 'provenance:inputs'
INPUTS_FORM = 2


def _signatures(hashes: dict, files: dict, imports: dict, edges: dict) -> dict[str, str]:
    """A short digest for each function of everything its record in a context payload is made of: its hash, its
    file's imports and its resolved edges. Equal digests mean an equal record."""
    digests = imports.digests if isinstance(imports, _StoredImports) else {path: imports_digest(value) for path, value in imports.items()}
    return {
        node: hashlib.sha1(repr((hashes[node], files[node], digests.get(files[node], _EMPTY_IMPORTS), sorted(edges[node]))).encode('utf-8')).hexdigest()[:16]
        for node in hashes
    }


def _reaching(changed: set, edges: dict) -> set:
    """`changed` and every function that can reach one of them through call or reference edges."""
    callers: dict = {}
    for caller, outgoing in edges.items():
        for edge in outgoing:
            callers.setdefault(edge[0], []).append(caller)
    reached = set()
    pending = list(changed)
    while pending:
        node = pending.pop()
        if node in reached:
            continue
        reached.add(node)
        pending.extend(callers.get(node, ()))
    return reached


def refresh_contexts(conn) -> dict[str, str]:
    """Recompute the context fingerprints that can have changed and store the ones that did.

    Rewriting rows whose value is the same made every publish cost time proportional to the whole project (about
    five seconds for 2,400 functions) however little had changed, and the worker holds a lock for the length of a
    publish. For the same reason only some fingerprints are recomputed: a short digest of each function's record
    (see `_signatures`) is kept beside them, and a fingerprint is made of the records of the functions its function
    reaches, so only the functions that reach a function whose digest changed are worked out again. A forced rescan
    of unchanged files recomputes none.
    """
    inputs = _read_inputs(conn)
    # The removal of a function is carried out when the publish ends, after this: an edge to it that the scan could not
    # diff (a call resolved at INFERRED or AMBIGUOUS is only deleted with its callee) is left out, as the removal will.
    pending = {row[0] for row in conn.execute("SELECT function_id FROM pending_removals").fetchall()}
    if pending:
        inputs = (*inputs[:3], {caller: [edge for edge in outgoing if edge[0] not in pending] for caller, outgoing in inputs[3].items()})
    signatures = _signatures(*inputs)
    stored = dict(conn.execute("SELECT key, value FROM config WHERE starts_with(key, ?)", [CONTEXT_KEY_PREFIX]).fetchall())
    previous = None
    try:
        raw = get_config(conn, INPUTS_KEY)
        loaded = json.loads(raw) if raw else None
        if isinstance(loaded, dict) and loaded.get('form') == INPUTS_FORM:
            previous = loaded['signatures']
    except ValueError:
        previous = None
    hashes = inputs[0]
    # What is recorded as seen leaves out the functions about to be removed: the removal is carried out when the publish
    # ends, after this, and a record that held them would keep them until the next change.
    seen = {node: digest for node, digest in signatures.items() if node not in pending}
    if previous is not None:
        # A function with no stored fingerprint (a new one, or one whose row was removed) counts as changed, and the
        # others keep theirs: one new function used to make every fingerprint of the project be worked out again.
        changed = {node for node, digest in signatures.items() if previous.get(node) != digest}
        changed.update(function_id for function_id in hashes if CONTEXT_KEY_PREFIX + function_id not in stored)
        if not changed:
            contexts = {function_id: stored[CONTEXT_KEY_PREFIX + function_id] for function_id in hashes}
            if set(previous) <= set(seen):
                _store_learned_digests(conn, inputs[2])
                return contexts
            # only functions that are gone: no fingerprint changes, but the record of what was seen drops them
        else:
            affected = _reaching(changed, inputs[3])
            contexts = {function_id: stored[CONTEXT_KEY_PREFIX + function_id] for function_id in hashes if function_id not in affected}
            contexts.update(_contexts_of(*inputs, roots=sorted(affected)))
    else:
        contexts = _contexts_of(*inputs)
    rows = [
        (CONTEXT_KEY_PREFIX + function_id, value)
        for function_id, value in contexts.items()
        if stored.get(CONTEXT_KEY_PREFIX + function_id) != value
    ]
    set_config_many(conn, rows + _learned_digest_rows(inputs[2]) + [(INPUTS_KEY, json.dumps({'form': INPUTS_FORM, 'signatures': seen}, sort_keys=True))])
    return contexts


def _learned_digest_rows(imports) -> list:
    """The imports digests that were worked out because none was stored (see `_StoredImports`), as rows to write."""
    return [(IMPORTS_SIG_KEY + path, digest) for path, digest in getattr(imports, 'learned', {}).items()]


def _store_learned_digests(conn, imports) -> None:
    rows = _learned_digest_rows(imports)
    if rows:
        set_config_many(conn, rows)


def contract_freshness(conn, function_id: str, contract: dict) -> dict:
    source = conn.execute('SELECT code_hash FROM artifacts WHERE function_id=?', [function_id]).fetchone()
    source_hash = source[0] if source else None
    context_hash = current_context(conn, function_id)
    contract_context = contract.get('context_hash')
    reason = None
    if not source_hash or contract.get('source_hash') != source_hash:
        reason = 'source_changed_or_provenance_unknown'
    elif not contract_context or contract_context != context_hash:
        reason = get_config(conn, 'artifact:stale_reason:' + function_id) or 'dependency_context_changed_or_unknown'
    return {"hash_strategy": get_config(conn, 'scan:hash_strategy'),
                "current_source_hash": source_hash, "contract_source_hash": contract.get('source_hash'),
                "current_context_hash": context_hash, "contract_context_hash": contract_context,
                "current": reason is None, "stale_reason": reason}


def evidence_is_current(freshness: dict, evidence: dict) -> bool:
    return bool(freshness['current'] and evidence.get('source_hash') == freshness['current_source_hash']
                and evidence.get('context_hash') == freshness['current_context_hash'])
