"""
Codex (OpenAI) scanner — prompt history + memory.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .base import Message, Session, register_scanner

logger = logging.getLogger(__name__)


@register_scanner("codex")
def scan_codex(home: Path) -> Iterator[Session]:
    """Scan ~/.codex/history.jsonl — prompt history (user side only)."""
    history_file = home / ".codex" / "history.jsonl"
    if not history_file.is_file():
        return

    # Group by session_id
    sessions: dict[str, list[Message]] = {}
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

                sid = entry.get("session_id", "unknown")
                text = entry.get("text", "")
                ts = entry.get("ts")

                if text and text.strip() and text.strip().lower() != "clear":
                    cleaned = text.strip()
                    # Filter noise: very short messages, slash commands, CONTINUE
                    cleaned_lower = cleaned.lower()
                    if len(cleaned) < 3:
                        continue
                    if cleaned_lower.startswith("/") or cleaned_lower in ("continue", "clear"):
                        continue
                    if cleaned_lower.startswith("[image") or cleaned == "[]":
                        continue
                    if sid not in sessions:
                        sessions[sid] = []
                    # Convert unix timestamp to ISO
                    iso_ts = None
                    if ts:
                        try:
                            iso_ts = datetime.fromtimestamp(int(ts)).isoformat()
                        except (ValueError, OSError):
                            pass
                    sessions[sid].append(Message(
                        role="user",
                        content=text.strip(),
                        timestamp=iso_ts,
                    ))
    except Exception:
        logger.warning("Failed to read Codex history: %s", history_file, exc_info=True)
        return

    for sid, msgs in sessions.items():
        if msgs:
            yield Session(
                source="codex",
                session_id=sid,
                messages=msgs,
                metadata={"file_path": str(history_file)},
            )

    # Also scan Codex memory
    memory_file = home / ".codex" / "memories" / "MEMORY.md"
    if memory_file.is_file():
        try:
            content = memory_file.read_text(encoding="utf-8", errors="replace")
            if len(content) > 8000:
                logger.warning("Codex memory file truncated from %d to 8000 chars: %s", len(content), memory_file)
            yield Session(
                source="codex-memory",
                session_id=f"codex-memory-{hash(content) % 100000:05d}",
                messages=[Message(
                    role="system",
                    content=f"Codex Memory:\n\n{content[:8000]}",
                    metadata={"file_path": str(memory_file)},
                )],
                metadata={"file_path": str(memory_file), "is_memory": True},
            )
        except Exception:
            logger.warning("Failed to read Codex memory: %s", memory_file, exc_info=True)
