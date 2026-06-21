"""
Continue.dev scanner — IDE extension sessions.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterator

from .base import Message, Session, register_scanner

logger = logging.getLogger(__name__)


@register_scanner("continue-dev")
def scan_continue_dev(home: Path) -> Iterator[Session]:
    """Scan Continue.dev IDE extension sessions."""
    continue_dir = home / ".continue"
    if not continue_dir.is_dir():
        return

    # Continue stores sessions in various formats — check common locations
    sessions_dir = continue_dir / "sessions"
    if sessions_dir.is_dir():
        for session_file in sessions_dir.glob("*.json"):
            try:
                data = json.loads(session_file.read_text(encoding="utf-8", errors="replace"))
                messages = []
                if isinstance(data, dict):
                    history = data.get("history") or data.get("messages") or []
                    for msg in history:
                        if isinstance(msg, dict):
                            role = msg.get("role", "")
                            content = msg.get("content") or msg.get("message") or ""
                            if role and content:
                                if len(content) > 3000:
                                    logger.warning("Continue.dev message truncated from %d to 3000 chars: %s", len(content), session_file)
                                messages.append(Message(
                                    role=role,
                                    content=str(content)[:3000],
                                ))
                if messages:
                    yield Session(
                        source="continue-dev",
                        session_id=session_file.stem,
                        messages=messages,
                        metadata={"file_path": str(session_file)},
                    )
            except Exception:
                logger.warning("Failed to read Continue.dev session: %s", session_file, exc_info=True)
                continue
