#!/usr/bin/env bash
set -euo pipefail

# ─── Antharmaya Memory Bridge — One-Line Installer ─────────────────────────
#
#   curl -fsSL https://antharmaya.com/memory-bridge/install.sh | bash
#
# Installs the unified agent memory plugin for Hermes Agent.
# Gives Hermes photographic memory of every AI conversation you've ever had.

REPO_URL="https://github.com/antharmaya/mem-bridge.git"
REPO_DIR="${HOME}/.hermes/plugins/memory/antharmaya-bridge"
VERSION="${VERSION:-v0.4.2}"

BOLD="\033[1m"
GREEN="\033[32m"
YELLOW="\033[33m"
CYAN="\033[36m"
RED="\033[31m"
RESET="\033[0m"

echo ""
echo -e "${BOLD}${CYAN}  🧠 Antharmaya Memory Bridge${RESET}"
echo -e "  ${BOLD}Unified Agent Memory for Hermes${RESET}"
echo ""

# ─── Pre-flight checks ─────────────────────────────────────────────────────

check_cmd() {
    if ! command -v "$1" &>/dev/null; then
        echo -e "${RED}✗${RESET} Missing required command: ${BOLD}$1${RESET}"
        echo "  Install it and try again."
        exit 1
    fi
}

check_cmd git
check_cmd python3

# ─── Detect Python ─────────────────────────────────────────────────────────

PYTHON=""
for candidate in python3 python3.11 python3.12 python3.13; do
    if command -v "$candidate" &>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo -e "${RED}✗${RESET} No Python 3 found"
    exit 1
fi

PYTHON_VERSION=$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo -e "  Python: ${GREEN}$PYTHON_VERSION${RESET}"

# Check Python version >= 3.11
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)
if [ "$PYTHON_MAJOR" -lt 3 ] || [ "$PYTHON_MAJOR" -eq 3 -a "$PYTHON_MINOR" -lt 11 ]; then
    echo -e "${RED}✗${RESET} Python 3.11+ required, found $PYTHON_VERSION"
    exit 1
fi

# ─── Check for Hermes ──────────────────────────────────────────────────────

HERMES_HOME=""
if [ -n "${HERMES_HOME:-}" ]; then
    HERMES_HOME="$HERMES_HOME"
elif [ -d "${HOME}/.hermes" ]; then
    HERMES_HOME="${HOME}/.hermes"
fi

if [ -z "$HERMES_HOME" ]; then
    echo -e "${YELLOW}!${RESET} Hermes Agent not detected."
    echo "  Memory Bridge works best with Hermes, but the standalone CLI also works."
    echo "  Install Hermes: curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash"
    echo ""
fi

# ─── Clone / Update ────────────────────────────────────────────────────────

if [ -d "$REPO_DIR/.git" ]; then
    echo -e "  ${GREEN}✓${RESET} Already installed — updating..."
    cd "$REPO_DIR"
    git fetch origin
    git checkout "$VERSION"
    git pull --ff-only origin "$VERSION" 2>/dev/null || true
else
    echo -e "  Downloading..."
    mkdir -p "$(dirname "$REPO_DIR")"
    git clone --depth 1 --branch "$VERSION" "$REPO_URL" "$REPO_DIR"
    cd "$REPO_DIR"
fi

# ─── Install Python dependencies ───────────────────────────────────────────

echo -e "  Installing dependencies..."

# Use pip to install only what's needed (stdlib-heavy, minimal deps)
"$PYTHON" -m pip install --quiet --user pyyaml 2>/dev/null || true

# ─── Verify ────────────────────────────────────────────────────────────────

echo ""
echo -e "  Running self-test..."

"$PYTHON" -c "
import sys
sys.path.insert(0, '$REPO_DIR')
from src.scanner import get_available_scanners
scanners = get_available_scanners()
print(f'  Scanners available: {len(scanners)}')
for s in scanners:
    print(f'    • {s}')
" || {
    echo -e "${RED}✗${RESET} Self-test failed"
    exit 1
}

echo ""

# ─── Initial scan ──────────────────────────────────────────────────────────

HAS_API_KEY="${MEMORY_BRIDGE_API_KEY:-${OPENROUTER_API_KEY:-}}"
if [ -n "$HAS_API_KEY" ]; then
    USE_LLM_FLAG=""
    echo -e "  ${GREEN}✓${RESET} API key detected — LLM-powered extraction enabled"
else
    echo -e "  ${YELLOW}!${RESET} No API key set — rules-based extraction only"
    echo "  Set MEMORY_BRIDGE_API_KEY for deep LLM consolidation"
fi

echo ""
echo -e "  Running initial scan of your agent histories..."
echo ""

"$PYTHON" -c "
import sys
sys.path.insert(0, '$REPO_DIR')
from pathlib import Path
from src.scanner import discover_all
from src.indexer import MemoryIndex
from src.extractor import FastExtractor

home = Path.home()
index = MemoryIndex(Path.home() / '.hermes' / 'antharmaya-memory' / 'index.db')
sessions = discover_all(home)

new = 0
skipped = 0
for session in sessions:
    if index.is_source_processed(session.source, session.session_id):
        skipped += 1
        continue
    entries = FastExtractor.extract(session)
    for entry in entries:
        index.upsert(entry)
    index.mark_source_processed(session.source, session.session_id, len(session.messages))
    new += 1

stats = index.stats()
print(f'  Scanned: {len(sessions)} sessions')
print(f'  New: {new}')
print(f'  Skipped: {skipped}')
print(f'  Total entries: {stats[\"total_entries\"]}')
if stats['by_source']:
    for src, count in stats['by_source'].items():
        print(f'    • {src}: {count}')
index.close()
" 2>&1 || echo -e "  ${YELLOW}!${RESET} Initial scan had issues (non-fatal — plugin still installed)"

# ─── Done ──────────────────────────────────────────────────────────────────

echo ""
echo -e "  ${BOLD}${GREEN}✓ Memory Bridge installed!${RESET}"
echo ""
echo -e "  ${BOLD}What it found:${RESET}"
echo "  ───────────────────────────────────────────────"
echo ""

"$PYTHON" -c "
import sys
sys.path.insert(0, '$REPO_DIR')
from pathlib import Path
from src.indexer import MemoryIndex
index = MemoryIndex(Path.home() / '.hermes' / 'antharmaya-memory' / 'index.db')
stats = index.stats()
print(f'  🧠 {stats[\"total_entries\"]} memories consolidated')
print(f'  📂 {stats[\"processed_sessions\"]} agent sessions processed')
for src, count in sorted(stats.get('by_source', {}).items()):
    print(f'  📝 {src}: {count} entries')
index.close()
" 2>&1 || true

echo ""
echo -e "  ${BOLD}Next steps:${RESET}"
echo "  ───────────────────────────────────────────────"
echo ""
echo "  1. Restart your Hermes session (the plugin loads on next start)"
echo "  2. Type /memory_bridge_stats to see your memory summary"
echo "  3. Type /memory_bridge_search \"your query\" to search"
echo "  4. Run 'memory-bridge scan' to re-scan anytime"
echo ""
echo -e "  ${BOLD}Optional: Auto-scan daily with cron${RESET}"
echo "  ───────────────────────────────────────────────"
echo ""
echo "  The bridge uses Hermes' own model for deep extraction"
echo "  (no separate API key needed — it uses ctx.llm)."
echo ""
echo "  To auto-scan for new agent conversations daily:"
echo ""
echo "    hermes cron create \"0 6 * * *\" \\"
echo "      --name \"memory-bridge-daily-scan\" \\"
echo "      --prompt \"Run memory_bridge_scan use_llm=true to import new agent conversations\""
echo ""
echo ""
echo -e "  ${CYAN}antharmaya.com/memory-bridge${RESET}  |  ${CYAN}github.com/antharmaya/mem-bridge${RESET}"
echo ""
echo "  This is open-source software. Contributions welcome!"
