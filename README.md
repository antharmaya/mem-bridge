# 🧠 Antharmaya Memory Bridge

**Give your Hermes agent photographic memory of every AI conversation you've ever had.**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Hermes Plugin](https://img.shields.io/badge/Hermes-Plugin-blue)](https://hermes-agent.nousresearch.com)

Memory Bridge auto-discovers every AI agent conversation on your machine — Claude Code, Codex, Gemini, Cursor, OpenCode, Goose, and more — and consolidates them into a unified, searchable memory index that Hermes can query in real-time.

```bash
curl -fsSL https://antharmaya.com/memory-bridge/install.sh | bash
```

---

## The Problem

You've had thousands of conversations with AI agents. Claude Code knows your architecture decisions. Codex remembers your preferences. Gemini has your project plans. But **none of them talk to each other**, and Hermes — your always-on agent — knows none of it.

Every new session starts from zero.

## The Solution

Memory Bridge scans your machine for ALL agent conversation histories, extracts durable facts/decisions/preferences/lessons, and stores them in a local SQLite+FTS5 index. Hermes queries this index on every turn — so it remembers what you discussed with Claude Code last week, what you decided with Gemini last month, and what Codex knows about your stack.

**It's like your agents finally started comparing notes.**

---

## Supported Agents

| Agent | What's Scanned | Format |
|-------|---------------|--------|
| **Claude Code** | Full conversations + MEMORY.md knowledge graph | `~/.claude/projects/*/` |
| **Codex (OpenAI)** | Prompt history + MEMORY.md | `~/.codex/` |
| **Gemini CLI / Anti-Gravity** | Chat history + brain/task plans | `~/.gemini/` |
| **OpenCode** | Prompt history | `~/.local/state/opencode/` |
| **Cursor** | Plans + AI tracking data | `~/.cursor/` |
| **Goose** | Session transcripts | `~/.local/share/goose/` |
| **Agent Linux Control** | Event logs | `~/.local/state/agent-linux-control/` |

More agents added continuously. See [CONTRIBUTING.md](CONTRIBUTING.md) to contribute a scanner.

---

## What Gets Extracted

Memory Bridge classifies every fact into one of six categories:

- 🎯 **Decisions** — "Decided to use Cloudflare Workers over Vercel for cost reasons"
- ⚙️ **Preferences** — "Prefers direct communication, no motivational filler"
- 📚 **Lessons** — "Razorpay OAuth flow breaks when redirect_uri has trailing slash"
- 📊 **Projects** — "Photoselect frontend deployed on Cloudflare Pages, backend on Cloud Run"
- 👤 **People** — "Design partner: Priya at PixelMemories Studio, Mumbai"
- 📝 **Facts** — "Postgres 16 on port 5433, Redis on 6380"

---

## How It Works

```
┌─────────────────────────────────────────────────────────────┐
│                    YOUR MACHINE                              │
│                                                              │
│  ~/.claude/projects/    ~/.codex/    ~/.gemini/    ...       │
│        │                    │            │                   │
│        └────────────────────┼────────────┘                   │
│                             ▼                                │
│                    ┌─────────────────┐                       │
│                    │   SCANNER       │  Auto-discovery       │
│                    │   6+ formats    │  of all agents        │
│                    └────────┬────────┘                       │
│                             ▼                                │
│                    ┌─────────────────┐                       │
│                    │   EXTRACTOR     │  Rules-based (free)   │
│                    │  + LLM (opt)    │  via ctx.llm          │
│                    └────────┬────────┘                       │
│                             ▼                                │
│                    ┌─────────────────┐                       │
│                    │  SQLite + FTS5  │  Ultra-fast local     │
│                    │   INDEX         │  full-text search     │
│                    └────────┬────────┘                       │
│                             ▼                                │
│                    ┌─────────────────┐                       │
│                    │  HERMES AGENT   │  prefetch() on        │
│                    │  MemoryProvider │  every turn           │
│                    └─────────────────┘                       │
└─────────────────────────────────────────────────────────────┘
```

1. **Scan** — Discovers all agent histories on disk
2. **Extract** — Rules-based extraction (free) catches decisions, preferences, lessons. Optional LLM pass for deep semantic extraction — uses your Hermes model via `ctx.llm`, no separate API key needed
3. **Index** — SQLite with FTS5 full-text search. Sub-millisecond queries. Zero external services
4. **Retrieve** — Hermes queries the index on every turn via `prefetch()`. Relevant memories are injected into context automatically

---

## Use it from any agent (MCP)

Memory Bridge began as a Hermes plugin, but the index it builds is host-neutral.
It ships a **zero-dependency MCP server** (stdio, JSON-RPC) so **any MCP client** —
Claude Desktop, Cursor, Codex, Windsurf — can search, recall, and review the same
local index. No SDK, no extra dependency.

```jsonc
// Claude Desktop / Cursor — mcpServers config
{
  "mcpServers": {
    "memory-bridge": { "command": "memory-bridge", "args": ["mcp"] }
  }
}
```

Exposes four tools to the client: `search_memory`, `recall` (time/agent-scoped),
`list_decisions`, and `memory_stats`. Run it directly with `memory-bridge mcp`.

---

## Usage

### In Hermes (after install)

```
/memory_bridge_stats                    # See what's been consolidated
/memory_bridge_search "razorpay auth"   # Search all agent memories
/memory_bridge_recall "what did I do with Claude Code last month"  # Agent + time-scoped recall
/memory_bridge_brain "photoselect"      # Explore the entity graph / associative recall
/memory_bridge_decisions                # Review structured decisions + outcomes
/memory_bridge_scan                     # Re-scan for new conversations
```

Or use the CLI:

```bash
hermes memory-bridge scan               # Scan for new agent histories
hermes memory-bridge search "deploy"    # Search your unified memory
hermes memory-bridge recall "what did I do with codex last week"  # Time/agent recall
hermes memory-bridge brain photoselect  # Entity graph: what's connected to what
hermes memory-bridge decisions          # List structured decisions + outcomes
hermes memory-bridge stats              # Show memory statistics
```

### Standalone (without Hermes)

```bash
python3 -c "
from src.scanner import discover_all
from src.indexer import MemoryIndex
from pathlib import Path

index = MemoryIndex(Path.home() / '.hermes' / 'antharmaya-memory' / 'index.db')
sessions = discover_all()
print(f'Found {len(sessions)} agent sessions')
stats = index.stats()
print(stats)
"
```

---

## Installation

### Method 1: Curl (Recommended)
```bash
curl -fsSL https://antharmaya.com/memory-bridge/install.sh | bash
```
Clones into `~/.hermes/plugins/memory/antharmaya-bridge/`, installs deps, runs initial scan.

### Method 2: Manual (git clone)
```bash
git clone https://github.com/antharmaya/mem-bridge.git \
  ~/.hermes/plugins/memory/antharmaya-bridge
```
Memory providers must live in `~/.hermes/plugins/memory/<name>/` — this is the Hermes convention for provider discovery.

### Activation

After installation, activate Memory Bridge as your memory provider:

**Option A — Interactive (easiest):**
```bash
hermes memory setup
# Select "antharmaya-bridge" when prompted
```

**Option B — Manual config:**
```yaml
# ~/.hermes/config.yaml
memory:
  provider: antharmaya-bridge
```

Then restart Hermes. The plugin loads automatically on next session. Verify with:
```
/memory_bridge_stats
```

### Requirements
- Python 3.11+
- Hermes Agent 0.16.0+
- 10MB disk space (SQLite index)
- No external services. No API keys. No cloud. No telemetry.

### Deep LLM Extraction

Memory Bridge uses **ctx.llm** — Hermes' own configured model — for deep fact extraction. Zero config, no separate API key. It just works.

If running standalone (without Hermes), set an API key for the fallback DirectEngine:
```bash
export MEMORY_BRIDGE_API_KEY="sk-or-v1-..."   # OpenRouter
# or
export OPENROUTER_API_KEY="sk-or-v1-..."       # OpenRouter
```

---

## Privacy

- **Local by default.** Scanning, rules-based extraction, and the index are 100% local — the SQLite index never leaves your machine.
- **You choose where LLM extraction runs.** The optional deep-extraction pass sends conversation text to *whatever model you've already configured in Hermes* via `ctx.llm`. Point Hermes at a local model and it stays fully offline; point it at a cloud provider and that text goes there — your call. The standalone `DirectEngine` fallback only activates if you set an API key.
- **No telemetry.** No analytics. No phoning home — Memory Bridge makes zero network calls of its own.
- **Read-only.** Memory Bridge never modifies your agent histories — only marks them as "processed" in its own index.
- **You own your data.** The index is a SQLite file in `~/.hermes/antharmaya-memory/`.

---

## Architecture

```
antharmaya-memory-bridge/
├── src/
│   ├── scanner.py       # Agent history discovery (re-exports from scanners/)
│   ├── scanners/        # Individual agent scanners (9 agents)
│   │   ├── base.py      # Session/Message types, scanner registry
│   │   ├── claude_code.py, codex.py, gemini.py, ...
│   ├── extractor.py     # Rules-based + LLM fact extraction
│   ├── indexer.py       # SQLite+FTS5 storage engine
│   ├── provider.py      # Hermes MemoryProvider plugin
│   └── cli.py           # Standalone CLI
├── plugin.yaml           # Hermes plugin manifest
├── install.sh            # One-line curl installer
└── README.md

### Adding a new agent scanner

1. Create `src/scanners/my_agent.py` with a `@register_scanner("agent-name")` function
2. Import it in `src/scanners/__init__.py`
3. Submit a PR

Example:
```python
# src/scanners/my_agent.py
from .base import Message, Session, register_scanner

@register_scanner("my-agent")
def scan_my_agent(home: Path) -> Iterator[Session]:
    history_file = home / ".my-agent" / "history.jsonl"
    if not history_file.is_file():
        return
    # Parse and yield Session objects
```

---

## Performance

| Operation | Cold (first scan) | Warm (incremental) |
|-----------|-------------------|---------------------|
| Scan 200+ sessions | ~2 seconds | ~0.5 seconds |
| Rules extraction | ~0.1s per session | — |
| LLM extraction | ~2-5s per session | — |
| FTS5 search | <1ms | <1ms |
| prefetch() | <5ms | <5ms |

FTS5 queries are sub-millisecond even with 100K+ entries. The index file grows ~10KB per 100 entries.

---

## FAQ

**Q: Does this send my conversations to the cloud?**
A: No. Everything runs locally. The only optional network call is LLM extraction if you provide an API key.

**Q: Can I use this without Hermes?**
A: Yes. The scanner and indexer work standalone. Use the Python API directly.

**Q: What if an agent changes its history format?**
A: Scanners are modular. If a format breaks, only that scanner fails — everything else keeps working. File an issue and we'll fix it.

**Q: Will this slow down my Hermes sessions?**
A: No. `prefetch()` runs in <5ms. The index is local SQLite with WAL mode and 64MB cache.

**Q: How is this different from Hermes built-in memory?**
A: Hermes memory only knows what happens IN Hermes sessions. Memory Bridge knows what happened in EVERY agent you've ever used — Claude Code, Codex, Gemini, Cursor, everything. It's the difference between remembering your own thoughts and remembering every conversation you've ever had.

---

## Roadmap

- [x] Core scanner + indexer
- [x] Hermes MemoryProvider plugin
- [x] One-line curl installer
- [ ] Rust rewrite of indexer for sub-microsecond retrieval
- [ ] Vector embeddings for semantic search (all-MiniLM-L6-v2)
- [ ] Aider, Cline scanner support
- [ ] Auto-consolidation cron job
- [ ] Memory health dashboard

---

Built with 🧠 by [Antharmaya Labs](https://antharmaya.com) — the substrate layer for AI-native India.
