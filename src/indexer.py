"""
Ultra-fast local index for consolidated agent memories.

Uses SQLite with FTS5 for full-text search + optional vector embeddings
for semantic similarity. Zero external services — everything runs locally.

Schema:
  entries: core fact/decision/preference storage
  entries_fts: FTS5 virtual table for full-text search
  embeddings: optional vector storage for semantic search
  sources: track which agent sessions have been processed

Schema versioning:
  PRAGMA user_version tracks the schema version.
  Migrations run automatically on connection.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ─── Schema versioning ───────────────────────────────────────────────────

SCHEMA_VERSION = 3  # Increment when schema changes

# Schema migrations: version -> SQL to execute
SCHEMA_MIGRATIONS = {
    1: """
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            content_hash TEXT UNIQUE NOT NULL,
            category TEXT NOT NULL DEFAULT 'fact',
            source_agent TEXT NOT NULL DEFAULT '',
            source_session TEXT NOT NULL DEFAULT '',
            importance REAL NOT NULL DEFAULT 0.5,
            created_at TEXT NOT NULL DEFAULT '',
            last_referenced TEXT NOT NULL DEFAULT '',
            reference_count INTEGER NOT NULL DEFAULT 0,
            tags TEXT NOT NULL DEFAULT '[]',
            metadata TEXT NOT NULL DEFAULT '{}'
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
            content,
            category,
            source_agent,
            tags,
            tokenize='trigram',
            content='entries',
            content_rowid='id'
        );

        CREATE TABLE IF NOT EXISTS sources (
            source_agent TEXT NOT NULL,
            source_session TEXT NOT NULL,
            processed_at TEXT NOT NULL,
            message_count INTEGER DEFAULT 0,
            PRIMARY KEY (source_agent, source_session)
        );

        CREATE TABLE IF NOT EXISTS embeddings (
            entry_id INTEGER PRIMARY KEY REFERENCES entries(id),
            vector BLOB NOT NULL,
            model TEXT NOT NULL DEFAULT '',
            dimensions INTEGER NOT NULL DEFAULT 384
        );

        CREATE INDEX IF NOT EXISTS idx_entries_category ON entries(category);
        CREATE INDEX IF NOT EXISTS idx_entries_source ON entries(source_agent);
        CREATE INDEX IF NOT EXISTS idx_entries_importance ON entries(importance DESC);
        CREATE INDEX IF NOT EXISTS idx_entries_created ON entries(created_at DESC);
    """,
    2: """
        -- Schema metadata table for future use
        CREATE TABLE IF NOT EXISTS index_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        INSERT OR IGNORE INTO index_meta (key, value) VALUES ('hash_algorithm', 'sha256_16');
        INSERT OR IGNORE INTO index_meta (key, value) VALUES ('hash_algorithm_version', '1');
        INSERT OR IGNORE INTO index_meta (key, value) VALUES ('schema_version', '2');
    """,
    3: """
        -- Structured Decisions (Module B): rationale, framework provenance, outcome verification
        CREATE TABLE IF NOT EXISTS structured_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            -- Core identity
            content_hash TEXT NOT NULL UNIQUE,          -- sha256[:16] of decision text
            decision_text TEXT NOT NULL,               -- WHAT we decided
            -- Rationale (the WHY)
            rationale TEXT,                            -- why this choice over alternatives
            framework_used TEXT,                       -- which decision framework applied
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
            discipline TEXT,
            description TEXT
        );

        -- Seed decision frameworks (generic engineering/AgentOps disciplines)
        INSERT OR IGNORE INTO frameworks (name, discipline, description)
        VALUES ('tradeoff_matrix', 'Systems trade-off analysis', 'Systematic trade-off analysis: cloud vs self-host, consistency vs availability');
        INSERT OR IGNORE INTO frameworks (name, discipline, description)
        VALUES ('failure_modes', 'Distributed failure analysis', 'Failure mode analysis: network, clock, and Byzantine faults');
        INSERT OR IGNORE INTO frameworks (name, discipline, description)
        VALUES ('end_to_end', 'End-to-end integrity', 'End-to-end verification: trust-but-verify principle for distributed systems');
        INSERT OR IGNORE INTO frameworks (name, discipline, description)
        VALUES ('ethics_triage', 'Privacy & ethics review', 'Data minimization, consent, and privacy impact triage');
        INSERT OR IGNORE INTO frameworks (name, discipline, description)
        VALUES ('agentops_trajectory', 'Agent trajectory evaluation', 'Agent reasoning trajectory evaluation and confidence calibration');

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

        -- Update index_meta
        INSERT OR IGNORE INTO index_meta (key, value) VALUES ('schema_version', '3');

        -- Rebuild FTS5 index to ensure consistency after schema migration
        INSERT INTO entries_fts(entries_fts) VALUES('rebuild');
    """,
}


@dataclass
class MemoryEntry:
    """A single consolidated memory fact."""
    id: Optional[int] = None
    content: str = ""               # The fact/decision/preference
    category: str = "fact"          # fact, decision, preference, lesson, project, person
    source_agent: str = ""          # claude-code, codex, gemini, etc.
    source_session: str = ""        # session_id where this was extracted
    importance: float = 0.5         # 0.0-1.0 computed importance
    created_at: str = ""
    last_referenced: str = ""
    reference_count: int = 0
    tags: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


class MemoryIndex:
    """SQLite+FTS5 index for consolidated agent memories."""

    def __init__(self, db_path: str | Path, auto_migrate: bool = True):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
        self.conn.execute("PRAGMA journal_size_limit=67108864")  # 64MB WAL limit
        if auto_migrate:
            self._init_schema()

    def _init_schema(self):
        """Create tables and run migrations if needed."""
        current_version = self._get_user_version()

        if current_version == 0:
            # Fresh install — run all migrations
            for version in sorted(SCHEMA_MIGRATIONS.keys()):
                self._run_migration(version)
            self._set_user_version(SCHEMA_VERSION)
            self._add_fts_triggers()
        elif current_version < SCHEMA_VERSION:
            # Needs migration
            for version in range(current_version + 1, SCHEMA_VERSION + 1):
                if version in SCHEMA_MIGRATIONS:
                    self._run_migration(version)
            self._set_user_version(SCHEMA_VERSION)
        elif current_version > SCHEMA_VERSION:
            logger.warning(
                "Index schema v%d is newer than code v%d. "
                "Upgrade the plugin to avoid compatibility issues.",
                current_version, SCHEMA_VERSION,
            )

        self.conn.commit()

    def _get_user_version(self) -> int:
        row = self.conn.execute("PRAGMA user_version").fetchone()
        return row[0] if row else 0

    def _set_user_version(self, version: int):
        self.conn.execute(f"PRAGMA user_version = {version}")

    def _run_migration(self, version: int):
        sql = SCHEMA_MIGRATIONS.get(version)
        if sql:
            logger.info("Running schema migration v%d", version)
            self.conn.executescript(sql)

    def _add_fts_triggers(self):
        """Add FTS sync triggers (separate from migration SQL for clarity)."""
        self.conn.executescript("""
            CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN
                INSERT INTO entries_fts(rowid, content, category, source_agent, tags)
                VALUES (new.id, new.content, new.category, new.source_agent, new.tags);
            END;

            CREATE TRIGGER IF NOT EXISTS entries_ad AFTER DELETE ON entries BEGIN
                INSERT INTO entries_fts(entries_fts, rowid, content, category, source_agent, tags)
                VALUES ('delete', old.id, old.content, old.category, old.source_agent, old.tags);
            END;

            CREATE TRIGGER IF NOT EXISTS entries_au AFTER UPDATE ON entries BEGIN
                INSERT INTO entries_fts(entries_fts, rowid, content, category, source_agent, tags)
                VALUES ('delete', old.id, old.content, old.category, old.source_agent, old.tags);
                INSERT INTO entries_fts(rowid, content, category, source_agent, tags)
                VALUES (new.id, new.content, new.category, new.source_agent, new.tags);
            END;
        """)

    # ─── CRUD ────────────────────────────────────────────────────────────

    VALID_CATEGORIES = frozenset({'fact', 'decision', 'preference', 'lesson', 'project', 'person'})

    @staticmethod
    def hash_content(content: str) -> str:
        """Hash content for deduplication.

        Uses sha256[:16] (version 1). If algorithm changes, old entries
        are still findable via FTS search — the hash only affects dedup.
        Version stored in index_meta for migration tracking.
        """
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def upsert(self, entry: MemoryEntry) -> int:
        """Insert or update a memory entry. Returns the entry ID.

        Validates: content length ≥ 3, importance 0.0-1.0, valid category.
        """
        # Input validation
        if not entry.content or len(entry.content.strip()) < 3:
            raise ValueError(f"Entry content must be at least 3 characters, got: {entry.content!r}")
        if entry.importance < 0.0 or entry.importance > 1.0:
            raise ValueError(f"Importance must be 0.0-1.0, got: {entry.importance}")
        if entry.category not in self.VALID_CATEGORIES:
            raise ValueError(f"Invalid category {entry.category!r}. Must be one of: {sorted(self.VALID_CATEGORIES)}")

        content_hash = self.hash_content(entry.content)
        now = datetime.now(timezone.utc).isoformat()

        existing = self.conn.execute(
            "SELECT id FROM entries WHERE content_hash = ?",
            (content_hash,)
        ).fetchone()

        if existing:
            entry_id = existing[0]
            self.conn.execute("""
                UPDATE entries SET
                    importance = MAX(importance, ?),
                    last_referenced = ?,
                    reference_count = reference_count + 1,
                    tags = ?,
                    metadata = ?
                WHERE id = ?
            """, (
                entry.importance,
                now,
                json.dumps(entry.tags),
                json.dumps(entry.metadata),
                entry_id,
            ))
        else:
            cursor = self.conn.execute("""
                INSERT INTO entries (content, content_hash, category, source_agent,
                    source_session, importance, created_at, last_referenced, tags, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                entry.content,
                content_hash,
                entry.category,
                entry.source_agent,
                entry.source_session,
                entry.importance,
                entry.created_at or now,
                now,
                json.dumps(entry.tags),
                json.dumps(entry.metadata),
            ))
            entry_id = cursor.lastrowid

        self.conn.commit()
        return entry_id

    def mark_source_processed(self, source_agent: str, source_session: str, message_count: int = 0):
        """Record that a source session has been processed."""
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute("""
            INSERT OR REPLACE INTO sources (source_agent, source_session, processed_at, message_count)
            VALUES (?, ?, ?, ?)
        """, (source_agent, source_session, now, message_count))
        self.conn.commit()

    def is_source_processed(self, source_agent: str, source_session: str) -> bool:
        """Check if a source session has already been processed."""
        row = self.conn.execute(
            "SELECT 1 FROM sources WHERE source_agent = ? AND source_session = ?",
            (source_agent, source_session)
        ).fetchone()
        return row is not None

    # ─── Structured decisions (the Decide → Remember → Verify loop) ──────────

    def upsert_decision(
        self,
        decision_text: str,
        *,
        agent_source: str,
        rationale: str | None = None,
        framework_used: str | None = None,
        alternatives: list[str] | str | None = None,
        constraints: str | None = None,
        session_id: str | None = None,
        decision_log_id: str | None = None,
        confidence: float = 1.0,
    ) -> int:
        """Insert/refresh a structured decision (dedup by content hash).

        This is the Memory Bridge half of the decision loop: decisions
        surfaced from agent conversations are stored with their rationale,
        the framework that shaped them, and a slot for later outcome
        verification. Returns the structured_decisions row id.
        """
        decision_text = (decision_text or "").strip()
        if len(decision_text) < 3:
            raise ValueError(f"decision_text must be at least 3 characters, got: {decision_text!r}")
        confidence = max(0.0, min(1.0, float(confidence)))
        if isinstance(alternatives, list):
            alternatives = json.dumps(alternatives)

        content_hash = self.hash_content(decision_text)
        now = datetime.now(timezone.utc).isoformat()

        existing = self.conn.execute(
            "SELECT id FROM structured_decisions WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()
        if existing:
            decision_id = existing[0]
            # Enrich an existing record without clobbering known values.
            self.conn.execute("""
                UPDATE structured_decisions SET
                    rationale = COALESCE(?, rationale),
                    framework_used = COALESCE(NULLIF(?, 'unknown'), framework_used),
                    alternatives_considered = COALESCE(?, alternatives_considered),
                    constraints = COALESCE(?, constraints),
                    decision_log_id = COALESCE(?, decision_log_id),
                    confidence = MAX(confidence, ?)
                WHERE id = ?
            """, (rationale, framework_used, alternatives, constraints,
                  decision_log_id, confidence, decision_id))
        else:
            cur = self.conn.execute("""
                INSERT INTO structured_decisions (
                    content_hash, decision_text, rationale, framework_used,
                    alternatives_considered, constraints, agent_source, session_id,
                    decision_log_id, confidence, extracted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (content_hash, decision_text, rationale, framework_used or "unknown",
                  alternatives, constraints, agent_source, session_id,
                  decision_log_id, confidence, now))
            decision_id = cur.lastrowid
        self.conn.commit()
        return decision_id

    def get_decisions(
        self,
        limit: int = 50,
        framework: str | None = None,
        unverified_only: bool = False,
    ) -> list[dict]:
        """Return structured decisions, newest first, as dicts."""
        clauses, params = ["archived = 0"], []
        if framework:
            clauses.append("framework_used = ?")
            params.append(framework)
        if unverified_only:
            clauses.append("outcome_verified = 0")
        where = " AND ".join(clauses)
        params.append(limit)
        cur = self.conn.execute(
            f"SELECT * FROM structured_decisions WHERE {where} "
            f"ORDER BY extracted_at DESC LIMIT ?",
            params,
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def mark_decision_outcome(self, decision_id: int, verified: int, notes: str | None = None) -> bool:
        """Record how a past decision turned out (the 'Verify' step).

        verified: 1 = worked out, -1 = went badly, 0 = back to unverified.
        """
        if verified not in (-1, 0, 1):
            raise ValueError("verified must be -1, 0, or 1")
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute("""
            UPDATE structured_decisions
            SET outcome_verified = ?, outcome_notes = ?, outcome_checked_at = ?
            WHERE id = ?
        """, (verified, notes, now, decision_id))
        self.conn.commit()
        return cur.rowcount > 0

    def list_frameworks(self) -> list[dict]:
        """Return the framework catalog (decision disciplines)."""
        cur = self.conn.execute(
            "SELECT name, discipline, description FROM frameworks ORDER BY name"
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    # ─── Search ───────────────────────────────────────────────────────────

    _FTS_SELECT = """
        SELECT e.id, e.content, e.category, e.source_agent, e.source_session,
               e.importance, e.created_at, e.last_referenced, e.reference_count,
               e.tags, e.metadata
        FROM entries_fts f
        JOIN entries e ON f.rowid = e.id
        WHERE entries_fts MATCH ?
        ORDER BY rank
        LIMIT ?
    """

    def search_fts(self, query: str, limit: int = 20) -> list[MemoryEntry]:
        """Full-text search using FTS5 with trigram tokenizer.

        Exact (substring) matches are tried first. If they return nothing,
        a trigram-decomposition fuzzy pass runs so typos and transpositions
        still surface results (e.g. "clouflare" → "cloudflare").
        """
        # Escape FTS5 special characters and handle multi-word queries
        safe_query = _escape_fts5_query(query)
        try:
            rows = self.conn.execute(self._FTS_SELECT, (safe_query, limit)).fetchall()

            # Typo-tolerant fallback: decompose into overlapping trigrams and
            # OR-match. Trigram tokenizer needs no extra deps for this.
            if not rows and query.strip():
                fuzzy_query = _trigram_fuzzy_query(query)
                if fuzzy_query:
                    rows = self.conn.execute(
                        self._FTS_SELECT, (fuzzy_query, limit)
                    ).fetchall()
        except Exception:
            # If FTS5 syntax error, fall back to LIKE search
            like_query = f"%{query.replace('%', '%%')}%"
            rows = self.conn.execute("""
                SELECT id, content, category, source_agent, source_session,
                       importance, created_at, last_referenced, reference_count, tags, metadata
                FROM entries
                WHERE content LIKE ?
                ORDER BY importance DESC
                LIMIT ?
            """, (like_query, limit)).fetchall()

        return [self._row_to_entry(r) for r in rows]

    def search_semantic(self, query_embedding: list[float], limit: int = 20) -> list[MemoryEntry]:
        """Semantic search using cosine similarity on stored embeddings.

        query_embedding should be a list of floats matching the stored dimensions.
        Falls back to FTS if no embeddings are stored.
        """
        if not query_embedding:
            return []

        # Compute cosine similarity against all stored embeddings
        # For efficiency with small datasets (<100K), compute in Python
        rows = self.conn.execute("""
            SELECT e.id, e.content, e.category, e.source_agent, e.source_session,
                   e.importance, e.created_at, e.last_referenced, e.reference_count,
                   e.tags, e.metadata, emb.vector
            FROM embeddings emb
            JOIN entries e ON emb.entry_id = e.id
            ORDER BY e.importance DESC
            LIMIT 5000
        """).fetchall()

        results = []
        for row in rows:
            stored_vec = _decode_vector(row[10])
            if stored_vec and len(stored_vec) == len(query_embedding):
                similarity = _cosine_similarity(query_embedding, stored_vec)
                entry = self._row_to_entry(row[:10])
                entry.metadata["_similarity"] = similarity
                results.append((similarity, entry))

        results.sort(key=lambda x: x[0], reverse=True)
        return [entry for _, entry in results[:limit]]

    def search_hybrid(self, query: str, query_embedding: list[float] | None = None, limit: int = 20) -> list[MemoryEntry]:
        """Combined FTS + semantic search. Falls back gracefully."""
        fts_results = self.search_fts(query, limit=limit * 2) if query else []
        sem_results = self.search_semantic(query_embedding, limit=limit * 2) if query_embedding else []

        # Merge: FTS results first, then semantic results not already in FTS
        seen = set()
        merged = []
        for entry in fts_results + sem_results:
            if entry.id not in seen:
                seen.add(entry.id)
                merged.append(entry)
        return merged[:limit]

    def get_recent(self, limit: int = 20) -> list[MemoryEntry]:
        """Get most recently referenced entries."""
        rows = self.conn.execute("""
            SELECT id, content, category, source_agent, source_session,
                   importance, created_at, last_referenced, reference_count, tags, metadata
            FROM entries
            ORDER BY last_referenced DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def get_by_category(self, category: str, limit: int = 50) -> list[MemoryEntry]:
        """Get entries by category."""
        rows = self.conn.execute("""
            SELECT id, content, category, source_agent, source_session,
                   importance, created_at, last_referenced, reference_count, tags, metadata
            FROM entries
            WHERE category = ?
            ORDER BY importance DESC
            LIMIT ?
        """, (category, limit)).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def recall(
        self,
        agent: str | None = None,
        since: str | None = None,
        until: str | None = None,
        query: str | None = None,
        limit: int = 30,
    ) -> list[MemoryEntry]:
        """Scoped recall: 'what did I do with <agent> between <since> and <until>'.

        Filters by source agent (substring, case-insensitive) and an ISO
        created_at date range, optionally narrowed by a full-text query. This
        is the retrieval shape vague/temporal questions actually need — the one
        plain FTS could not serve. All inputs are bound parameters.
        """
        clauses, params = [], []
        if agent:
            clauses.append("LOWER(source_agent) LIKE ?")
            params.append(f"%{agent.lower()}%")
        if since:
            clauses.append("created_at >= ?")
            params.append(since)
        if until:
            clauses.append("created_at <= ?")
            params.append(until)

        if query and query.strip():
            ids = [e.id for e in self.search_fts(query, limit=limit * 3) if e.id is not None]
            if not ids:
                return []
            clauses.append(f"id IN ({','.join('?' for _ in ids)})")
            params.extend(ids)

        where = " AND ".join(clauses) if clauses else "1=1"
        params.append(limit)
        rows = self.conn.execute(f"""
            SELECT id, content, category, source_agent, source_session,
                   importance, created_at, last_referenced, reference_count, tags, metadata
            FROM entries
            WHERE {where}
            ORDER BY created_at DESC, importance DESC
            LIMIT ?
        """, params).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def recall_decisions(
        self,
        agent: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Structured decisions scoped by agent + date range (Council ⨉ Bridge).

        Lets 'what did I decide with <agent> on <date>' return the actual
        decisions and the framework that shaped each — not just loose facts.
        """
        clauses, params = ["archived = 0"], []
        if agent:
            clauses.append("LOWER(agent_source) LIKE ?")
            params.append(f"%{agent.lower()}%")
        if since:
            clauses.append("extracted_at >= ?")
            params.append(since)
        if until:
            clauses.append("extracted_at <= ?")
            params.append(until)
        where = " AND ".join(clauses)
        params.append(limit)
        try:
            cur = self.conn.execute(
                f"SELECT * FROM structured_decisions WHERE {where} "
                f"ORDER BY extracted_at DESC LIMIT ?",
                params,
            )
        except sqlite3.OperationalError:
            return []  # pre-v3 index
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    # ─── Integrity & repair ──────────────────────────────────────────────

    def integrity_check(self) -> list[str]:
        """Run PRAGMA integrity_check. Returns list of issues (empty = clean)."""
        try:
            row = self.conn.execute("PRAGMA integrity_check").fetchone()
            result = row[0] if row else "ok"
            if result == "ok":
                return []
            return [result]
        except Exception as e:
            return [f"Integrity check failed: {e}"]

    def repair(self) -> bool:
        """Attempt to repair a corrupted index.

        Backs up current db, recreates schema, and vacuums.
        Returns True if successful.
        """
        try:
            # Backup first
            backup_path = self.db_path.with_suffix(".db.corrupted")
            import shutil
            if self.db_path.exists():
                shutil.copy2(str(self.db_path), str(backup_path))
                logger.info("Backed up corrupted index to %s", backup_path)

            # Close existing connection
            self.close()

            # Recreate
            self.conn = sqlite3.connect(str(self.db_path))
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA cache_size=-64000")
            self.conn.execute("PRAGMA journal_size_limit=67108864")

            # Re-init schema
            self._init_schema()

            logger.info("Index repaired successfully")
            return True
        except Exception as e:
            logger.error("Failed to repair index: %s", e)
            return False

    def vacuum(self):
        """Run VACUUM to reclaim space and defragment."""
        try:
            self.conn.execute("VACUUM")
            logger.info("VACUUM completed on %s", self.db_path)
        except Exception as e:
            logger.warning("VACUUM failed: %s", e)

    # ─── Export / Import ─────────────────────────────────────────────────

    def export_to(self, export_path: str | Path) -> int:
        """Export the index to a tar.gz file.

        Returns the size in bytes of the export file.
        """
        import tarfile

        export_path = Path(export_path)
        self.conn.commit()

        # Ensure WAL is checkpointed
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        with tarfile.open(str(export_path), "w:gz") as tar:
            tar.add(str(self.db_path), arcname="index.db")
            # Also include WAL/SHM if present
            for ext in [".db-wal", ".db-shm"]:
                p = self.db_path.with_suffix(ext)
                if p.exists():
                    tar.add(str(p), arcname=f"index{ext}")

        return export_path.stat().st_size

    @classmethod
    def import_from(cls, import_path: str | Path, target_path: str | Path) -> MemoryIndex:
        """Import an index from a tar.gz file.

        Returns a new MemoryIndex pointing at the imported data.
        """
        import tarfile

        import_path = Path(import_path)
        target_path = Path(target_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        dest = target_path.parent

        with tarfile.open(str(import_path), "r:gz") as tar:
            _safe_extractall(tar, dest)

        return cls(target_path)

    # ─── Stats ────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        """Return index statistics."""
        total = self.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        by_category = {}
        for row in self.conn.execute(
            "SELECT category, COUNT(*) FROM entries GROUP BY category"
        ).fetchall():
            by_category[row[0]] = row[1]
        by_source = {}
        for row in self.conn.execute(
            "SELECT source_agent, COUNT(*) FROM entries GROUP BY source_agent"
        ).fetchall():
            by_source[row[0]] = row[1]
        processed = self.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]

        # Structured decisions (present from schema v3 onward).
        total_decisions = 0
        verified_decisions = 0
        try:
            total_decisions = self.conn.execute(
                "SELECT COUNT(*) FROM structured_decisions WHERE archived = 0"
            ).fetchone()[0]
            verified_decisions = self.conn.execute(
                "SELECT COUNT(*) FROM structured_decisions WHERE outcome_verified != 0 AND archived = 0"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            pass  # Pre-v3 index — table not present yet.

        # Schema info
        schema_version = self._get_user_version()
        hash_algo = "sha256_16"
        try:
            row = self.conn.execute(
                "SELECT value FROM index_meta WHERE key = 'hash_algorithm'"
            ).fetchone()
            if row:
                hash_algo = row[0]
        except Exception:
            pass

        return {
            "total_entries": total,
            "by_category": by_category,
            "by_source": by_source,
            "processed_sessions": processed,
            "total_decisions": total_decisions,
            "verified_decisions": verified_decisions,
            "schema_version": schema_version,
            "hash_algorithm": hash_algo,
        }

    # ─── Helpers ──────────────────────────────────────────────────────────

    def _row_to_entry(self, row: tuple) -> MemoryEntry:
        return MemoryEntry(
            id=row[0],
            content=row[1],
            category=row[2],
            source_agent=row[3],
            source_session=row[4],
            importance=row[5],
            created_at=row[6],
            last_referenced=row[7],
            reference_count=row[8],
            tags=json.loads(row[9]) if isinstance(row[9], str) else (row[9] or []),
            metadata=json.loads(row[10]) if isinstance(row[10], str) else (row[10] or {}),
        )

    def close(self):
        """Close the database connection explicitly."""
        if self.conn:
            self.conn.close()
            self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


# ─── Vector helpers ──────────────────────────────────────────────────────

def _decode_vector(blob: bytes) -> list[float] | None:
    """Decode a BLOB to a list of floats (simple 4-byte float packing)."""
    try:
        count = len(blob) // 4
        return list(struct.unpack(f'{count}f', blob))
    except Exception:
        return None


def encode_vector(vec: list[float]) -> bytes:
    """Encode a list of floats to a BLOB."""
    return struct.pack(f'{len(vec)}f', *vec)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _trigram_fuzzy_query(query: str) -> str:
    """Build a typo-tolerant FTS5 query by OR-ing overlapping trigrams.

    The trigram tokenizer indexes every 3-char window, so OR-ing a misspelled
    term's trigrams matches entries that share most of them. Results are ranked
    by FTS5 rank, so the closest match floats to the top.
    """
    import re as _re
    terms = [t for t in _re.findall(r'\w+', query.lower()) if len(t) >= 3]
    trigrams: set[str] = set()
    for t in terms:
        for i in range(len(t) - 2):
            trigrams.add(t[i:i + 3])
    if not trigrams:
        return ""
    return " OR ".join(f'"{tg}"' for tg in sorted(trigrams))


def _safe_extractall(tar, dest: str | Path) -> None:
    """Extract a tarball, rejecting members that escape ``dest``.

    Guards against the CVE-2007-4559 path-traversal class: absolute paths,
    ``..`` traversal, and links pointing outside the destination directory.
    Prefers the stdlib ``data`` filter (Python 3.12+/3.11.4+) and validates
    members explicitly so older runtimes are still protected.
    """
    dest = Path(dest).resolve()
    for member in tar.getmembers():
        member_path = (dest / member.name).resolve()
        if os.path.commonpath([str(dest), str(member_path)]) != str(dest):
            raise ValueError(f"Unsafe path in archive (path traversal): {member.name!r}")
        if member.islnk() or member.issym():
            link_target = (dest / member.linkname).resolve()
            if os.path.commonpath([str(dest), str(link_target)]) != str(dest):
                raise ValueError(f"Unsafe link in archive: {member.name!r} -> {member.linkname!r}")
    try:
        tar.extractall(path=str(dest), filter="data")  # type: ignore[call-arg]
    except TypeError:
        # Runtime predates the ``filter`` keyword; members already validated above.
        tar.extractall(path=str(dest))


def _escape_fts5_query(query: str) -> str:
    """Escape FTS5 special characters and produce a safe query string.

    With trigram tokenizer, each term is searched independently.
    Multi-word queries are joined with implicit AND.
    """
    # Strip special FTS5 operators
    for char in '*^(){}[]~':
        query = query.replace(char, ' ')
    # Remove boolean operators as standalone words (any position)
    import re as _re
    query = _re.sub(r'\b(AND|OR|NOT|NEAR)\b', ' ', query, flags=_re.IGNORECASE)
    # Split into terms and quote each
    terms = [t.strip() for t in query.split() if t.strip()]
    if not terms:
        return '""'
    # Quote each term to prevent FTS5 syntax errors
    quoted = ' '.join(f'"{t.replace(chr(34), chr(34)+chr(34))}"' for t in terms)
    return quoted
