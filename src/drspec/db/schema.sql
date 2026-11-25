-- DrSpec Database Schema
-- Version: 1.0
-- Strategy: Rebuild (no migrations)

-- artifacts: Stores scanned function information
CREATE TABLE IF NOT EXISTS artifacts (
    function_id TEXT PRIMARY KEY,           -- format: filepath::qualified_name#<8 hex of the parameters' hash>
    file_path TEXT NOT NULL,                -- relative path from project root
    function_name TEXT NOT NULL,            -- function/method name
    signature TEXT NOT NULL,                -- full function signature
    body TEXT NOT NULL,                     -- function source code
    code_hash TEXT NOT NULL,                -- SHA-256 hash of normalized code
    language TEXT NOT NULL,                 -- python, javascript, cpp
    start_line INTEGER NOT NULL,            -- 1-indexed start line
    end_line INTEGER NOT NULL,              -- 1-indexed end line
    parent TEXT,                            -- parent class/namespace if applicable
    status TEXT DEFAULT 'PENDING',          -- PENDING, VERIFIED, NEEDS_REVIEW, STALE, BROKEN
    is_property BOOLEAN DEFAULT FALSE,      -- Python @property methods only (design.md §9); always FALSE for JS/C++
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- artifact_embeddings: the local embedding of a function's `body` (all-MiniLM-L6-v2, 384 values; a list, not
-- a fixed array). In its own table because DuckDB cannot update a list column of a row other tables
-- reference: a vector is written as a delete plus an insert here, and a changed function just loses its row.
CREATE TABLE IF NOT EXISTS artifact_embeddings (
    function_id TEXT PRIMARY KEY REFERENCES artifacts(function_id),
    embedding FLOAT[] NOT NULL
);

-- pending_removals: functions that left the source and whose rows are to be deleted. A scan records them in the
-- same transaction as everything else and finishes the deletion right after; whatever a crash leaves is finished
-- at the next worker start. No foreign key: the function row is deleted last, while this row still names it.
CREATE TABLE IF NOT EXISTS pending_removals (
    function_id TEXT PRIMARY KEY
);

-- function_declarations: C++ declarations without a body (a header's `int mul(int, int) const;`). A function is an
-- index entry only where it has a body, so a declaration whose definition exists elsewhere in the project is an
-- alias of it (definition_id) and has no artifact row; a declaration with no definition anywhere (a library header)
-- stays an artifact of its own and has definition_id NULL. The columns repeat what is needed to make the artifact
-- again if its definition is removed. No foreign keys: it names functions that come and go.
CREATE TABLE IF NOT EXISTS function_declarations (
    function_id TEXT PRIMARY KEY,
    file_path TEXT NOT NULL,
    function_name TEXT NOT NULL,
    signature TEXT NOT NULL,
    body TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    language TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    parent TEXT,
    definition_id TEXT
);

-- Documents (Markdown) as searchable sections, linked to the code they name. Derived data: no foreign keys, rewritten freely.
CREATE TABLE IF NOT EXISTS doc_sections (
    section_id TEXT PRIMARY KEY,            -- <file>::<heading path>#<8 hex>
    file_path TEXT NOT NULL,
    level INTEGER NOT NULL,
    title TEXT NOT NULL,
    heading_path TEXT NOT NULL,
    parent_id TEXT,
    prev_id TEXT,
    next_id TEXT,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    keywords TEXT,                          -- JSON list
    important TEXT,                         -- JSON list
    mentions TEXT,                          -- JSON list of the names, paths and identifiers the section contains
    code_blocks TEXT,                       -- JSON: language and text, truncated
    front_matter TEXT                       -- JSON, first section only
);

CREATE TABLE IF NOT EXISTS doc_links (
    section_id TEXT NOT NULL,
    function_id TEXT NOT NULL,
    kind TEXT NOT NULL,                     -- id, qualified, path+name, name, fence-call, fuzzy
    confidence TEXT NOT NULL,               -- EXTRACTED, INFERRED, AMBIGUOUS
    score DOUBLE NOT NULL,
    state TEXT NOT NULL DEFAULT 'ok',       -- ok, drifted, renamed, removed
    reason TEXT,
    in_code BOOLEAN DEFAULT FALSE,          -- the mention is inside a code block or a table
    code_hash_at_link TEXT,
    signature_at_link TEXT,
    previous_id TEXT,
    changed_at TIMESTAMP,
    PRIMARY KEY (section_id, function_id)
);

CREATE TABLE IF NOT EXISTS doc_file_links (
    section_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    kind TEXT NOT NULL,                     -- file, near
    confidence TEXT NOT NULL,
    file_hash_at_link TEXT,
    PRIMARY KEY (section_id, file_path)
);

-- contracts: Stores generated contracts for functions
CREATE TABLE IF NOT EXISTS contracts (
    function_id TEXT PRIMARY KEY REFERENCES artifacts(function_id),
    contract_json TEXT NOT NULL,            -- JSON contract data (Pydantic validated)
    confidence_score REAL DEFAULT 0.0,      -- 0.0 to 1.0 confidence score
    verification_script TEXT,               -- Generated Python verification script
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- dependencies: Caller/callee relationships between functions
CREATE TABLE IF NOT EXISTS dependencies (
    caller_id TEXT NOT NULL REFERENCES artifacts(function_id),
    callee_id TEXT NOT NULL REFERENCES artifacts(function_id),
    call_site TEXT,                         -- call expression text(s) for this pair (concatenated if multiple)
    edge_hash TEXT,                          -- sha256(caller.code_hash + callee.code_hash + call_site)
    embedding FLOAT[],                      -- local embedding of `call_site` (384 values; a list, not a fixed array)
    confidence TEXT,                        -- EXTRACTED, INFERRED, AMBIGUOUS
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (caller_id, callee_id)
);

-- code_references: Non-call dependencies between functions (design.md §9).
-- Starts narrowly with Python @property reads only. Kept as a separate
-- table (named `code_references`, not `references` — a reserved SQL
-- keyword) rather than a new column on `dependencies`, so the proven
-- `calls`/`dependencies` schema and resolver stay completely untouched,
-- and a call and a property read between the same two functions can
-- coexist without colliding on one shared PRIMARY KEY.
CREATE TABLE IF NOT EXISTS code_references (
    caller_id TEXT NOT NULL REFERENCES artifacts(function_id),
    callee_id TEXT NOT NULL REFERENCES artifacts(function_id),
    reference_site TEXT,                    -- reference expression text(s) for this pair (concatenated if multiple)
    edge_hash TEXT,                          -- sha256(caller.code_hash + callee.code_hash + reference_site)
    embedding FLOAT[],                      -- local embedding of `reference_site` (384 values; a list, not a fixed array)
    confidence TEXT,                        -- EXTRACTED only, for now — receiver type is never guessed (§9), so there's no INFERRED/AMBIGUOUS tier yet
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (caller_id, callee_id)
);

-- member_references: reads and writes of a member or field of a class (C++ first), by the function that does them.
-- A field has no function id, so it is not a node and not a row of `dependencies`/`code_references` (their call and
-- reference counts do not change): one row per access, keyed by the function, the class that declares the member, the
-- member, the kind and the line. `owner_class` is '' when the receiver's type is not known (an unresolved access: the member
-- name is kept, the class is not guessed). No foreign key: the rows of a function are deleted with it and replaced when it
-- is read again (like its call edges), by `caller_id`.
CREATE TABLE IF NOT EXISTS member_references (
    caller_id TEXT NOT NULL,
    owner_class TEXT NOT NULL,              -- the class (qualified name) that declares the member; '' when unknown
    member TEXT NOT NULL,
    kind TEXT NOT NULL,                     -- read, write or readwrite
    file_path TEXT NOT NULL,
    line INTEGER NOT NULL,
    site TEXT,                              -- the expression as written (`r.data`, `this->ok`, `count`)
    receiver TEXT,                          -- the receiver as written, for an unresolved access ('' when it is too complex)
    written_through TEXT                    -- for the base of a chain whose last member is written: the members written through (`c.d`); NULL or '' otherwise
);  -- no PRIMARY KEY: its index costs blocks when empty and time on every insert; `replace_member_references` writes one row per access
-- An index made before `written_through` gets the column once, here (NULL for its rows until their functions are read again).
ALTER TABLE member_references ADD COLUMN IF NOT EXISTS written_through TEXT;

-- config_keys: the leaves of the YAML config files of the project (ROS parameters and the like), one row per key: the key path
-- joined with `/` (normalized: `avoidance/retry/count`), its line and its value (clipped). Written and removed with the file's
-- snapshot (`core/config_keys.py`); no primary key, like member_references.
CREATE TABLE IF NOT EXISTS config_keys (
    file_path TEXT NOT NULL,
    key TEXT NOT NULL,
    line INTEGER NOT NULL,
    value TEXT NOT NULL
);
-- config_files: one row per YAML file that was read: how many keys it has, and why it has none (`problem`: the first syntax
-- error as JSON, `{"line", "message"}`; `skipped`: why it was not read, for example over the size limit). A file with a row here
-- is listed by `strings get` and `status`.
CREATE TABLE IF NOT EXISTS config_files (
    file_path TEXT NOT NULL,
    keys INTEGER NOT NULL,
    problem TEXT,
    skipped TEXT
);

-- reasoning_traces: Audit trail for agent decisions
-- Note: DuckDB uses sequences for auto-increment; we omit id from inserts
CREATE SEQUENCE IF NOT EXISTS seq_reasoning_traces_id START 1;

CREATE TABLE IF NOT EXISTS reasoning_traces (
    id INTEGER PRIMARY KEY DEFAULT nextval('seq_reasoning_traces_id'),
    function_id TEXT NOT NULL REFERENCES artifacts(function_id),
    agent TEXT NOT NULL,                    -- agent name: librarian, proposer, critic, judge, debugger
    trace_json TEXT NOT NULL,               -- JSON trace data
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- config: Application configuration settings
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,                   -- configuration key
    value TEXT NOT NULL,                    -- configuration value (JSON or plain text)
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- scanned_files: one row per file `drspec scan` has ever successfully
-- processed, recording its on-disk mtime/size *as of that scan* (design.md
-- §22). Populated as a side effect of scan's own normal file-walking — the
-- stat info is free, since the file is already being read/parsed — so this
-- adds no extra I/O to `drspec scan` itself. Purely a snapshot, not a
-- staleness signal on its own; the background worker is what compares it
-- against the file's *current* on-disk state to detect drift.
CREATE TABLE IF NOT EXISTS scanned_files (
    file_path TEXT PRIMARY KEY,
    mtime DOUBLE NOT NULL,
    size BIGINT NOT NULL,
    last_scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- staleness_queue: files the background worker's cheap mtime/size check
-- (against `scanned_files`) found drifted from their last-known on-disk
-- state, not yet re-verified for real (design.md §22). A row's mere
-- presence means "possibly stale" — the worker deletes it once it has
-- re-hashed/re-synced that file for real via the same incremental-scan
-- machinery `drspec scan` already uses. Detection and remediation are
-- deliberately decoupled (§22's own "decided already" note): this table
-- only ever records *detection*; it's not a work queue with a status
-- column, since nothing else ever reads a stale, abandoned "PROCESSING"
-- state from a previous worker crash.
CREATE TABLE IF NOT EXISTS staleness_queue (
    file_path TEXT PRIMARY KEY,
    flagged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    reason TEXT                             -- e.g. "mtime_changed", "size_changed", "missing"
);

-- scan_requests: the files a waiting `drspec scan` asked the worker to read, with the options that scan was
-- given. One row per file (a file asked for by two scans belongs to the later one); the row goes when the file
-- has been published, failed, or the scan was cancelled. A scan is finished when none of its rows are left.
CREATE TABLE IF NOT EXISTS scan_requests (
    file_path TEXT PRIMARY KEY,
    scan_id TEXT NOT NULL,
    deep BOOLEAN NOT NULL DEFAULT FALSE,
    embed BOOLEAN NOT NULL DEFAULT FALSE,
    full_scan BOOLEAN NOT NULL DEFAULT FALSE
);

-- rescan_backoff: the files the worker could not read, and their last error.
-- A failing file stays in `staleness_queue`, is not read again while its mtime
-- and size are still the `failed_*` values, and is read again once it is edited
-- or a facts version changes (both clear the row). `retry_at` is the time of the
-- failure; nothing waits on it.
CREATE TABLE IF NOT EXISTS rescan_backoff (
    file_path TEXT PRIMARY KEY,
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_at DOUBLE NOT NULL,
    last_error TEXT,
    failed_mtime DOUBLE,
    failed_size BIGINT
);

-- usage_stats: how often search / source_get / deps_get returned each node,
-- edge and reference. Clients only add to `pending`; the background worker
-- folds it into `score` (decayed by `as_of`, epoch seconds), which decides what
-- gets embedded. For edges and references `item_id` is the caller and
-- `related_id` the callee; for nodes `related_id` is ''.
CREATE TABLE IF NOT EXISTS usage_stats (
    kind TEXT NOT NULL,                     -- node | edge | reference
    item_id TEXT NOT NULL,
    related_id TEXT NOT NULL DEFAULT '',
    pending DOUBLE NOT NULL DEFAULT 0,
    score DOUBLE NOT NULL DEFAULT 0,
    as_of DOUBLE NOT NULL DEFAULT 0,
    PRIMARY KEY (kind, item_id, related_id)
);

-- vision_findings: Visual analysis findings from Vision Analyst
-- Note: DuckDB uses sequences for auto-increment; we omit id from inserts
CREATE SEQUENCE IF NOT EXISTS seq_vision_findings_id START 1;

CREATE TABLE IF NOT EXISTS vision_findings (
    id INTEGER PRIMARY KEY DEFAULT nextval('seq_vision_findings_id'),
    function_id TEXT NOT NULL REFERENCES artifacts(function_id),
    finding_type TEXT NOT NULL,             -- outlier, discontinuity, boundary, correlation, missing_pattern
    significance TEXT NOT NULL,             -- HIGH, MEDIUM, LOW
    description TEXT NOT NULL,              -- Description of the finding
    location TEXT,                          -- Where in the plot (x range, cluster, etc.)
    invariant_implication TEXT,             -- Suggested invariant change
    status TEXT DEFAULT 'NEW',              -- NEW, ADDRESSED, IGNORED
    resolution_note TEXT,                   -- How it was addressed or why ignored
    plot_path TEXT,                         -- Path to the plot image
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for common queries
-- artifacts has no secondary indexes on purpose: DuckDB rewrites an UPDATE of an indexed column as a delete
-- plus an insert, which a row other tables reference by foreign key cannot survive.
CREATE INDEX IF NOT EXISTS idx_contracts_confidence ON contracts(confidence_score);
CREATE INDEX IF NOT EXISTS idx_dependencies_callee ON dependencies(callee_id);
CREATE INDEX IF NOT EXISTS idx_code_references_callee ON code_references(callee_id);
CREATE INDEX IF NOT EXISTS idx_reasoning_traces_function ON reasoning_traces(function_id);
CREATE INDEX IF NOT EXISTS idx_reasoning_traces_agent ON reasoning_traces(agent);
CREATE INDEX IF NOT EXISTS idx_vision_findings_function ON vision_findings(function_id);
CREATE INDEX IF NOT EXISTS idx_vision_findings_status ON vision_findings(status);
