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
VERSION="${VERSION:-v0.6.0}"
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

# ─── Detect Python ─────────────────────────────────────────────────────────

# Take the first interpreter that is 3.11+, not merely the first one found:
# a system python3 that is too old must not hide an installed python3.12.
PYTHON=""
for candidate in python3 python3.14 python3.13 python3.12 python3.11; do
    if command -v "$candidate" &>/dev/null \
        && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
        PYTHON="$(command -v "$candidate")"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo -e "${RED}✗${RESET} Python 3.11+ required (looked for python3, python3.11 … python3.14)"
    exit 1
fi

PYTHON_VERSION=$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo -e "  Python: ${GREEN}$PYTHON_VERSION${RESET} ($PYTHON)"

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

# Everything the installer writes lives under HERMES_HOME, except the
# `memory-bridge` command, which goes in BIN_DIR.
# Hermes (0.10+) discovers user-installed memory providers in
# $HERMES_HOME/plugins/<name>/. Up to v0.5.0 this installer used
# $HERMES_HOME/plugins/memory/<name>/, which Hermes never scans.
REPO_DIR="${HERMES_HOME}/plugins/antharmaya-bridge"
LEGACY_DIR="${HERMES_HOME}/plugins/memory/antharmaya-bridge"
INDEX_DIR="${HERMES_HOME}/antharmaya-memory"
BIN_DIR="${MEMORY_BRIDGE_BIN_DIR:-${HOME}/.local/bin}"

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

# Carry a v0.5.x install over from the old location instead of re-cloning.
if [ ! -e "$REPO_DIR" ] && [ ! -L "$REPO_DIR" ] \
    && [ ! -L "$LEGACY_DIR" ] && is_our_checkout "$LEGACY_DIR"; then
    echo -e "  ${YELLOW}!${RESET} Moving the old install to the location Hermes actually scans:"
    echo "    $LEGACY_DIR -> $REPO_DIR"
    mv "$LEGACY_DIR" "$REPO_DIR"
    rmdir "$(dirname "$LEGACY_DIR")" 2>/dev/null || true
fi

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
    if ! git -c advice.detachedHead=false checkout --force "$VERSION" 2>/dev/null; then
        echo -e "${RED}✗${RESET} Could not check out ${VERSION} in $REPO_DIR"
        echo "  Reinstall cleanly with:"
        echo "    rm -rf \"$REPO_DIR\""
        echo "    curl -fsSL https://antharmaya.com/memory-bridge/install.sh | bash"
        exit 1
    fi
else
    echo -e "  Downloading..."
    git -c advice.detachedHead=false clone --depth 1 --branch "$VERSION" "$REPO_URL" "$REPO_DIR"
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

PYTHONPATH="$REPO_DIR" "$PYTHON" -c "
from memory_bridge import __version__
from memory_bridge.scanner import get_available_scanners
scanners = get_available_scanners()
print(f'  memory-bridge {__version__}')
print(f'  Scanners available: {len(scanners)}')
for s in scanners:
    print(f'    • {s}')
" || {
    echo -e "${RED}✗${RESET} Self-test failed"
    exit 1
}

echo ""

# ─── Put the CLI on PATH ───────────────────────────────────────────────────

# A small wrapper that runs this checkout with the interpreter chosen above.
# It never replaces a memory-bridge it did not write (e.g. one from
# `pip install memory-bridge`); the marker line identifies its own.
WRAPPER_MARKER="# memory-bridge-installer-wrapper"
WRAPPER="${BIN_DIR}/memory-bridge"
CLI_ON_PATH=0
WROTE_WRAPPER=0
mkdir -p "$BIN_DIR"
if [ -e "$WRAPPER" ] && ! grep -qF "$WRAPPER_MARKER" "$WRAPPER" 2>/dev/null; then
    echo -e "  ${YELLOW}!${RESET} $WRAPPER exists and was not written by this installer — leaving it alone."
else
    cat > "$WRAPPER" <<WRAPPER_EOF
#!/usr/bin/env bash
$WRAPPER_MARKER
# Written by the Memory Bridge installer; rerunning it rewrites this file.
# Runs the plugin checkout in $REPO_DIR
export HERMES_HOME="\${HERMES_HOME:-$HERMES_HOME}"
export PYTHONPATH="$REPO_DIR\${PYTHONPATH:+:\$PYTHONPATH}"
exec "$PYTHON" -m memory_bridge "\$@"
WRAPPER_EOF
    chmod +x "$WRAPPER"
    WROTE_WRAPPER=1
    echo -e "  ${GREEN}✓${RESET} Installed the memory-bridge command: $WRAPPER"
fi
case ":${PATH}:" in
    *":${BIN_DIR}:"*) CLI_ON_PATH=1 ;;
    *)
        echo -e "  ${YELLOW}!${RESET} $BIN_DIR is not on your PATH. Add this to your shell profile:"
        echo "    export PATH=\"$BIN_DIR:\$PATH\""
        ;;
esac

echo ""

# ─── Initial scan ──────────────────────────────────────────────────────────

# The first scan is rules-based only: no network calls, no API key needed.
# Deep LLM extraction comes later, through Hermes' own model (the
# memory_bridge_scan tool) or `memory-bridge scan` with MEMORY_BRIDGE_API_KEY.
echo -e "  Running initial scan of your agent histories (rules-based, offline)..."
echo ""

HERMES_HOME="$HERMES_HOME" PYTHONPATH="$REPO_DIR" "$PYTHON" -m memory_bridge scan --no-llm 2>&1 \
    || echo -e "  ${YELLOW}!${RESET} Initial scan had issues (non-fatal — plugin still installed)"

# ─── Done ──────────────────────────────────────────────────────────────────

echo ""
echo -e "  ${BOLD}${GREEN}✓ Memory Bridge installed!${RESET}"
echo ""
echo -e "  ${BOLD}What it found:${RESET}"
echo "  ───────────────────────────────────────────────"
echo ""

HERMES_HOME="$HERMES_HOME" PYTHONPATH="$REPO_DIR" "$PYTHON" -m memory_bridge stats 2>&1 || true

echo ""
echo -e "  ${BOLD}Next steps:${RESET}"
echo "  ───────────────────────────────────────────────"
echo ""
echo "  1. Make Memory Bridge your Hermes memory provider:"
echo "       hermes memory setup          (pick antharmaya-bridge)"
echo "     or set it in ${HERMES_HOME}/config.yaml:"
echo "       memory:"
echo "         provider: antharmaya-bridge"
echo "  2. Start a new Hermes session. Hermes now has memory_bridge_* tools;"
echo "     ask it things like \"search my memory bridge for razorpay\"."
echo "  3. From a terminal:"
if [ "$CLI_ON_PATH" -eq 1 ] || [ "$WROTE_WRAPPER" -eq 0 ]; then
    MB_CMD="memory-bridge"
else
    MB_CMD="$WRAPPER"
fi
echo "       $MB_CMD scan              # re-scan any time"
echo "       $MB_CMD search \"deploy\""
echo "     With the provider active, the same commands also run as"
echo "       hermes antharmaya-bridge <command>"
echo "  4. Any MCP client can use the same index: memory-bridge mcp"
echo ""
echo -e "  ${BOLD}Optional: re-scan daily${RESET}"
echo "  ───────────────────────────────────────────────"
echo ""
echo "    crontab:  0 6 * * * $( [ "$WROTE_WRAPPER" -eq 1 ] && echo "$WRAPPER" || echo memory-bridge ) scan --no-llm"
echo ""
echo "  or let Hermes do it with its own model (deep extraction via ctx.llm):"
echo ""
echo "    hermes cron create \"0 6 * * *\" \\"
echo "      \"Call memory_bridge_scan with use_llm=true to import new agent conversations\" \\"
echo "      --name memory-bridge-daily-scan"
echo ""
if [ -e "$LEGACY_DIR" ] || [ -L "$LEGACY_DIR" ]; then
    echo -e "  ${YELLOW}!${RESET} $LEGACY_DIR is left over from an older layout."
    echo "    Hermes does not read it; remove it when you no longer need it."
    echo ""
fi
echo -e "  ${CYAN}antharmaya.com/tools/memory-bridge${RESET}  |  ${CYAN}github.com/antharmaya/mem-bridge${RESET}"
echo ""
echo "  Memory Bridge is independent open-source software. Hermes Agent is built"
echo "  by Nous Research; this plugin is not affiliated with or endorsed by them."
