"""
Shared configuration helpers for Antharmaya Memory Bridge.
"""
from __future__ import annotations

import os
from pathlib import Path


def get_default_db_path() -> Path:
    """Return the canonical path to the memory index database.

    Uses HERMES_HOME env var if set, otherwise ~/.hermes.
    """
    hermes_home = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
    return Path(hermes_home) / "antharmaya-memory" / "index.db"
