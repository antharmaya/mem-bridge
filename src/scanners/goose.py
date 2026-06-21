"""
Goose scanner — session transcripts.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterator

from .base import Message, Session, register_scanner

logger = logging.getLogger(__name__)


@register_scanner("goose")
def scan_goose(home: Path) -> Iterator[Session]:
    """Scan Goose session files."""
    sessions_dir = home / ".local" / "share" / "goose" / "sessions"
    if not sessions_dir.is_dir():
        return

    for session_file in sessions_dir.glob("*.jsonl"):
        try:
            messages = []
            with open(session_file, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    role = entry.get("role", "")
                    content = entry.get("content") or entry.get("text") or ""
                    # Handle content that's a list (multi-part messages)
                    if isinstance(content, list):
                        content = " ".join(
                            p.get("text", "") if isinstance(p, dict) else str(p)
                            for p in content
                        )
                    content = str(content).strip()
                    if role and content:
                        messages.append(Message(
                            role=role,
                            content=str(content).strip(),
                            timestamp=entry.get("timestamp"),
                        ))
            if messages:
                yield Session(
                    source="goose",
                    session_id=session_file.stem,
                    messages=messages,
                    metadata={"file_path": str(session_file)},
                )
        except Exception:
            logger.warning("Failed to read Goose session: %s", session_file, exc_info=True)
            continue
