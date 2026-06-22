"""
Zero-dependency MCP server — exposes the unified memory index to ANY MCP client.

Memory Bridge started as a Hermes plugin, but the index it builds is host-neutral.
This is the framework-agnostic frontend: a Model Context Protocol server speaking
JSON-RPC 2.0 over stdio, so Claude Desktop, Cursor, Codex, Windsurf — any MCP
client — can search, recall, and review the same local index. No SDK, no extra
dependency; stdlib only, true to the local-first identity.

Run:  memory-bridge mcp   (or: python3 -m src.mcp_server)

Client config (e.g. Claude Desktop / Cursor mcpServers):
    {
      "mcpServers": {
        "memory-bridge": { "command": "memory-bridge", "args": ["mcp"] }
      }
    }
"""

from __future__ import annotations

import json
import sys
from typing import Any, Optional

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "antharmaya-memory-bridge"
SERVER_VERSION = "0.3.0"

# ─── Tool catalogue (agent-neutral names) ────────────────────────────────

TOOLS = [
    {
        "name": "search_memory",
        "description": (
            "Full-text search across every AI agent conversation on this machine "
            "(Claude Code, Codex, Gemini, Cursor, and more), consolidated into one "
            "typo-tolerant local index. Use to recall any fact, decision, or "
            "preference from past sessions with any agent."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for"},
                "limit": {"type": "integer", "description": "Max results (default 10)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "recall",
        "description": (
            "Time- and agent-scoped recall. Answers questions like 'what did I do "
            "with Claude Code last month' or 'what did Codex and I decide on the "
            "15th' — the agent and date range are parsed from the question."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "Natural-language recall question"},
            },
            "required": ["question"],
        },
    },
    {
        "name": "list_decisions",
        "description": (
            "List structured decisions consolidated from past sessions — what was "
            "decided, the framework that shaped it, and whether the outcome was "
            "later verified."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "framework": {"type": "string", "description": "Optional framework filter"},
                "unverified_only": {"type": "boolean", "description": "Only outcome-unverified decisions"},
                "limit": {"type": "integer", "description": "Max results (default 20)"},
            },
        },
    },
    {
        "name": "memory_stats",
        "description": "Summary of the unified index: entry counts by source/category, decisions, schema version.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# ─── Index access + tool execution ───────────────────────────────────────

def _open_index():
    from src.indexer import MemoryIndex
    from src.config import get_default_db_path

    db_path = get_default_db_path()
    if not db_path.exists():
        return None
    return MemoryIndex(db_path)


def execute_tool(name: str, args: dict, index=None) -> str:
    """Run a tool and return human-readable text. `index` is injectable for tests."""
    own = False
    if index is None:
        index = _open_index()
        own = True
    if index is None:
        return "No memory index found yet. Run `memory-bridge scan` first."

    try:
        if name == "search_memory":
            results = index.search_fts(args.get("query", ""), limit=int(args.get("limit", 10)))
            if not results:
                return "No matching memories."
            return "\n".join(
                f"- [{r.category.upper()}] ({r.source_agent} · {(r.created_at or '')[:10]}) {r.content}"
                for r in results
            )

        if name == "recall":
            from src.recall_query import parse_recall_query
            p = parse_recall_query(args.get("question", ""))
            entries = index.recall(
                agent=p["agent"], since=p["since"], until=p["until"],
                query=None if (p["agent"] or p["since"]) else args.get("question"),
                limit=20,
            )
            decisions = index.recall_decisions(
                agent=p["agent"], since=p["since"], until=p["until"], limit=10
            )
            scope = []
            if p["agent"]:
                scope.append(f"agent={p['agent']}")
            if p["since"]:
                scope.append(f"{p['since'][:10]}→{(p['until'] or '')[:10]}")
            lines = [f"Recall ({', '.join(scope) or 'full-text'}): {len(entries)} memories"]
            for e in entries:
                lines.append(f"- ({(e.created_at or '')[:10]}) [{e.category}] {e.content}")
            if decisions:
                lines.append("\nDecisions in this window:")
                for d in decisions:
                    lines.append(f"- ({d.get('framework_used') or 'unknown'}) {d.get('decision_text', '')}")
            return "\n".join(lines)

        if name == "list_decisions":
            decisions = index.get_decisions(
                limit=int(args.get("limit", 20)),
                framework=args.get("framework") or None,
                unverified_only=bool(args.get("unverified_only", False)),
            )
            if not decisions:
                return "No structured decisions found."
            verdict = {1: "✓ worked", -1: "✗ failed", 0: "unverified"}
            return "\n".join(
                f"- [{d.get('framework_used') or 'unknown'}] {d.get('decision_text', '')} "
                f"({verdict.get(d.get('outcome_verified', 0), 'unverified')})"
                for d in decisions
            )

        if name == "memory_stats":
            return json.dumps(index.stats(), indent=2)

        return f"Unknown tool: {name}"
    finally:
        if own:
            index.close()


# ─── JSON-RPC handling ───────────────────────────────────────────────────

def _ok(mid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def handle_message(msg: dict, index=None) -> Optional[dict]:
    """Process one JSON-RPC message. Returns a response dict, or None for notifications."""
    method = msg.get("method")
    mid = msg.get("id")

    if method == "initialize":
        requested = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION
        return _ok(mid, {
            "protocolVersion": requested,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    # Notifications (no id) get no response.
    if mid is None or (isinstance(method, str) and method.startswith("notifications/")):
        return None

    if method == "tools/list":
        return _ok(mid, {"tools": TOOLS})

    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            text = execute_tool(name, args, index=index)
            return _ok(mid, {"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as e:  # surface tool errors as MCP tool errors, not crashes
            return _ok(mid, {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True})

    if method == "ping":
        return _ok(mid, {})

    return _err(mid, -32601, f"Method not found: {method}")


def serve(stdin=None, stdout=None) -> None:
    """Run the stdio JSON-RPC loop until EOF."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle_message(msg)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()


if __name__ == "__main__":
    serve()
