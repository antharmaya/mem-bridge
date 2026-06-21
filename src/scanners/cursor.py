"""
Cursor IDE scanner — plans + AI tracking data.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Iterator

from .base import Message, Session, register_scanner, _validate_table_name

logger = logging.getLogger(__name__)


@register_scanner("cursor")
def scan_cursor(home: Path) -> Iterator[Session]:
    """Scan Cursor IDE plans and AI tracking data."""
    # Cursor plans
    plans_dir = home / ".cursor" / "plans"
    if plans_dir.is_dir():
        for plan_file in plans_dir.glob("*.plan.md"):
            try:
                content = plan_file.read_text(encoding="utf-8", errors="replace")
                if len(content) > 5000:
                    logger.warning("Cursor plan truncated from %d to 5000 chars: %s", len(content), plan_file)
                yield Session(
                    source="cursor",
                    session_id=f"plan-{plan_file.stem[:40]}",
                    messages=[Message(
                        role="system",
                        content=f"Cursor Plan: {plan_file.name}\n\n{content[:5000]}",
                    )],
                    metadata={"file_path": str(plan_file), "is_plan": True},
                )
            except Exception:
                logger.warning("Failed to read Cursor plan: %s", plan_file, exc_info=True)
                continue

    # Cursor AI tracking DB
    tracking_db = home / ".cursor" / "ai-tracking" / "ai-code-tracking.db"
    if tracking_db.is_file():
        try:
            conn = sqlite3.connect(str(tracking_db))
            cursor = conn.cursor()
            tables = cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            if tables:
                content_parts = []
                for (table_name,) in tables:
                    if not _validate_table_name(table_name):
                        continue
                    try:
                        rows = cursor.execute(
                            f"SELECT * FROM [{table_name}] LIMIT 50"
                        ).fetchall()
                        if rows:
                            col_names = [d[0] for d in cursor.description]
                            content_parts.append(f"Table {table_name}: {len(rows)} rows, cols={col_names}")
                    except Exception:
                        logger.warning("Failed to read Cursor DB table: %s", table_name, exc_info=True)
                        continue
                if content_parts:
                    yield Session(
                        source="cursor",
                        session_id="cursor-ai-tracking",
                        messages=[Message(
                            role="system",
                            content="Cursor AI Tracking:\n" + "\n".join(content_parts),
                        )],
                        metadata={"db_path": str(tracking_db)},
                    )
            conn.close()
        except Exception:
            logger.warning("Failed to open Cursor AI tracking DB: %s", tracking_db, exc_info=True)
