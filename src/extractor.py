"""
LLM-powered consolidation engine.

Two extraction strategies:
1. PluginLlmEngine — uses ctx.llm (Hermes host model), zero config, no API key
2. DirectEngine — uses an LLM API directly (fallback, optional)

Plus FastExtractor for rules-based extraction (always available, free).

All engines return structured MemoryEntry objects stored in the SQLite index.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

from .indexer import MemoryEntry
from .scanner import Session

logger = logging.getLogger(__name__)

# ─── Extraction prompts ─────────────────────────────────────────────────

EXTRACTION_SYSTEM_PROMPT = """You are a memory consolidation engine. Your job is to read AI agent 
conversation transcripts and extract STRUCTURED FACTS that should be preserved 
for future reference.

Extract ONLY durable, reusable knowledge — not transient chat.

The user is a SOFTWARE DEVELOPER building SaaS products, APIs, and web applications.
Focus on facts relevant to a developer: architecture decisions, tooling preferences,
debugging lessons, infrastructure choices, and design patterns.

For each fact you find, classify it into exactly ONE category:

- fact: Objective information, technical details, environment config, data
- decision: A choice the human made, a direction they committed to
- preference: How the human likes things done, their style, pet peeves
- lesson: Something they learned from failure or success
- project: Project state, milestones, blockers, architecture decisions
- person: Information about other people, relationships, contacts

FEW-SHOT EXAMPLES:

Good facts (extract these):
- "Decided to use Cloudflare Workers over Vercel for API layer because edge deployment is cheaper at scale"
- "User prefers explicit CORS origins — never use wildcard * in production"
- "Lesson learned: Razorpay OAuth flow breaks when redirect_uri has trailing slash — always strip it"
- "Database is PostgreSQL 16 on port 5433, Redis 7 on port 6380, both self-hosted on Hetzner CX32"
- "Docker Compose setup has healthcheck delays that cause startup race conditions — fixed with depends_on.condition"
- "User's pet peeve: LLMs that add motivational filler like 'Great question!' — wants direct answers only"
- "Design partner: Priya at PixelMemories Studio, Mumbai — contact via priya@pixelmemories.com"

Bad facts (DO NOT extract — these are transient or too vague):
- "User said hello" (greeting, no durable value)
- "Assistant explained how to use the tool" (meta, not durable knowledge)
- "The conversation had 5 messages" (metadata, not knowledge)
- "User seems frustrated" (inference, not explicit)

RULES:
1. Extract ONLY what is explicitly stated — never infer or fabricate
2. Skip small talk, greetings, debugging noise, transient errors
3. Each fact should be ONE clear sentence, self-contained
4. If a fact updates a previous fact, note it: "UPDATE: <new fact>"
5. If nothing durable was discussed, return an empty list
6. Prioritize decisions, preferences, and lessons over generic facts
7. Cross-reference facts that relate to each other using fact_ids
8. Provide a confidence score per fact (how certain are you this is correct)

Return a JSON object with this exact structure:
{
  "facts": [
    {
      "fact_id": "mem_001",
      "content": "The single-sentence fact with specific details (names, versions, costs)",
      "category": "decision|fact|preference|lesson|project|person",
      "importance": 0.0-1.0,
      "confidence": 0.0-1.0,
      "tags": ["tag1", "tag2"],
      "related_facts": ["mem_002", "mem_017"]
    }
  ]
}

Importance scoring:
- 0.9-1.0: Critical decisions, core preferences, major architecture choices
- 0.7-0.9: Important project state, lessons learned, key contacts
- 0.5-0.7: Useful context, environment details, minor decisions
- 0.3-0.5: Background information, nice-to-have context
- 0.0-0.3: Low-value facts (probably don't extract these at all)

Confidence scoring:
- 0.9-1.0: Explicitly stated, unambiguous
- 0.7-0.9: Clearly implied, strong signal
- 0.5-0.7: Reasonable inference from context
- 0.0-0.5: Speculative — better to skip these
"""

EXTRACTION_USER_TEMPLATE = """Extract durable facts from this AI agent conversation.

Source: {source_agent}
Session: {session_id}
Project: {project}

Conversation:
{conversation}

Return ONLY the JSON object with extracted facts. No other text."""


# ─── Quality Metrics ────────────────────────────────────────────────────

class ExtractionMetrics:
    """Track extraction quality across sessions."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.sessions_processed = 0
        self.total_facts = 0
        self.total_messages = 0
        self.llm_failures = 0
        self.sessions_skipped_short = 0
        self.sessions_skipped_noise = 0
        self.category_distribution: dict[str, int] = {}
        self.engine_usage: dict[str, int] = {}

    def record_session(self, messages: int, facts: int, engine: str):
        self.sessions_processed += 1
        self.total_messages += messages
        self.total_facts += facts
        self.engine_usage[engine] = self.engine_usage.get(engine, 0) + 1

    def record_llm_failure(self):
        self.llm_failures += 1

    def record_skipped_short(self):
        self.sessions_skipped_short += 1

    def record_skipped_noise(self):
        self.sessions_skipped_noise += 1

    def record_category(self, category: str):
        self.category_distribution[category] = self.category_distribution.get(category, 0) + 1

    def summary(self) -> dict:
        ratio = self.total_facts / max(self.total_messages, 1)
        return {
            "sessions_processed": self.sessions_processed,
            "total_facts": self.total_facts,
            "total_messages": self.total_messages,
            "facts_per_message": round(ratio, 2),
            "llm_failures": self.llm_failures,
            "sessions_skipped_short": self.sessions_skipped_short,
            "sessions_skipped_noise": self.sessions_skipped_noise,
            "category_distribution": dict(sorted(self.category_distribution.items())),
            "engine_usage": dict(sorted(self.engine_usage.items())),
        }


_metrics = ExtractionMetrics()


def get_extraction_metrics() -> dict:
    """Get current extraction quality metrics."""
    return _metrics.summary()


# ─── Low-value session filtering ────────────────────────────────────────

NOISE_PATTERNS: set[str] = {
    # Slash commands
    "/usage", "/resume", "/tasks", "/exit", "/clear",
    "/help", "/model", "/status", "/compact",
    # Claude Code metadata
    "compress-jobs", "compress tasks",
    # Continue keywords
    "continue", "clear",
    # UI noise
    "[image", "[]",
}

def is_low_value_session(session: Session) -> tuple[bool, str]:
    """Check if a session is likely low-value and should skip LLM extraction.

    Returns (is_low_value, reason) tuple.
    """
    msg_count = len(session.messages)

    # Sessions with <3 messages are too short for meaningful extraction
    if msg_count < 3:
        return True, "too_short"

    # Check if ALL messages are noise (slash commands, metadata, etc.)
    all_noise = True
    for msg in session.messages:
        content = msg.content.strip().lower()
        if not content:
            continue
        # Check against noise patterns
        is_noise = False
        for pattern in NOISE_PATTERNS:
            if pattern in content:
                is_noise = True
                break
        # Single word messages are usually noise
        if len(content.split()) <= 2:
            is_noise = True
        if not is_noise:
            all_noise = False
            break

    if all_noise:
        return True, "all_noise"

    return False, ""

def filter_sessions_for_llm(sessions: list[Session]) -> tuple[list[Session], list[Session]]:
    """Split sessions into those worth LLM extraction and those that should use FastExtractor only.

    Returns (good_sessions, low_value_sessions).
    """
    good = []
    low = []
    for s in sessions:
        is_low, _ = is_low_value_session(s)
        if is_low:
            low.append(s)
        else:
            good.append(s)
    return good, low


# ─── PluginLlmEngine — uses Hermes host model (ctx.llm) ─────────────────

class PluginLlmEngine:
    """Extraction engine that uses Hermes' own model via ctx.llm.

    ZERO configuration — no API key, no base URL, no model selection.
    Uses whatever model the user already configured in Hermes.

    This is the RECOMMENDED engine. It's what makes Memory Bridge unique.
    """

    def __init__(self, plugin_ctx: Any = None):
        """plugin_ctx: Hermes PluginContext with .llm property."""
        self._ctx = plugin_ctx
        self._available = plugin_ctx is not None and hasattr(plugin_ctx, 'llm')

    @property
    def available(self) -> bool:
        return self._available

    def extract_from_session(self, session: Session, max_messages: int = 40) -> list[MemoryEntry]:
        """Extract facts using Hermes host model via ctx.llm."""
        if not self._available or not self._ctx:
            return []

        conversation_text = _format_conversation(session, max_messages)
        if len(conversation_text) < 100:
            return []

        user_prompt = EXTRACTION_USER_TEMPLATE.format(
            source_agent=session.source,
            session_id=session.session_id,
            project=session.project or "unknown",
            conversation=conversation_text[:10000],
        )

        try:
            # Use ctx.llm.complete() — the real Hermes PluginLlm API
            result = self._ctx.llm.complete(
                messages=[
                    {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
                max_tokens=2000,
                timeout=60,
            )
            facts_data = _parse_response(result.text if hasattr(result, 'text') else str(result))
        except Exception as e:
            logger.warning("ctx.llm extraction failed: %s", e, exc_info=True)
            return []

        return _facts_to_entries(facts_data, session)


# ─── DirectEngine — uses API directly (fallback) ────────────────────────

class DirectEngine:
    """Extraction engine using direct API calls (bring your own LLM key).

    Requires MEMORY_BRIDGE_API_KEY or OPENROUTER_API_KEY.
    Used as fallback when ctx.llm is unavailable.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = "openrouter/auto",
    ):
        self.api_key = api_key or os.getenv("MEMORY_BRIDGE_API_KEY") or os.getenv("OPENROUTER_API_KEY") or ""
        self.base_url = base_url or os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        self.model = model

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def extract_from_session(self, session: Session, max_messages: int = 40) -> list[MemoryEntry]:
        if not self.available:
            return []

        conversation_text = _format_conversation(session, max_messages)
        if len(conversation_text) < 100:
            return []

        user_prompt = EXTRACTION_USER_TEMPLATE.format(
            source_agent=session.source,
            session_id=session.session_id,
            project=session.project or "unknown",
            conversation=conversation_text[:10000],
        )

        try:
            response = self._call_api(EXTRACTION_SYSTEM_PROMPT, user_prompt)
            facts_data = _parse_response(response)
        except Exception as e:
            logger.warning("Direct API extraction failed: %s", e, exc_info=True)
            return []

        return _facts_to_entries(facts_data, session)

    def _call_api(self, system_prompt: str, user_prompt: str) -> str:
        import urllib.request

        body = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 2000,
            "response_format": {"type": "json_object"},
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
            return data["choices"][0]["message"]["content"]


# ─── FastExtractor — rules-based, always available, free ────────────────

class FastExtractor:
    """Extract key signals without LLM — fast, free, deterministic.

    Catches decision markers, preferences, lessons, and project updates
    using keyword patterns. Always works, zero cost, instant.
    """

    PATTERNS = {
        "decision": [
            "i've decided", "let's go with", "final answer", "we will",
            "the plan is", "committed to", "moving forward with",
            "our approach", "we're going to", "we are going to",
            "i'll go with", "lets do", "let's do", "we should",
            "priority is", "focus on", "ship it", "merge it",
            # Technical decisions
            "use ", "migrate from", "deprecate", "replace ",
            "switch to", "upgrade to", "downgrade to",
            "migrate to", "convert to", "change to",
        ],
        "preference": [
            "i prefer", "i don't like", "i hate", "always use",
            "never use", "my style", "pet peeve", "i like",
            "i love", "favorite", "go-to", "default choice",
            "personally", "in my opinion", "i'd rather",
        ],
        "lesson": [
            "lesson learned", "never again", "what worked", "what failed",
            "i learned", "key takeaway", "mistake was", "turns out",
            "in hindsight", "next time", "should have", "could have",
        ],
        "project": [
            "blocked by", "milestone", "launched", "deployed",
            "in progress", "completed", "ready for", "shipped",
            "released", "done with", "wrapped up", "finished",
            # Architecture patterns
            "deploy to", "host on", "database is", "server is",
            "infrastructure", "architecture is", "stack is",
            # Tool patterns
            "npm install", "pip install", "docker compose",
            "apt install", "brew install", "cargo install",
            "go install", "yarn add", "pnpm add",
        ],
    }

    IMPORTANCE = {
        "decision": 0.7,
        "preference": 0.6,
        "lesson": 0.8,
        "project": 0.5,
    }

    @classmethod
    def extract(cls, session: Session) -> list[MemoryEntry]:
        entries = []
        seen = set()  # Deduplicate within session

        for msg in session.messages:
            if msg.role not in ("user", "assistant"):
                continue
            text_lower = msg.content.lower()

            for category, patterns in cls.PATTERNS.items():
                for pattern in patterns:
                    if pattern in text_lower:
                        sentence = cls._extract_sentence(msg.content, pattern)
                        if sentence and sentence not in seen:
                            seen.add(sentence)
                            entries.append(MemoryEntry(
                                content=sentence,
                                category=category,
                                source_agent=session.source,
                                source_session=session.session_id,
                                importance=cls.IMPORTANCE[category],
                                tags=["auto-extracted", category],
                            ))
                        break  # One category per message

        return entries

    # Substrings that mark machine output (log lines, JSON blobs, stack traces)
    # rather than durable human knowledge.
    _NOISE_MARKERS = ('","level":', '","msg":', '"timestamp":', '","ts":',
                      'traceback (most recent', 'stacktrace', '\\u00')

    @classmethod
    def _looks_like_noise(cls, sentence: str) -> bool:
        """Reject sentences that are clearly machine output, not durable facts."""
        low = sentence.lower()
        if any(marker in low for marker in cls._NOISE_MARKERS):
            return True
        # Mostly-punctuation / structural fragments (JSON, code) — low signal.
        symbols = sum(1 for c in sentence if c in '{}[]<>|\\=/`"')
        if symbols > len(sentence) * 0.25:
            return True
        return False

    @staticmethod
    def _extract_sentence(text: str, pattern: str) -> str | None:
        text_lower = text.lower()
        idx = text_lower.find(pattern)
        if idx == -1:
            return None

        # Find sentence boundaries
        start = max(0, text.rfind('.', 0, idx) + 1)
        start = max(start, text.rfind('\n', 0, idx) + 1)
        end = text.find('.', idx + len(pattern))
        if end == -1:
            end = text.find('\n', idx + len(pattern))
        if end == -1:
            end = len(text)

        sentence = text[start:end].strip().strip('.,;:')
        if 15 < len(sentence) < 300 and not FastExtractor._looks_like_noise(sentence):
            return sentence
        return None


# ─── Combined extractor (orchestrates all engines) ──────────────────────

class SmartExtractor:
    """Orchestrates all extraction engines, preferring the best available.

    Priority:
    1. ctx.llm (Hermes host model — zero config, free) — ALWAYS attempted first
    2. Direct API (if API key is set) — fallback
    3. FastExtractor (always works, free) — ALWAYS runs for keyword signal coverage

    Low-value sessions (<3 msgs, all noise) skip LLM extraction entirely.
    """

    def __init__(self, plugin_ctx: Any = None):
        self._plugin_llm = PluginLlmEngine(plugin_ctx) if plugin_ctx else None
        self._direct = DirectEngine()
        self._best_engine = None  # Cached after first check

    def extract_from_session(self, session: Session) -> list[MemoryEntry]:
        """Extract using the best available engine.

        ALWAYS runs FastExtractor for keyword signal catch.
        PRIMARILY uses ctx.llm if available for deep extraction.
        Falls back to DirectEngine if ctx.llm unavailable but API key exists.
        """
        # Always run fast extraction (free, catches keyword signals)
        fast_entries = FastExtractor.extract(session)

        # Check if session is worth LLM extraction
        is_low, low_reason = is_low_value_session(session)
        if is_low:
            _metrics.record_session(
                len(session.messages), len(fast_entries), "fast_only"
            )
            if low_reason == "too_short":
                _metrics.record_skipped_short()
            elif low_reason == "all_noise":
                _metrics.record_skipped_noise()
            return fast_entries

        # Try LLM extraction for deeper understanding
        llm_entries = []
        engine = self._get_best_engine()
        if engine:
            try:
                llm_entries = engine.extract_from_session(session)
                if not llm_entries and hasattr(engine, '_available') and not engine.available:
                    logger.debug("LLM engine not available, using FastExtractor only")
            except Exception as e:
                logger.warning("LLM extraction in SmartExtractor failed: %s", e, exc_info=True)
                _metrics.record_llm_failure()

        # Record metrics
        engine_name = type(engine).__name__ if engine else "fast_only"
        llm_count = len(llm_entries)
        _metrics.record_session(len(session.messages), llm_count + len(fast_entries), engine_name)

        # Merge: LLM entries first, then fast entries not already covered
        llm_contents = {e.content for e in llm_entries}
        merged = llm_entries + [e for e in fast_entries if e.content not in llm_contents]

        return merged

    def _get_best_engine(self):
        # Re-check availability each time (engine may go online/offline)
        if self._plugin_llm and self._plugin_llm.available:
            return self._plugin_llm

        # Fall back to direct API
        if self._direct.available:
            return self._direct

        return None  # None available, use FastExtractor only


# ─── Helpers ────────────────────────────────────────────────────────────

def _format_conversation(session: Session, max_messages: int = 40) -> str:
    """Format session messages into a compact conversation transcript."""
    messages = session.messages[-max_messages:]
    lines = []
    for msg in messages:
        role = msg.role.upper()
        content = msg.content[:1500]
        lines.append(f"[{role}] {content}")
    return "\n\n".join(lines)


def _parse_response(response: str) -> list[dict]:
    """Parse LLM response into list of fact dicts. Handles messy outputs."""
    try:
        data = json.loads(response)
        return data.get("facts", [])
    except json.JSONDecodeError:
        pass

    # Try markdown code blocks
    match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', response, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1)).get("facts", [])
        except json.JSONDecodeError:
            pass

    # Try bare JSON object
    match = re.search(r'\{.*"facts".*\}', response, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0)).get("facts", [])
        except json.JSONDecodeError:
            pass

    return []


def _facts_to_entries(facts: list[dict], session: Session) -> list[MemoryEntry]:
    """Convert raw fact dicts to MemoryEntry objects.

    LLM output is untrusted: categories are coerced to the valid set and
    importance is clamped to 0.0-1.0 so a malformed completion can never
    raise out of the indexer and abort an entire scan.
    """
    from .indexer import MemoryIndex

    entries = []
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        content = (fact.get("content") or "").strip()
        if not content or len(content) < 10:
            continue

        category = str(fact.get("category", "fact")).strip().lower()
        if category not in MemoryIndex.VALID_CATEGORIES:
            category = "fact"

        try:
            importance = float(fact.get("importance", 0.5))
        except (TypeError, ValueError):
            importance = 0.5
        importance = max(0.0, min(1.0, importance))

        tags = fact.get("tags", [])
        if not isinstance(tags, list):
            tags = []

        entries.append(MemoryEntry(
            content=content,
            category=category,
            source_agent=session.source,
            source_session=session.session_id,
            importance=importance,
            tags=tags,
            metadata={
                "project": session.project,
                "source_file": session.metadata.get("file_path", ""),
                "confidence": fact.get("confidence", 0.5),
                "fact_id": fact.get("fact_id", ""),
                "related_facts": fact.get("related_facts", []),
            },
        ))
    return entries


# ─── Structured decision extraction (the "Remember" half of the loop) ─────
#
# Decision-category entries are promoted into structured_decisions, with the
# decision framework inferred from PatternDetector signal phrases. This is
# what wires PatternDetector + the structured_decisions/frameworks tables into
# the normal scan flow.

# Highest-weight signal phrases → framework name in the frameworks catalog.
_FRAMEWORK_PHRASES: dict[str, str] = {
    "trade-off matrix": "tradeoff_matrix",
    "tradeoff matrix": "tradeoff_matrix",
    "trade-off": "tradeoff_matrix",
    "tradeoff": "tradeoff_matrix",
    "trade-offs": "tradeoff_matrix",
    "trade offs": "tradeoff_matrix",
    "failure modes": "failure_modes",
    "end-to-end": "end_to_end",
    "trust but verify": "end_to_end",
    "privacy impact": "ethics_triage",
    "data minimization": "ethics_triage",
    "agent trajectory": "agentops_trajectory",
}


def _infer_framework(matches: list[dict]) -> str:
    """Pick the framework implied by the highest-weight matched phrase."""
    best_framework = "unknown"
    best_weight = -1
    for m in matches:
        fw = _FRAMEWORK_PHRASES.get(m.get("phrase", ""))
        if fw and m.get("weight", 0) > best_weight:
            best_framework = fw
            best_weight = m["weight"]
    return best_framework


def extract_structured_decisions(
    session: Session,
    entries: list[MemoryEntry],
    detector: Any = None,
) -> list[dict]:
    """Promote decision-category entries to structured decision records.

    Uses PatternDetector to attribute a decision framework and to weight
    confidence by signal strength. Returns dicts ready for
    MemoryIndex.upsert_decision().
    """
    decision_entries = [e for e in entries if e.category == "decision"]
    if not decision_entries:
        return []

    if detector is None:
        from .extractors.pattern_detector import PatternDetector
        detector = PatternDetector()

    decisions = []
    seen: set[str] = set()
    for entry in decision_entries:
        text = (entry.content or "").strip()
        if len(text) < 10 or text in seen:
            continue
        seen.add(text)
        score, matches = detector.scan(text)
        framework = _infer_framework(matches)
        # Base confidence on the rules-based importance, nudged by signal score.
        confidence = max(0.0, min(1.0, round(entry.importance + 0.05 * score, 2)))
        decisions.append({
            "decision_text": text,
            "framework_used": framework,
            "agent_source": session.source,
            "session_id": session.session_id,
            "confidence": confidence,
        })
    return decisions
