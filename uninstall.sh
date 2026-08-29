#!/usr/bin/env bash
# Uninstall the Rolling Context plugin.

set -e

CLAUDE_DIR="$HOME/.claude"
PIDFILE="$CLAUDE_DIR/rolling-context-proxy.pid"
PLUGIN_LINK="$CLAUDE_DIR/plugins/rolling-context"
MARKETPLACE_CACHE="$CLAUDE_DIR/plugins/cache/rolling-context-marketplace"
MARKETPLACE_DIR="$CLAUDE_DIR/plugins/marketplaces/rolling-context-marketplace"
PORT="${ROLLING_CONTEXT_PORT:-5588}"

echo "=== Uninstalling Rolling Context ==="

# Stop proxy — try PID file first, then find by port
STOPPED=false
if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID"
        echo "Stopped proxy (PID $PID)"
        STOPPED=true
    fi
    rm -f "$PIDFILE"
fi
rm -f "$CLAUDE_DIR/rolling-context-proxy.version"
if [ "$STOPPED" = false ]; then
    PROXY_PID=$(lsof -ti:$PORT 2>/dev/null || ss -tlnp "sport = :$PORT" 2>/dev/null | grep -oP 'pid=\K\d+' || true)
    if [ -n "$PROXY_PID" ]; then
        kill $PROXY_PID 2>/dev/null || true
        echo "Stopped proxy on port $PORT"
    fi
fi

# Remove all log files
rm -f "$CLAUDE_DIR/rolling-context-proxy.log"
rm -f "$CLAUDE_DIR/rolling-context-proxy.log.err"
rm -f "$CLAUDE_DIR/rolling-context-debug.log"
rm -f "$CLAUDE_DIR/rolling-context-hook.log"

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

# Clean installed_plugins.json
INSTALLED_FILE="$CLAUDE_DIR/plugins/installed_plugins.json"
if [ -f "$INSTALLED_FILE" ] && command -v python3 &>/dev/null; then
    python3 -c "
import json
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

# Unwire from Claude Code settings.json — removes HTTPS_PROXY / chaining /
# NODE_EXTRA_CA_CERTS (repairing the chain so pii-proxy keeps working if it is
# still installed). Shared, tested implementation in wire.py.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SETTINGS_FILE="$CLAUDE_DIR/settings.json"
if [ -f "$SETTINGS_FILE" ]; then
    PY_CMD=""
    if command -v python3 &>/dev/null; then PY_CMD="python3"
    elif command -v python &>/dev/null; then PY_CMD="python"
    fi
    if [ -n "$PY_CMD" ]; then
        "$PY_CMD" "$SCRIPT_DIR/proxy/wire.py" --name rolling-context --unwire --settings "$SETTINGS_FILE" || \
            echo "WARNING: settings.json could not be cleaned — remove HTTPS_PROXY by hand."
        # Remove leftover plugin config knobs (routing keys handled by wire.py).
        "$PY_CMD" - "$SETTINGS_FILE" <<'PYEOF'
import json, sys
f = sys.argv[1]
try:
    with open(f, encoding="utf-8-sig") as fh:
        s = json.load(fh)
except Exception:
    sys.exit(0)
env = s.get("env", {})
for k in [k for k in env if k.startswith("ROLLING_CONTEXT_")]:
    del env[k]
s["env"] = env
with open(f, "w", encoding="utf-8") as fh:
    json.dump(s, fh, indent=2); fh.write("\n")
PYEOF
    fi
fi

echo ""
echo "Uninstalled."
