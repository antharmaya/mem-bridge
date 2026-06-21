# Contributing to Antharmaya Memory Bridge

Thanks for your interest in contributing! Memory Bridge thrives on community
scanners — every new agent scanner makes the bridge more valuable for everyone.

## Adding a New Agent Scanner

Scanners are self-contained modules that auto-register via the
`@register_scanner` decorator. Here's the pattern:

### 1. Create the scanner file

```python
# src/scanners/my_agent.py
"""Scanner for MyAgent history files."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator

from .base import Message, Session, register_scanner

logger = logging.getLogger(__name__)


@register_scanner("my-agent")
def scan_my_agent(home: Path) -> Iterator[Session]:
    """Scan MyAgent conversation history."""
    history_file = home / ".my-agent" / "history.jsonl"
    if not history_file.is_file():
        return

    try:
        with open(history_file, encoding="utf-8", errors="replace") as f:
            for line in f:
                entry = json.loads(line.strip())
                # Parse and yield a Session per conversation
                yield Session(
                    source="my-agent",
                    session_id=entry.get("id", "unknown"),
                    messages=[Message(
                        role=entry.get("role", "user"),
                        content=entry.get("content", ""),
                    )],
                    metadata={"file_path": str(history_file)},
                )
    except Exception as e:
        logger.warning("Failed to read MyAgent history: %s", e, exc_info=True)
```

### 2. Register the scanner

Add an import in `src/scanners/__init__.py`:

```python
from . import (
    ...
    my_agent,  # Add this line
)
```

### 3. Test it

```bash
python3 -c "from src.scanner import get_available_scanners; print(get_available_scanners())"
# Verify 'my-agent' appears in the list
```

### 4. Update the README

Add your agent to the supported agents table in `README.md`.

### 5. Submit a PR

Open a pull request with your new scanner. That's it!

## Scanner Guidelines

- **Use `logger.warning(..., exc_info=True)`** for error handling — never bare
  `except Exception: pass`. One agent's broken data should never silence all
  other scanners.
- **Validate table names** when querying `sqlite_master` — use
  `_validate_table_name()` from `base.py` to prevent SQL injection.
- **Keep it focused** — one scanner per file, one source per scanner.
- **Yield Session objects** — don't collect all sessions into a list and
  return; generators are more memory-efficient.
- **Handle missing data gracefully** — if the agent's directory doesn't exist,
  just `return` (don't raise).
- **Don't read full files** into memory if you're only counting sessions.
  Use `scan_stats()` for lightweight metadata.

## Development Setup

```bash
# Clone the repo
git clone https://github.com/antharmaya-labs/hermes-memory-bridge.git
cd hermes-memory-bridge

# Install dev dependencies
pip install -e ".[dev]"

# Run tests
python3 -m pytest tests/ -v
```

## Code Standards

- Python 3.11+ type annotations everywhere
- `except Exception:` replaced with `except SpecificError:` + `logger.warning()`
- Thread safety on all public methods that touch `_index`
- Input validation at API boundaries (upsert, tool handlers)
- FTS5 operations wrapped in try/except with LIKE fallback
- All public functions have docstrings

## Questions?

Open an issue at [github.com/antharmaya-labs/hermes-memory-bridge](https://github.com/antharmaya-labs/hermes-memory-bridge)
or join the [Hermes Discord](https://discord.gg/hermes-agent).
