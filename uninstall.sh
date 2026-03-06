#!/usr/bin/env bash
# Uninstall the Rolling Context plugin.

set -e

CLAUDE_DIR="$HOME/.claude"
PIDFILE="$CLAUDE_DIR/rolling-context-proxy.pid"
PLUGIN_LINK="$CLAUDE_DIR/plugins/rolling-context"
MARKETPLACE_CACHE="$CLAUDE_DIR/plugins/cache/rolling-context-marketplace"
MARKETPLACE_DIR="$CLAUDE_DIR/plugins/marketplaces/rolling-context-marketplace"

echo "=== Uninstalling Rolling Context ==="

# Stop proxy if running
if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID"
        echo "Stopped proxy (PID $PID)"
    fi
    rm -f "$PIDFILE"
fi

# Remove log files
rm -f "$CLAUDE_DIR/rolling-context-proxy.log" "$CLAUDE_DIR/rolling-context-proxy.log.err"

# Remove plugin link (manual install)
if [ -L "$PLUGIN_LINK" ] || [ -d "$PLUGIN_LINK" ]; then
    rm -rf "$PLUGIN_LINK"
    echo "Removed plugin link"
fi

# Remove marketplace-installed plugin cache
if [ -d "$MARKETPLACE_CACHE" ]; then
    rm -rf "$MARKETPLACE_CACHE"
    echo "Removed marketplace plugin cache"
fi

# Remove marketplace registration
if [ -d "$MARKETPLACE_DIR" ]; then
    rm -rf "$MARKETPLACE_DIR"
    echo "Removed marketplace registration"
fi

# Clean installed_plugins.json (remove our entry)
INSTALLED_FILE="$CLAUDE_DIR/plugins/installed_plugins.json"
if [ -f "$INSTALLED_FILE" ] && command -v python3 &>/dev/null; then
    python3 -c "
import json, sys
with open('$INSTALLED_FILE') as f:
    data = json.load(f)
if 'rolling-context@rolling-context-marketplace' in data.get('plugins', {}):
    del data['plugins']['rolling-context@rolling-context-marketplace']
    with open('$INSTALLED_FILE', 'w') as f:
        json.dump(data, f, indent=2)
    print('Removed from installed plugins')
"
fi

# Clean known_marketplaces.json
MARKETPLACES_FILE="$CLAUDE_DIR/plugins/known_marketplaces.json"
if [ -f "$MARKETPLACES_FILE" ] && command -v python3 &>/dev/null; then
    python3 -c "
import json
with open('$MARKETPLACES_FILE') as f:
    data = json.load(f)
if 'rolling-context-marketplace' in data:
    del data['rolling-context-marketplace']
    with open('$MARKETPLACES_FILE', 'w') as f:
        json.dump(data, f, indent=2)
    print('Removed marketplace')
"
fi

# Remove ANTHROPIC_BASE_URL from shell profiles
for profile in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.bash_profile"; do
    if [ -f "$profile" ]; then
        if grep -q "Rolling Context" "$profile" 2>/dev/null; then
            sed -i.bak '/# Rolling Context/d' "$profile"
            sed -i.bak '/ROLLING_CONTEXT_UPSTREAM/d' "$profile"
            sed -i.bak '/ANTHROPIC_BASE_URL.*127.0.0.1.*5588/d' "$profile"
            rm -f "$profile.bak"
            echo "Cleaned $profile"
        fi
    fi
done

echo ""
echo "Uninstalled. Restart your terminal to complete."
