#!/usr/bin/env bash
# Ensure rolling context proxy is running
# Pure stdlib — no venv needed, just python

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROXY_DIR="$SCRIPT_DIR/../proxy"
PIDFILE="$HOME/.claude/rolling-context-proxy.pid"
VERFILE="$HOME/.claude/rolling-context-proxy.version"
HOOKLOG="$HOME/.claude/rolling-context-hook.log"
PORT="${ROLLING_CONTEXT_PORT:-5588}"
PROXY_URL="http://127.0.0.1:$PORT"
CURRENT_VERSION=$(cat "$SCRIPT_DIR/../.claude-plugin/plugin.json" 2>/dev/null | grep '"version"' | head -1 | sed 's/.*"version".*"\(.*\)".*/\1/')

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$HOOKLOG"
}

# Detect Windows (git bash)
if [[ "$(uname -s)" == MINGW* ]] || [[ "$(uname -s)" == MSYS* ]]; then
    IS_WINDOWS=true
else
    IS_WINDOWS=false
fi

log "Hook started. PROXY_DIR=$PROXY_DIR IS_WINDOWS=$IS_WINDOWS"

# Always update settings.json first (even if proxy is already running)
SETTINGS_FILE="$HOME/.claude/settings.json"
update_settings() {
    local py_cmd=""
    if [ "$IS_WINDOWS" = true ]; then
        py_cmd="python"
    elif command -v python3 &>/dev/null; then
        py_cmd="python3"
    else
        py_cmd="python"
    fi

    $py_cmd - "$SETTINGS_FILE" "$PROXY_URL" <<'PYEOF'
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

# Set ANTHROPIC_BASE_URL
existing = env.get("ANTHROPIC_BASE_URL", "")
if not existing:
    env["ANTHROPIC_BASE_URL"] = proxy_url
    print("set")
elif "127.0.0.1" not in existing:
    env["ROLLING_CONTEXT_UPSTREAM"] = existing
    env["ANTHROPIC_BASE_URL"] = proxy_url
    print("chained")
else:
    print("already")

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
PYEOF
}

RESULT=$(update_settings 2>/dev/null)
case "$RESULT" in
    set)     log "Set ANTHROPIC_BASE_URL=$PROXY_URL (settings.json)" ;;
    chained) log "Chaining upstream (settings.json)" ;;
    already) log "ANTHROPIC_BASE_URL already set (settings.json)" ;;
    *)       log "WARNING: Could not update settings.json" ;;
esac

# Check if proxy is already running
_kill_pid() {
    local pid="$1"
    if [ "$IS_WINDOWS" = true ]; then
        powershell.exe -Command "Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue" 2>/dev/null
    else
        kill "$pid" 2>/dev/null
        sleep 1
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
    fi
}

_pid_alive() {
    local pid="$1"
    if [ "$IS_WINDOWS" = true ]; then
        powershell.exe -Command "if (Get-Process -Id $pid -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" 2>/dev/null
    else
        kill -0 "$pid" 2>/dev/null
    fi
}

if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    if _pid_alive "$PID"; then
        # Check if version changed — restart if so
        RUNNING_VERSION=$(cat "$VERFILE" 2>/dev/null)
        if [ "$CURRENT_VERSION" = "$RUNNING_VERSION" ]; then
            log "Proxy already running (PID $PID, v$RUNNING_VERSION)"
            exit 0
        fi
        log "Version changed ($RUNNING_VERSION -> $CURRENT_VERSION), restarting proxy (PID $PID)"
        _kill_pid "$PID"
    fi
    rm -f "$PIDFILE" "$VERFILE"
fi

# Start proxy directly — no venv needed (pure stdlib)
log "Starting proxy..."
(
    cd "$PROXY_DIR" || { log "ERROR: cannot cd to $PROXY_DIR"; exit 1; }
    PYTHON_CMD=""
    if [ "$IS_WINDOWS" = true ]; then
        PYTHON_CMD="python"
    elif command -v python3 &>/dev/null; then
        PYTHON_CMD="python3"
    else
        PYTHON_CMD="python"
    fi
    nohup $PYTHON_CMD server.py > "$HOME/.claude/rolling-context-proxy.log" 2>&1 &
    echo $! > "$PIDFILE"
    echo "$CURRENT_VERSION" > "$VERFILE"
    log "Proxy started with PID $! (v$CURRENT_VERSION)"
) &

exit 0
