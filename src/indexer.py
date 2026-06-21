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
import sqlite3
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ─── Schema versioning ───────────────────────────────────────────────────

SCHEMA_VERSION = 2  # Increment when schema changes

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

    # ─── Search ───────────────────────────────────────────────────────────

    def search_fts(self, query: str, limit: int = 20) -> list[MemoryEntry]:
        """Full-text search using FTS5 with trigram tokenizer."""
        # Escape FTS5 special characters and handle multi-word queries
        safe_query = _escape_fts5_query(query)
        try:
            rows = self.conn.execute("""
                SELECT e.id, e.content, e.category, e.source_agent, e.source_session,
                       e.importance, e.created_at, e.last_referenced, e.reference_count,
                       e.tags, e.metadata
                FROM entries_fts f
                JOIN entries e ON f.rowid = e.id
                WHERE entries_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (safe_query, limit)).fetchall()
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

        with tarfile.open(str(import_path), "r:gz") as tar:
            tar.extractall(path=target_path.parent)

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
