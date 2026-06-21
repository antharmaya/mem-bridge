"""
Stats helper — provides scan_stats() for lightweight metadata discovery.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .base import discover_all


def scan_stats(home: str | Path | None = None) -> dict:
    """Return a summary of what was found without reading full content.

    Note: Currently calls discover_all() which reads full sessions.
    A future optimization (discover_metadata) would only scan directories
    without reading message content.

    TODO: Implement lightweight directory-only discovery to avoid
    loading all session messages into memory just for summary counts.
    For large histories (1000+ sessions), this is a performance concern.
    """
    sessions = discover_all(home)
    stats = {}
    for s in sessions:
        source = s.source
        if source not in stats:
            stats[source] = {"sessions": 0, "messages": 0, "projects": set()}
        stats[source]["sessions"] += 1
        stats[source]["messages"] += len(s.messages)
        if s.project:
            stats[source]["projects"].add(s.project)

    # Convert sets to counts for serialization
    result = {}
    for source, data in stats.items():
        result[source] = {
            "sessions": data["sessions"],
            "messages": data["messages"],
            "projects": len(data["projects"]),
        }
    return result
