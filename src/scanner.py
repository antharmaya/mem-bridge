"""
Agent history scanner — auto-discovers every AI agent's chat history on disk.

Supports: Claude Code, Codex (OpenAI), Gemini CLI, Anti-Gravity, OpenCode,
Cursor, Goose, Agent Linux Control, and any future agent.

Each scanner returns a list of Session objects with canonicalized messages.

This module now re-exports from src/scanners/ for backward compatibility.
"""
from __future__ import annotations

from src.scanners import (
    Message,
    Session,
    _validate_table_name,
    discover_all,
    get_available_scanners,
    register_scanner,
    scan_stats,
)

__all__ = [
    "Message",
    "Session",
    "discover_all",
    "get_available_scanners",
    "register_scanner",
    "scan_stats",
    "_validate_table_name",
]
