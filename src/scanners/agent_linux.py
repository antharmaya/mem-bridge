"""
Agent Linux Control scanner — event logs.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Iterator

from .base import Message, Session, register_scanner

logger = logging.getLogger(__name__)


@register_scanner("agent-linux-control")
def scan_agent_linux_control(home: Path) -> Iterator[Session]:
    """Scan Agent Linux Control events."""
    events_file = home / ".local" / "state" / "agent-linux-control" / "events.jsonl"
    if not events_file.is_file():
        return

    messages = []
    try:
        with open(events_file, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event_type = entry.get("type") or entry.get("event") or "event"
                data = entry.get("data") or entry.get("content") or ""
                if data:
                    messages.append(Message(
                        role="system",
                        content=f"[{event_type}] {str(data)[:2000]}",
                        timestamp=entry.get("timestamp") or entry.get("ts"),
                    ))
    except Exception:
        logger.warning("Failed to read Agent Linux Control events: %s", events_file, exc_info=True)
        return

    if messages:
        # Content-hash-based session_id: changes when file content changes
        file_stat = events_file.stat()
        session_id = hashlib.sha256(
            f"{events_file}:{file_stat.st_mtime}:{file_stat.st_size}".encode()
        ).hexdigest()[:16]
        yield Session(
            source="agent-linux-control",
            session_id=session_id,
            messages=messages,
            metadata={"file_path": str(events_file)},
        )
