"""
Claude Code scanner — reads full conversations + memory files.

Format version detection:
- Reads first 5 lines of JSONL to detect schema version
- v1: {'display', 'pastedContents', 'timestamp', 'project', 'sessionId'}
- v2: {'type', 'message', 'sessionId', 'parentUuid'} (current)
- v3+: unknown future format → log warning, attempt best-effort parse

Stores format version in session metadata for diagnostics.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterator

from .base import Message, Session, register_scanner

logger = logging.getLogger(__name__)


@register_scanner("claude-code")
def scan_claude_code(home: Path) -> Iterator[Session]:
    """Scan ~/.claude/projects/<project>/<session-id>.jsonl files."""
    projects_dir = home / ".claude" / "projects"
    if not projects_dir.is_dir():
        return

    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        project_name = project_dir.name

        for session_file in project_dir.glob("*.jsonl"):
            try:
                format_version = detect_claude_format_version(session_file)
                messages = _parse_claude_jsonl(session_file)
                if not messages:
                    continue
                session = Session(
                    source="claude-code",
                    session_id=session_file.stem,
                    project=project_name,
                    messages=messages,
                    metadata={
                        "file_path": str(session_file),
                        "format_version": format_version,
                    },
                )
                yield session
            except Exception:
                logger.warning("Failed to parse Claude Code session: %s", session_file, exc_info=True)
                continue

    # Also scan Claude Code memory files
    for project_dir in projects_dir.iterdir():
        memory_dir = project_dir / "memory"
        if memory_dir.is_dir():
            for mem_file in memory_dir.glob("*.md"):
                try:
                    content = mem_file.read_text(encoding="utf-8", errors="replace")
                    if len(content) > 50000:
                        logger.warning("Claude Code memory file truncated from %d to 50000 chars: %s", len(content), mem_file)
                    # Create a synthetic session for memory entries
                    # Include project in session_id to avoid collisions across projects
                    project_tag = project_dir.name.replace("/", "_").replace("\\", "_")
                    session = Session(
                        source="claude-code-memory",
                        session_id=f"memory-{project_tag}-{mem_file.stem}",
                        project=project_dir.name,
                        messages=[
                            Message(
                                role="system",
                                content=f"Claude Code Memory: {mem_file.stem}\n\n{content[:50000]}",
                                metadata={"file_path": str(mem_file)},
                            )
                        ],
                        metadata={"file_path": str(mem_file), "is_memory": True},
                    )
                    yield session
                except Exception:
                    logger.warning("Failed to read Claude Code memory file: %s", mem_file, exc_info=True)
                    continue


def detect_claude_format_version(path: Path) -> str:
    """Detect Claude Code JSONL format version by examining first 5 lines.

    Returns:
        "v1", "v2", or "unknown" with version details
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = []
            for _ in range(5):
                line = f.readline()
                if not line:
                    break
                line = line.strip()
                if line:
                    lines.append(line)

        if not lines:
            return "empty"

        # Examine fields in first valid JSON line
        for line in lines:
            try:
                entry = json.loads(line)
                fields = set(entry.keys())

                # v1: older format with 'display', 'pastedContents'
                if fields & {"display", "pastedContents", "project"}:
                    return "v1"

                # v2: current format with 'type', 'message', 'parentUuid'
                if fields & {"type", "message", "parentUuid"}:
                    return "v2"

            except json.JSONDecodeError:
                continue

        # Unknown format — log diagnostic info
        first_line_preview = lines[0][:100] if lines else ""
        logger.warning(
            "Unknown Claude Code format in %s. First line fields: %s",
            path, first_line_preview,
        )
        return "unknown"

    except Exception as e:
        logger.warning("Failed to detect format version for %s: %s", path, e)
        return "unknown"


def _parse_claude_jsonl(path: Path) -> list[Message]:
    """Parse Claude Code's JSONL conversation format.

    Handles v1 and v2 formats transparently.
    Captures tool results as 'tool' role messages.
    """
    messages = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            entry_type = entry.get("type", "")

            if entry_type == "user":
                msg_data = entry.get("message", {})
                content = msg_data.get("content", "")
                if isinstance(content, list):
                    # Multi-part content (text + images)
                    text_parts = [p.get("text", "") for p in content if p.get("type") == "text"]
                    content = "\n".join(text_parts)
                if content and isinstance(content, str) and content.strip():
                    messages.append(Message(
                        role="user",
                        content=str(content),
                        timestamp=entry.get("timestamp"),
                    ))

            elif entry_type == "assistant":
                msg_data = entry.get("message", {})
                content = msg_data.get("content", "")
                if isinstance(content, list):
                    text_parts = [p.get("text", "") for p in content if p.get("type") == "text"]
                    content = "\n".join(text_parts)
                if content and isinstance(content, str) and content.strip():
                    messages.append(Message(
                        role="assistant",
                        content=str(content),
                        timestamp=entry.get("timestamp"),
                    ))

            elif entry_type == "system":
                content = entry.get("content", "")
                if content and isinstance(content, str) and content.strip():
                    messages.append(Message(
                        role="system",
                        content=str(content),
                        timestamp=entry.get("timestamp"),
                    ))

            elif entry_type == "tool_result":
                # Tool results contain important context (compile output, etc.)
                content = entry.get("content", "")
                if isinstance(content, list):
                    text_parts = [p.get("text", "") for p in content if p.get("type") == "text"]
                    content = "\n".join(text_parts)
                if content and isinstance(content, str) and content.strip():
                    # Truncate very long tool results
                    content_str = str(content)[:3000]
                    if len(content_str) >= 3000:
                        content_str += "\n...[truncated]"
                    messages.append(Message(
                        role="tool",
                        content=content_str,
                        timestamp=entry.get("timestamp"),
                    ))

            # Handle v1 format: entries with 'display' field
            elif not entry_type and entry.get("display"):
                content = entry.get("display", "")
                if content and isinstance(content, str) and content.strip():
                    messages.append(Message(
                        role="user",
                        content=str(content),
                        timestamp=entry.get("timestamp"),
                    ))

    return messages
