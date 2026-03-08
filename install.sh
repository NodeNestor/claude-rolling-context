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

# 2. Configure ANTHROPIC_BASE_URL in Claude Code settings.json
echo "[2/3] Configuring Claude Code settings.json..."

SETTINGS_FILE="$HOME/.claude/settings.json"
mkdir -p "$HOME/.claude"

PY_CMD=""
if command -v python3 &>/dev/null; then PY_CMD="python3"
elif command -v python &>/dev/null; then PY_CMD="python"
fi

$PY_CMD - "$SETTINGS_FILE" "$PROXY_URL" <<'PYEOF'
import json, sys, os

settings_file = sys.argv[1]
proxy_url = sys.argv[2]

settings = {}
if os.path.exists(settings_file):
    try:
        with open(settings_file, "r") as f:
            settings = json.load(f)
    except (json.JSONDecodeError, IOError):
        settings = {}

if "env" not in settings or not isinstance(settings["env"], dict):
    settings["env"] = {}

env = settings["env"]

existing = env.get("ANTHROPIC_BASE_URL", "")
if not existing:
    env["ANTHROPIC_BASE_URL"] = proxy_url
    print(f"  Set ANTHROPIC_BASE_URL={proxy_url}")
elif "127.0.0.1" not in existing:
    env["ROLLING_CONTEXT_UPSTREAM"] = existing
    env["ANTHROPIC_BASE_URL"] = proxy_url
    print(f"  Chaining: ANTHROPIC_BASE_URL={proxy_url} -> upstream={existing}")
else:
    print(f"  ANTHROPIC_BASE_URL already set")

# Set plugin config defaults (only if not already present)
defaults = {
    "ROLLING_CONTEXT_PORT": "5588",
    "ROLLING_CONTEXT_TRIGGER": "100000",
    "ROLLING_CONTEXT_TARGET": "40000",
    "ROLLING_CONTEXT_MODEL": "claude-haiku-4-5-20251001",
}
for key, value in defaults.items():
    if key not in env:
        env[key] = value

with open(settings_file, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")

print(f"  Settings written to {settings_file}")
PYEOF

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
