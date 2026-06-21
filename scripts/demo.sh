#!/usr/bin/env bash
#
# demo.sh — Antharmaya Memory Bridge Demo
#
# Generates fake agent history files and runs the full pipeline:
# scan → extract → index → search
#
# Usage:
#   bash scripts/demo.sh
#
# This creates temporary files in /tmp/memory-bridge-demo/
# and cleans up on exit.

set -euo pipefail

DEMO_DIR="/tmp/memory-bridge-demo"
SCAN_DIR="${DEMO_DIR}/home"

echo "🧠 Antharmaya Memory Bridge — Demo"
echo "======================================"
echo ""

# ── Setup ────────────────────────────────────────────────────────────────
echo "📁 Creating fake agent history in ${SCAN_DIR}..."
rm -rf "${DEMO_DIR}" 2>/dev/null || true
mkdir -p "${SCAN_DIR}"

# ── Claude Code sessions ────────────────────────────────────────────────
echo "  → Claude Code sessions..."
mkdir -p "${SCAN_DIR}/.claude/projects/myapp"
mkdir -p "${SCAN_DIR}/.claude/projects/myapp/memory"

# Session 1: Architecture decision
cat > "${SCAN_DIR}/.claude/projects/myapp/session_001.jsonl" << 'EOF'
{"type": "user", "message": {"content": "Let's design the architecture for a photo-sharing app"}, "timestamp": "2026-01-15T10:00:00"}
{"type": "assistant", "message": {"content": "I've decided we will use Cloudflare Workers for the API layer. It's the right call for edge deployment and cost at scale."}, "timestamp": "2026-01-15T10:00:05"}
{"type": "user", "message": {"content": "What about the database?"}, "timestamp": "2026-01-15T10:00:10"}
{"type": "assistant", "message": {"content": "We'll use PostgreSQL 16 on port 5433 hosted on Hetzner CX32. Lesson learned from last project: always set connection pooling limits explicitly."}, "timestamp": "2026-01-15T10:00:15"}
{"type": "user", "message": {"content": "I prefer explicit CORS origins — never use wildcard * in production"}, "timestamp": "2026-01-15T10:00:20"}
{"type": "assistant", "message": {"content": "Noted. Your preference for explicit CORS origins will be followed. Let me document that in the API config."}, "timestamp": "2026-01-15T10:00:25"}
EOF

# Memory file
cat > "${SCAN_DIR}/.claude/projects/myapp/memory/stack.md" << 'EOF'
# Stack Decisions

## Backend
- Cloudflare Workers for API layer (edge deployment)
- PostgreSQL 16 on Hetzner CX32 (port 5433)
- Redis 7 on port 6380

## Frontend
- React with TypeScript
- Cloudflare Pages for hosting

## Key Rules
- Never use wildcard CORS in production
- Always set connection pooling limits
EOF

# Session 2: Deployment lesson
cat > "${SCAN_DIR}/.claude/projects/myapp/session_002.jsonl" << 'EOF'
{"type": "user", "message": {"content": "The deployment failed again"}, "timestamp": "2026-01-16T14:00:00"}
{"type": "assistant", "message": {"content": "Turns out the issue was the trailing slash in the OAuth redirect URI. Lesson learned from Razorpay integration: always strip trailing slashes from redirect URIs."}, "timestamp": "2026-01-16T14:00:05"}
{"type": "tool_result", "content": "npm run build succeeded\nwrangler publish --env production\nUploaded to Cloudflare Workers\nDeployed at https://api.myapp.workers.dev", "timestamp": "2026-01-16T14:00:10"}
EOF

# ── Codex sessions ──────────────────────────────────────────────────────
echo "  → Codex sessions..."
mkdir -p "${SCAN_DIR}/.codex"

cat > "${SCAN_DIR}/.codex/history.jsonl" << 'EOF'
{"session_id": "codex-web-001", "text": "Generate a React component for photo upload", "ts": "1700000000"}
{"session_id": "codex-web-001", "text": "Use TypeScript strict mode", "ts": "1700000001"}
{"session_id": "codex-web-002", "text": "Docker compose configuration for local dev", "ts": "1700000002"}
{"session_id": "codex-web-002", "text": "Add healthcheck with depends_on condition", "ts": "1700000003"}
{"session_id": "codex-web-001", "text": "Refactor to use React Query", "ts": "1700000004"}
EOF

# Codex memory
mkdir -p "${SCAN_DIR}/.codex/memories"
cat > "${SCAN_DIR}/.codex/memories/MEMORY.md" << 'EOF'
# Codex Memory

## Tech Stack
- React 18 with TypeScript (strict mode)
- React Query for server state
- TailwindCSS for styling
- Docker Compose for local development

## Preferences
- Prefers function components over class components
- Uses absolute imports with @/ prefix
- Enzyme-free testing (React Testing Library only)
EOF

# ── Gemini sessions ─────────────────────────────────────────────────────
echo "  → Gemini CLI sessions..."
mkdir -p "${SCAN_DIR}/.gemini/antigravity-cli"

cat > "${SCAN_DIR}/.gemini/antigravity-cli/history.jsonl" << 'EOF'
{"conversationId": "gemini-plan-001", "display": "Plan the photo sharing platform architecture", "timestamp": "2026-01-10", "workspace": "/projects/photoshare"}
{"conversationId": "gemini-plan-001", "display": "Deploy to Cloudflare Workers for the API", "timestamp": "2026-01-10", "workspace": "/projects/photoshare"}
{"conversationId": "gemini-plan-001", "display": "Use PostgreSQL with vector extensions for image search", "timestamp": "2026-01-10", "workspace": "/projects/photoshare"}
{"conversationId": "gemini-cost-001", "display": "Calculate monthly costs for Hetzner vs AWS", "timestamp": "2026-01-11", "workspace": "/projects/photoshare"}
{"conversationId": "gemini-cost-001", "display": "Hetzner CX32 at 40EUR/month beats AWS t3.medium at 60EUR", "timestamp": "2026-01-11", "workspace": "/projects/photoshare"}
EOF

echo ""
echo "✅ Fake agent history created!"
echo ""

# ── Run the pipeline ────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "${SCRIPT_DIR}"

# Override HOME so discover_all picks up our fake data
export HOME="${SCAN_DIR}"

echo "🚀 Running scan (FastExtractor-only mode)..."
echo ""

export MEMORY_BRIDGE_DB_PATH="${DEMO_DIR}/index.db"

python3 -c "
import sys
sys.path.insert(0, '${SCRIPT_DIR}')
from src.scanner import discover_all
from src.indexer import MemoryIndex
from src.extractor import FastExtractor
from src.config import get_default_db_path
from pathlib import Path

# Override db path
db_path = Path('${DEMO_DIR}/index.db')
index = MemoryIndex(db_path)

sessions = discover_all()
sources = set(s.source for s in sessions)
print(f'Discovered {len(sessions)} sessions across {len(sources)} sources')
for s in sessions:
    print(f'  [{s.source}] {s.session_id[:30]:30s} ({len(s.messages)} msgs)')
print()

# Extract facts
total_facts = 0
for session in sessions:
    entries = FastExtractor.extract(session)
    for e in entries:
        index.upsert(e)
    index.mark_source_processed(session.source, session.session_id, len(session.messages))
    total_facts += len(entries)

print(f'Extracted {total_facts} facts via FastExtractor')
print()

# Show stats
stats = index.stats()
print('📊 Index Statistics:')
print(f'  Total entries: {stats[\"total_entries\"]}')
print(f'  Schema version: {stats[\"schema_version\"]}')
print(f'  Hash algorithm: {stats[\"hash_algorithm\"]}')
print()
print('  By category:')
for cat, count in sorted(stats['by_category'].items()):
    print(f'    {cat}: {count}')
print()
print('  By source:')
for src, count in sorted(stats['by_source'].items()):
    print(f'    {src}: {count}')
print()

# Search
print('🔍 Search results for \"Cloudflare\":')
results = index.search_fts('Cloudflare', limit=5)
for r in results:
    print(f'  [{r.category.upper()}] {r.content}')
if not results:
    print('  (none found)')

print()
print('🔍 Search results for \"PostgreSQL\":')
results = index.search_fts('PostgreSQL', limit=5)
for r in results:
    print(f'  [{r.category.upper()}] {r.content}')
if not results:
    print('  (none found)')

print()
print('🔍 Search results for \"lesson\":')
results = index.search_fts('lesson', limit=5)
for r in results:
    print(f'  [{r.category.upper()}] {r.content}')
if not results:
    print('  (none found)')

index.close()
" 2>&1

echo ""
echo "======================================"
echo "✅ Demo complete!"
echo ""
echo "Files created:"
echo "  6 Claude Code sessions (3 real + 1 memory)"
echo "  2 Codex sessions (3 real + 1 memory)"
echo "  2 Gemini CLI conversations"
echo ""
echo "To run a full scan:"
echo "  memory-bridge scan --home ${SCAN_DIR} --verbose"
echo ""
echo "To search:"
echo "  memory-bridge search 'Cloudflare'"
echo ""
echo "To see quality metrics:"
echo "  memory-bridge quality"
