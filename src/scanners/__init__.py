"""
Antharmaya Memory Bridge — Scanners package.

Each scanner is a self-contained module that discovers agent history files
on disk and yields Session objects. Scanners are auto-registered via the
@register_scanner decorator.

To add a new scanner:
    1. Create a new file in src/scanners/
    2. Decorate your scanner function with @register_scanner("agent-name")
    3. Import it in this __init__.py
    4. Submit a PR

See CONTRIBUTING.md for details.
"""
from __future__ import annotations

# Import all scanners so they register themselves
from . import (
    aider,
    agent_linux,
    claude_code,
    codex,
    continue_dev,
    cursor,
    gemini,
    goose,
    opencode,
)

# Re-export public API from base
from .base import (
    Message,
    Session,
    _validate_table_name,
    discover_all,
    get_available_scanners,
    register_scanner,
)

# Stats helper
from .stats import scan_stats

__all__ = [
    "Message",
    "Session",
    "discover_all",
    "get_available_scanners",
    "register_scanner",
    "scan_stats",
    "_validate_table_name",
]
