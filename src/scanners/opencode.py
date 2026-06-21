"""
OpenCode scanner — prompt history.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterator

from .base import Message, Session, register_scanner

logger = logging.getLogger(__name__)


@register_scanner("opencode")
def scan_opencode(home: Path) -> Iterator[Session]:
    """Scan OpenCode prompt history."""
    history_file = home / ".local" / "state" / "opencode" / "prompt-history.jsonl"
    if not history_file.is_file():
        return

    messages = []
    try:
        with open(history_file, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # OpenCode format: typically has prompt/text field
                text = entry.get("prompt") or entry.get("text") or entry.get("content") or ""
                if text and text.strip():
                    messages.append(Message(
                        role="user",
                        content=text.strip(),
                        timestamp=entry.get("timestamp") or entry.get("ts"),
                    ))
    except Exception:
        logger.warning("Failed to read OpenCode history: %s", history_file, exc_info=True)
        return

    if messages:
        yield Session(
            source="opencode",
            session_id="opencode-history",
            messages=messages,
            metadata={"file_path": str(history_file)},
        )
