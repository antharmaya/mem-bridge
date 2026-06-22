"""
Antharmaya Memory Bridge — Hermes MemoryProvider plugin.

Gives Hermes photographic memory of every AI conversation you've ever had.
Auto-discovers Claude Code, Codex, Gemini, Cursor, OpenCode, and other
agent histories on your machine, consolidates them into a local index,
and injects relevant context into every Hermes session.

Install: curl -fsSL https://antharmaya.com/memory-bridge/install.sh | bash
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

# When loaded by Hermes, agent.memory_provider is on the path.
# When running standalone (CLI/testing), fall back to a stub.
try:
    from agent.memory_provider import MemoryProvider
except ImportError:
    class MemoryProvider:
        """Full-stub for standalone/testing — mirrors the Hermes MemoryProvider ABC."""
        def is_available(self) -> bool: return True
        def initialize(self, session_id: str, **kwargs) -> None: pass
        def system_prompt_block(self) -> str: return ""
        def prefetch(self, query: str, *, session_id: str = "") -> str: return ""
        def queue_prefetch(self, query: str, *, session_id: str = "") -> None: pass
        def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "", messages=None) -> None: pass
        def get_tool_schemas(self) -> List[Dict[str, Any]]: return []
        def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str: return "{}"
        def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "", reset: bool = False, rewound: bool = False, **kwargs) -> None: pass
        def on_session_end(self, messages: List[Dict[str, Any]]) -> None: pass
        def on_memory_write(self, action: str, target: str, content: str, metadata: Optional[Dict[str, Any]] = None) -> None: pass
        def get_config_schema(self) -> List[Dict[str, Any]]: return []
        def save_config(self, values: Dict[str, Any], hermes_home: str) -> None: pass
        def shutdown(self) -> None: pass

from .indexer import MemoryEntry, MemoryIndex
from .scanner import Session, discover_all, get_available_scanners
from .extractor import (
    SmartExtractor,
    FastExtractor,
    get_extraction_metrics,
    extract_structured_decisions,
)
from .recall_query import parse_recall_query
from .entities import extract_entities

logger = logging.getLogger(__name__)


class AntharmayaMemoryProvider(MemoryProvider):
    """Hermes MemoryProvider that unifies all agent histories."""

    @property
    def name(self) -> str:
        return "antharmaya-bridge"

    def __init__(self, plugin_ctx=None):
        super().__init__()
        self._index: Optional[MemoryIndex] = None
        self._hermes_home: Optional[str] = None
        self._session_id: str = ""
        self._extractor: Optional[SmartExtractor] = None
        self._config: dict = {}
        self._last_prefetch_query: str = ""
        self._plugin_ctx = plugin_ctx  # For ctx.llm access
        self._prefetch_lock = threading.Lock()
        self._sync_lock = threading.Lock()

    # ─── MemoryProvider ABC ──────────────────────────────────────────────

    def is_available(self) -> bool:
        """Always available — works locally with zero credentials."""
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        """Set up the index for a session."""
        hermes_home = kwargs.get("hermes_home", str(Path.home() / ".hermes"))
        self._hermes_home = hermes_home
        self._session_id = session_id

        db_path = Path(hermes_home) / "antharmaya-memory" / "index.db"
        self._index = MemoryIndex(db_path)

        # Load config
        self._load_config()

        # Set up SmartExtractor — prefers ctx.llm (Hermes model), falls back to API key
        self._extractor = SmartExtractor(plugin_ctx=self._plugin_ctx)

        logger.info(f"[antharmaya-bridge] Initialized for session {session_id}")

    def system_prompt_block(self) -> str:
        """Return a compact status block for the system prompt."""
        if not self._index:
            return ""

        stats = self._index.stats()
        total = stats.get("total_entries", 0)
        if total == 0:
            return (
                "\n## Antharmaya Memory Bridge\n"
                "Memory bridge is active but has no consolidated entries yet. "
                "Run `hermes memory-bridge scan` to import your agent histories.\n"
            )

        by_source = stats.get("by_source", {})
        source_summary = ", ".join(f"{k}: {v}" for k, v in sorted(by_source.items()))

        return (
            "\n## Antharmaya Memory Bridge\n"
            f"Unified memory index with **{total} entries** across "
            f"{len(by_source)} agent sources ({source_summary}).\n"
            "The bridge has read your Claude Code, Codex, Gemini, and other "
            "agent conversations. Relevant memories are injected below.\n"
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Inject relevant memories for the current turn.

        Temporal / "what did I do with <agent>" turns are routed to scoped
        recall (agent + date window) so the answer is injected silently and the
        agent never has to fall back to a tool call or shell archaeology.
        Everything else uses full-text search.
        """
        if not self._index or not query.strip():
            return ""

        parsed = parse_recall_query(query)
        scoped = parsed["is_recall"] and (parsed["agent"] or parsed["since"])

        with self._prefetch_lock:
            self._last_prefetch_query = query
            if scoped:
                results = self._index.recall(
                    agent=parsed["agent"], since=parsed["since"],
                    until=parsed["until"], query=None, limit=8,
                )
                decisions = self._index.recall_decisions(
                    agent=parsed["agent"], since=parsed["since"],
                    until=parsed["until"], limit=4,
                )
            else:
                # Blend lexical search with the brain's associative recall, so
                # entries connected through shared entities surface too. FTS
                # leads; graph fills in related memories it would have missed.
                results = self._index.search_fts(query, limit=5)
                seen_ids = {e.id for e in results}
                for g in self._index.graph_recall(query, limit=4):
                    if g.id not in seen_ids:
                        seen_ids.add(g.id)
                        results.append(g)
                decisions = []

        if not results and not decisions:
            return ""

        lines = ["\n## Relevant Memories (from your other AI agents)", ""]
        for entry in results:
            source_tag = entry.source_agent.replace("-", " ").title()
            when = (entry.created_at or "")[:10]
            stamp = f" · {when}" if when else ""
            lines.append(f"- [{entry.category.upper()}] ({source_tag}{stamp}) {entry.content}")
        if decisions:
            lines.append("")
            lines.append("**Decisions made in this window:**")
            for d in decisions:
                fw = d.get("framework_used") or "unknown"
                lines.append(f"- ({fw}) {d.get('decision_text', '')}")

        return "\n".join(lines) + "\n"

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Schedule a background prefetch for the next turn."""
        self._last_prefetch_query = query

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Persist this Hermes turn to the bridge index (thread-safe)."""
        if not self._index:
            return

        with self._sync_lock:
            # Store the user message as a memory entry (low importance unless flagged)
            if user_content and len(user_content) > 20:
                self._index.upsert(MemoryEntry(
                    content=f"[Hermes conversation] User: {user_content[:500]}",
                    category="fact",
                    source_agent="hermes",
                    source_session=session_id or self._session_id,
                    importance=0.3,
                    tags=["hermes-turn"],
                ))

    # ─── Optional lifecycle hooks ──────────────────────────────────────

    def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "", reset: bool = False, rewound: bool = False, **kwargs) -> None:
        """Handle session switching for branch/resume workflows."""
        if reset:
            self._last_prefetch_query = ""
        self._session_id = new_session_id
        logger.debug("[antharmaya-bridge] Session switched: %s (reset=%s)", new_session_id, reset)

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Flush any pending writes on session end."""
        if self._index:
            self._index.conn.commit()
        logger.debug("[antharmaya-bridge] Session ended")

    def on_memory_write(self, action: str, target: str, content: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Mirror built-in memory writes to the bridge index."""
        if not self._index or target not in ("memory", "user"):
            return
        self._index.upsert(MemoryEntry(
            content=f"[Hermes {target}] {content[:800]}",
            category="preference" if target == "user" else "fact",
            source_agent="hermes",
            source_session=self._session_id,
            importance=0.5 if target == "user" else 0.3,
            tags=["hermes-memory", target],
        ))

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        """Persist non-secret config for hermes memory setup."""
        import yaml
        config_path = Path(hermes_home) / "config.yaml"
        try:
            if config_path.exists():
                with open(config_path) as f:
                    raw = yaml.safe_load(f) or {}
            else:
                raw = {}
            raw.setdefault("antharmaya_memory_bridge", {}).update(values)
            with open(config_path, "w") as f:
                yaml.safe_dump(raw, f)
        except Exception as e:
            logger.warning("[antharmaya-bridge] Failed to save config: %s", e)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Register tools for querying the unified memory."""
        return [
            {
                "name": "memory_bridge_search",
                "description": (
                    "Search your unified AI agent memory — finds facts, decisions, "
                    "and preferences from ALL your past conversations with Claude Code, "
                    "Codex, Gemini, Cursor, and other AI agents. Use this to recall "
                    "anything you've discussed with any agent."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "What to search for in your agent memory",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum results (default: 10)",
                            "default": 10,
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "memory_bridge_stats",
                "description": (
                    "Get statistics about your unified agent memory — how many "
                    "facts from each agent source, categories, and session counts."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "memory_bridge_quality",
                "description": (
                    "Get extraction quality metrics — facts per message, category "
                    "distribution, LLM failure rates, and sessions skipped."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "memory_bridge_scan",
                "description": (
                    "Scan your machine for new AI agent conversations and consolidate "
                    "them into the memory bridge. Run this after using Claude Code, "
                    "Codex, or other agents to bring their knowledge into Hermes."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "use_llm": {
                            "type": "boolean",
                            "description": "Use LLM for deep extraction (default: true if API key set)",
                            "default": True,
                        },
                    },
                },
            },
            {
                "name": "memory_bridge_recall",
                "description": (
                    "Recall what happened with a specific agent in a time window — "
                    "answers questions like 'what did I do with Claude Code last "
                    "month' or 'what did Codex and I decide on the 15th'. Pass a "
                    "natural-language question; the agent and date range are parsed "
                    "automatically. Use this for any time- or agent-scoped recall."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "Natural-language recall question (agent + timeframe are parsed out)",
                        },
                    },
                    "required": ["question"],
                },
            },
            {
                "name": "memory_bridge_brain",
                "description": (
                    "Explore the memory brain — an entity graph linking your projects, "
                    "tools, products, and files across all agents. Pass a query for "
                    "associative recall (memories connected through shared entities), "
                    "or an entity name to see what it's connected to. Use to understand "
                    "how things relate, not just to find a single fact."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Associative recall query"},
                        "entity": {"type": "string", "description": "Entity name to map its neighborhood"},
                    },
                },
            },
            {
                "name": "memory_bridge_decisions",
                "description": (
                    "List structured decisions consolidated from your past AI agent "
                    "conversations — what you decided, which framework shaped it, and "
                    "whether the outcome was later verified. Use this to recall and "
                    "review prior architecture/product decisions."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "framework": {
                            "type": "string",
                            "description": "Optional framework filter (e.g. tradeoff_matrix, failure_modes)",
                        },
                        "unverified_only": {
                            "type": "boolean",
                            "description": "Only decisions whose outcome hasn't been verified yet",
                            "default": False,
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum results (default: 20)",
                            "default": 20,
                        },
                    },
                },
            },
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        """Dispatch memory bridge tool calls."""
        if tool_name == "memory_bridge_search":
            return self._handle_search(args)
        elif tool_name == "memory_bridge_stats":
            return self._handle_stats()
        elif tool_name == "memory_bridge_quality":
            return self._handle_quality()
        elif tool_name == "memory_bridge_scan":
            return self._handle_scan(args)
        elif tool_name == "memory_bridge_recall":
            return self._handle_recall(args)
        elif tool_name == "memory_bridge_brain":
            return self._handle_brain(args)
        elif tool_name == "memory_bridge_decisions":
            return self._handle_decisions(args)
        else:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})

    def shutdown(self) -> None:
        """Clean shutdown."""
        if self._index:
            if self._index.conn:
                self._index.conn.commit()
            self._index.close()

    # ─── Tool handlers ───────────────────────────────────────────────────

    def _handle_search(self, args: dict) -> str:
        if not self._index:
            return json.dumps({"error": "Index not initialized"})

        query = args.get("query", "")
        limit = int(args.get("limit", 10))

        with self._prefetch_lock:
            results = self._index.search_fts(query, limit=limit)
        return json.dumps({
            "query": query,
            "count": len(results),
            "results": [
                {
                    "content": r.content,
                    "category": r.category,
                    "source": r.source_agent,
                    "importance": r.importance,
                    "tags": r.tags,
                }
                for r in results
            ],
        })

    def _handle_stats(self) -> str:
        if not self._index:
            return json.dumps({"error": "Index not initialized"})

        with self._prefetch_lock:
            stats = self._index.stats()
        scanners = get_available_scanners()
        return json.dumps({
            **stats,
            "available_scanners": scanners,
        })

    def _handle_brain(self, args: dict) -> str:
        """Explore the entity graph: associative recall and/or entity neighborhood."""
        if not self._index:
            return json.dumps({"error": "Index not initialized"})
        out: Dict[str, Any] = {}
        with self._prefetch_lock:
            entity = args.get("entity")
            query = args.get("query")
            if entity:
                out["neighborhood"] = self._index.entity_neighborhood(entity)
            if query:
                out["associative"] = [
                    {"content": e.content, "category": e.category, "source": e.source_agent}
                    for e in self._index.graph_recall(query, limit=10)
                ]
            if not entity and not query:
                out["top_entities"] = self._index.top_entities(limit=20)
        return json.dumps(out)

    def _handle_recall(self, args: dict) -> str:
        """Scoped recall: parse the question for agent + timeframe, then recall."""
        if not self._index:
            return json.dumps({"error": "Index not initialized"})
        question = args.get("question", "") or args.get("query", "")
        parsed = parse_recall_query(question)
        with self._prefetch_lock:
            entries = self._index.recall(
                agent=parsed["agent"], since=parsed["since"], until=parsed["until"],
                query=None if (parsed["agent"] or parsed["since"]) else question, limit=20,
            )
            decisions = self._index.recall_decisions(
                agent=parsed["agent"], since=parsed["since"], until=parsed["until"], limit=10,
            )
        return json.dumps({
            "parsed": {"agent": parsed["agent"], "since": parsed["since"], "until": parsed["until"]},
            "count": len(entries),
            "memories": [
                {"content": e.content, "category": e.category, "source": e.source_agent,
                 "date": (e.created_at or "")[:10]}
                for e in entries
            ],
            "decisions": [
                {"decision": d.get("decision_text"), "framework": d.get("framework_used"),
                 "outcome_verified": d.get("outcome_verified")}
                for d in decisions
            ],
        })

    def _handle_decisions(self, args: dict) -> str:
        """List structured decisions consolidated from agent conversations."""
        if not self._index:
            return json.dumps({"error": "Index not initialized"})
        framework = args.get("framework") or None
        unverified_only = bool(args.get("unverified_only", False))
        limit = int(args.get("limit", 20))
        with self._prefetch_lock:
            decisions = self._index.get_decisions(
                limit=limit, framework=framework, unverified_only=unverified_only
            )
        return json.dumps({
            "count": len(decisions),
            "decisions": [
                {
                    "id": d.get("id"),
                    "decision": d.get("decision_text"),
                    "framework": d.get("framework_used"),
                    "source": d.get("agent_source"),
                    "confidence": d.get("confidence"),
                    "outcome_verified": d.get("outcome_verified"),
                }
                for d in decisions
            ],
        })

    def _handle_quality(self) -> str:
        """Return extraction quality metrics."""
        try:
            return json.dumps(get_extraction_metrics())
        except Exception as e:
            return json.dumps({"error": f"Failed to get quality metrics: {e}"})

    def _handle_scan(self, args: dict) -> str:
        if not self._index:
            return json.dumps({"error": "Index not initialized"})

        use_llm = args.get("use_llm", True)
        home = Path(self._config.get("scan_home", str(Path.home())))

        # Discover all sessions
        sessions = discover_all(home)

        new_count = 0
        skipped_count = 0

        for session in sessions:
            # Skip already processed
            if self._index.is_source_processed(session.source, session.session_id):
                skipped_count += 1
                continue

            # SmartExtractor handles LLM + fast extraction and merging
            entries = []
            if use_llm and self._extractor:
                entries = self._extractor.extract_from_session(session)
            else:
                entries = FastExtractor.extract(session)

            for entry in entries:
                entry_id = self._index.upsert(entry)
                # Build the brain graph: link this memory to its entities.
                try:
                    ents = extract_entities(entry.content, (entry.metadata or {}).get("project"))
                    self._index.index_entities_for_entry(entry_id, ents, entry.created_at)
                except Exception as e:
                    logger.debug("[antharmaya-bridge] entity indexing skipped: %s", e)

            # Promote decisions into the structured_decisions table (Remember).
            for d in extract_structured_decisions(session, entries):
                try:
                    self._index.upsert_decision(
                        d["decision_text"],
                        agent_source=d["agent_source"],
                        framework_used=d["framework_used"],
                        session_id=d["session_id"],
                        confidence=d["confidence"],
                    )
                except Exception as e:
                    logger.debug("[antharmaya-bridge] decision upsert skipped: %s", e)

            self._index.mark_source_processed(
                session.source,
                session.session_id,
                len(session.messages),
            )
            new_count += 1

        stats = self._index.stats()

        # Include quality metrics in response
        quality_metrics = {}
        try:
            quality_metrics = get_extraction_metrics()
        except Exception:
            pass

        return json.dumps({
            "scanned": len(sessions),
            "new_sessions": new_count,
            "skipped": skipped_count,
            "total_entries": stats.get("total_entries", 0),
            "total_decisions": stats.get("total_decisions", 0),
            "by_source": stats.get("by_source", {}),
            "quality_metrics": quality_metrics,
        })

    # ─── Config ───────────────────────────────────────────────────────────

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "api_key",
                "description": "OpenRouter API key for LLM-powered consolidation (optional — ctx.llm is primary)",
                "secret": True,
                "required": False,
                "url": "https://openrouter.ai/keys",
                "env_var": "MEMORY_BRIDGE_API_KEY",
            },
            {
                "key": "scan_home",
                "description": "Home directory to scan for agent histories",
                "required": False,
                "default": str(Path.home()),
            },
            {
                "key": "auto_scan_interval_hours",
                "description": "Hours between automatic scans (0 to disable)",
                "required": False,
                "default": "24",
            },
        ]

    def _load_config(self):
        """Load config from Hermes config.yaml or env vars."""
        import os
        config_path = Path(self._hermes_home) / "config.yaml" if self._hermes_home else None

        self._config = {
            "api_key": os.getenv("MEMORY_BRIDGE_API_KEY", ""),
            "scan_home": str(Path.home()),
            "auto_scan_interval_hours": "24",
        }

        if config_path and config_path.exists():
            try:
                import yaml
                with open(config_path) as f:
                    raw = yaml.safe_load(f) or {}
                bridge_cfg = raw.get("antharmaya_memory_bridge", {})
                for k, v in bridge_cfg.items():
                    if k in self._config:
                        self._config[k] = v
            except Exception as e:
                logger.warning("Failed to load config: %s", e)


# ─── Hermes plugin entry point ──────────────────────────────────────────

def register(ctx):
    """Register this memory provider with Hermes."""
    provider = AntharmayaMemoryProvider(plugin_ctx=ctx)
    ctx.register_memory_provider(provider)
    logger.info("[antharmaya-bridge] Memory provider registered (ctx.llm: %s)", hasattr(ctx, 'llm'))
