"""
Pattern Detector — deterministic phrase scanner with weighted signal detection.

Gates LLM calls for structured decision extraction. Runs a rules-based
pre-filter with weighted phrase matching. If total signal points >= 4,
it's worth invoking the LLM for deeper decision enrichment.

Users can extend patterns via the `patterns` table in the decision index.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# ─── Built-in default patterns (weighted signals) ────────────────────
#
# These are always available. Users can add/override via the patterns table.
# Weight ≥4 forces LLM invocation. Weight ≥2 indicates strong signal.
# Weight 1 is supporting evidence.

DEFAULT_PATTERNS: dict[str, int] = {
    # Decision-framework pattern names — highest weight (4 points)
    "trade-off matrix": 4,
    "tradeoff matrix": 4,
    "trade-off": 3,
    "tradeoff": 3,
    "failure modes": 4,
    "end-to-end": 4,
    "trust but verify": 4,
    "privacy impact": 4,
    "data minimization": 4,
    "agent trajectory": 4,
    # Architecture decision markers — strong signals (3 points)
    "architecture decision": 3,
    "arch decision": 3,
    "architectural decision": 3,
    # Comparative decision language (2 points)
    "go with": 2,
    "over ": 2,  # "X over Y"
    "instead of": 2,
    "rather than": 2,
    "pros and cons": 2,
    "we'll use": 2,
    "we will use": 2,
    "let's use": 2,
    "let us use": 2,
    "decided to": 2,
    "decision was": 2,
    "our choice": 2,
    "we chose": 2,
    "settled on": 2,
    "opted for": 2,
    "selected ": 2,
    "chose ": 2,
    "prefer ": 2,
    "recommend ": 2,
    # Rationale markers (1 point)
    "because": 1,
    "since ": 1,  # trailing space to avoid "since_version"
    "rationale": 1,
    "the reason": 1,
    "given that": 1,
    "due to": 1,
    "as a result": 1,
    "this means": 1,
    "key insight": 1,
    "important because": 1,
    # Alternative consideration (1 point)
    "alternative": 1,
    "alternatives": 1,
    "option ": 1,
    "options ": 1,
    "compare ": 1,
    "trade-offs": 1,
    "trade offs": 1,
}

# Threshold: total points needed to invoke LLM
DEFAULT_THRESHOLD = 4


class PatternDetector:
    """Deterministic phrase scanner for decision signals.

    Scans text for weighted decision-related phrases. Returns total
    signal score. If score >= threshold, the passage likely contains
    a decision worth enriching via LLM.

    Patterns are loaded from an optional SQLite patterns table,
    falling back to built-in defaults. User can extend at runtime.

    Usage:
        detector = PatternDetector(db_path=Path("/path/to/index.db"))
        score, matches = detector.scan("We decided to use Postgres over MySQL")
        if score >= 4:
            # Invoke structured extraction LLM
            ...
    """

    def __init__(
        self,
        db_path: Path | str | None = None,
        threshold: int = DEFAULT_THRESHOLD,
    ):
        self._threshold = threshold
        self._conn: sqlite3.Connection | None = None
        self._db_path: Path | None = None
        self._patterns: dict[str, int] = dict(DEFAULT_PATTERNS)

        if db_path:
            self._db_path = Path(db_path)
            self._conn = sqlite3.connect(str(self._db_path))
            self._ensure_table()
            self._load_user_patterns()

    def _ensure_table(self):
        """Ensure the patterns table exists (user-extensible)."""
        if not self._conn:
            return
        try:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS detector_patterns (
                    phrase TEXT PRIMARY KEY,
                    weight INTEGER NOT NULL DEFAULT 1,
                    source TEXT DEFAULT 'user',
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            self._conn.commit()
        except Exception as e:
            logger.warning("Failed to create detector_patterns table: %s", e)

    def _load_user_patterns(self):
        """Load user-defined patterns from SQLite, overriding defaults."""
        if not self._conn:
            return
        try:
            rows = self._conn.execute(
                "SELECT phrase, weight FROM detector_patterns ORDER BY weight DESC"
            ).fetchall()
            for phrase, weight in rows:
                self._patterns[phrase.lower().strip()] = weight
        except Exception as e:
            logger.warning("Failed to load user patterns: %s", e)

    def add_pattern(self, phrase: str, weight: int, source: str = "user") -> None:
        """Add or update a pattern at runtime and persist to DB if available."""
        phrase = phrase.lower().strip()
        self._patterns[phrase] = weight
        if self._conn:
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO detector_patterns (phrase, weight, source) VALUES (?, ?, ?)",
                    (phrase, weight, source),
                )
                self._conn.commit()
            except Exception as e:
                logger.warning("Failed to persist pattern: %s", e)

    def remove_pattern(self, phrase: str) -> None:
        """Remove a user pattern. Built-in defaults cannot be removed."""
        phrase = phrase.lower().strip()
        if phrase in DEFAULT_PATTERNS:
            logger.info("Cannot remove built-in pattern: %s", phrase)
            return
        self._patterns.pop(phrase, None)
        if self._conn:
            try:
                self._conn.execute("DELETE FROM detector_patterns WHERE phrase = ?", (phrase,))
                self._conn.commit()
            except Exception as e:
                logger.warning("Failed to remove pattern: %s", e)

    @property
    def patterns(self) -> dict[str, int]:
        """Return copy of current patterns (built-in + user)."""
        return dict(self._patterns)

    @property
    def threshold(self) -> int:
        return self._threshold

    @threshold.setter
    def threshold(self, value: int) -> None:
        self._threshold = value

    def scan(self, text: str) -> tuple[int, list[dict[str, Any]]]:
        """Scan text for decision signal phrases.

        Args:
            text: The text to scan (conversation transcript, message, etc.)

        Returns:
            Tuple of (total_score, matches) where matches is a list of
            dicts with 'phrase', 'weight', and 'position' keys.
        """
        if not text or not text.strip():
            return 0, []

        text_lower = text.lower()
        total_score = 0
        matches = []

        for phrase, weight in self._patterns.items():
            idx = text_lower.find(phrase)
            if idx != -1:
                total_score += weight
                matches.append({
                    "phrase": phrase,
                    "weight": weight,
                    "position": idx,
                })

        return total_score, matches

    def should_invoke_llm(self, text: str) -> tuple[bool, int, list[dict[str, Any]]]:
        """Check if text meets threshold for LLM invocation.

        Returns:
            Tuple of (should_invoke, score, matches)
        """
        score, matches = self.scan(text)
        return score >= self._threshold, score, matches

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


# ─── Module-level convenience ─────────────────────────────────────────────

_default_detector: PatternDetector | None = None


def get_detector(db_path: Path | str | None = None) -> PatternDetector:
    """Get or create the default PatternDetector instance."""
    global _default_detector
    if _default_detector is None:
        _default_detector = PatternDetector(db_path=db_path)
    return _default_detector


def scan_for_decisions(text: str, db_path: Path | str | None = None) -> tuple[int, list[dict[str, Any]]]:
    """Convenience: scan text and return (score, matches).

    Uses default threshold of 4 points.
    """
    detector = get_detector(db_path)
    return detector.scan(text)


def should_extract(text: str, db_path: Path | str | None = None) -> bool:
    """Convenience: quick check if text warrants LLM extraction.

    Uses default threshold of 4 points.
    """
    detector = get_detector(db_path)
    result, _, _ = detector.should_invoke_llm(text)
    return result
