# Antharmaya Memory Bridge — Architecture

## Overview

Memory Bridge is a Hermes MemoryProvider plugin that unifies all AI agent
conversations on your machine into a single, searchable index. It auto-discovers
Claude Code, Codex, Gemini, Cursor, OpenCode, Goose, Aider, Continue.dev, and
Agent Linux Control histories, extracts durable facts, and injects relevant
context into every Hermes session.

## Core Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        SCANNERS                                   │
│  claude_code  codex  gemini  cursor  opencode  goose  aider      │
│  continue_dev  agent_linux_control                                │
│                                                                   │
│  Each scanner: generator fn decorated with @register_scanner()    │
│  Returns: Iterator[Session]                                       │
└───────────────────────────┬──────────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│                        EXTRACTOR                                  │
│                                                                   │
│  SmartExtractor (orchestrator):                                   │
│    PRIMARY:   PluginLlmEngine (ctx.llm — Hermes host model)       │
│    SECONDARY: DirectEngine (API key — OpenRouter/DeepSeek)        │
│    TERTIARY:  FastExtractor (rules-based, always runs)            │
│                                                                   │
│  Low-value session filtering (<3 msgs, all noise → skip LLM)     │
│  Extraction quality metrics tracked per session                   │
└───────────────────────────┬──────────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│                        INDEX                                      │
│                                                                   │
│  SQLite + FTS5 trigram tokenizer                                  │
│  Schema versioning via PRAGMA user_version                        │
│  WAL mode with 64MB journal size limit                            │
│  Entries deduped via sha256[:16] content hash                     │
│  Export/import via tar.gz                                         │
└───────────────────────────┬──────────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│                    MEMORY PROVIDER                                 │
│                                                                   │
│  Hermes Plugin: AntharmayaMemoryProvider                          │
│  - prefetch() — injects relevant memories every turn              │
│  - system_prompt_block() — shows index status                     │
│  - sync_turn() — captures current Hermes conversation             │
│  - 4 tools: search, stats, quality, scan                          │
└──────────────────────────────────────────────────────────────────┘
```

## Design Decisions

### Why SQLite+FTS5 instead of a vector database?

**Decision:** FTS5 trigram tokenizer for v0.1.1, vector embeddings deferred to v0.2.

**Rationale:**
- FTS5 trigram handles partial matches ("photo" matches "PhotoSelect"),
  typo tolerance, and substring search without any dependencies
- BM25 ranking provides quality relevance scoring
- SQLite is zero-dependency (stdlib), zero-configuration
- Vector search adds 80MB+ of model files (all-MiniLM-L6-v2) for marginal
  improvement on this dataset size
- Upgrade path: v0.2 adds sqlite-vec or LanceDB as optional extension

**Tradeoff:** Semantic understanding (synonyms, concept matching) is limited.
"database cost" won't match "PostgreSQL pricing" unless they share trigrams.
Acceptable for v0.1.1 — FTS gives 80% of the value at 0% of the complexity.

### Why ctx.llm over direct API calls?

**Decision:** ctx.llm (Hermes host model) is the PRIMARY extraction path.

**Rationale:**
- Zero configuration — no API key needed, uses user's configured model
- Respects user's budget, rate limits, and model preferences
- Unique competitive advantage (no other memory tool does this)
- Falls back to DirectEngine (API key) or FastExtractor (rules-based)
- The Hermes PluginLlm API provides `complete()`, `chat()`, and `structured()`

**Tradeoff:** ctx.llm may be slower (depends on host model) and may have
different quality characteristics. For users who want deterministic
extraction, FastExtractor is always available as a fallback.

### Why content-hash dedup instead of semantic dedup?

**Decision:** sha256[:16] content hash for deduplication (version 1 in index_meta).

**Rationale:**
- Deterministic, fast, zero-dependency
- Same content from multiple agents maps to same entry
- `importance = MAX(importance, ...)` ensures highest importance wins
- `reference_count` tracks how many times a fact was encountered
- Hash algorithm version stored in `index_meta` for migration

**Tradeoff:** "Use Cloudflare Workers" and "Using Cloudflare Workers for API"
are different hashes even though they describe the same decision.
Acceptable because:
1. LLM extraction (primary path) normalizes content
2. FastExtractor (secondary) uses keyword patterns
3. FTS search finds both variants
4. Semantic dedup is a future enhancement (v0.2+)

### Why generator-based discovery?

**Decision:** `discover_sessions()` yields sessions without accumulating.

**Rationale:**
- Users with 1000+ sessions shouldn't load everything into memory
- Batch processing (default 10) with progress reporting
- Early termination possible (skip already-processed sessions mid-stream)
- Backward compatible: `discover_all()` wraps the generator in a list

### Why schema versioning?

**Decision:** `PRAGMA user_version` + `SCHEMA_MIGRATIONS` dict.

**Rationale:**
- Future schema changes won't break existing indices
- Migrations auto-run on connection
- Forward compatibility warning if code is older than index schema
- `index_meta` table stores hash algorithm version for migration path

## Schema

### entries table

```sql
CREATE TABLE entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    content_hash TEXT UNIQUE NOT NULL,     -- sha256(content)[:16]
    category TEXT NOT NULL DEFAULT 'fact',  -- fact|decision|preference|lesson|project|person
    source_agent TEXT NOT NULL DEFAULT '',
    source_session TEXT NOT NULL DEFAULT '',
    importance REAL NOT NULL DEFAULT 0.5,  -- 0.0-1.0
    created_at TEXT NOT NULL DEFAULT '',
    last_referenced TEXT NOT NULL DEFAULT '',
    reference_count INTEGER NOT NULL DEFAULT 0,
    tags TEXT NOT NULL DEFAULT '[]',       -- JSON array
    metadata TEXT NOT NULL DEFAULT '{}'    -- JSON object (includes confidence, fact_id, related_facts)
);
```

### entries_fts (FTS5 virtual table)

```sql
CREATE VIRTUAL TABLE entries_fts USING fts5(
    content, category, source_agent, tags,
    tokenize='trigram',
    content='entries',
    content_rowid='id'
);
```

### sources table

```sql
CREATE TABLE sources (
    source_agent TEXT NOT NULL,
    source_session TEXT NOT NULL,
    processed_at TEXT NOT NULL,
    message_count INTEGER DEFAULT 0,
    PRIMARY KEY (source_agent, source_session)
);
```

### index_meta table (schema v2+)

```sql
CREATE TABLE index_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Pre-populated keys:
-- hash_algorithm = 'sha256_16'
-- hash_algorithm_version = '1'
-- schema_version = '2'
```

## Migration Policy

1. Each schema version is defined in `SCHEMA_MIGRATIONS` dict
2. `SCHEMA_VERSION` constant tracks the current version
3. On `MemoryIndex.__init__()`, `_init_schema()` checks `PRAGMA user_version`
4. If current_version < SCHEMA_VERSION, migrations run incrementally
5. If current_version > SCHEMA_VERSION, a warning is logged
6. Fresh installs (version 0) run all migrations sequentially

### Adding a migration:

```python
SCHEMA_VERSION = 3  # Increment

SCHEMA_MIGRATIONS[3] = """
    ALTER TABLE entries ADD COLUMN embedding_model TEXT DEFAULT '';
"""
```

## Scanner Contract

Each scanner:
1. Is a function decorated with `@register_scanner("name")`
2. Takes `home: Path` (user's home directory)
3. Returns `Iterator[Session]`
4. Handles its own exceptions (logs and continues)
5. May yield 0 or more sessions

### Adding a new scanner:

```python
from .base import Message, Session, register_scanner

@register_scanner("my-agent")
def scan_my_agent(home: Path) -> Iterator[Session]:
    history_dir = home / ".my-agent" / "sessions"
    if not history_dir.is_dir():
        return
    for session_file in history_dir.glob("*.json"):
        yield Session(
            source="my-agent",
            session_id=session_file.stem,
            messages=[Message(role="user", content=session_file.read_text())],
        )
```

## Performance Characteristics

| Operation | Scale | Performance |
|-----------|-------|-------------|
| Session discovery | 1000 sessions | <2s |
| Rules extraction | Per session | <1ms |
| LLM extraction | Per session | 2-5s (ctx.llm) |
| FTS5 search | 100K entries | <1ms |
| Semantic search (future) | 100K entries | ~5ms |
| Export | 10K entries | <500ms |
| Import | 10K entries | <1s |

### Load test results (v0.1.1):

- 50 batch inserts: <50ms
- FTS5 search across 50 entries: <1ms
- FTS5 search across 50K entries (projected): <100ms
- Full scan of 415 real sessions: ~0.5s (FastExtractor only)

## Export/Import Protocol

Export creates a tar.gz containing:
- `index.db` (with WAL checkpointed)

Import extracts the tar.gz to the target path and returns a MemoryIndex.

Merge semantics (future): latest timestamp wins, importance=MAX.

## Security & Privacy

- All data stays local. No network calls unless API key provided.
- No telemetry, no analytics, no phoning home.
- Scanners read-only — never modify agent history files.
- Permission-aware: skips unreadable directories on shared machines.
- SQL injection prevention: `_validate_table_name()` checks all table/column names.
- FTS5 query escaping prevents syntax-injection.
