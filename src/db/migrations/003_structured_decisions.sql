-- Migration 003: Structured Decisions & Frameworks
-- Schema version bump: 2 -> 3
--
-- Adds structured_decisions table with rationale, framework provenance,
-- outcome verification, and Module A linkage (decision_log_id).
-- Adds frameworks catalog table (user-extensible).
-- Backfills existing decisions from the entries table.

BEGIN TRANSACTION;

-- Structured Decisions (Module B): rationale, framework provenance, outcome verification
CREATE TABLE IF NOT EXISTS structured_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Core identity
    content_hash TEXT NOT NULL UNIQUE,          -- sha256[:16] of decision text
    decision_text TEXT NOT NULL,               -- WHAT we decided
    -- Rationale (the WHY — currently MISSING)
    rationale TEXT,                            -- why this choice over alternatives
    framework_used TEXT,                       -- which DDIA/AgentOps pattern applied
    alternatives_considered TEXT,              -- JSON array of rejected options
    constraints TEXT,                          -- what shaped the decision (budget, time, etc.)
    -- Provenance
    agent_source TEXT NOT NULL,                -- 'claude_code', 'hermes', 'codex', etc.
    session_id TEXT,                           -- for full traceability
    message_offset INTEGER,                    -- approximate location in session
    extracted_at TEXT NOT NULL DEFAULT (datetime('now')),
    -- Module A linkage
    decision_log_id TEXT,                      -- FK to Hermes question engine decision_log.db
    -- Outcome tracking
    outcome_verified INTEGER DEFAULT 0,        -- 0=unverified, 1=verified-good, -1=verified-bad
    outcome_notes TEXT,                        -- what happened when we checked
    outcome_checked_at TEXT,                   -- when we last verified
    -- Lifecycle
    confidence REAL DEFAULT 1.0,               -- extraction confidence (downgrade if unsure)
    reviewed INTEGER DEFAULT 0,                -- has a human reviewed this?
    archived INTEGER DEFAULT 0                 -- soft-delete
);

CREATE INDEX IF NOT EXISTS idx_decisions_source ON structured_decisions(agent_source);
CREATE INDEX IF NOT EXISTS idx_decisions_framework ON structured_decisions(framework_used);
CREATE INDEX IF NOT EXISTS idx_decisions_outcome ON structured_decisions(outcome_verified);
CREATE INDEX IF NOT EXISTS idx_decisions_log_id ON structured_decisions(decision_log_id);

-- Frameworks catalog (user-extensible)
CREATE TABLE IF NOT EXISTS frameworks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    source_book TEXT,
    description TEXT
);

-- Seed DDIA frameworks
INSERT OR IGNORE INTO frameworks (name, source_book, description)
VALUES ('tradeoff_matrix', 'DDIA Ch.1', 'Systematic trade-off analysis: cloud vs self-host, consistency vs availability');
INSERT OR IGNORE INTO frameworks (name, source_book, description)
VALUES ('failure_modes', 'DDIA Ch.9', 'Failure mode analysis: network, clock, Byzantine faults');
INSERT OR IGNORE INTO frameworks (name, source_book, description)
VALUES ('end_to_end', 'DDIA Ch.13', 'End-to-end argument: trust-but-verify principle for distributed systems');
INSERT OR IGNORE INTO frameworks (name, source_book, description)
VALUES ('ethics_triage', 'DDIA Ch.14', 'Data minimization, consent, privacy impact triage');
INSERT OR IGNORE INTO frameworks (name, source_book, description)
VALUES ('agentops_trajectory', 'Google Agent Guide', 'Agent reasoning trajectory evaluation and confidence calibration');

-- Backfill existing decision entries from entries table
INSERT OR IGNORE INTO structured_decisions (
    content_hash, decision_text, framework_used, agent_source, session_id,
    confidence, outcome_verified, extracted_at
)
SELECT
    content_hash, content, 'unknown', source_agent, source_session,
    MIN(0.5, importance), 0, created_at
FROM entries
WHERE category = 'decision';

-- Update schema version in index_meta
INSERT OR IGNORE INTO index_meta (key, value) VALUES ('schema_version', '3');

-- Rebuild FTS5 index to ensure consistency
INSERT INTO entries_fts(entries_fts) VALUES('rebuild');

COMMIT;
