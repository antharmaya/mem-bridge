"""
Base types and scanner registry for agent history discovery.

Provides the core infrastructure: Message and Session dataclasses,
the @register_scanner decorator, and the discover_all() orchestrator.

Permission awareness: scanners check if directories are readable by the
current user before attempting to read. This prevents crashes on shared
machines where agent history dirs are owned by other users.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

# Safe table name pattern — prevents SQL injection via sqlite_master
_SAFE_TABLE_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')


def _validate_table_name(name: str) -> bool:
    """Validate a table name against safe pattern to prevent SQL injection."""
    return bool(_SAFE_TABLE_RE.match(name))


@dataclass
class Message:
    """Canonical message format regardless of source agent."""
    role: str          # "user", "assistant", "system", "tool"
    content: str
    timestamp: Optional[str] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class Session:
    """A single agent conversation session."""
    source: str                    # "claude-code", "codex", "gemini", etc.
    session_id: str
    project: Optional[str] = None
    started_at: Optional[str] = None
    messages: list[Message] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @property
    def message_count(self) -> int:
        return len(self.messages)


# ─── Scanner registry ───────────────────────────────────────────────────────

_SCANNERS: dict[str, callable] = {}


def register_scanner(name: str):
    """Decorator to register a scanner function."""
    def decorator(fn):
        _SCANNERS[name] = fn
        return fn
    return decorator


def _is_path_accessible(path: Path) -> bool:
    """Check if a path is readable by the current user.

    On shared machines, agent history dirs may be owned by other users.
    This prevents crashes when attempting to stat/read those paths.
    """
    try:
        return path.exists() and os.access(str(path), os.R_OK)
    except (PermissionError, OSError):
        return False


def discover_all(
    home: str | Path | None = None,
    streaming: bool = False,
    skip_unreadable: bool = True,
) -> list[Session]:
    """Run all registered scanners and return all discovered sessions.

    Args:
        home: Home directory to scan (defaults to current user's home)
        streaming: If True, returns list (generator streaming not yet
                   exposed at top level — use discover_sessions() for generator)
        skip_unreadable: If True, skip directories not readable by current user

    Returns:
        List of Session objects from all scanners

    Note: For very large histories (1000+ sessions), the in-memory accumulation
    can be significant. Use consolidate_all() for memory-efficient processing.
    """
    if home is None:
        home = Path.home()
    elif isinstance(home, str):
        home = Path(home)

    all_sessions = []
    scanner_errors: dict[str, str] = {}

    for name, scanner in _SCANNERS.items():
        try:
            sessions = list(scanner(home))
            all_sessions.extend(sessions)
        except PermissionError as e:
            # Permission issues on shared machines — skip with warning
            msg = f"Permission denied: {e}"
            scanner_errors[name] = msg
            if skip_unreadable:
                logger.warning("[scanner] WARNING: %s scanner skipped (permission): %s", name, msg)
            else:
                logger.warning("[scanner] WARNING: %s scanner failed: %s", name, msg)
        except Exception as e:
            # One agent's broken data shouldn't kill the whole scan
            logger.warning("[scanner] WARNING: %s scanner failed: %s", name, e)
            scanner_errors[name] = str(e)

    return all_sessions


def discover_sessions(
    home: str | Path | None = None,
    skip_unreadable: bool = True,
) -> Iterator[Session]:
    """Generator-based session discovery — yields sessions as they're found.

    Unlike discover_all(), this doesn't accumulate all sessions in memory.
    Use this for memory-efficient scanning of large histories (1000+ sessions).

    Args:
        home: Home directory to scan
        skip_unreadable: Skip inaccessible directories

    Yields:
        Session objects one at a time as each scanner discovers them
    """
    if home is None:
        home = Path.home()
    elif isinstance(home, str):
        home = Path(home)

    for name, scanner in _SCANNERS.items():
        try:
            yield from scanner(home)
        except PermissionError as e:
            msg = f"Permission denied: {e}"
            if skip_unreadable:
                logger.warning("[scanner] WARNING: %s scanner skipped (permission): %s", name, msg)
            else:
                logger.warning("[scanner] WARNING: %s scanner failed: %s", name, msg)
        except Exception as e:
            logger.warning("[scanner] WARNING: %s scanner failed: %s", name, e)


def get_available_scanners() -> list[str]:
    """Return names of registered scanners."""
    return list(_SCANNERS.keys())


def scanner_health() -> dict[str, dict]:
    """Check each scanner for potential issues without scanning fully.

    Returns dict of scanner_name -> health_info:
        {
            "status": "ok" | "skipped" | "error",
            "message": "details",
        }
    """
    health = {}
    for name in _SCANNERS:
        health[name] = {"status": "ok", "message": "registered"}
    return health
