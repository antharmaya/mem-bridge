"""
Aider scanner — AI coding assistant chat history.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator

from .base import Message, Session, register_scanner

logger = logging.getLogger(__name__)


@register_scanner("aider")
def scan_aider(home: Path) -> Iterator[Session]:
    """Scan Aider AI coding assistant chat history."""
    aider_dir = home / ".aider"
    if not aider_dir.is_dir():
        return

    # Aider stores chat history in .aider.chat.history.md or similar
    for chat_file in aider_dir.glob("*.md"):
        try:
            content = chat_file.read_text(encoding="utf-8", errors="replace")
            if not content.strip():
                continue
            # Parse markdown chat format (#### user / #### assistant blocks)
            messages = _parse_aider_markdown(content)
            if messages:
                yield Session(
                    source="aider",
                    session_id=chat_file.stem,
                    messages=messages,
                    metadata={"file_path": str(chat_file)},
                )
        except Exception:
            logger.warning("Failed to read Aider chat: %s", chat_file, exc_info=True)
            continue


def _parse_aider_markdown(content: str) -> list[Message]:
    """Parse Aider's markdown chat format."""
    messages = []
    current_role = None
    current_content = []

    for line in content.split('\n'):
        if line.startswith('#### ') or line.startswith('### ') or line.startswith('## ') or line.startswith('# '):
            # Save previous block
            if current_role and current_content:
                text = '\n'.join(current_content).strip()
                if text:
                    messages.append(Message(role=current_role, content=text))
            # Detect new block
            header = line.lower()
            if 'user' in header:
                current_role = 'user'
            elif 'assistant' in header or 'aider' in header or 'ai' in header:
                current_role = 'assistant'
            elif 'system' in header:
                current_role = 'system'
            else:
                current_role = None
            current_content = []
        elif current_role:
            current_content.append(line)

    # Save last block
    if current_role and current_content:
        text = '\n'.join(current_content).strip()
        if text:
            messages.append(Message(role=current_role, content=text))

    return messages
