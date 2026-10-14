# Memory Bridge — Metadata Schema Documentation

## Entry Metadata (`entries.metadata` JSON)

Each `MemoryEntry` stores metadata as a JSON blob in the `metadata` column.
Below is the documented schema of all recognized keys.

### Core Keys (set by LLM extraction)

| Key | Type | Description | Example |
|-----|------|-------------|---------|
| `project` | `str` | Project context from session | `"/home/user/project"` |
| `source_file` | `str` | File path of the source agent log | `"/home/user/.claude/projects/sess001.jsonl"` |
| `confidence` | `float` | Extraction confidence (0.0–1.0) | `0.85` |
| `fact_id` | `str` | LLM-assigned fact identifier | `"mem_034"` |
| `related_facts` | `list[str]` | IDs of related facts | `["mem_012", "mem_019"]` |

### Import/Export Metadata

| Key | Type | Description | Example |
|-----|------|-------------|---------|
| `export_timestamp` | `str` | When this entry was exported | `"2026-06-21T12:00:00+00:00"` |
| `export_version` | `str` | Export format version | `"1.0"` |

### Hybrid Search Metadata

| Key | Type | Description | Example |
|-----|------|-------------|---------|
| `_similarity` | `float` | Cosine similarity score (set by `search_semantic`) | `0.923` |

### Custom/User-Defined Keys

Any additional keys are preserved as-is. The bridge never strips unknown keys.
This allows consumers to attach custom data:
- `user_notes` — human annotations
- `review_status` — custom review workflow state
- `external_refs` — links to external systems

---

## Source Metadata (`sources` table)

| Column | Type | Description |
|--------|------|-------------|
| `source_agent` | `TEXT` | Agent name (`claude-code`, `codex`, etc.) |
| `source_session` | `TEXT` | Session identifier (scanner-dependent) |
| `processed_at` | `TEXT` | ISO-8601 timestamp of processing |
| `message_count` | `INTEGER` | Number of messages in the session |

---

## Schema Metadata (`index_meta` table)

| Key | Value | Description |
|-----|-------|-------------|
| `hash_algorithm` | `"sha256_16"` | Content dedup hash algorithm |
| `hash_algorithm_version` | `"1"` | Version of hash algorithm |
| `schema_version` | `"3"` | Current schema version |

---

## FTS5 Schema (`entries_fts`)

Virtual table for full-text search using trigram tokenizer.

**Columns indexed:** `content`, `category`, `source_agent`, `tags`

**Triggers:** `entries_ai` (after insert), `entries_ad` (after delete),
`entries_au` (after update) keep FTS in sync with `entries` table.

**Rebuild:** Run `INSERT INTO entries_fts(entries_fts) VALUES('rebuild')`
after bulk imports or schema migrations.
