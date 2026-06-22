"""
Deterministic recall-query understanding — no LLM, stdlib only.

Turns a natural question ("what did I do with Claude Code last month, the 15th
to 16th?") into structured recall filters: which agent, and which date window.
This is what lets prefetch() answer temporal/meta questions that plain
full-text search cannot.
"""

from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

# Spoken agent name → substring that matches the stored source_agent value.
AGENT_ALIASES: dict[str, str] = {
    "claude code": "claude-code",
    "claude-code": "claude-code",
    "claude": "claude-code",
    "codex": "codex",
    "anti-gravity": "antigravity",
    "antigravity": "antigravity",
    "gemini": "gemini",
    "cursor": "cursor",
    "opencode": "opencode",
    "open code": "opencode",
    "goose": "goose",
    "continue.dev": "continue-dev",
    "continue": "continue-dev",
    "aider": "aider",
    "agent linux": "agent-linux",
    "agent-linux": "agent-linux",
    "hermes": "hermes",
}

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})

# Verbs/phrasings that signal a "recall what happened" intent.
_RECALL_HINT = re.compile(
    r"\b(what did i|what was|remind me|recall|last (month|week|year)|yesterday|"
    r"this (month|week)|back (in|on)|earlier|history|did i (do|decide|say|build|ship))\b",
    re.IGNORECASE,
)


def _iso(dt: datetime) -> str:
    return dt.replace(tzinfo=timezone.utc).isoformat()


def _day_bounds(year: int, month: int, d1: int, d2: int) -> tuple[str, str]:
    last = calendar.monthrange(year, month)[1]
    d1 = max(1, min(d1, last))
    d2 = max(1, min(d2, last))
    lo, hi = min(d1, d2), max(d1, d2)
    return (
        _iso(datetime(year, month, lo, 0, 0, 0)),
        _iso(datetime(year, month, hi, 23, 59, 59)),
    )


def _month_bounds(year: int, month: int) -> tuple[str, str]:
    return _day_bounds(year, month, 1, calendar.monthrange(year, month)[1])


def parse_timeframe(text: str, now: Optional[datetime] = None) -> tuple[Optional[str], Optional[str]]:
    """Parse a since/until ISO window from natural text. Either may be None."""
    now = now or datetime.now(timezone.utc)
    t = text.lower()

    # Pure relative windows.
    if re.search(r"\byesterday\b", t):
        y = now - timedelta(days=1)
        return _day_bounds(y.year, y.month, y.day, y.day)
    if re.search(r"\btoday\b", t):
        return _day_bounds(now.year, now.month, now.day, now.day)
    m = re.search(r"\blast (\d{1,3}) days?\b", t)
    if m:
        return (_iso(now - timedelta(days=int(m.group(1)))), _iso(now))
    if re.search(r"\blast week\b", t):
        start = now - timedelta(days=now.weekday() + 7)
        return (_iso(start.replace(hour=0, minute=0, second=0)), _iso(start + timedelta(days=6, hours=23, minutes=59, seconds=59)))
    if re.search(r"\bthis week\b", t):
        start = now - timedelta(days=now.weekday())
        return (_iso(start.replace(hour=0, minute=0, second=0)), _iso(now))

    # Establish a reference month/year.
    year, month = now.year, now.month
    if re.search(r"\blast month\b", t):
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    elif re.search(r"\bthis month\b", t):
        pass
    else:
        for name, idx in _MONTHS.items():
            if re.search(rf"\b{name}\b", t):
                month = idx
                # If that month is still ahead of us this year, assume last year.
                if month > now.month:
                    year = now.year - 1
                break

    # Day or day-range within the reference month.
    rng = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s*(?:-|to|–|through)\s*(\d{1,2})(?:st|nd|rd|th)?\b", t)
    if rng:
        return _day_bounds(year, month, int(rng.group(1)), int(rng.group(2)))
    single = re.search(r"\b(?:on\s+)?(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\b", t)
    if single:
        d = int(single.group(1))
        return _day_bounds(year, month, d, d)

    # A month was named (or last/this month) but no day → whole month.
    if re.search(r"\b(last month|this month)\b", t) or any(re.search(rf"\b{n}\b", t) for n in _MONTHS):
        return _month_bounds(year, month)

    return (None, None)


def detect_agent(text: str) -> Optional[str]:
    """Return the source substring for the first agent named, else None."""
    t = text.lower()
    # Longest aliases first so "claude code" wins over "claude".
    for alias in sorted(AGENT_ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", t):
            return AGENT_ALIASES[alias]
    return None


def parse_recall_query(text: str, now: Optional[datetime] = None) -> dict:
    """Parse a turn into recall filters.

    Returns {agent, since, until, query, is_recall}. ``is_recall`` is True when
    the turn names an agent, a timeframe, or uses recall phrasing — the signal
    prefetch() uses to switch from plain search to scoped recall.
    """
    agent = detect_agent(text)
    since, until = parse_timeframe(text, now=now)
    is_recall = bool(agent or since or _RECALL_HINT.search(text))
    return {
        "agent": agent,
        "since": since,
        "until": until,
        "query": text.strip() or None,
        "is_recall": is_recall,
    }
