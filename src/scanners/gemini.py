"""
Gemini CLI / Anti-Gravity scanner — chat history + brain plans + SQLite logs.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Iterator

from .base import Message, Session, register_scanner, _validate_table_name

logger = logging.getLogger(__name__)


@register_scanner("gemini")
def scan_gemini(home: Path) -> Iterator[Session]:
    """Scan Gemini CLI and Anti-Gravity history files."""
    # Gemini CLI history — group lines by conversationId
    ag_cli_history = home / ".gemini" / "antigravity-cli" / "history.jsonl"
    if ag_cli_history.is_file():
        try:
            # Read all lines and group by conversationId
            conversations: dict[str, list[dict]] = {}
            with open(ag_cli_history, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    conv_id = entry.get("conversationId", "unknown")
                    if conv_id not in conversations:
                        conversations[conv_id] = []
                    conversations[conv_id].append(entry)

            # Yield one session per conversation group
            NOISE_PATTERNS = {'/usage', '/resume', '/tasks', '/exit', '/clear',
                              'continue', '/help', '/model', '/status'}
            for conv_id, entries in conversations.items():
                messages = []
                workspace = ""
                for entry in entries:
                    display = entry.get("display", "").strip()
                    if not display:
                        continue
                    # Skip noise: slash commands and single-word continuations
                    if display.lower() in NOISE_PATTERNS:
                        continue
                    if not workspace and entry.get("workspace"):
                        workspace = entry["workspace"]
                    messages.append(Message(
                        role="user",
                        content=display,
                        timestamp=entry.get("timestamp"),
                    ))

                if messages:
                    yield Session(
                        source="gemini-cli",
                        session_id=conv_id,
                        project=workspace,
                        messages=messages,
                        metadata={"workspace": workspace},
                    )
        except Exception:
            logger.warning("Failed to read Gemini CLI history", exc_info=True)

    # Anti-Gravity brain (task/implementation plans)
    brain_dir = home / ".gemini" / "antigravity" / "brain"
    if brain_dir.is_dir():
        for task_dir in brain_dir.iterdir():
            if not task_dir.is_dir():
                continue
            task_file = task_dir / "task.md"
            impl_file = task_dir / "implementation_plan.md"
            messages = []
            if task_file.is_file():
                try:
                    content = task_file.read_text(encoding="utf-8", errors="replace")
                    if len(content) > 50000:
                        logger.warning("Anti-Gravity task file truncated from %d to 50000 chars: %s", len(content), task_file)
                    messages.append(Message(
                        role="system",
                        content=f"Anti-Gravity Task: {task_dir.name}\n\n{content[:50000]}",
                    ))
                except Exception:
                    logger.warning("Failed to read AG task: %s", task_file, exc_info=True)
            if impl_file.is_file():
                try:
                    content = impl_file.read_text(encoding="utf-8", errors="replace")
                    if len(content) > 50000:
                        logger.warning("Anti-Gravity plan file truncated from %d to 50000 chars: %s", len(content), impl_file)
                    messages.append(Message(
                        role="system",
                        content=f"Implementation Plan: {task_dir.name}\n\n{content[:50000]}",
                    ))
                except Exception:
                    logger.warning("Failed to read AG plan: %s", impl_file, exc_info=True)
            if messages:
                yield Session(
                    source="antigravity",
                    session_id=task_dir.name,
                    messages=messages,
                    metadata={"brain_dir": str(task_dir)},
                )

    # Gemini logs database (try to extract if accessible)
    logs_db = home / ".gemini" / "logs_2.sqlite"
    if logs_db.is_file():
        try:
            conn = sqlite3.connect(str(logs_db))
            cursor = conn.cursor()
            tables = cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            if tables:
                for (table_name,) in tables:
                    if not _validate_table_name(table_name):
                        continue
                    try:
                        rows = cursor.execute(
                            f"SELECT * FROM [{table_name}] LIMIT 100"
                        ).fetchall()
                        if rows:
                            col_names = [d[0] for d in cursor.description]
                            content = f"Gemini DB table: {table_name}\n"
                            content += "Columns: " + ", ".join(col_names) + "\n"
                            content += f"Rows: {len(rows)}\n"
                            messages = [Message(
                                role="system",
                                content=content[:50000],
                                metadata={"table": table_name},
                            )]
                            yield Session(
                                source="gemini-db",
                                session_id=f"gemini-{table_name}",
                                messages=messages,
                                metadata={"db_path": str(logs_db)},
                            )
                    except Exception:
                        logger.warning("Failed to read Gemini DB table: %s", table_name, exc_info=True)
                        continue
            conn.close()
        except Exception:
            logger.warning("Failed to open Gemini logs DB: %s", logs_db, exc_info=True)
