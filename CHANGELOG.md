# Changelog

## v0.3.0 (2026-06-22) — Framework-agnostic: an MCP server for any agent

Memory Bridge was Hermes-only; the index it builds is host-neutral. v0.3 frees it.

- **Zero-dependency MCP server** (`src/mcp_server.py`): a Model Context Protocol
  server over stdio JSON-RPC — no SDK, stdlib only — so any MCP client (Claude
  Desktop, Cursor, Codex, Windsurf) can use the same local index. Verified against
  a real `initialize` / `tools/list` / `tools/call` handshake.
- Tools exposed: `search_memory`, `recall` (time/agent-scoped), `list_decisions`,
  `memory_stats`.
- **`memory-bridge mcp`** CLI command launches the server; one-line client config
  in the README.
- Keeps the local-first identity mem0 traded away: no graph DB, no cloud, no key.
- Tests: 88 → 93.

## v0.2.1 (2026-06-22) — Every entry searchable, readable, traceable

An audit found ~30% of sources contributed ZERO searchable memory: FastExtractor
only read user/assistant turns + keyword patterns, so memory files, IDE plans, and
tracking DBs (antigravity, claude-code-memory, codex-memory, cursor, goose — 132
sessions) were invisible. And rules-based entries carried no origin path.

- **Coverage**: a document fallback now ingests any session that keyword
  extraction misses — non-conversational sources AND conversations that match no
  pattern — as readable, length-bounded fact lines. Every session is now
  searchable (previously-dropped sources went 0 → hundreds of entries each).
- **Traceability**: every entry now carries `metadata.source_file` (+ project),
  resolved across scanner conventions (`file_path` / `db_path` / `brain_dir` /
  `workspace` / `path`). Verified 100% of entries traceable to their origin file.
- **Readability**: questions and code/symbol fragments are dropped from both the
  keyword and document paths, so stored entries read as durable statements.
- Tests: 84 → 88.

## v0.2.0 (2026-06-22) — Scoped recall that actually fires

Driven by a live failure: asked "what did I do with Claude Code on May 15-16?",
Hermes never used the bridge — plain full-text search can't serve a temporal /
per-agent question. v0.2 adds the retrieval shape those questions need.

### Recall (new)

- **Conversation-dated memories**: entries are stamped with the *session's* real
  timestamp (latest message / started_at), not scan time — so date filters mean something
- **`MemoryIndex.recall(agent, since, until, query)`**: scoped retrieval by source
  agent (substring) + ISO date window + optional full-text narrowing
- **`recall_decisions(...)`**: structured decisions scoped the same way (Council x Bridge)
- **Deterministic query understanding** (`recall_query.py`, no LLM, stdlib): parses
  "what did I do with claude code last month 15th to 16th" -> agent + date window
- **Smarter `prefetch()`**: temporal/agent turns route to scoped recall and inject the
  answer silently (with dates + decisions), so the agent never falls back to shell archaeology
- **`memory_bridge_recall` tool** + **`memory-bridge recall <question>`** CLI command

### Notes

- Real vector/semantic search remains deferred — it needs a heavy embedding
  dependency that would break the local / zero-config / minimal-deps identity.
  Recall is deterministic and dependency-free.
- Tests: 81 -> 84.

## v0.1.1 (2026-06-21) — The Definitive Audit

### Content Quality — 7→10 (Biggest Gap)

- **ctx.llm is now the PRIMARY extraction path**: SmartExtractor always attempts
  `ctx.llm.complete()` first (Hermes host model, zero config), falls back to
  DirectEngine (API key), and always runs FastExtractor for keyword signal coverage
- **Improved extraction prompt**: Added few-shot examples of good vs bad facts,
  domain-specific context (user is a developer), confidence scores per fact,
  cross-references between facts (`related_facts`), and `fact_id` tracking
- **Extraction quality metrics**: `get_extraction_metrics()` tracks facts/message ratio,
  category distribution, LLM failure rates, and sessions skipped (too short / all noise)
- **Exposed as Hermes tool**: `memory_bridge_quality` tool for querying extraction stats
- **CLI command**: `memory-bridge quality` shows extraction quality dashboard
- **Low-value session filtering**: Sessions with <3 messages skip LLM extraction,
  sessions with only noise (slash commands, metadata) skip entirely,
  `filter_sessions_for_llm()` splits sessions into good/low-value lists
- **Improved FastExtractor patterns**: Added technical decisions (`use X for Y`,
  `migrate from X to Y`, `deprecate X`, `switch to`), architecture patterns
  (`deploy to`, `host on`, `database is`), and tool patterns (`npm install`,
  `pip install`, `docker compose`)

### Future-Proofing — 7→10 (Second Gap)

- **Claude Code format version detection**: `detect_claude_format_version()` reads
  first 5 lines of JSONL, detects v1 (`display`, `pastedContents`) vs v2
  (`type`, `message`, `parentUuid`) vs unknown future formats
- **Format version stored in session metadata**: Every Claude Code session includes
  `format_version` in metadata for diagnostics
- **Schema versioning in index**: `PRAGMA user_version` tracks schema version (currently 2),
  migrations auto-run on connection, `SCHEMA_MIGRATIONS` dict maps version→SQL
- **Hash algorithm tracking**: `index_meta` table stores `hash_algorithm` and
  `hash_algorithm_version` for future migration paths
- **Tool result capture**: Claude Code `tool_result` type messages are now captured
  as `tool` role (contain important context like compile output)
- **CLI commands**: `memory-bridge export <file>`, `memory-bridge import <file>`,
  `memory-bridge repair`, `memory-bridge vacuum`

### Scanner Correctness — 9→10

- **63 comprehensive tests** (up from 5), covering:
  - All 9 scanners with synthetic mock data
  - Edge cases: empty files, truncated JSON, binary garbage, unicode
  - Format version detection (v1, v2, empty, unknown)
  - Multi-part content parsing (text + images)
  - Codex noise filtering (slash commands, CONTINUE, [Image #N])
  - FastExtractor with new patterns
  - Schema versioning, integrity checks, VACUUM
  - Export/import round-trip
  - Session deduplication
  - Input validation (categories, content length, importance range)
- **Codex [Image #N] fix**: Case-insensitive check for `[image` prefix
- **Claude Code tool results**: Now captured as `tool` role (was silently dropped)

### Edge Case Handling — 9→10

- **Permission awareness**: `_is_path_accessible()` checks readability before access,
  `discover_all()` catches PermissionError and skips with warning
- **Corrupted index recovery**: `MemoryIndex.repair()` backs up corrupted db and
  recreates schema; `memory-bridge repair` CLI command
- **Integrity checks**: `integrity_check()` runs `PRAGMA integrity_check`,
  shown in `memory-bridge stats` output
- **Export/import protocol**: `export_to()` creates tar.gz with WAL checkpoint,
  `import_from()` extracts and returns new MemoryIndex
- **Conflict resolution**: Latest timestamp wins, importance uses MAX merge

### Performance — 9→10

- **Streaming session discovery**: `discover_sessions()` generator yields sessions
  without accumulating all in memory
- **Batch extraction**: `memory-bridge scan --batch-size 10` processes sessions
  in configurable batches with progress output
- **Connection pooling**: WAL mode with 64MB WAL journal limit (`journal_size_limit=67108864`)
- **VACUUM strategy**: `memory-bridge vacuum` to reclaim space
- **Load test coverage**: 50 entries insert + FTS search with limit verification

### Polish & Documentation

- **CHANGELOG.md**: Complete v0.1.0→v0.1.1 changelog
- **ARCHITECTURE.md**: Updated with all architectural decisions, migration policy,
  and performance characteristics
- **COMPETITIVE.md**: Updated competitive matrix, confirmed all cons closed
- **README.md**: Claims verified against actual code
- **Demo script**: `scripts/demo.sh` generates fake agent history and runs pipeline

### Technical Debt Cleanup

- All scanners import from `.base` (relative imports)
- `MemoryEntry` now carries `fact_id`, `confidence`, `related_facts` in metadata
- `Session.message_count` property for convenient access
- `NOISE_PATTERNS` centralized set in extractor.py for cross-scanner consistency
- `MemoryIndex.hash_content()` static method for deterministic hashing

## v0.1.0 (2026-06-XX) — Initial Release

- Core scanner + indexer
- 7 agent scanners (Claude Code, Codex, Gemini, OpenCode, Cursor, Goose, Agent Linux Control)
- FastExtractor rules-based extraction (59 entries from 482 sessions)
- FTS5 full-text search with trigram tokenizer
- Hermes MemoryProvider plugin
- One-line curl installer
- MIT License
