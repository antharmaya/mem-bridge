#!/usr/bin/env bash
set -euo pipefail

# ─── Antharmaya Memory Bridge — One-Line Installer ─────────────────────────
#
#   curl -fsSL https://antharmaya.com/memory-bridge/install.sh | bash
#
# Installs the unified agent memory plugin for Hermes Agent (by Nous Research; this
# plugin is independent and not affiliated with them).
# Gives Hermes photographic memory of every AI conversation you've ever had.

# Overridable so forks, mirrors and the installer tests can point elsewhere.
REPO_URL="${REPO_URL:-https://github.com/antharmaya/mem-bridge.git}"
VERSION="${VERSION:-v0.5.0}"
# REPO_DIR is derived from HERMES_HOME once that is resolved, below.

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

# Honour an externally set HERMES_HOME; fall back to the default location.
HERMES_HOME="${HERMES_HOME:-}"
if [ -z "$HERMES_HOME" ] && [ -d "${HOME}/.hermes" ]; then
    HERMES_HOME="${HOME}/.hermes"
fi

if [ -z "$HERMES_HOME" ]; then
    echo -e "${YELLOW}!${RESET} Hermes not detected — installing to the default location."
    echo "  Memory Bridge works best with Hermes, but the standalone CLI also works."
    echo "  Set HERMES_HOME to install somewhere else."
    echo ""
    HERMES_HOME="${HOME}/.hermes"
fi

# Everything the installer writes lives under HERMES_HOME.
REPO_DIR="${HERMES_HOME}/plugins/memory/antharmaya-bridge"
INDEX_DIR="${HERMES_HOME}/antharmaya-memory"

# ─── Clone / Update ────────────────────────────────────────────────────────

# $REPO_DIR can be in any of these states. Testing only `-d $REPO_DIR/.git`
# missed most of them and fell through to a clone onto an occupied path, which
# dies with "could not create work tree dir ... File exists":
#   absent                          -> clone
#   our checkout                    -> update in place
#   symlink (live or dangling)      -> replace the link, never write through it
#   unrelated directory or repo     -> move aside, then clone
# Nothing is deleted; anything displaced is renamed with a timestamp.

is_our_checkout() {
    [ -d "$1/.git" ] || return 1
    local origin
    origin="$(git -C "$1" remote get-url origin 2>/dev/null || true)"
    [ -n "$origin" ] || return 1
    # Whatever REPO_URL is configured (fork, mirror, test), plus every spelling
    # of the canonical remote (https/ssh, with or without .git).
    [ "$origin" = "$REPO_URL" ] && return 0
    case "$origin" in
        *antharmaya/mem-bridge*) return 0 ;;
        *) return 1 ;;
    esac
}

move_aside() {
    local backup="${1}.backup-$(date +%Y%m%d-%H%M%S)"
    mv "$1" "$backup"
    echo -e "    Saved as: ${backup}"
}

mkdir -p "$(dirname "$REPO_DIR")"

NEEDS_CLONE=1
if [ -L "$REPO_DIR" ]; then
    # Nearly always a developer checkout linked in. Updating through the link
    # would run `git checkout` inside a live working tree, so move the link.
    echo -e "  ${YELLOW}!${RESET} Found a symlink to $(readlink "$REPO_DIR") — replacing it."
    move_aside "$REPO_DIR"
elif is_our_checkout "$REPO_DIR"; then
    NEEDS_CLONE=0
elif [ -e "$REPO_DIR" ]; then
    echo -e "  ${YELLOW}!${RESET} $REPO_DIR exists but is not a Memory Bridge checkout."
    move_aside "$REPO_DIR"
fi

if [ "$NEEDS_CLONE" -eq 0 ]; then
    echo -e "  ${GREEN}✓${RESET} Already installed — updating..."
    cd "$REPO_DIR"
    # A --depth 1 clone carries only the tag it was cloned at, so fetch the
    # wanted tag explicitly or every later release is an "unknown revision".
    git fetch --depth 1 origin "refs/tags/${VERSION}:refs/tags/${VERSION}" 2>/dev/null \
        || git fetch --tags origin 2>/dev/null \
        || true
    if ! git checkout --force "$VERSION" 2>/dev/null; then
        echo -e "${RED}✗${RESET} Could not check out ${VERSION} in $REPO_DIR"
        echo "  Reinstall cleanly with:"
        echo "    rm -rf \"$REPO_DIR\""
        echo "    curl -fsSL https://antharmaya.com/memory-bridge/install.sh | bash"
        exit 1
    fi
else
    echo -e "  Downloading..."
    git clone --depth 1 --branch "$VERSION" "$REPO_URL" "$REPO_DIR"
    cd "$REPO_DIR"
fi

# The clone/update logic above is the part that has broken in the field, so let
# the tests exercise it without scanning the whole machine afterwards.
if [ -n "${MEMORY_BRIDGE_SETUP_ONLY:-}" ]; then
    echo -e "  ${GREEN}✓${RESET} Setup complete (MEMORY_BRIDGE_SETUP_ONLY)"
    exit 0
fi

# ─── Install Python dependencies ───────────────────────────────────────────

echo -e "  Installing dependencies..."

# Stdlib-heavy by design; pyyaml is the only extra, and only for Hermes config
# writing. Report the truth rather than hiding a failed install behind || true.
if ! "$PYTHON" -c "import yaml" 2>/dev/null; then
    "$PYTHON" -m pip install --quiet --user pyyaml 2>/dev/null || true
    if ! "$PYTHON" -c "import yaml" 2>/dev/null; then
        echo -e "  ${YELLOW}!${RESET} pyyaml not installed — Hermes config writing will be skipped."
        echo "    Install it with: $PYTHON -m pip install --user pyyaml"
    fi
fi

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
index = MemoryIndex(Path('$INDEX_DIR') / 'index.db')
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
index = MemoryIndex(Path('$INDEX_DIR') / 'index.db')
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
echo -e "  ${CYAN}antharmaya.com/tools/memory-bridge${RESET}  |  ${CYAN}github.com/antharmaya/mem-bridge${RESET}"
echo ""
echo "  This is open-source software. Contributions welcome!"
