-- DrSpec Database Schema
-- Version: 1.0
-- Strategy: Rebuild (no migrations)

-- artifacts: Stores scanned function information
CREATE TABLE IF NOT EXISTS artifacts (
    function_id TEXT PRIMARY KEY,           -- format: filepath::function_name
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
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

-- queue: Processing queue for functions awaiting contract generation
CREATE TABLE IF NOT EXISTS queue (
    function_id TEXT PRIMARY KEY REFERENCES artifacts(function_id),
    priority INTEGER DEFAULT 100,           -- lower = higher priority
    status TEXT DEFAULT 'PENDING',          -- PENDING, PROCESSING, COMPLETED, FAILED
    reason TEXT DEFAULT 'NEW',              -- NEW, HASH_MISMATCH, DEPENDENCY_CHANGED, MANUAL_RETRY
    attempts INTEGER DEFAULT 0,             -- retry count to prevent infinite loops
    max_attempts INTEGER DEFAULT 3,         -- maximum retry attempts
    error_message TEXT,                     -- last error message if failed
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- dependencies: Caller/callee relationships between functions
CREATE TABLE IF NOT EXISTS dependencies (
    caller_id TEXT NOT NULL REFERENCES artifacts(function_id),
    callee_id TEXT NOT NULL REFERENCES artifacts(function_id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (caller_id, callee_id)
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
CREATE INDEX IF NOT EXISTS idx_artifacts_status ON artifacts(status);
CREATE INDEX IF NOT EXISTS idx_artifacts_file_path ON artifacts(file_path);
CREATE INDEX IF NOT EXISTS idx_contracts_confidence ON contracts(confidence_score);
CREATE INDEX IF NOT EXISTS idx_queue_priority ON queue(priority, created_at);
CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status);
CREATE INDEX IF NOT EXISTS idx_dependencies_callee ON dependencies(callee_id);
CREATE INDEX IF NOT EXISTS idx_reasoning_traces_function ON reasoning_traces(function_id);
CREATE INDEX IF NOT EXISTS idx_reasoning_traces_agent ON reasoning_traces(agent);
CREATE INDEX IF NOT EXISTS idx_vision_findings_function ON vision_findings(function_id);
CREATE INDEX IF NOT EXISTS idx_vision_findings_status ON vision_findings(status);

-- Indexes for debugger agent contract queries (Story 5-1)
CREATE INDEX IF NOT EXISTS idx_artifacts_function_name ON artifacts(function_name);
