#!/usr/bin/env bash
# Install the Rolling Context plugin for Claude Code.
#
# Pure stdlib — no pip install needed. Just requires Python 3.7+.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROXY_DIR="$SCRIPT_DIR/proxy"
PORT="${ROLLING_CONTEXT_PORT:-5588}"
PROXY_URL="http://127.0.0.1:$PORT"

echo "=== Rolling Context Proxy Installer ==="
echo ""

# 1. Check Python is available
echo "[1/3] Checking Python..."
if command -v python3 &>/dev/null; then
    PY_VERSION=$(python3 --version 2>&1)
    echo "  Found $PY_VERSION (pure stdlib — no pip install needed)"
elif command -v python &>/dev/null; then
    PY_VERSION=$(python --version 2>&1)
    echo "  Found $PY_VERSION (pure stdlib — no pip install needed)"
else
    echo "  ERROR: Python not found. Install Python 3.7+ and try again."
    exit 1
fi

# 2. Wire into Claude Code via HTTPS_PROXY + NODE_EXTRA_CA_CERTS (NOT
#    ANTHROPIC_BASE_URL, which trips the Remote Control / GrowthBook gate).
echo "[2/3] Configuring Claude Code settings.json..."

SETTINGS_FILE="$HOME/.claude/settings.json"
mkdir -p "$HOME/.claude"

PY_CMD=""
if command -v python3 &>/dev/null; then PY_CMD="python3"
elif command -v python &>/dev/null; then PY_CMD="python"
fi

if ! "$PY_CMD" "$PROXY_DIR/wire.py" --name rolling-context --settings "$SETTINGS_FILE"; then
    echo "  ERROR: could not update settings.json — left untouched."
    exit 1
fi
echo "  Settings written to $SETTINGS_FILE"

# 3. Register plugin
echo "[3/3] Registering Claude Code plugin..."

PLUGIN_LINK="$HOME/.claude/plugins/rolling-context"
mkdir -p "$HOME/.claude/plugins"

if [ -L "$PLUGIN_LINK" ] || [ -d "$PLUGIN_LINK" ]; then
    rm -rf "$PLUGIN_LINK"
fi
ln -s "$SCRIPT_DIR" "$PLUGIN_LINK"
echo "  Plugin linked at $PLUGIN_LINK"

echo ""
echo "=== Installation Complete ==="
echo ""
echo "The proxy will auto-start when you launch Claude Code."
echo "To start it manually: cd $PROXY_DIR && python3 server.py"
echo ""
echo "Configuration (via environment variables):"
echo "  ROLLING_CONTEXT_PORT    = $PORT"
echo "  ROLLING_CONTEXT_TRIGGER = ${ROLLING_CONTEXT_TRIGGER:-80000} tokens"
echo "  ROLLING_CONTEXT_TARGET  = ${ROLLING_CONTEXT_TARGET:-40000} tokens"
echo ""
echo "Start a new Claude Code session to activate the proxy."
